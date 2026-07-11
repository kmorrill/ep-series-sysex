# Project deployment during playback

The normal sender optimizes for recoverability: capture a checkpoint, enumerate
the sound library, write, read back every byte, and reload. That is the right
default for ordinary project management. For performance experiments, each of
those steps can be moved outside the playback-critical window.

## Verified EP-133 results

All measurements below used OS 2.5.1, a 59,904-byte Project 1 archive, the exact
`EP-133` MIDI and USB-audio endpoints, and continuous stereo capture. The EP-40
connected at the same time was not addressed.

| workflow | deployment window | detected audio interruption |
|---|---:|---:|
| checkpoint + slot enumeration + write + readback + reload | 15.68 s | 0.82 s + 1.67 s |
| combined fast write/reload, deferred readback | 9.16 s | 2.42 s |
| fast write/reload + cached sound slots | 8.27 s | 2.48 s |
| cached slots, 3 ms page delay | 8.63 s | 0.81 s + 1.59 s |
| cached slots, 1 ms page delay | 8.50 s | 2.28 s |
| cached slots, 0 ms page delay | 7.91 s | 2.46 s |
| inactive-slot upload, then direct switch | 5.64 s upload + 1.82 s switch | 0.54 s + 0.50 s |
| inactive-slot upload, preflight-aligned switch | 6.04 s upload + 1.87 s switch | 0.72 s + 0.08 s + 0.57 s |

Caching known sound slots saved 0.89 seconds but did not reduce the firmware's
roughly 2.4-second commit/reload interruption. Zero additional pacing produced
the fastest single run, 7.91 seconds, and all four pacing variants matched on
deferred byte-for-byte readback. The 3 ms and 1 ms single-run timings show that
firmware/run-to-run variance is large enough that the theoretical sleep savings
do not appear monotonically without repeated trials.

## Opt-in low-overhead command

The JSON must name a previously captured `baseArchive`; fast mode deliberately
does not read the live slot before compiling.

```sh
python3 tools/send_project.py \
  --json project.json --product ep133 --port EP-133 --project 1 \
  --fast-live --trust-sound-slots
```

`--fast-live` combines FILE_PUT and reload in one guarded MIDI session and skips
the immediate checkpoint/readback. `--trust-sound-slots` skips library enumeration.
Only use it when every referenced slot was previously verified. Keep an external
checkpoint and perform a normal readback after the performance-critical period.

## FILE_PUT pacing

The EP-133 advertises a 512-byte FILE chunk. Framing plus MIDI 7-bit packing leave
433 raw project bytes per page, so a 59,904-byte archive needs 139 acknowledged
pages. The default sender waits 6 ms after every acknowledgement, about 0.83 s of
explicit pacing for this archive.

Use `--put-page-delay-ms 3`, `1`, or `0` to research tighter pacing. Acknowledgement
serialization remains in place. Lower pacing may expose a distinction between
"accepted" and "fully committed" inside firmware, so retain a checkpoint.

On the tested EP-133, 3 ms, 1 ms, and 0 ms all completed and verified. Zero delay
is therefore a useful opt-in for latency-sensitive work on this firmware, but the
6 ms default remains conservative because this is one device/firmware sample.

## Oversized pages: research only

Sending more than 433 raw bytes contradicts the device's advertised 512-byte
packed-frame capacity. It is separately gated:

```sh
python3 tools/send_project.py ... \
  --unsafe-put-payload-bytes 866 \
  --acknowledge-oversized-put "I ACCEPT OVERSIZED FILE_PUT"
```

An oversized request may be rejected, time out, or put firmware into a debug loop.
Stop all FILE traffic on a debug frame, power-cycle the EP-133, and restore the
checkpoint. Never use this option on a project without a recovery copy.

### Measured boundary

The EP-133 rejected a 512-raw-byte page (only 18% above the advertised 433-byte
payload) immediately with clean status text `buffer overflow`. The failed PUT had
already opened the project destination: the slot subsequently read as a 53,248-byte
prior image rather than the expected 59,904-byte archive, and playback remained
silent for the final 10.62 seconds of the capture. Restoring through normal
433-byte pages succeeded and verified.

This establishes the advertised 512-byte packed-frame size as a real enforced
boundary on the tested unit. Do not use oversized pages as a performance option;
the gated flag remains only for protocol research on recoverable scratch slots.

## Double-buffering through an inactive project

Writing the archive to an inactive slot materially reduces audible interruption.
The tested sequence was:

1. checkpoint the destination slot outside the critical window;
2. FILE_PUT the archive with 0 ms extra page delay, without reload or readback;
3. wait for a musical boundary;
4. activate the destination directly with `reload_project(project, cycle=False)`;
5. verify the destination archive after capture.

Two Project 1 ↔ Project 2 runs completed and verified byte-identically. Inactive
FILE_PUT still caused a short 0.54–0.72 second commit interruption, but the direct
activation interruption was only 0.50–0.57 seconds. Total detected silence was
about 1.0–1.4 seconds rather than the roughly 2.4 seconds observed while replacing
and cycling the active slot.

`tools/double_buffer_capture.py` implements the measurement workflow. The guarded
activation session listens for debug frames for 1.2 seconds before sending. Start
the command about 1.22 seconds before the desired boundary so the actual activation
lands near it. In the compensated run, silence began at 12.82 seconds for a nominal
12.857-second boundary (about 37 ms early).

```sh
python3 tools/double_buffer_capture.py \
  --tar compiled.tar --project 2 \
  --wav double-buffer.wav --report double-buffer-report.json \
  --bpm 112 --switch-preflight-lead 1.22
```

The script's boundary is calculated relative to capture start, not the sequencer's
true bar phase. Production tooling should phase-lock to returned sequencer MIDI,
external clock, or a user-supplied transport epoch. The experiment nevertheless
isolates upload and activation effects and validates the preflight compensation.
