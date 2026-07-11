"""Hardware-in-the-loop playback and evidence helpers.

This module deliberately contains no project FILE writes.  The destructive
transaction lives in ``tools/test_device_json.py`` and uses :class:`FileClient`;
the helpers here only enumerate endpoints, drive MIDI transport, capture the
device's MIDI output/USB audio, and reduce the capture to stable measurements.

The optional HIL dependencies are imported lazily so the offline compiler and
test suite remain stdlib-only.
"""
from __future__ import annotations

import math
import time
import wave
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional


@dataclass
class MidiEvent:
    """One captured channel MIDI message, timed from MIDI START."""

    time: float
    type: str
    channel: Optional[int] = None
    note: Optional[int] = None
    velocity: Optional[int] = None
    control: Optional[int] = None
    value: Optional[int] = None


@dataclass
class AudioSummary:
    sample_rate: int
    frames: int
    peak: float
    rms: float
    left_rms: float
    right_rms: float
    noise_rms: float
    onset_count: int
    onset_times: list[float]


@dataclass
class PlaybackCapture:
    midi_events: list[MidiEvent]
    audio: Optional[object]
    audio_summary: Optional[AudioSummary]
    sample_rate: Optional[int]

    def report(self):
        return {
            "midiEvents": [asdict(event) for event in self.midi_events],
            "audio": asdict(self.audio_summary) if self.audio_summary else None,
        }


def list_endpoints():
    """Return currently visible MIDI and audio endpoint descriptions."""
    import mido

    result = {
        "midiInputs": list(mido.get_input_names()),
        "midiOutputs": list(mido.get_output_names()),
        "audioInputs": [],
    }
    try:
        import sounddevice as sd

        for index, device in enumerate(sd.query_devices()):
            if int(device["max_input_channels"]) > 0:
                result["audioInputs"].append({
                    "index": index,
                    "name": device["name"],
                    "channels": int(device["max_input_channels"]),
                    "defaultSampleRate": float(device["default_samplerate"]),
                })
    except (ImportError, OSError):
        pass
    return result


def _pick_name(names, hint, kind):
    matches = [name for name in names if hint.lower() in name.lower()]
    if not matches:
        raise RuntimeError(f"no {kind} matching {hint!r}; available: {list(names)!r}")
    if len(matches) > 1:
        exact = [name for name in matches if name.lower() == hint.lower()]
        if len(exact) == 1:
            return exact[0]
        raise RuntimeError(f"multiple {kind}s match {hint!r}: {matches!r}")
    return matches[0]


def resolve_midi_ports(hint, input_name=None, output_name=None):
    import mido

    return (
        input_name or _pick_name(mido.get_input_names(), hint, "MIDI input"),
        output_name or _pick_name(mido.get_output_names(), hint, "MIDI output"),
    )


def resolve_audio_input(hint):
    """Resolve an input-capable sounddevice index by case-insensitive hint."""
    import sounddevice as sd

    matches = []
    for index, device in enumerate(sd.query_devices()):
        if int(device["max_input_channels"]) and hint.lower() in device["name"].lower():
            matches.append((index, device))
    if not matches:
        available = [d["name"] for d in sd.query_devices() if int(d["max_input_channels"])]
        raise RuntimeError(f"no audio input matching {hint!r}; available: {available!r}")
    exact = [item for item in matches if item[1]["name"].lower() == hint.lower()]
    if len(exact) == 1:
        return exact[0][0]
    if len(matches) > 1:
        raise RuntimeError(f"multiple audio inputs match {hint!r}: {[d['name'] for _, d in matches]}")
    return matches[0][0]


def send_transport_stop(midi_output):
    """Put sequencer runtime in a quiet state before FILE mutation."""
    import mido

    with mido.open_output(midi_output) as output:
        output.send(mido.Message("stop"))
    time.sleep(0.05)


