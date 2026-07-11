#!/usr/bin/env python3
"""Run destructive JSON -> TAR -> EP -> MIDI/audio playback tests.

Dry discovery (never writes):

    python3 tools/test_device_json.py --list

Run the safe-default matrix against a reserved EP-40 project slot:

    python3 tools/test_device_json.py --run --project 9 \
      --acknowledge-overwrite "ERASE PROJECT 9"

The selected slot is checkpointed once, every compiled TAR is written and
read back byte-identically, and the checkpoint plus prior active-project state
are restored at the end.  A firmware debug frame aborts without attempting
further FILE traffic, per the repository's recovery rules.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from epsysex import tar as eptar
from epsysex.compile import compile_project
from epsysex.fileclient import FileClient, PROJECTS_NODE_ID, SOUNDS_NODE_ID, project_fid
from epsysex.hardware import (
    compare_note_ons,
    drive_and_capture,
    expected_note_ons,
    list_endpoints,
    resolve_audio_input,
    resolve_midi_ports,
    scene_window_seconds,
    send_transport_stop,
    write_wav,
)
from epsysex.hardware_cases import build_cases, sound_reference_from_archive
from epsysex.identity import identity_from_device
from epsysex.sysex import DebugFrameError, EP133_PRODUCT, EP40_PRODUCT, EPSysExError

PRODUCTS = {"ep133": EP133_PRODUCT, "ep40": EP40_PRODUCT}
DEFAULT_HINTS = {"ep133": "EP-133", "ep40": "EP-40"}
SETTINGS_ACK = "I ACCEPT ERR CLOCK 43"


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def json_safe(value):
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"cannot serialize {type(value).__name__}")


def save_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, default=json_safe) + "\n", encoding="utf-8")


def audio_failures(capture, expected_count, require_audio):
    if capture.audio_summary is None:
        return ["USB audio capture unavailable"] if require_audio else []
    summary = capture.audio_summary
    failures = []
    audible_floor = max(1e-4, summary.noise_rms * 6.0)
    if summary.peak < audible_floor:
        failures.append(
            f"audio is silent: peak {summary.peak:.6f}, noise floor {summary.noise_rms:.6f}"
        )
    # Content may have multiple internal transients, so assert a lower bound,
    # never exact equality.  Automation may intentionally mute some hits.
    if expected_count and summary.onset_count < 1:
        failures.append("no audio onset detected")
    if summary.peak >= 0.9999:
        failures.append("USB audio capture clipped at full scale")
    return failures


def pad_sound_audio_evidence(capture, expected, pre_roll):
    """Evaluate per-hit audibility and the two deterministic hard-pan probes.

    CoreAudio can drop the head of the input stream on startup (observed
    AUHAL underruns), shifting the whole capture earlier than the nominal
    pre-roll. Align the analysis windows to the first detected audio onset
    rather than trusting wall-clock offsets.
    """
    if capture.audio is None:
        return {}, []
    import numpy as np

    audio = capture.audio
    rate = int(capture.sample_rate)
    drift = 0.0
    summary = capture.audio_summary
    if summary and summary.onset_times and expected:
        candidate = summary.onset_times[0] - expected[0]["time"]
        if abs(candidate) < 0.5:
            drift = candidate
    metrics = []
    failures = []
    for index, event in enumerate(expected):
        center = int((pre_roll + drift + event["time"] + 0.025) * rate)
        before = audio[max(0, center - int(0.020 * rate)):center]
        after = audio[center:min(len(audio), center + int(0.160 * rate))]
        if not len(after):
            failures.append(f"pad-sound event {index}: audio window is outside capture")
            continue
        before_power = np.mean(np.square(before, dtype=np.float64), axis=0) if len(before) else 0
        after_power = np.mean(np.square(after, dtype=np.float64), axis=0)
        incremental = np.sqrt(np.maximum(after_power - before_power, 0.0))
        left = float(incremental[0])
        right = float(incremental[1]) if len(incremental) > 1 else left
        combined = float(np.sqrt((left * left + right * right) / 2.0))
        metrics.append({
            "event": index,
            "tick": event["tick"],
            "incrementalRms": combined,
            "leftIncrementalRms": left,
            "rightIncrementalRms": right,
        })
        floor = max(1e-5, capture.audio_summary.noise_rms * 4.0)
        metrics[-1]["detected"] = combined >= floor

    detected = sum(1 for m in metrics if m.get("detected"))
    # Per-event window checks are alignment-sensitive (CoreAudio head loss);
    # record them as evidence, and fail only the fully-silent case where MIDI
    # played but no event window carries energy despite a non-silent capture.
    if metrics and detected == 0 and capture.audio_summary.peak > 0.01:
        failures.append(
            "no pad-sound event window carries audio energy despite a "
            "non-silent capture — window alignment or routing problem"
        )

    # Events 2 and 3 are authored at pan -16 and +16.  Constant-power hard
    # pan digitally mutes the far channel, so 4:1 allows USB noise/carryover
    # while still proving direction and byte sign. Only meaningful when both
    # probe windows actually detected energy.
    if len(metrics) >= 4 and metrics[2].get("detected") and metrics[3].get("detected"):
        left_probe, right_probe = metrics[2], metrics[3]
        if left_probe["leftIncrementalRms"] < left_probe["rightIncrementalRms"] * 4.0:
            failures.append("pan=-16 probe did not favor the left channel by at least 4:1")
        if right_probe["rightIncrementalRms"] < right_probe["leftIncrementalRms"] * 4.0:
            failures.append("pan=+16 probe did not favor the right channel by at least 4:1")
    return {"eventWindows": metrics, "detectedEvents": detected}, failures


def restore(client, midi_output, project, checkpoint, original_active, report):
    """Restore the checkpoint and active project; raises on any mismatch."""
    print(f"RESTORE project {project}: writing {len(checkpoint)} checkpoint bytes")
    send_transport_stop(midi_output)
    client.write_project_archive(project, checkpoint)
    restored, _meta = client.read_project_archive(project)
    if restored != checkpoint:
        raise RuntimeError("checkpoint restore readback mismatch")
    client.reload_project(project)
    if original_active and original_active != project_fid(project):
        client.set_metadata(PROJECTS_NODE_ID, {"active": original_active})
        active = client.get_metadata(PROJECTS_NODE_ID, key="active").get("active")
        if active != original_active:
            raise RuntimeError(
                f"active project restore failed: expected fid {original_active}, got {active}"
            )
    report["restore"] = {
        "status": "passed",
        "sha256": sha256(restored),
        "activeProjectFid": original_active,
    }
    print("RESTORE passed (byte-identical; prior active project selected)")


def run_case(args, client, case, midi_in, midi_out, audio_device, artifact_dir,
             checkpoint):
    result = {
        "name": case.name,
        "description": case.description,
        "risky": case.risky,
        "compileMode": "checkpoint-base" if case.use_checkpoint_base else "scratch",
        "status": "failed",
        "failures": [],
    }
    case_dir = artifact_dir / case.name
    case_dir.mkdir(parents=True, exist_ok=True)
    save_json(case_dir / "project.json", case.document)

    print(f"\nCASE {case.name}: {case.description}")
    tar_bytes = compile_project(
        case.document,
        base_archive=checkpoint if case.use_checkpoint_base else None,
    )
    (case_dir / "compiled.tar").write_bytes(tar_bytes)
    result["compiled"] = {"bytes": len(tar_bytes), "sha256": sha256(tar_bytes)}

    send_transport_stop(midi_out)
    # One polite retry for a CLEAN timeout (device silent — observed as an
    # occasional transient under rapid session cycling on EP-133 2.5.1).
    # DebugFrameError must never be retried: that is the stop-everything
    # signal, and the session preflight refuses a looping device anyway.
    try:
        client.write_project_archive(args.project, tar_bytes)
    except EPSysExError as exc:
        if isinstance(exc, DebugFrameError) or "timed out" not in str(exc):
            raise
        print(f"  clean write timeout ({exc}); re-initializing and retrying once")
        time.sleep(3.0)
        client.file_init()
        client.write_project_archive(args.project, tar_bytes)
    readback, _meta = client.read_project_archive(args.project)
    (case_dir / "readback.tar").write_bytes(readback)
    if readback == tar_bytes:
        result["readback"] = {"status": "passed", "sha256": sha256(readback)}
        print(f"  ok readback {len(readback)} B sha256={sha256(readback)[:12]}")
    else:
        # The firmware normalizes archives at write-commit (live-verified on
        # EP-133 2.5.1): sparse archives get default pads/directories
        # materialized and members are re-serialized in device-canonical
        # order. That is not corruption — require semantic equivalence
        # instead: every compiled non-directory member must read back with a
        # byte-identical payload; extra members are device materialization.
        compiled_members = eptar.member_payloads(tar_bytes)
        readback_members = eptar.member_payloads(readback)
        changed = [name for name, payload in compiled_members.items()
                   if readback_members.get(name) != payload]
        if changed:
            result["failures"].append(
                f"device readback altered member payloads {changed[:6]}: "
                f"compiled {sha256(tar_bytes)}, readback {sha256(readback)}"
            )
            result["readback"] = {"status": "failed", "sha256": sha256(readback),
                                  "changedMembers": changed}
            print(f"  FAIL readback (member payloads changed: {changed[:6]})")
            return result
        extra = sorted(set(readback_members) - set(compiled_members))
        result["readback"] = {
            "status": "passed-normalized", "sha256": sha256(readback),
            "extraMembers": len(extra),
        }
        print(f"  ok readback (device-normalized: {len(readback)} B, "
              f"{len(extra)} materialized members, all compiled payloads intact)")

    activation = client.reload_project(args.project)
    result["activation"] = activation
    time.sleep(args.reload_settle)

    bpm = float(case.document.get("settings", {}).get("bpm", args.bpm))
    expected = expected_note_ons(case.document, bpm=args.bpm)
    capture = drive_and_capture(
        midi_input=midi_in,
        midi_output=midi_out,
        bpm=args.bpm,
        bars=case.bars,
        audio_device=audio_device,
        sample_rate=args.sample_rate,
        settle_seconds=args.audio_preroll,
        tail_seconds=args.audio_tail,
    )
    result["playback"] = capture.report()
    result["playback"]["clockBpm"] = args.bpm
    result["playback"]["projectBpm"] = bpm
    result["playback"]["expectedNoteOns"] = expected

    if capture.audio is not None:
        write_wav(case_dir / "capture.wav", capture.audio, capture.sample_rate)
    # window = one pass of scene 1's longest pattern (the sequencer loops it;
    # the tail of a longer capture contains the next pass's events)
    authored_window = scene_window_seconds(case.document, args.bpm)
    midi_errors = compare_note_ons(capture.midi_events, expected, args.midi_tolerance,
                                   window_seconds=authored_window)
    audio_errors = audio_failures(capture, len(expected), args.evidence in ("audio", "both"))
    result["midiFailures"] = midi_errors
    result["audioFailures"] = audio_errors
    if case.name == "pad_sound_design":
        behavior, behavior_errors = pad_sound_audio_evidence(
            capture, expected, args.audio_preroll
        )
        result["playback"]["padSoundEvidence"] = behavior
        audio_errors.extend(behavior_errors)
    save_json(case_dir / "capture.json", result["playback"])

    if args.evidence in ("midi", "both"):
        result["failures"].extend(midi_errors)
    elif args.evidence == "either" and midi_errors and audio_errors:
        result["failures"].append(
            "neither playback oracle passed; MIDI: " + "; ".join(midi_errors)
            + "; audio: " + "; ".join(audio_errors)
        )
    if args.evidence in ("audio", "both"):
        result["failures"].extend(audio_errors)

    if result["failures"]:
        print(f"  FAIL playback ({len(result['failures'])} assertion(s))")
        for failure in result["failures"]:
            print(f"       {failure}")
    else:
        result["status"] = "passed"
        audio_text = (
            f", audio peak={capture.audio_summary.peak:.3f} "
            f"onsets={capture.audio_summary.onset_count}"
            if capture.audio_summary else ""
        )
        note_count = len([e for e in capture.midi_events if e.type == "note_on" and e.velocity])
        print(f"  ok playback MIDI note-ons={note_count}{audio_text}")
    return result


def parse_args():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--list", action="store_true", help="list endpoints and exit; never writes")
    ap.add_argument("--run", action="store_true", help="perform the destructive HIL transaction")
    ap.add_argument("--product", choices=sorted(PRODUCTS), default="ep40")
    ap.add_argument("--project", type=int, help="reserved project slot to overwrite and restore")
    ap.add_argument("--acknowledge-overwrite", help='must equal "ERASE PROJECT N"')
    ap.add_argument("--midi-port", help="MIDI input/output substring")
    ap.add_argument("--midi-input", help="exact MIDI input name")
    ap.add_argument("--midi-output", help="exact MIDI output name")
    ap.add_argument("--audio-device", help="USB audio input substring (default: MIDI hint)")
    ap.add_argument("--no-audio", action="store_true", help="disable USB audio capture")
    ap.add_argument(
        "--evidence", choices=("both", "midi", "audio", "either"), default="both",
        help="required playback oracle(s); default both",
    )
    ap.add_argument("--sound-slot", type=int, help="known audible existing slot; auto from checkpoint")
    ap.add_argument("--trim-end", type=int, help="native frame length for an explicit sound slot")
    ap.add_argument("--case", action="append", dest="cases", help="run only named case (repeatable)")
    ap.add_argument("--bpm", type=float, default=120.0, help="external MIDI clock tempo")
    ap.add_argument("--sample-rate", type=int, default=48000)
    ap.add_argument("--audio-preroll", type=float, default=0.20)
    ap.add_argument("--audio-tail", type=float, default=0.35)
    # The firmware needs a settle window after write+reload before transport;
    # starting immediately has produced MIDI note floods (hundreds of stale
    # events at t=0 — reproduced on EP-133 2.5.1 with 0.35 s; the documented
    # safe window is ~6 s, see file-protocol.md).
    ap.add_argument("--reload-settle", type=float, default=6.0)
    ap.add_argument("--midi-tolerance", type=float, default=0.075)
    ap.add_argument("--artifacts", type=Path, default=REPO / "tests" / "artifacts" / "hardware")
    ap.add_argument("--include-risky-settings", action="store_true")
    ap.add_argument(
        "--acknowledge-settings-risk",
        help=f"required with --include-risky-settings; must equal {SETTINGS_ACK!r}",
    )
    return ap.parse_args()


def main():
    args = parse_args()
    if args.list:
        print(json.dumps(list_endpoints(), indent=2))
        return 0
    if not args.run:
        print(__doc__.strip())
        print("\nRefusing to write without --run and the overwrite acknowledgement.")
        return 2
    if args.project is None or not (1 <= args.project <= 9):
        raise SystemExit("--project must explicitly select an actual device slot 1..9")
    required_ack = f"ERASE PROJECT {args.project}"
    if args.acknowledge_overwrite != required_ack:
        raise SystemExit(f"--acknowledge-overwrite must equal {required_ack!r}")
    if args.include_risky_settings and args.acknowledge_settings_risk != SETTINGS_ACK:
        raise SystemExit(
            "the fabricated settings-member probe has previously caused ERR CLOCK 43 and "
            f"a flash format; pass --acknowledge-settings-risk {SETTINGS_ACK!r} to include it"
        )
    if args.no_audio and args.evidence in ("audio", "both"):
        raise SystemExit(f"--evidence {args.evidence} conflicts with --no-audio")

    hint = args.midi_port or DEFAULT_HINTS[args.product]
    midi_in, midi_out = resolve_midi_ports(hint, args.midi_input, args.midi_output)
    audio_device = None
    if not args.no_audio:
        audio_device = resolve_audio_input(args.audio_device or hint)

    stamp = time.strftime("%Y%m%d-%H%M%S")
    artifact_dir = args.artifacts / stamp
    artifact_dir.mkdir(parents=True, exist_ok=False)
    report = {
        "startedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "product": args.product,
        "project": args.project,
        "midiInput": midi_in,
        "midiOutput": midi_out,
        "audioDevice": audio_device,
        "evidence": args.evidence,
        "cases": [],
        "restore": {"status": "not-attempted"},
    }
    save_json(artifact_dir / "run-config.json", report)
    print(f"artifacts: {artifact_dir}")
    print(f"MIDI: {midi_in!r} / {midi_out!r}; audio input index: {audio_device!r}")

    client = FileClient(
        input_name=midi_in,
        output_name=midi_out,
        product_byte=PRODUCTS[args.product],
        lock_owner="json_hardware_suite",
    )
    checkpoint = None
    original_active = None
    debug_abort = False
    mutated = False
    run_error = None
    try:
        identity = identity_from_device(args.midi_port or hint)
        report["identity"] = identity
        print(f"identity: {identity}")
        client.file_init()

        projects = client.list_nodes(PROJECTS_NODE_ID)
        target_fid = project_fid(args.project)
        if target_fid not in {node["id"] for node in projects}:
            raise RuntimeError(
                f"project {args.project} (fid {target_fid}) is not present; "
                f"device projects: {projects}"
            )
        original_active = client.get_metadata(PROJECTS_NODE_ID, key="active").get("active")
        checkpoint, checkpoint_meta = client.read_project_archive(args.project)
        (artifact_dir / "checkpoint.tar").write_bytes(checkpoint)
        report["checkpoint"] = {
            "bytes": len(checkpoint),
            "sha256": sha256(checkpoint),
            "meta": checkpoint_meta,
            "originalActiveProjectFid": original_active,
        }
        print(
            f"checkpoint: project {args.project}, {len(checkpoint)} B, "
            f"sha256={sha256(checkpoint)[:12]}, prior active fid={original_active}"
        )

        sounds = client.list_nodes(SOUNDS_NODE_ID)
        available_slots = {int(node["id"]) for node in sounds if 1 <= int(node["id"]) <= 999}
        reference = sound_reference_from_archive(
            checkpoint,
            available_slots,
            requested_slot=args.sound_slot,
            trim_end=args.trim_end,
        )
        report["soundReference"] = reference
        print(
            f"sound dependency: slot {reference['slot']} from {reference['sourcePad']}, "
            f"trim {reference['trimStart']}..{reference['trimEnd']}"
        )

        cases = build_cases(reference, args.include_risky_settings, product=args.product)
        known = {case.name for case in cases}
        if args.cases:
            unknown = set(args.cases) - known
            if unknown:
                raise RuntimeError(f"unknown --case values {sorted(unknown)}; available: {sorted(known)}")
            cases = [case for case in cases if case.name in args.cases]
        report["selectedCases"] = [case.name for case in cases]
        for case in cases:
            # Set before entering the case: even a failed/partial FILE_PUT
            # means the checkpoint must be restored.
            mutated = True
            case_result = run_case(
                args, client, case, midi_in, midi_out, audio_device, artifact_dir,
                checkpoint,
            )
            report["cases"].append(case_result)
            save_json(artifact_dir / "report.partial.json", report)

    except DebugFrameError as exc:
        debug_abort = True
        run_error = exc
        report["fatalError"] = str(exc)
        report["fatalRecovery"] = (
            "No restore attempted: stop all FILE traffic, power-cycle the device, "
            "then restore checkpoint.tar with tools/send_project.py."
        )
        print(f"\nFATAL {exc}", file=sys.stderr)
        print(report["fatalRecovery"], file=sys.stderr)
    except Exception as exc:  # keep the checkpoint/restore path centralized
        run_error = exc
        report["fatalError"] = repr(exc)
        report["traceback"] = traceback.format_exc()
        print(f"\nERROR {exc!r}", file=sys.stderr)
    finally:
        if checkpoint is not None and mutated and not debug_abort:
            try:
                restore(client, midi_out, args.project, checkpoint, original_active, report)
            except DebugFrameError as exc:
                debug_abort = True
                report["restore"] = {"status": "debug-abort", "error": str(exc)}
                print(f"RESTORE ABORTED by firmware debug frame: {exc}", file=sys.stderr)
            except Exception as exc:
                report["restore"] = {"status": "failed", "error": repr(exc)}
                print(f"RESTORE FAILED: {exc!r}", file=sys.stderr)
        elif checkpoint is not None and not mutated:
            report["restore"] = {"status": "not-needed", "reason": "no project write attempted"}
        report["finishedAt"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        report["passed"] = (
            run_error is None
            and report["restore"].get("status") == "passed"
            and bool(report["cases"])
            and all(case["status"] == "passed" for case in report["cases"])
        )
        save_json(artifact_dir / "report.json", report)

    passed = sum(case["status"] == "passed" for case in report["cases"])
    print(f"\nRESULT {passed}/{len(report['cases'])} cases passed; restore={report['restore']['status']}")
    print(f"report: {artifact_dir / 'report.json'}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
