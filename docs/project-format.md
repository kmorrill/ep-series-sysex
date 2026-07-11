# Project archive format (`/projects/PNN.tar`)

Byte-level specification of the project TAR an EP serializes over
the [FILE protocol](file-protocol.md) and inside
[`.pak`/`.ppak` containers](pak-container.md). **Firmware of record: OS 2.5.1**
(core offsets re-verified live on EP-133 2.5.1 — Riddim-only deltas live in
[ep40.md](ep40.md); member formats look stable across
2.0.5→2.5.1 so far). Riddim-only deltas are in [ep40.md](ep40.md); measured
audio response curves for the fields below are in
[measured-curves.md](measured-curves.md); what remains undecoded is in
[coverage.md](coverage.md).

Multi-byte fields inside members are **little-endian** (unlike the big-endian
FILE protocol payloads).

Confidence markers: **verified** = round-trip or audio-proven on device;
*observed* = seen consistently in captures/corpora but not round-tripped;
*disputed/unknown* flagged inline. Confidence is never upgraded here.

<a name="-safety-invariants-read-first"></a>
## ⚠ Safety invariants (read first)

These are firmware-validated. Violating them has bricked projects and wedged
the device (full taxonomy in [errors.md](errors.md)):

1. **Pattern header byte 0 must be zero.** It is firmware-validated: writing 3
   made OS 2.5.1 emit debug SysEx `err pattern 71`, reject the FILE write
   mid-stream, and **wedge the FILE subsystem until power-cycle** (in one
   incident, project 1 stayed broken until a flash format). Never fuzz it.
2. **Pad record byte 0 must be zero.** Any nonzero value silences the pad
   entirely (verified by probe).
3. **Never fabricate a `settings` member from scratch.** A fabricated member
   has produced a fatal `ERR CLOCK 43` requiring a flash format. Either omit
   the member, transplant a genuine device-written one (works on 2.5.1,
   verified), or generate it byte-for-byte from the decoded field rules below
   (a rules-generated default member has been proven byte-identical to a
   device-written template).
4. **Never write `scenes` referencing pattern members that don't exist** in
   the same archive/project — reproducibly triggers `err pattern 187`.
5. Clamp fader-automation values to **0…32767** — wider values loop
   `err fader 93`.
6. The 27-byte pad-record form (Sample-Tool artifact) can throw
   `ERR PATTERN 189` at scene transitions; native records are 26 bytes.

## The EP TAR dialect

Device-created archives are TAR-like but **not generic `ustar`**. Header
fields observed in healthy device readback:

```text
name       normal null-terminated path in bytes 0-99
mode       zero-padded ASCII octal, e.g. 0000755\0 or 0000644\0
uid/gid    null-filled (not ASCII 0000000\0)
size       unpadded ASCII octal followed by nulls; zero-size dirs all null
mtime      null-filled
checksum   normal TAR checksum, but unpadded ASCII octal then \0 and spaces
type       5 for directories, 0 for files
magic      null-filled — no "ustar" marker
```

Example: a 27-byte pad member stores size as `33\0…` and checksum as
`3054\0   `. Emit this stripped dialect for byte-exact comparison with device
output; generic `ustar` headers are an uncontrolled variable (the device has
accepted them, but treat that as untested tolerance, not a contract). The
archive is uncompressed; members are block-padded; it ends with the standard
two 512-byte zero blocks.

## Member inventory

```text
pads/{a,b,c,d}/p01..p12   pad records: assignment + per-project sound design
patterns/{a,b,c,d}NN      note events + fader-automation records
scenes                    scene→pattern map + song mode (712 B)
settings                  BPM, fader bases + assignment (~222/224 B)
fx_settings               master FX, output comp, sidechain (144/152/160 B by firmware era)
```

Pattern members live directly under `patterns/` — the group is encoded in the
filename (`a01` = group A pattern 1), not a subdirectory.

Not every project contains every member; factory projects may ship pads plus
an empty `patterns/` directory only.

### Canonicalization and minimum input (verified by write probes)