def _captured_message(message, elapsed):
    if message.type not in ("note_on", "note_off", "control_change"):
        return None
    return MidiEvent(
        time=elapsed,
        type=message.type,
        channel=getattr(message, "channel", None),
        note=getattr(message, "note", None),
        velocity=getattr(message, "velocity", None),
        control=getattr(message, "control", None),
        value=getattr(message, "value", None),
    )


def drive_and_capture(
    *,
    midi_input,
    midi_output,
    bpm,
    bars,
    audio_device=None,
    sample_rate=48000,
    channels=2,
    settle_seconds=0.20,
    tail_seconds=0.35,
):
    """Drive START + external clock and capture device MIDI/audio output.

    The device must already be in MIDI clock-in mode (system code 101).  One
    MIDI clock advances four of the project's 96-PPQN ticks.  Capture begins
    before START, continues for ``bars`` 4/4 bars, then records a short tail
    before STOP.  STOP is sent last because the EP firmware chokes voices on
    receipt of STOP.
    """
    import mido

    if not (40 <= float(bpm) <= 399):
        raise ValueError("bpm must be in the device range 40..399")
    if int(bars) < 1:
        raise ValueError("bars must be positive")

    audio_chunks = []
    stream = None
    if audio_device is not None:
        import sounddevice as sd

        def callback(indata, _frames, _time_info, status):
            if status:
                # Status is retained on the stream by PortAudio; the capture
                # remains useful, and discontinuities surface in audio checks.
                pass
            audio_chunks.append(indata.copy())

        stream = sd.InputStream(
            device=audio_device,
            channels=channels,
            samplerate=sample_rate,
            dtype="float32",
            callback=callback,
        )

    events = []
    interval = 60.0 / (float(bpm) * 24.0)
    clocks = int(bars) * 4 * 24

    def drain(mi, start):
        while True:
            message = mi.poll()
            if message is None:
                break
            event = _captured_message(message, time.monotonic() - start)
            if event:
                events.append(event)

    with mido.open_input(midi_input) as mi, mido.open_output(midi_output) as mo:
        while mi.poll() is not None:
            pass
        if stream:
            stream.start()
        try:
            time.sleep(settle_seconds)
            start = time.monotonic()
            mo.send(mido.Message("start"))
            next_clock = start
            for _index in range(clocks):
                while True:
                    now = time.monotonic()
                    drain(mi, start)
                    remaining = next_clock - now
                    if remaining <= 0:
                        break
                    time.sleep(min(0.001, remaining))
                mo.send(mido.Message("clock"))
                next_clock += interval
            tail_deadline = time.monotonic() + tail_seconds
            while time.monotonic() < tail_deadline:
                drain(mi, start)
                time.sleep(0.001)
            mo.send(mido.Message("stop"))
            drain(mi, start)
        finally:
            if stream:
                stream.stop()
                stream.close()

    audio = None
    summary = None
    if audio_chunks:
        import numpy as np

        audio = np.concatenate(audio_chunks, axis=0)
        summary = summarize_audio(audio, sample_rate, pre_roll=settle_seconds)
    return PlaybackCapture(events, audio, summary, sample_rate if audio is not None else None)


