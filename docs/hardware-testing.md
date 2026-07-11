# JSON authoring hardware-in-the-loop suite

`tools/test_device_json.py` exercises the playback-visible `ep.project.v1`
authoring surface through real EP firmware. Offline tests separately cover
raw-member encoding and sound preparation. Ordinary HIL cases compile as
patches over the captured slot, so unknown-member and unknown-byte preservation
also travel through the device; intentionally minimal or research cases use a
scratch archive:

```text
JSON → compiler → TAR → FILE write → byte readback → project reload
     → MIDI START/clock → device MIDI output + stereo USB-audio capture
```

It is intentionally separate from `tests/test_roundtrip.py`: the offline tests
are safe and deterministic, while this suite overwrites a real EP project slot.

## Requirements

- An EP with a transport product byte supported by the runner, connected over USB-C.
- OS 2.5+ for class-compliant USB audio.
- Device system MIDI clock set to **IN** (`SHIFT+ERASE`, code `101`). There is
  no known SysEx setter for this global option.
- A reserved scratch project slot. The runner checkpoints and restores it, but
  a power loss or firmware debug loop can prevent automatic restoration.
- At least one audible sample already assigned in that project's checkpoint.
  Auto-discovery reuses its slot and exact trim bounds. Alternatively pass
  `--sound-slot N --trim-end FRAMES`.
- The HIL dependencies:

  ```sh
  python3 -m pip install -e ".[hil]"
  ```

The suite does not upload or modify the shared `/sounds` library.

## Discover the endpoints

This is read-only:

```sh
python3 tools/test_device_json.py --list
```

On a Riddim, both MIDI directions and a two-channel audio input should normally
appear as `EP-40`. Select ambiguous endpoints with `--midi-input`,
`--midi-output`, and `--audio-device`.

## Run

Choose a slot you are willing to overwrite temporarily. The acknowledgement is
matched exactly and includes the selected slot number:

```sh
python3 tools/test_device_json.py \
  --run \
  --product ep40 \
  --project 9 \
  --acknowledge-overwrite "ERASE PROJECT 9"
```

The default evidence policy is `--evidence both`: returned MIDI and USB audio
must pass. Other useful modes are `midi`, `audio`, and `either`. `either` is
appropriate on hosts where only one of the two device transports is routed.
Use `--no-audio --evidence midi` on firmware before USB audio was added.

Select individual cases while diagnosing:

```sh
python3 tools/test_device_json.py --run --project 9 \
  --acknowledge-overwrite "ERASE PROJECT 9" \
  --case authoring_surface --case pad_sound_design
```

## Coverage and oracles

| Case | JSON surface | Device evidence |
|---|---|---|
| `authoring_surface` | captured-base patching, schema/metadata handling, canonical directories, all pads, all four groups, `pad` and `padByte`, note flags, bars, time signatures, two scenes, song order, `fxSettings` | preservation plus exact TAR readback; reload; MIDI note, velocity, tick, duration and authored channel; audible USB signal |
| `sparse_archive` | `includeDirectories=false`, `padMembers=used` | firmware acceptance, exact readback and playback |
| `pad_sound_design` | slot, trim start/end, sample BPM, amplitude, pitch and fraction, pan, attack, release, time mode, choke group, play mode, root note and MIDI channel | exact readback; every pad emits MIDI and audible signal; hard-left/right behavior is checked from per-hit stereo energy |
| `automation_matrix` | all parameter IDs `0…11`, values at 0/mid/max, same-tick note/automation interleaving, velocity range | exact readback and ordered MIDI playback; USB signal confirms the lane matrix does not make the project wholly silent |
| `fx_output_sidechain` | master FX type/X/Y, output compressor drive/speed, sidechain length/shape and source/destination routing | exact offsets in readback plus multi-group MIDI/audio playback |
| `supertone_pads` | Riddim engines and both saved knob values in 29-byte pad records | exact readback, emitted MIDI, and USB-audio output from three engines |
| `loop_startup` | Riddim loop mode and 48-byte `live` bitmap | exact readback/reload and USB-audio response under external clock |
| `settings_member` | BPM, every fader-base float, all group assignment addresses and base-value clamping | exact readback, reload and playback; **excluded by default because of the risk below** |

The exact-byte readback oracle proves that every JSON field survived compile,
transfer, and device storage. MIDI is the strongest content-independent
playback oracle: it proves the loaded sequencer emitted the authored pitch,
velocity, channel, event time and duration. USB audio proves that the referenced
sound path is actually audible and checks deterministic stereo pan behavior.
Detailed pitch/envelope/trim curve conformance is content-dependent; for those
measurements, choose a long, steady, broadband or tonal reference sample with
`--sound-slot` rather than relying on auto-selected percussion.

Known firmware limitation: after reload, external transport may play the last
counted scene rather than following the saved song list. The two scenes in the
general case therefore use identical pattern references; song bytes are still
verified exactly on device, but runtime song-order activation is not claimed.

## The settings-member probe is deliberately gated

Fabricating a `settings` member in a scratch archive has previously caused
`ERR CLOCK 43` and required a flash format on at least one firmware/device
state. Normal JSON sending now patches a captured settings member; this one
case deliberately exercises the unsafe legacy fabrication path and therefore
requires two independent acknowledgements:

```sh
python3 tools/test_device_json.py --run --project 9 \
  --acknowledge-overwrite "ERASE PROJECT 9" \
  --include-risky-settings \
  --acknowledge-settings-risk "I ACCEPT ERR CLOCK 43"
```

Use this only on a device you can fully restore from a `.pak` backup. The risk
is a firmware behavior, not something checkpointing can make harmless.

## Artifacts and recovery

Each run writes under `tests/artifacts/hardware/<timestamp>/` (gitignored):

- `checkpoint.tar` — the pre-test slot, saved before the first write;
- one directory per case with authored `project.json`, compiled/readback TARs,
  `capture.json`, and stereo `capture.wav`;
- `report.partial.json` during the run and final `report.json`.

Normal exceptions enter the checkpoint restore path and verify its SHA-256 and
bytes after writing. A firmware debug SysEx frame is different: the runner
immediately stops all FILE traffic and does **not** attempt an automatic
restore, because more requests can deepen the debug loop. In that situation:

1. power-cycle the device;
2. restore the captured slot with:

   ```sh
   python3 tools/send_project.py \
     --product ep40 --project 9 \
     --tar tests/artifacts/hardware/<timestamp>/checkpoint.tar
   ```

3. if the device shows `ERR CLOCK 43` or cannot load the slot, follow
   [errors.md](errors.md)'s flash-format and `.pak` recovery procedure.

Never run this suite concurrently with the Sample Tool or another repository
device command. All repository FILE calls share a cross-process lock, but an
external client does not participate in that lock.
