"""Sound dependency planning and upload preparation for authored projects."""
from __future__ import annotations

import json
import sys
import wave
from array import array
from pathlib import Path

from .compile import CompileError

NATIVE_SAMPLE_RATE = 46875
TIME_MODES = {0: "off", 1: "bpm", 2: "bar"}
PLAY_MODES = {0: "oneshot", 1: "key", 2: "legato", 3: "loop"}


def project_sound_slots(doc):
    """Return sample slots referenced by pads (supertone symbols excluded)."""
    slots = set()
    for pad in doc.get("pads", []):
        if pad.get("supertone") is not None:
            continue
        slot = int(pad.get("slot", 0))
        if slot:
            if not (1 <= slot <= 999):
                raise CompileError(f"sample dependency slot {slot} is outside 1..999")
            slots.add(slot)
    return slots


def dependency_specs(doc):
    """Validate and index the top-level ``sounds`` dependency list by slot."""
    indexed = {}
    for index, spec in enumerate(doc.get("sounds", [])):
        if not isinstance(spec, dict):
            raise CompileError(f"sounds[{index}] must be an object")
        slot = int(spec.get("slot", 0))
        if not (1 <= slot <= 999):
            raise CompileError(f"sounds[{index}].slot must be 1..999")
        if slot in indexed:
            raise CompileError(f"duplicate sound dependency slot {slot}")
        indexed[slot] = spec
    return indexed


def dependency_plan(doc, available_slots=()):
    """Return required/present/missing/declarative dependency information."""
    required = project_sound_slots(doc)
    declared = dependency_specs(doc)
    available = {int(slot) for slot in available_slots}
    return {
        "requiredSlots": sorted(required),
        "declaredSlots": sorted(declared),
        "presentSlots": sorted(required & available),
        "missingSlots": sorted(required - available),
        "undeclaredMissingSlots": sorted((required - available) - set(declared)),
        "unusedDeclaredSlots": sorted(set(declared) - required),
    }


def _convert_channels(samples, source_channels, target_channels):
    if source_channels == target_channels:
        return samples
    if source_channels == 2 and target_channels == 1:
        output = array("h")
        for offset in range(0, len(samples), 2):
            output.append(int((int(samples[offset]) + int(samples[offset + 1])) / 2))
        return output
    if source_channels == 1 and target_channels == 2:
        output = array("h")
        for sample in samples:
            output.extend((sample, sample))
        return output
    raise CompileError("WAV conversion supports only mono and stereo")


def _resample_pcm16(samples, channels, source_rate, target_rate):
    if source_rate == target_rate:
        return samples
    source_frames = len(samples) // channels
    if source_frames < 2:
        raise CompileError("sound dependency is too short to resample")
    target_frames = max(1, round(source_frames * target_rate / source_rate))
    output = array("h", [0]) * (target_frames * channels)
    scale = source_rate / target_rate
    for target_frame in range(target_frames):
        position = min(source_frames - 1, target_frame * scale)
        left_frame = int(position)
        right_frame = min(source_frames - 1, left_frame + 1)
        fraction = position - left_frame
        for channel in range(channels):
            left = samples[left_frame * channels + channel]
            right = samples[right_frame * channels + channel]
            value = round(left + (right - left) * fraction)
            output[target_frame * channels + channel] = max(-32768, min(32767, value))
    return output


def wav_to_pcm16(path, target_rate=NATIVE_SAMPLE_RATE, target_channels=None):
    """Read PCM16 WAV and return device-ready PCM plus stream metadata."""
    path = Path(path)
    with wave.open(str(path), "rb") as handle:
        if handle.getcomptype() != "NONE" or handle.getsampwidth() != 2:
            raise CompileError(f"{path}: only uncompressed 16-bit PCM WAV is supported")
        source_channels = handle.getnchannels()
        source_rate = handle.getframerate()
        samples = array("h")
        samples.frombytes(handle.readframes(handle.getnframes()))
    if sys.byteorder != "little":
        samples.byteswap()
    target_channels = int(target_channels or source_channels)
    samples = _convert_channels(samples, source_channels, target_channels)
    samples = _resample_pcm16(samples, target_channels, source_rate, int(target_rate))
    if sys.byteorder != "little":
        samples.byteswap()
    return samples.tobytes(), {
        "samplerate": int(target_rate),
        "channels": target_channels,
        "format": "s16",
        "frames": len(samples) // target_channels,
        "sourceSampleRate": source_rate,
        "sourceChannels": source_channels,
    }