def summarize_audio(audio, sample_rate, pre_roll=0.20):
    """Calculate content-agnostic signal and onset measurements."""
    import numpy as np

    if audio.ndim == 1:
        audio = audio[:, None]
    mono = np.max(np.abs(audio), axis=1)
    noise_frames = max(1, min(len(mono), int(pre_roll * sample_rate * 0.8)))
    noise_rms = float(np.sqrt(np.mean(np.square(audio[:noise_frames], dtype=np.float64))))
    rms = float(np.sqrt(np.mean(np.square(audio, dtype=np.float64))))
    channel_rms = np.sqrt(np.mean(np.square(audio, dtype=np.float64), axis=0))
    peak = float(np.max(np.abs(audio))) if len(audio) else 0.0

    # 5 ms energy envelope, thresholded relative to both the noise floor and
    # the capture peak.  A 50 ms refractory period avoids counting a sample's
    # internal waveform cycles as separate hits.
    window = max(1, int(sample_rate * 0.005))
    kernel = np.ones(window, dtype=np.float64) / window
    energy = np.sqrt(np.convolve(np.square(mono, dtype=np.float64), kernel, mode="same"))
    threshold = max(1e-5, noise_rms * 8.0, peak * 0.025)
    above = energy >= threshold
    rising = np.flatnonzero(above & np.concatenate(([True], ~above[:-1])))
    refractory = int(sample_rate * 0.050)
    onsets = []
    for index in rising:
        if not onsets or index - onsets[-1] >= refractory:
            onsets.append(int(index))
    onset_times = [round(index / sample_rate - pre_roll, 6) for index in onsets]
    return AudioSummary(
        sample_rate=int(sample_rate),
        frames=int(len(audio)),
        peak=peak,
        rms=rms,
        left_rms=float(channel_rms[0]) if len(channel_rms) else 0.0,
        right_rms=float(channel_rms[1]) if len(channel_rms) > 1 else float(channel_rms[0]),
        noise_rms=noise_rms,
        onset_count=len(onsets),
        onset_times=onset_times,
    )


def write_wav(path, audio, sample_rate):
    """Write a float capture as a portable signed-16-bit WAV artifact."""
    import numpy as np

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    clipped = np.clip(audio, -1.0, 1.0)
    pcm = (clipped * 32767.0).astype("<i2")
    channels = 1 if pcm.ndim == 1 else pcm.shape[1]
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(2)
        handle.setframerate(int(sample_rate))
        handle.writeframes(pcm.tobytes())


def _cursor_scene(doc):
    """The scene external MIDI start plays: doc currentScene, default last
    (matching the compiler's scenes-trailer cursor default)."""
    scenes = doc.get("scenes", [])
    if not scenes:
        return None
    cursor = int(doc.get("currentScene", len(scenes)))
    return scenes[max(0, min(len(scenes), cursor) - 1)]


