#!/usr/bin/env python3
"""Measure inactive-slot upload separately from an active-project switch."""
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import sounddevice as sd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from epsysex.fileclient import FileClient
from epsysex.hardware import resolve_audio_input, summarize_audio, write_wav
from epsysex.sysex import EP133_PRODUCT


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tar", required=True)
    ap.add_argument("--project", type=int, default=2)
    ap.add_argument("--wav", required=True)
    ap.add_argument("--report", required=True)
    ap.add_argument("--seconds", type=float, default=30.0)
    ap.add_argument("--upload-delay", type=float, default=4.0)
    ap.add_argument("--bpm", type=float, default=112.0)
    ap.add_argument("--switch-preflight-lead", type=float, default=1.22,
                    help="start guarded switch this many seconds before the target boundary")
    args = ap.parse_args()

    rate = 48000
    chunks = []
    statuses = []

    def callback(indata, _frames, _time_info, status):
        chunks.append(indata.copy())
        if status:
            statuses.append(str(status))

    device = resolve_audio_input("EP-133")
    started = time.monotonic()
    with sd.InputStream(device=device, channels=2, samplerate=rate,
                        dtype="float32", callback=callback):
        time.sleep(args.upload_delay)
        upload_started = time.monotonic()
        upload = subprocess.run([
            "python3", "tools/send_project.py", "--tar", args.tar,
            "--product", "ep133", "--port", "EP-133",
            "--project", str(args.project), "--no-checkpoint", "--no-reload",
            "--defer-verify", "--put-page-delay-ms", "0",
        ], text=True, capture_output=True)
        upload_finished = time.monotonic()

        # Schedule to the next nominal 4/4 boundary relative to capture start.
        # Capture start is not guaranteed to equal sequencer phase, so this is a
        # timing estimate; audio analysis reports the actual transition.
        bar_seconds = 240.0 / args.bpm
        elapsed = upload_finished - started
        boundary_index = int(elapsed / bar_seconds) + 2
        switch_target = started + boundary_index * bar_seconds
        switch_command_target = switch_target - args.switch_preflight_lead
        time.sleep(max(0.0, switch_command_target - time.monotonic()))
        switch_started = time.monotonic()
        client = FileClient(product_byte=EP133_PRODUCT, port_hint="EP-133",
                            lock_owner="double_buffer_switch")
        switch_result = client.reload_project(args.project, cycle=False)
        switch_finished = time.monotonic()

        remaining = args.seconds - (switch_finished - started)
        if remaining > 0:
            time.sleep(remaining)

    audio = np.concatenate(chunks, axis=0) if chunks else np.zeros((0, 2), np.float32)
    write_wav(Path(args.wav), audio, rate)
    summary = summarize_audio(audio, rate, pre_roll=0.2)
    report = {
        "audioDevice": device,
        "captureSeconds": len(audio) / rate,
        "bpmForBoundaryEstimate": args.bpm,
        "uploadStartSeconds": upload_started - started,
        "uploadEndSeconds": upload_finished - started,
        "uploadDurationSeconds": upload_finished - upload_started,
        "switchTargetSeconds": switch_target - started,
        "switchCommandTargetSeconds": switch_command_target - started,
        "switchPreflightLeadSeconds": args.switch_preflight_lead,
        "switchStartSeconds": switch_started - started,
        "switchEndSeconds": switch_finished - started,
        "switchDurationSeconds": switch_finished - switch_started,
        "uploadExitCode": upload.returncode,
        "uploadStdout": upload.stdout,
        "uploadStderr": upload.stderr,
        "switchResult": switch_result,
        "streamStatuses": statuses,
        "audioSummary": summary.__dict__,
    }
    Path(args.report).write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    return upload.returncode


if __name__ == "__main__":
    raise SystemExit(main())