The device accepts far less than it emits, and rewrites input into a canonical
shape on readback:

- **Canonical readback shape**: directory members (`pads/`, `pads/a`–`d`,
  `patterns/`), the full 48-pad lattice (missing empty pad files are
  materialized), and pattern members sorted into group/index order
  (`a01,a02,b01,…`).
- **Minimum accepted, playable input**: one pad-assignment member + one
  non-empty pattern member. Directories, unused pads, `scenes`, `settings`,
  and `fx_settings` can all be omitted. An entirely empty TAR (just the two
  end blocks) is accepted and inert.
- The device does **not** materialize missing `scenes` or pattern members.
- A project is only audible if every nonzero sound slot it assigns exists
  under `/sounds` — the project TAR and the sound store are separate
  deliverables ([file-protocol.md](file-protocol.md#beyond-project-io)).
- After reload, external MIDI start/clock plays the **last counted scene** in
  `scenes`; the song trailer alone does not activate song-order playback
  (open question — see [coverage.md](coverage.md)).

<a name="scope-of-control"></a>
## Scope of control — what is NOT in the archive

The device scopes state across four levels; **only the middle two live in the
project TAR**. Do not hunt in the archive for sample-level or system-level
state. (Confirmed by capture + TE's own "scope of control" tutorial.)

| Level | Contains | In the TAR? |
|---|---|---|
| **Sample** | raw PCM + 19-field slot metadata (defaults that *seed* pad records on assignment) | No — `/sounds` fids 1–999, shared across projects |
| **Project** | tempo (`settings` BPM), master FX/output (`fx_settings`), and the containers: groups, patterns, scenes, sound assignments | **Yes** |
| **Group** | fader **assignment** + fader **base** position (always applied), the 12 pad sound designs, the group's pattern pool | **Yes** — inside `settings` + `pads/*`, keyed by group |
| **System (global)** | **timing/swing**, MIDI clock/sync/thru, sequencer options, pad settings incl. **scale & key**, velocity curve, LED, rec-rate, takeover | **No** — device-wide; changing them affects every project |

Verified/flagged specifics:

- **Swing/timing is global** — capture-proven (three byte-identical archives
  across swing moves, zero automation records). Author swing by baking tick
  offsets into note positions.
- **Scale & key**: a TE tutorial (fw 1.1) places these at the system level,
  contradicting an earlier "settings bytes 220–223 = scale/root" hypothesis.
  **Treat scale/root as unverified / possibly global** — see
  [coverage.md](coverage.md).

Consequence: a generated project can only control project- and group-level
state. None of the system globals are SysEx-reachable either
([file-protocol.md](file-protocol.md#beyond-project-io)) — reproduce their
effect in generated content instead.

## Pad record — `pads/{group}/pNN` (26 B native / 27 B Sample-Tool)

Factory/native records are **26 bytes**; the official Sample Tool and some
community round-trips emit **27 bytes** (adding the pitch-fraction byte 26).
The 27-byte form can throw `ERR PATTERN 189` at scene transitions. 26-vs-27
provenance remains a known conflict.

⚠ `pNN` numbering follows the physical keypad **bottom-up** (p10 = the "."
pad = the group's lowest MIDI note); see
[midi-reference.md](midi-reference.md#midi-note--pad-map).

| Byte(s) | Field | Semantics | Confidence |
|---|---|---|---|
| 0 | validity flag | **must be 0 — any nonzero value silences the pad** | verified |
| 1–2 | sample slot, u16 LE | 1–999 (0 = unassigned). Riddim supertone pads store `sym − 1` for synth symbols — see [ep40.md](ep40.md) | verified |
| 3 | MIDI channel | 0 = channel 1; N = channel N (byte-patch probe: 5 → note-ons on ch 5) | verified |
| 4–7 | trim start | frames; **width disputed: u24 vs u32** — only 0 tested live | observed |
| 8–11 | trim length, u32 LE | frames at the sample's native rate; audibly truncates playback, scaled by repitch rate | verified |
| 12–15 | sample BPM, f32 LE | with time-mode 1, playback rate = clockBPM / sampleBPM (follows the *external* clock); override encoding byte 13 = 0x80 observed | verified (moderate ratios) |
| 16 | amplitude | 0–200, default 100 ≈ unity; **dB-domain fader**, ~0.5 dB/unit below 100, soft +6 dB ceiling above ([curve](measured-curves.md#amplitude-byte-16)) | verified, 9-point |
| 17 | pitch, i8 | whole semitones −12..+12, by resample; stacks with event note−root | verified ±12 |
| 18 | pan, i8 | −16..+16, negative = left; **constant-power law**, ±16 fully mutes the far channel ([curve](measured-curves.md#pan-byte-18)) | verified |
| 19 | attack | 0–255 attack ramp ([measured points](measured-curves.md#attack-byte-19)) | verified |
| 20 | release | **255 = play-through in every mode.** <255 in one-shot = decay from note-**on**, time ≈ (byte/70)² s; <255 in key/legato = release fade at note-off. 0 chokes to a ~20 ms click ([curve](measured-curves.md#release-byte-20)) | verified, 10-point |
| 21 | time-stretch mode | 0 off / 1 bpm / 2 bar / 3 reverse. Modes 1 and 3 audio-verified; **mode 2 inconclusive** on musical content | partial |
| 22 | mute group | **nonzero = member of the single shared choke group; the value itself is ignored** (1 vs 200 choke each other; 0 never participates). No multiple named groups in the record on 2.5.1 | verified |
| 23 | play mode | 0 one-shot / 1 key / 2 legato (Riddim adds 3 loop — [ep40.md](ep40.md)). Envelope model: with release 255 all modes play through identically; with release <255, one-shot decays from note-on (duration-independent), key/legato sustain to note-off then fade — event duration only matters in key/legato | verified (single-note; legato overlap untested) |
| 24 | root note | MIDI note, default 60. **Playback rate = 2^((event note − root)/12) in every play mode including one-shot** | verified |
| 25 | — | audibly inert at 255; presumed padding. (One older parser called it a bar/stretch value in some records — unreconciled) | observed |
| 26 | pitch fraction (27-B form only) | `pitch = byte17 + byte26/100` (cents/100); +50 measured ≈ +52 cents | verified |

**Voice model (audio-proven):** pads are **monophonic** — every same-pad
retrigger chokes the previous voice. Long samples never ring past their pad's
next event regardless of release 255.

Assignment footgun (live-verified on the EP-133): when assigning a new slot, never patch
bytes 1–2 alone — stale trim/length/mode bytes make the new sample play as a
truncated fragment. Rewrite the playback region (trim start 0, trim length =
sample frame count), BPM, amplitude, release, time mode, play mode, and root
together.

## Pattern member — `patterns/{group}NN`

4-byte header, then `record count` × 8-byte records at 96 PPQN (step = 24
ticks; MIDI clock is 24 PPQN — don't conflate). Riddim OS 2.5.1 captures use
the same header: each populated member is exactly `4 + record_count × 8`.

### Header

| Byte | Meaning | Confidence |
|---|---|---|
| 0 | **must be 0** (firmware-validated — see safety invariants) | verified the hard way |
| 1 | bars (1–99 per the guide; bars=4 measured as an exact 8.00 s loop at 120 BPM) | verified |
| 2 | record count — notes **and** automation records together | verified |
| 3 | unknown; candidate count-high-byte or reserved. **Not time signature** (time signature is per-scene) | unknown |

### Note event (8 bytes)

| Byte(s) | Field | Semantics | Confidence |
|---|---|---|---|
| 0–1 | position, u16 LE | ticks, 96 PPQN; free-time micro-offsets are encoded in the position | verified ±9 ms |
| 2 | pad | `(pad_number − 1) * 8`, zero-based (p07/p08/p09 → `0x30/0x38/0x40` confirmed) | verified |
| 3 | note | **musical pitch relative to pad root byte 24** — repitches the sample even in one-shot mode, and is the MIDI note emitted during playback. `0x3c` (60) = unison with default root | verified |
| 4 | velocity | 1–127; values ≥128 emit velocity 1 (not plain 8-bit). Loudness curve is shallower than linear amplitude ([curve](measured-curves.md#velocity-event-byte-4)). Do not encode "silent" events as velocity 0 — the device still emits velocity 1 | verified, 9-point |
| 5–6 | duration, u16 LE | ticks; min 1, max 1 bar; audible only in key/legato modes | verified |
| 7 | flag | values 0/2/4/6/8/255 **audibly identical** on isolated hits (playback-inert); device writes 0/6/8/31/255 as apparent editor metadata; meanings unknown. Flag 255 as an "accent" caused a rapid retrigger flood over MIDI — **write 0 for generated hits** | partial |

### Fader-automation record (8 bytes, interleaved)

Recorded group-fader automation is stored inline among the note events,
counted by the same header record count. **Round-trip verified** (synthesized
lanes play back exactly):

```text
bytes 0-1   tick, u16 LE          (6-tick sampling while the fader moves)
byte 2      0x01 record type      (0x05 also valid; 2/3/4/6/7 inert)
byte 3      fader parameter index (same table as settings bases, 0-11)
byte 4      0x00
bytes 5-6   value, u16 LE, 0..32767   ⚠ never scale to 0..65535 (err fader 93)
byte 7      0x00                  (Riddim supertone patterns show 6/8/16 here — undecoded)
```

Semantics: **latching** — a value holds until the next record for that
parameter. Automation overrides the group's `settings` fader base from its
tick onward. The group is implicit in the member. The value scale matches the
settings base floats: `base f32 = value / 32767` (cross-verified).

Note events whose byte 2 is not a multiple of 8 below `0x08` are this
automation lane, not pad hits — decoders must separate them.

## `scenes` member (712 B)

7-byte header + 99 × 6-byte scene chunks (from offset 7) + 111-byte song
trailer. Created on the first scene commit; absent from a scene-less project.
Chunk layout **round-trip verified**; trailer decode verified, live song
playback pending.

| Region | Layout | Confidence |
|---|---|---|
| header (0–6) | `[0,0,0,0,0,4,4]` — trailing 4/4 = default time signature; internals otherwise unknown | observed |
| chunk bytes 0–3 | **1-based pattern index** per group A–D (0 = none), plain u8 1–99. **Scene 1 = the live/current arrangement** — changing a group's active pattern writes scene 1 directly (and mints the member). Referenced patterns must exist as members before the scenes member is written (see invariant 4), though the device tolerates implicit empties in device-created projects | verified |
| chunk bytes 4–5 | time-signature numerator, denominator — **per-scene, both literal values** (6/4 → `[6,4]`, 1/2 → `[1,2]`); all 4 groups in a scene share it. **Unused chunks must keep `00 00 00 00 04 04`** — zeroing the `04 04` triggers `err scene 156` | verified |
| trailer byte 3 | scene count / visible scene count (matched scene count in captures; one confused song-editor state left it at 1 with a scene-2 chunk present — treat exact UI meaning as count/cursor-adjacent) | observed |
| trailer byte 11 | song-list length | verified (byte diff + offline decode) |
| trailer bytes 12… | one **1-based scene number per song position** (e.g. `[1,2,1]`) | verified (decode); live song-order playback **unverified** — after reload, external transport plays the last counted scene regardless of the song list |

Commit behavior (SHIFT+MAIN, verified): locks the current scene, opens a fresh
next scene with all groups → pattern 1, increments trailer byte 3, and
**copies each group's currently-playing pattern content into that scene's
pattern-1 members**.

⚠ **2.0.5-era `scenes` members do not transplant to 2.5.1** — a project
carrying one plays nothing under external transport (unlike `settings` /
`fx_settings`, which transplant fine). Patch a native member instead.

## `settings` member (~222 or 224 B — size varies across captures)

| Byte(s) | Field | Semantics | Confidence |
|---|---|---|---|
| 0–3 | unknown (all 0) | version/header candidates | unknown |
| 4–7 | project BPM, f32 LE | loaded on project reload (wrote 100/140, measured exactly) | verified |
| 8–23 | unknown (all 0) | swing ruled out (global); quantize / input gain candidates | unknown |
| 24–215 | fader **bases**: 4 groups × 12 params × f32 LE, `offset = 24 + group*48 + param*4` | −1.0 = untouched, else 0..1 (= automation u16 / 32767, cross-verified). **Always applied to the group's playback; pattern automation overrides per tick.** Param index 5 = the per-group **FX send**, which gates the master FX entirely (send −1/0 = fully dry). LVL law: amplitude ∝ (value/32767)² ([curve](measured-curves.md#group-lvl-fader-law)) | verified (LVL/FX; others located) |
| 216–219 | fader **assignment**, one parameter index byte per group A–D | live diff: assigning A=LPF, B=HPF, C=PTC, D=FX wrote `03 04 01 05` | verified |
| 220–223 | disputed | `[0,1]` observed at 220–221 on 2.5.1 members (both sizes); `[0,2]` in a 2.0.5 corpus. Historically claimed as scale/root, but scale/key is likely **system-global** — see [coverage.md](coverage.md) | disputed |

Fader parameter indices (used by bases, assignment, and automation):

```text
0 LVL · 1 PTC · 2 TIM · 3 LPF · 4 HPF · 5 FX · 6 ATK · 7 REL · 8 PAN ·
9 TUNE · 10 VEL · 11 MOD
```

A default member generated from these rules (zero prefix, BPM f32, forty-eight
−1.0 bases, assignment bytes, tail `[0,1]`) has proven **byte-identical** to a
device-written template for the default state — but see safety invariant 3
before authoring one.

## `fx_settings` member (144 / 152 / 160 B by firmware era)

| Byte(s) | Field | Semantics | Confidence |
|---|---|---|---|
| 4 | master FX type | 0 off · 1 Delay · 2 Reverb · 3 Distortion · 4 Chorus · 5 Filter · 6 Compressor (guide menu order; all live fingerprints consistent). Audible **only** when a group FX send is nonzero | verified, 7-point |
| 8–11 | f32, 0.5 written | unknown — global wet? punch-in depth? | unknown |
| `12 + (type−1)*4` | FX param 1 / knob X, f32 LE | Delay Length @12 · Reverb Length @16 · Distortion Drive @20 · Chorus Mod @24 · Filter Cutoff @28 · Compressor Drive @32 | verified (all 6 types) |
| `76 + (type−1)*4` | FX param 2 / knob Y, f32 LE | Delay Feedback @76 · Reverb Color @80 · Distortion Color @84 · Chorus Feedback @88 · Filter Resonance @92 · Compressor Speed @96 | verified (all 6 types) |
| other bank slots | f32 0.5 defaults | punch-in FX params? unknown | unknown |
| 136–139 | output/master compressor **Drive** (SHIFT+FX knob X), f32 LE | min 0.0 / max 1.0 | verified |
| 140–143 | output/master compressor **Speed** (SHIFT+FX knob Y), f32 LE | default-ish 0.5, max 1.0 | verified |
| 144–147 | sidechain **Length** (knob X), f32 LE | isolated knob diff | verified |
| 148–151 | sidechain **Shape** (knob Y), f32 LE | placed by elimination from fixed-routing captures; Y-only edit would pin it | observed |
| 152/154/156/158 | sidechain **routing**, u16 LE per group A/B/C/D | bit 15 (`0x8000`) = ducked **destination** group; bits 0–11 = **source pad mask**, bit `(pad−1)` for p01..p12; 0 = off | verified |

The master-FX type 6 (insert compressor) is distinct from the output/master
compressor at 136–143. Remaining unknowns: sidechain-adjacent byte-4
enable/mode semantics, byte 8–11, and the unused bank slots
([coverage.md](coverage.md)).

A genuine 2.0.5-era `fx_settings` member is accepted and functional on 2.5.1.