def expected_note_ons(doc, bpm):
    """Derive expected note-on messages for the CURSOR scene from authored
    JSON (external MIDI start plays the cursor scene, not scene 1)."""
    patterns = {str(p["id"]).upper(): p for p in doc.get("patterns", [])}
    pads = {
        (str(p["group"]).upper(), int(p["pad"])): p
        for p in doc.get("pads", [])
    }
    scene = _cursor_scene(doc)
    if scene is None:
        return []
    refs = scene["groupPatterns"]
    expected = []
    for group_index, ref in enumerate(refs):
        if not int(ref):
            continue
        pattern = patterns[f"{'ABCD'[group_index]}{int(ref):02d}"]
        for event in pattern.get("events", []):
            pad_number = int(event.get("pad", int(event.get("padByte", 0)) // 8 + 1))
            pad = pads.get(("ABCD"[group_index], pad_number), {})
            stored_channel = int(pad.get("midiChannel", 0))
            # Emitted note-off timing is only characterized for plain
            # one-shots: key/legato voices and time-stretched pads release on
            # their own laws. Assert duration only where the law is known.
            plain_oneshot = (int(pad.get("playMode", 0)) == 0
                             and int(pad.get("timeMode", 0)) == 0)
            expected.append({
                "tick": int(event["tick"]),
                "time": int(event["tick"]) * 60.0 / (float(bpm) * 96.0),
                "note": int(event.get("note", 60)),
                "velocity": int(event.get("velocity", 100)),
                "duration": (int(event["duration"]) * 60.0 / (float(bpm) * 96.0)
                             if plain_oneshot else None),
                # Stored 0 and 1 both resolve to channel 1; N>1 resolves to
                # channel N (mido exposes channels zero-based).
                "channel": max(0, stored_channel - 1),
            })
    # Note-off pairing is by (note, channel); when several pads emit the same
    # pair, offs are ambiguous (and the device may merge them under
    # retriggering) — only assert durations on unambiguous voices.
    keys = {}
    for item in expected:
        keys[(item["note"], item["channel"])] = keys.get((item["note"], item["channel"]), 0) + 1
    for item in expected:
        if keys[(item["note"], item["channel"])] > 1:
            item["duration"] = None
    return sorted(expected, key=lambda item: (item["tick"], item["note"], item["velocity"]))


def scene_window_seconds(doc, bpm):
    """One loop pass of the cursor scene, honoring its time signature.

    A 3/4 scene at 120 BPM loops a 1-bar pattern every 1.5 s (verified by
    MIDI capture) — the window must use the scene's meter, not assume 4/4.
    """
    patterns = {str(p["id"]).upper(): p for p in doc.get("patterns", [])}
    scene = _cursor_scene(doc)
    if scene is None:
        return 4 * 60.0 / float(bpm)
    bars = [
        int(patterns.get(f"{'ABCD'[gi]}{int(ref):02d}", {}).get("bars", 1))
        for gi, ref in enumerate(scene["groupPatterns"]) if int(ref)
    ]
    numerator, denominator = (scene.get("timeSignature") or [4, 4])[:2]
    beats = float(numerator) * (4.0 / float(denominator))
    return (max(bars) if bars else 1) * beats * 60.0 / float(bpm)


def compare_note_ons(captured, expected, timing_tolerance=0.060, window_seconds=None):
    """Match expected note-ons in order and return assertion-style failures.

    ``window_seconds`` bounds the note-ons considered: the sequencer loops the
    scene, so the capture tail contains the next pass's first events —
    without a window the count assertion fails on correct playback. Note-offs
    are still searched across the whole capture so durations that cross the
    boundary resolve. Same-time events (within tolerance) are ordered by
    (note, velocity) to match the expected list's deterministic sort: the
    device's emission order for simultaneous voices is not defined.
    """
    actual = [event for event in captured if event.type == "note_on" and (event.velocity or 0) > 0]
    if window_seconds is not None:
        actual = [event for event in actual if event.time < window_seconds - 0.005]
    actual.sort(key=lambda e: (round(e.time / max(timing_tolerance, 1e-3)), e.note, e.velocity or 0))
    failures = []
    if len(actual) != len(expected):
        failures.append(f"expected {len(expected)} note-ons, captured {len(actual)}")
    for index, (want, got) in enumerate(zip(expected, actual)):
        if got.note != want["note"]:
            failures.append(f"event {index}: expected note {want['note']}, got {got.note}")
        if got.velocity != want["velocity"]:
            failures.append(
                f"event {index}: expected velocity {want['velocity']}, got {got.velocity}"
            )
        if want.get("channel") is not None and got.channel != want["channel"]:
            failures.append(
                f"event {index}: expected channel {want['channel'] + 1}, got {got.channel + 1}"
            )
        if not math.isclose(got.time, want["time"], abs_tol=timing_tolerance):
            failures.append(
                f"event {index}: expected at {want['time']:.3f}s, got {got.time:.3f}s"
            )
        if want.get("duration") is None:
            continue  # note-off law uncharacterized for this pad mode
        matching_off = next(
            (
                event for event in captured
                if event.time >= got.time
                and event.note == got.note
                and event.channel == got.channel
                and (event.type == "note_off" or (event.type == "note_on" and event.velocity == 0))
            ),
            None,
        )
        if matching_off is None:
            failures.append(f"event {index}: no matching note-off captured")
        else:
            actual_duration = matching_off.time - got.time
            if not math.isclose(actual_duration, want["duration"], abs_tol=timing_tolerance):
                failures.append(
                    f"event {index}: expected duration {want['duration']:.3f}s, "
                    f"got {actual_duration:.3f}s"
                )
    return failures
