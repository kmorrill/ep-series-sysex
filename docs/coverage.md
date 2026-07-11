# Coverage — what's verified, believed, and unknown

An honest map of the decoding frontier: which bytes are proven, which are
merely explained, and which are open. **Firmware of record: EP-133 OS 2.5.1;
EP-40 OS 2.5.1** (EP-40 format bytes originally decoded on 1.0.3 and verified
identical — see [ep40.md](ep40.md)). Open questions below are contributions
welcome — the fastest wins are single hand-gesture captures.

## Provenance and method

This documentation is derived from a **private capture lab**: a connected
EP-133 and EP-40 driven over SysEx/MIDI, with every claim tied to a firmware
version and a hashed, firmware-stamped capture. Method:

- **Single-variable isolation** — change exactly one thing on the device (or
  one byte in a synthesized archive), then **full-TAR before/after diff**.
- **Firmware-stamped, hashed captures** — no diffing across firmware versions
  unless the diff *is* the firmware comparison (byte layouts change; e.g.
  `fx_settings` 144 B → 160 B).
- **USB-audio closed loop** (OS 2.5+): synthesize bytes → FILE write → play
  over external MIDI clock → capture the device's own audio → measure. This
  is what upgraded claims from "mapped" to "verified" for the perceptual
  fields ([measured-curves.md](measured-curves.md)).
- Cross-checked against the official EP-133/EP-40 guides and community
  reference implementations (see below).

## Prior art and acknowledgments

Four community projects independently reverse-engineered parts of this
protocol/format before or alongside this work, and their published findings
were used to cross-check (and were re-verified against device captures before
inclusion here). No code from these projects is included in this repository —
the `epsysex` implementation was written independently from device captures —
but the knowledge lineage deserves credit:

- [ZacharySBrown/ep133-ppak](https://github.com/ZacharySBrown/ep133-ppak)
  (MIT) — SysEx protocol + `.ppak` documentation; the FILE sub-command,
  metadata-schema, and `meta.json` write-ups here were reconciled against its
  `PROTOCOL.md`.
- [icherniukh/ep133-krate](https://github.com/icherniukh/ep133-krate) (MIT) —
  live FILE list/get/put/delete/metadata behavior, `active`/`sym` metadata
  semantics, `sample.start`/`sample.end` trim writes.
- [gabriel-roth/knockout](https://github.com/gabriel-roth/knockout) (MIT) —
  practical `.pak`/`.ppak` generation and pad-record encode/decode prior art.
- [phones24/ep133-export-to-daw](https://github.com/phones24/ep133-export-to-daw)
  (AGPL-3.0) — pad/pattern/scene/settings/fx parser offsets used as prior art
  for cross-checking only; all layouts documented here were independently
  re-derived and verified from device captures, and no code from that project
  is included or derived from (its AGPL license therefore does not extend to
  this repository).

Confidence vocabulary used throughout these docs:

- **verified** — round-trip proven (bytes → device → measured audio/MIDI, or
  byte-exact write/readback).
- **observed / believed** — consistent across corpora or explained in
  principle, not yet held to byte-exact validation.
- **unknown / disputed** — flagged as such; never silently resolved.

## Shared EP coverage summary (by member)

"Mapped" = we know what the bytes mean; "verified" = round-trip proven.

| Member | Mapped | Verified | Frontier |
|---|---:|---:|---|
| pad record (27 B × 48) | ~96% | ~90% | byte 25 (inert), legato overlap, time-mode 2, trim-start width |
| pattern member | ~95% | ~90% | header bytes 0/3, flag-byte *values* (playback-inert) |
| settings (222/224 B) | ~90% | ~70% | bytes 0–3, 8–23, scale/root offsets (may be global) |
| scenes (712 B) | ~94% | ~85% | header internals, trailer flags, song-order activation |
| fx_settings (160 B) | ~80% | ~65% | byte 4 sidechain enable/mode, bytes 8–11, punch-in bank slots |
| **byte-weighted total** | **~85%** | **~70%** | |

## Solved highlights (do not re-derive)

All in [project-format.md](project-format.md): the full 27-byte pad record;
note events incl. the velocity/pitch/amp/pan/release laws; fader automation
(encoding, latching, base interplay — round-trip); scenes chunks + per-scene
time signature (round-trip); settings BPM / fader bases / FX sends / fader
assignment; fx_settings type + both param banks (all 6 types), output
compressor, sidechain routing/length; mute groups; MIDI channel; pad
monophony. Riddim: loop mode, `live`/LSS member, supertone format + knob
semantics ([ep40.md](ep40.md)).

Also **resolved as global (not in the archive)**: swing/timing (three
byte-identical archives across swing moves) — bake swing into note ticks.

## Open questions

### Pattern member

- **Header byte 3** — always 0 observed; candidate record-count high byte or
  reserved. Not time signature (tested). Probe only if a native capture ever
  changes it. (Header byte 0 is CLOSED: firmware-validated must-be-zero —
  never fuzz, see [errors.md](errors.md#err-pattern-71).)
- **Event flag byte 7 values** — half-closed: 0/2/4/6/8/255 are audibly
  identical on isolated hits (playback-inert); the device writes 0/6/8/31/255,
  presumably editor metadata. Specific meanings unknown. Flag 255 as an
  accent floods over MIDI.

### settings

- **Bytes 0–3 and 8–23** (all zero) — version/header, quantize, input-gain
  candidates; swing ruled out.
- **Scale / root — scope question.** Bytes 220–221 read `[0,1]` on 2.5.1
  members (`[0,2]` in a 2.0.5 corpus) and were historically claimed as
  scale/root, but a TE tutorial places scale & key at the **system (global)
  level**. Member size itself is inconsistent (222 vs 224 B), and a 222-byte
  member can't hold the originally claimed offsets 222/223. Verify scope
  first (change scale, save, diff two projects), then decode.
- **Member size 222 vs 224 B** across device-created 2.5.1 captures —
  unexplained.

### scenes / song mode

- ~~**Song-order activation.**~~ **CLOSED (EP-40 2.5.1, live-verified with
  per-scene MIDI markers):** external MIDI start/clock plays exactly the
  scene selected by **trailer byte 3 — the 1-based scene cursor** — looped;
  the song list is never followed by external transport. Song playback is
  the device's own SONG-mode UI transport. The earlier "last counted scene"
  observation was the cursor resting on the last-created scene. Authoring
  consequence: set the cursor (compiler `currentScene`) to the scene
  external starts should play; the song list matters only in on-device song
  mode. **Verified on both devices** (EP-40 and EP-133, OS 2.5.1, marker
  scenes + cursor 1-vs-3 probes).
- ~~**Trailer byte 3**~~ **CLOSED:** the 1-based selected-scene cursor (see
  above); "scene count" was a misread that coincided with the cursor
  position in every prior capture.
- Header internals (`[0,0,0,0,0,4,4]`), remaining trailer zeros (repeat/loop
  flags?), whether per-scene group mutes exist (mutes look runtime-only).
- **2.0.5 scenes members do not transplant to 2.5.1** (project plays nothing);
  cross-version scenes compatibility is itself undocumented.

### fx_settings

- Byte 4 sidechain-adjacent enable/mode semantics.
- Bytes 8–11 (f32 0.5 written) — global wet? punch-in depth?
- The unused param-bank slots — punch-in FX 2.0 assignments? Probably
  performance-only; confirm-not-saved sweep pending.

### Pad record

- Trim start (bytes 4–7): **u24 vs u32 width dispute**; only 0 tested live.
- 26 vs 27-byte record provenance (27 is a Sample-Tool artifact that can throw
  `ERR PATTERN 189`).
- Byte 25: audibly inert; one older parser called it a bar/stretch value.
- Time-stretch mode 2 (bar) behavior on musical content; legato-overlap voice
  behavior; extreme bpm-stretch ratios.

### OS 2.5 features (unprobed as format extensions)

- **Arpeggiator persistence** — TE docs are silent on whether arp state saves
  with the project; resolvable only by an on-device capture/diff.
- Per-pad pattern copy/paste and per-pad time shift (expect tick rewrites —
  good regression probes), auto-chop serialization, sample reverse vs the
  existing time-mode 3.

### Misc

- Which UI gesture (hold-group+fader "group volume" vs plain fader move)
  writes the settings base vs pattern automation — one hand-edit capture
  closes it.
- Input gain / internal FX send (MAIN-mode knobs): per-project or global?
- Bank/program-change sound selection on 2.5.1 (conflicting secondary source —
  see [midi-reference.md](midi-reference.md#midi-input-the-device-recognizes)).
- FILE `MOVE` (0x0c) payload shape; PLAY suffix form for pad fids (slot
  audition, STAT, and sample delete are now implemented and live-verified on
  EP-40 2.5.1 — see [file-protocol.md](file-protocol.md#beyond-project-io); the
  working delete opcode is `0x06 <fid>`, not the `0x06 0x02` some references
  show).

### Riddim ([ep40.md](ep40.md#residual-unknowns))

- Supertone automation flag byte values 6/8/16.
- Subtle knobY parameters (age/PWM/sweep) unquantified.
- `live` + `patterns` coexistence and whether time-signature persistence
  differs from the shared per-scene representation.

## Contributing

The highest-value contributions are **one-gesture captures**: put a device in
a known state, change exactly one thing, export/read the project before and
after, and diff. Always record the firmware version with the capture
([sysex-protocol.md](sysex-protocol.md#firmware-version--midi-universal-identity-request),
or `meta.json` in a [`.pak`](pak-container.md)), keep one variable per
capture, and mind the safety invariants in
[project-format.md](project-format.md#-safety-invariants-read-first) — some
zero bytes are firmware-validated, and fuzzing them on a live device has
required a flash format to undo.
