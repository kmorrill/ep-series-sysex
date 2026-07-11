# EP SysEx

Reverse-engineered documentation and tooling for the shared EP SysEx protocol and
saved-project format. Transport and firmware claims are currently verified on
EP-133 and EP-40 hardware; Riddim-only structures are identified explicitly.

Everything here was derived from capture-verified experiments against real hardware:
single-variable device changes, full-archive before/after byte diffs, firmware-stamped
hashed captures, and (for audio behavior) a closed loop that captures the device's own
USB audio and measures it. Claims carry confidence markers — *verified* (round-trip or
audio-proven), *observed*, or *inferred* — and open questions are catalogued rather
than papered over. See [docs/coverage.md](docs/coverage.md) for the provenance model
and the current frontier.

**Firmware of record: OS 2.5.1.** Byte layouts can change across firmware
releases; model-specific evidence is identified where it matters.

> **Unofficial.** This project is not affiliated with or endorsed by Teenage
> Engineering. EP-133, K.O. II, and EP-40 are trademarks of Teenage Engineering.
> This documentation exists for interoperability. Writing to a device can corrupt
> projects if you violate the documented invariants — read
> [docs/errors.md](docs/errors.md) before sending anything, and keep backups.

## The 60-second tour

- A project exports as a `.pak`/`.ppak` — a **ZIP** with leading-slash entries
  (`/meta.json`, `/projects/PNN.tar`, `/sounds/NNN name.wav`).
- `/projects/PNN.tar` is a stripped-header **TAR** whose members hold the project:
  pad records, pattern events + fader automation, scenes/song, settings, master FX.
- Over the wire it's TE SysEx: `F0 00 20 76 <product> 40 <flags|reqid> <cmd>
  <packed7> F7` — product byte `0x33` (EP-133) or `0x3C` (EP-40) — with a
  filesystem-over-SysEx FILE protocol on top (read/write/list/metadata), which also
  exposes non-project state: the sample library, slot metadata, and active
  project/group/pad switching.

## Documentation

| Doc | Covers |
|---|---|
| [docs/sysex-protocol.md](docs/sysex-protocol.md) | Wire frame, packed7 encoding, request ids, GREET, status/rejections, debug frames, live events, identity |
| [docs/file-protocol.md](docs/file-protocol.md) | FILE sub-commands, fid/node tree, read/write/metadata flows, project activation, non-project capabilities |
| [docs/project-format.md](docs/project-format.md) | The project TAR, byte-by-byte: pads, patterns, scenes, settings, fx_settings; safety invariants |
| [docs/pak-container.md](docs/pak-container.md) | The `.pak`/`.ppak` ZIP outer container |
| [docs/project-json.md](docs/project-json.md) | The JSON project-description schema the compiler accepts |
| [docs/hardware-testing.md](docs/hardware-testing.md) | JSON→device HIL suite, MIDI/audio oracles, artifacts and recovery |
| [docs/live-deployment.md](docs/live-deployment.md) | Low-overhead project writes during playback, measured tradeoffs and research flags |
| [docs/ep40.md](docs/ep40.md) | Riddim-only loop mode, `live`, supertone engines and knob semantics |
| [docs/midi-reference.md](docs/midi-reference.md) | MIDI note→pad maps, CCs, clock, device limits, fader parameter and FX enums |
| [docs/errors.md](docs/errors.md) | Device error taxonomy, what causes each, and recovery |
| [docs/measured-curves.md](docs/measured-curves.md) | Audio-measured response laws: amp, velocity, attack/release, pan, pitch |
| [docs/coverage.md](docs/coverage.md) | What's verified vs believed vs unknown; open questions — contributions welcome |

## Tooling

`epsysex/` is a self-contained Python package (stdlib-only core; `mido` needed only
for talking to a device):

```sh
pip install -e .            # offline: compile, parse, diff
pip install -e ".[device]"  # + mido/python-rtmidi for device I/O
pip install -e ".[hil]"     # + NumPy/sounddevice for USB-audio hardware tests
```

```sh
# Compile a JSON project description into a project TAR (or a .ppak)
python3 tools/compile_project.py examples/example-project.json --out out.tar
python3 tools/compile_project.py examples/example-project.json --out out.tar --ppak out.ppak

