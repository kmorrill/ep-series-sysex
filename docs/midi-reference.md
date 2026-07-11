# MIDI reference — notes, CCs, ids, limits

Shared EP MIDI capabilities (what can be driven or observed without SysEx),
plus device limits and enum tables. **Firmware provenance: EP-133 OS 2.5.1**
for the live pad-map capture; system-setting codes were cross-checked against
the device guide. SysEx-side ids live in
[file-protocol.md](file-protocol.md).

## MIDI note → pad map

Group note ranges (official, live-confirmed):

```text
Group A  36–47      Group C  60–71
Group B  48–59      Group D  72–83
Keys mode: chromatic 0–127
```

Formula: `note = 36 + group*12 + pad_index`, group A=0…D=3. Within a group the
chromatic order follows the physical keypad **bottom-left to top-right**:

```text
note order: . 0 enter 1 2 3 4 5 6 7 8 9
            (note 36 = pad ".", 37 = "0", 38 = "enter", …)
```

<a name="pad-numbering-warning"></a>
### ⚠ Pad numbering warning (three conventions)

Archive `pNN` member names, physical keypad labels, and MIDI notes do **not**
line up in naive ascending order. The archive numbering matches the physical
keypad top row first; MIDI notes run chromatically from the bottom row.
SysEx `pad_fid` numbering ([file-protocol.md](file-protocol.md#the-node--file-id-tree))
counts top→bottom, i.e. it matches the archive `pNN` order below. Never infer
an archive pad path from a MIDI note by simple ascending `p01` order.

| Archive member | Physical pad | Note offset (add to group base) |
|---|---|---:|
| p01 | 7 | +9 |
| p02 | 8 | +10 |
| p03 | 9 | +11 |
| p04 | 4 | +6 |
| p05 | 5 | +7 |
| p06 | 6 | +8 |
| p07 | 1 | +3 |
| p08 | 2 | +4 |
| p09 | 3 | +5 |
| p10 | . | +0 |
| p11 | 0 | +1 |
| p12 | enter | +2 |

(Live-calibrated by pressing every pad and reading the `active` pad-fid
metadata events; e.g. group A pad "." = `pads/a/p10` = fid 3210 = MIDI 36/C1.)

Pattern-event pad bytes are zero-based on the archive number:
`(pad_number − 1) * 8` — see
[project-format.md](project-format.md#note-event-8-bytes).

## MIDI input the device recognizes

The input surface is deliberately minimal:

- **Notes** with velocity (ranges above).
- **CC**: CC1 vibrato/mod · CC12 FX param knob X · CC13 FX param knob Y ·
  CC64 sustain (key/legato voices only).
- **Bank select (CC0/32) + program change** → select sounds 1–999.
  ⚠ Unconfirmed on 2.5.1 — one secondary source claims PC-driven sound
  selection was removed/relay-only in a later OS; retest before relying on it.
- **MIDI clock + start/stop/continue** (realtime `0xF8/0xFA/0xFB/0xFC`,
  24 PPQN). With the device set to clock-in (system code `101`), external
  clock + start drives the sequencer — the standard way to play a project
  programmatically. Note the sequencer's internal resolution is 96 PPQN,
  4× MIDI clock. MIDI stop chokes ringing one-shots immediately.

**No CC maps to the 12 fader parameters**, so fader automation cannot be
recorded over MIDI (corroborated by omission in TE's own docs) — it can only
be authored in the pattern bytes
([project-format.md](project-format.md#fader-automation-record-8-bytes-interleaved)).

MIDI system settings (device-global, `SHIFT+ERASE` codes, **not in the
project archive** and not SysEx-settable):

- clock: `100` off · `101` in · `102` out
- channel: `110` receive all / send ch.1 (default) · `111`–`126` fixed
  ch.1–16 · `127` send only if assigned per sound (ties to pad-record byte 3)
- thru: `130` off (default) · `131` on
- reset controllers at stop: `140` don't (default) · `141` do

Do not conflate MIDI clock with the 8/16/24 **sync rate** menus (`200`–`212`) —
those belong to the separate 3.5 mm TRS sync jack (sync24, needs a TRS→DIN
sync cable), unrelated to USB/DIN MIDI clock.

## MIDI output

During sequencer playback the device emits each event's note byte (which is
also its musical pitch relative to the pad root) with the event velocity;
manual pad presses emit the pad-map notes above. Note output is a good guard
rail for verifying generated projects. Pad MIDI channel is per-pad
(pad-record byte 3).

⚠ **Fader automation modulates the emitted MIDI** (live-verified EP-133
2.5.1): pitch automation (param 1) at 32767 transposes note-out **+12
semitones** (16384 = neutral), and velocity automation (param 10) scales
velocity-out (16384 halves it; 32767 = neutral). A MIDI oracle for a
pattern with PTC/TUNE/VEL automation must model or neutralize those lanes.
External MIDI start also plays the **cursor scene** (scenes trailer byte 3),
looping at that scene's own time signature — a 3/4 scene at 120 BPM loops a
1-bar pattern every 1.5 s (verified by capture).

## USB transports

- **USB-MIDI and 5-pin DIN are equivalent** for notes/CC/clock/SysEx per TE.
  The device's own USB-C port is not documented as a USB-MIDI *host*; a
  computer or MIDI-host box is required in between.
- **USB audio** (OS 2.5+): class-compliant, **stereo in / stereo out only** —
  no per-track/group stems. macOS enumerates it at 48,000 Hz while the engine
  runs 46,875 Hz native; assume device-side resampling and do not treat
  captured-audio sample counts as device frames.
- **Sample transfer**: only via the EP Sample Tool web app (or the
  [FILE protocol](file-protocol.md) it is built on). **No mass-storage /
  drag-and-drop mode exists.**
- **Firmware update**: browser-based WebUSB tool. No documented
  DFU/mass-storage/bootloader recovery path — treat an interrupted flash as
  an unaddressed risk.

## Device limits (from the official guides)

- Samples: **1–999 slots / 128 MB** (whichever first); native rate 46,875 Hz
  (also 26,250 / 32,000 recording rates); mono 40 s / stereo 20 s per sample.
- Projects: **1–9**; **4 groups × 12 pads** each.
- Tempo 40–399 BPM (knob range 60–180; type for full range), decimals allowed.
- Pattern length up to **99 bars** per group pattern.
- **99 scenes**; song list up to 99 positions.
- Sequencer **96 PPQN**; step = 24 ticks; note intervals 1/2/4/8/16/32,
  8T = 12 ticks, 16T = 24 ticks; note duration 1 tick … 1 bar.
- Default factory sample banks: kick 1–99, snare 100–199, hat 200–299,
  perc 300–399, bass 400–499, melodic 500–599.

## Fader parameter indices

Used by `settings` fader bases, fader assignment, and pattern automation:

```text
0 LVL · 1 PTC · 2 TIM · 3 LPF · 4 HPF · 5 FX · 6 ATK · 7 REL · 8 PAN ·
9 TUNE · 10 VEL · 11 MOD
```

## FX type enum + params

`fx_settings` byte 4 (byte offsets in
[project-format.md](project-format.md#fx_settings-member-144--152--160-b-by-firmware-era)):

| Type | FX | knob X (param 1) | knob Y (param 2) |
|---:|---|---|---|
| 0 | off | — | — |
| 1 | delay | length | feedback |
| 2 | reverb | length | color |
| 3 | distortion | drive | color |
| 4 | chorus | modulation | feedback |
| 5 | filter | cutoff | resonance |
| 6 | compressor | drive | speed |