def prepare_sound(spec, base_dir="."):
    """Resolve one dependency into raw PCM and upload metadata."""
    base_dir = Path(base_dir)
    path = (base_dir / spec["path"]).resolve()
    if not path.is_file():
        raise CompileError(f"sound dependency file does not exist: {path}")
    if path.suffix.lower() == ".wav":
        pcm, stream = wav_to_pcm16(
            path,
            target_rate=int(spec.get("samplerate", NATIVE_SAMPLE_RATE)),
            target_channels=spec.get("channels"),
        )
    else:
        pcm = path.read_bytes()
        stream = {
            "samplerate": int(spec.get("samplerate", NATIVE_SAMPLE_RATE)),
            "channels": int(spec.get("channels", 1)),
            "format": str(spec.get("format", "s16")),
            "frames": len(pcm) // (2 * int(spec.get("channels", 1))),
        }
    metadata = dict(spec.get("metadata", {}))
    if "bpm" in spec:
        metadata["sound.bpm"] = float(spec["bpm"])
    if "timeMode" in spec:
        value = spec["timeMode"]
        if isinstance(value, int):
            if value not in TIME_MODES:
                raise CompileError("sound dependency timeMode must be 0..2 or an enum string")
            value = TIME_MODES[value]
        if str(value) not in set(TIME_MODES.values()):
            raise CompileError("sound dependency timeMode must be off, bpm, or bar")
        metadata["time.mode"] = str(value)
    if "playMode" in spec:
        value = spec["playMode"]
        if isinstance(value, int):
            if value not in PLAY_MODES:
                raise CompileError("sound dependency playMode must be 0..3 or an enum string")
            value = PLAY_MODES[value]
        if str(value) not in set(PLAY_MODES.values()):
            raise CompileError("sound dependency playMode is not a known enum")
        metadata["sound.playmode"] = str(value)
        metadata.setdefault("envelope.release", int(spec.get("release", 255)))
    return {
        "slot": int(spec["slot"]),
        "path": str(path),
        "pcm": pcm,
        "name": spec.get("name", path.stem[:20]),
        "nodeName": spec.get("nodeName", f"{int(spec['slot']):03d}.pcm"),
        "stream": stream,
        "metadata": metadata,
    }


def ensure_sound_dependencies(client, doc, base_dir=".", upload_missing=False, verify=True):
    """Preflight required slots and optionally upload declared missing sounds."""
    occupied = {int(node["id"]) for node in client.list_sounds()}
    plan = dependency_plan(doc, occupied)
    if plan["undeclaredMissingSlots"]:
        raise CompileError(
            "project references missing, undeclared sound slots: "
            + ", ".join(map(str, plan["undeclaredMissingSlots"]))
        )
    if plan["missingSlots"] and not upload_missing:
        raise CompileError(
            "project sound slots are missing: " + ", ".join(map(str, plan["missingSlots"]))
            + "; pass the explicit upload-missing option"
        )
    declared = dependency_specs(doc)
    uploads = []
    for slot in plan["missingSlots"]:
        prepared = prepare_sound(declared[slot], base_dir=base_dir)
        stream = prepared["stream"]
        summary = client.upload_sound(
            slot,
            prepared["pcm"],
            name=prepared["name"],
            node_name=prepared["nodeName"],
            samplerate=stream["samplerate"],
            channels=stream["channels"],
            sample_format=stream["format"],
            extra_metadata=prepared["metadata"],
        )
        if verify:
            readback, _meta = client.read_sound(slot)
            if readback != prepared["pcm"]:
                raise CompileError(f"sound slot {slot} failed byte-exact upload verification")
        uploads.append({
            "slot": slot,
            "bytes": len(prepared["pcm"]),
            "frames": stream["frames"],
            "path": prepared["path"],
            "verified": bool(verify),
            "summary": summary,
        })
    plan["uploads"] = uploads
    return plan


def load_document(path):
    path = Path(path)
    return json.loads(path.read_text(encoding="utf-8"))
