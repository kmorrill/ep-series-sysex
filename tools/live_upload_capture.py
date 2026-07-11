#!/usr/bin/env python3
"""Capture EP-133 USB audio while deploying a project JSON change."""
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import sounddevice as sd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from epsysex.hardware import resolve_audio_input, summarize_audio, write_wav


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", required=True)
    ap.add_argument("--wav", required=True)
    ap.add_argument("--report", required=True)
    ap.add_argument("--seconds", type=float, default=35.0)
    ap.add_argument("--delay", type=float, default=4.0)
    ap.add_argument("--put-page-delay-ms", type=float, default=6.0)
    ap.add_argument("--put-payload-bytes", type=int)
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
        time.sleep(args.delay)
        upload_started = time.monotonic()
        command = [
            "python3", "tools/send_project.py", "--json", args.json,
            "--product", "ep133", "--port", "EP-133", "--project", "1",
            "--fast-live", "--trust-sound-slots",
            "--put-page-delay-ms", str(args.put_page_delay_ms),
        ]
        if args.put_payload_bytes is not None:
            command.extend(["--unsafe-put-payload-bytes", str(args.put_payload_bytes),
                            "--acknowledge-oversized-put", "I ACCEPT OVERSIZED FILE_PUT"])
        proc = subprocess.run(command, text=True, capture_output=True)
        upload_finished = time.monotonic()
        remaining = args.seconds - (upload_finished - started)
        if remaining > 0:
            time.sleep(remaining)

    audio = np.concatenate(chunks, axis=0) if chunks else np.zeros((0, 2), np.float32)
    write_wav(Path(args.wav), audio, rate)
    summary = summarize_audio(audio, rate, pre_roll=0.2)
    report = {
        "audioDevice": device,
        "captureSeconds": len(audio) / rate,
        "uploadStartSeconds": upload_started - started,
        "uploadEndSeconds": upload_finished - started,
        "uploadDurationSeconds": upload_finished - upload_started,
        "uploadExitCode": proc.returncode,
        "uploadStdout": proc.stdout,
        "uploadStderr": proc.stderr,
        "streamStatuses": statuses,
        "audioSummary": summary.__dict__,
    }
    Path(args.report).write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