# Talk to a connected device
python3 tools/identity.py                              # firmware/device info
python3 tools/read_project.py --project 1 --out p1.tar
python3 tools/send_project.py --tar out.tar --project 2  # checkpoints, writes, verifies, reloads
python3 tools/send_project.py --json project.json --project 2  # patches the live checkpoint
python3 tools/send_project.py --json project.json --project 2 --upload-missing-sounds

# Opt-in low-overhead live write (requires a captured baseArchive in the JSON)
python3 tools/send_project.py --json project.json --project 2 \
  --fast-live --trust-sound-slots --put-page-delay-ms 0

# Measure inactive-slot upload followed by a boundary-timed project switch
python3 tools/double_buffer_capture.py --tar out.tar --project 2 \
  --wav double-buffer.wav --report double-buffer-report.json --bpm 112

# Manage the shared sample library (slots 1–999, not part of any project)
python3 tools/sound_tool.py list
python3 tools/sound_tool.py put 56 --in kick.pcm --name kick --verify
python3 tools/sound_tool.py delete 56 --acknowledge-delete "DELETE SOUND 56"

# Inspect
python3 tools/diff_tars.py before.tar after.tar    # member-by-member changed byte ranges
```

The sample library is global — shared across all projects and never part of a
project TAR. `tools/sound_tool.py` exposes list/stat/get/put/meta/play/delete on
it (see [docs/file-protocol.md](docs/file-protocol.md#sample-library-tooling)).
Destructive ops (delete, replacing an occupied slot) require an explicit
acknowledgement string. Select the model-specific transport product only when
the connected device requires it.

`tools/send_project.py` defaults to the safe path: it checkpoints the existing
project, byte-verifies the write by reading it back, and runs the activation
dance so the device actually loads it. JSON mode uses that checkpoint as a
safe base, preserves unknown members, preflights referenced sound slots, and
can upload declared missing WAV dependencies explicitly.

Run the test suite (no device needed):

```sh
python3 tests/test_roundtrip.py
```

Run the JSON-to-device hardware suite against a connected Riddim (this
overwrites and then restores one explicitly acknowledged scratch slot):

```sh
python3 tools/test_device_json.py --list
python3 tools/test_device_json.py --run --project 9 \
  --acknowledge-overwrite "ERASE PROJECT 9"
```

The runner checkpoints the slot, compiles the authored JSON matrix, writes and
byte-verifies every TAR, reloads it, drives playback with external MIDI clock,
captures returned note events and stereo USB audio, writes a machine-readable
report plus WAV evidence, and restores the checkpoint and previously active
project. See [docs/hardware-testing.md](docs/hardware-testing.md) for setup,
coverage, failure recovery, and the separately gated settings-member probe.

Offline tests generate their project archives from authored JSON. Device captures,
OEM projects, samples, checkpoints, and live-test audio are intentionally excluded
from source control.

## Safety, briefly

Device FILE I/O must be serialized (the library takes a lock file for you — never run
two device tools in parallel). A nonzero status with an ASCII reason is a clean, safe
rejection. An unsolicited **debug SysEx frame is not** — stop, power-cycle, restore
from your checkpoint. Never write a nonzero pattern-header byte 0, and don't fabricate
a `settings` member from scratch. Details in [docs/errors.md](docs/errors.md) and
[docs/project-format.md](docs/project-format.md).

## Contributing

The highest-value contributions are captures that close entries in
[docs/coverage.md](docs/coverage.md): remaining unknown byte ranges, OS 2.5
arpeggiator persistence, song-list repeat semantics, and the scale/root scope
question. The methodology that produced everything here is described there too —
one variable per capture, firmware-stamped, byte-diffed.

## Prior art

Community projects that independently reverse-engineered parts of this
protocol were used to cross-check findings (all re-verified against device
captures; no third-party code is included):
[ep133-ppak](https://github.com/ZacharySBrown/ep133-ppak),
[ep133-krate](https://github.com/icherniukh/ep133-krate),
[knockout](https://github.com/gabriel-roth/knockout), and
[ep133-export-to-daw](https://github.com/phones24/ep133-export-to-daw).
Details and licenses in [docs/coverage.md](docs/coverage.md#prior-art-and-acknowledgments).

## License

MIT — see [LICENSE](LICENSE).
