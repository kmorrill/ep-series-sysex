# FILE protocol — the filesystem over SysEx

The `0x05 FILE` command (see [sysex-protocol.md](sysex-protocol.md)) carries a
small filesystem protocol: a node tree of sounds, projects, groups, and pads,
with read/write/list/stat/metadata/play operations. This is the layer the
official EP Sample Tool uses, and it is the only programmatic way in or out of
the device — there is no mass-storage mode. **Firmware of record: OS 2.5.1**;
model-specific transport or opcode evidence is identified where relevant.

All multi-byte integers in FILE payloads are **big-endian** (unlike the
little-endian project TAR internals — see
[project-format.md](project-format.md)).

⚠ **The FILE subsystem is globally single-threaded.** Overlapping FILE
sessions — even two read-only clients — reproducibly wedge the device into a
debug loop (`err lfs 6327`, see [errors.md](errors.md#err-lfs-6327)).
Serialize every read and write behind one lock — and make it **one lock path
per device shared by every tool that talks to it**. Locks from different
tool lineages do not compose: two agents each holding "their own" lock have
zero mutual exclusion (observed live: concurrent tooling with separate lock
files was one missed timeslice away from an overlap wedge). `epsysex` uses
`/tmp/ep-device.lock`; anything else driving an EP device should take the
same file. Relatedly, a client killed mid-stream leaves device session
state that survives `FILE_INIT` — power-cycle before the next write
([errors.md](errors.md#err-82--err-8200)).

## Sub-commands

| Sub-command | Payload | Meaning |
|---|---|---|
| `0x01` | `<flags> <maxlen u32>` | FILE_INIT (flags 0 read / 1 write); response carries the device chunk size |
| `0x02 0x00` | `<flags> <fid u16> <parent u16> <size u32> <name> 00` | PUT open / create. flags `0x05` = READ\|FILE (sounds; `0x04` alone is rejected "can only create files"), `0x06` = READ\|DIR (project TARs) |
| `0x02 0x01` | `<page u16> <data...>` | PUT data page; an **empty page = EOF terminator** |
| `0x03 0x00` | `<fid u16> <offset u32>` | GET open (offset must be 0); response includes the exact byte size |
| `0x03 0x01` | `<page u16>` | GET data page |
| `0x04` | `<page u16> <node u16>` | LIST nodes (paged) |
| `0x05 0x01` | `<fid u16> [...]` | PLAY / audition (see below) |
| `0x06` | `<fid u16>` | DELETE (destructive). Bare command byte + big-endian fid — **not** `0x06 0x02`; see the delete note below |
| `0x07 0x01` | `<fid u16> <json> 00` | METADATA set (partial JSON merge; enums as strings) |
| `0x07 0x02` | `<fid u16> <page u16> [<key> 00]` | METADATA get (paginated JSON, optionally one key) |
| `0x0b` | `<fid u16>` | STAT / info |
| `0x0c` | — | MOVE (inventoried from the official app and community libraries; payload not independently verified here) |

⚠ **DELETE opcode (verified EP-40 OS 2.5.1).** The delete payload is the bare
`0x06` command byte followed by the big-endian fid (`0x06 <fid_hi> <fid_lo>`).
The `0x06 0x02 <fid>` form documented by some community references returns
either `failed to delete` (a clean rejection) or a misleading status-0 **no-op**
and leaves the slot in the `/sounds` LIST — confirmed by upload → `0x06 0x02`
(slot persists) → `0x06 <fid>` (slot disappears) on a bench EP-40. `list_sounds`
is authoritative for confirming a delete; slot metadata can read back stale
("ghost") for a just-deleted slot.

Response payload shapes (after the raw status byte, packed7-decoded):

```text
INIT:      <flags-ish byte> <chunk size u32>
GET open:  <file id/flags> ... <size u32 at bytes 3-6> <name> 00
GET data:  <page u16 echo> <raw bytes...>
LIST:      <page u16 echo> then repeated: <fid u16> <flags u8> <size u32> <name> 00
STAT:      <fid u16> <parent u16> <flags u8> <size u32> <name> 00
META get:  <page u16 echo> <json text...>   (last page ends with a 0 byte)
```

## The node / file-id tree

Live-walked with STAT + LIST on OS 2.5.1:

```text
0     /                       dir (flags 0x0e)
1000  /sounds                 dir; children = sample slots 1..999 (fid = slot number)
2000  /projects               dir
3000+ /projects/PP            one dir per project: project_fid = 3000 + (P-1)*1000
                              (P1=3000 … P9=11000)
3100  /projects/01/groups     dir (project_fid + 100)
3200  /projects/01/groups/A   dir (+100 per group: B=3300, C=3400, D=3500)
3201  …/A/01                  pad "file" (flags 0x1d, size 0); metadata = {"sym"}
```

```text
pad_fid = 3200 + (P-1)*1000 + group*100 + pad     group A=0 … D=3
```

⚠ SysEx pad numbering counts **top→bottom**; the TAR's `pNN` member names
count **bottom→up**. See the
[pad numbering warning](midi-reference.md#pad-numbering-warning).

Two important structural facts (verified):

- **Pattern/settings/scenes members have no fids.** The project TAR is a
  serialization produced when the project fid is read — its members are not
  individually addressable files.
- Sound STAT sizes match raw PCM byte counts exactly.
- Group-dir metadata is `{"active": <pad fid>}` (the group's pad cursor); pad
  metadata `{"sym": <slot>}` is the assignment key, but it reads 0 unless set
  via metadata — it is **not** synced from the project pad records.

## Reading a project TAR

```text
FILE_INIT  (read)
FILE_GET open:  03 00 <project_fid u16> <offset u32 = 0>
FILE_GET data:  03 01 <page u16>        repeat, incrementing page
```

- The open response carries the **exact TAR byte size**; read exactly that
  many bytes and stop. Speculative reads past EOF put the device into a bad
  file-session state ([errors.md](errors.md#err-82--err-8200)).
- Each data page is `<page u16 echo> <raw TAR bytes>` (up to 324 content bytes
  per page observed with the 512-byte device chunk size).
- Remember the status byte is **outside** packed7
  ([sysex-protocol.md](sysex-protocol.md#response-structure-and-the-status-byte));
  decoding it as payload makes pages appear to echo wrong numbers.

## Writing a project TAR

The Sample-Tool-derived flow replaces the whole archive (there is no partial
member write — patch the TAR client-side and re-upload):

```text
FILE_INIT write:   01 01 00 40 00 00                     (maxlen 0x00400000)
FILE_PUT open:     02 00 06 <project_fid u16> 07 d0 <tar_size u32> <name> 00
                   (flags 0x06 = READ|DIR; parent 0x07d0 = /projects = 2000;
                    name e.g. "01")
FILE_PUT data:     02 01 <page u16> <tar bytes...>       repeat
FILE_PUT EOF:      02 01 <next page u16>                 (empty page)
FILE_INIT:         01 01 00 40 00 00                     (re-init)
```

For a 512-byte device chunk, 433 raw TAR bytes fit per PUT data page (packed7
overhead included), so a ~59 KB project spans ~136 pages plus the terminator.

Community protocol notes once described the project data phase as
unmapped/read-only; **whole-TAR writes are live-verified working on 2.5.1
on both the EP-133 and the EP-40** (write → reload → readback → activate →
play, repeatedly; EP-133 evidence includes the full HIL-suite artifacts of
2026-07-11, EP-40 evidence the arrangement campaign of 2026-07-10).

⚠ **The firmware normalizes non-native-shaped archives at write-commit**
(live-verified EP-133 2.5.1): a sparse archive (used pads only, no directory
entries) reads back as the full form with all 48 default pad records and
directories materialized, and members may be re-serialized in
device-canonical order. Every written member's payload survives intact —
this is normalization, not corruption — but it means **byte-exact readback
is only guaranteed for native-shaped archives**; verification of sparse or
reordered writes must compare member payloads, not raw bytes. Note also
that byte-exact readback never proves loadability: activation is a separate
validation boundary (see [errors.md](errors.md#err-pattern-213)).

### Project reload / activation sequence

A freshly written archive is not live until the project is reloaded. The
official-app-derived cycle switches the active project away and back, then
reselects group and pad, using metadata set/get pairs:

```text
07 01 07 d0 {"active":4000} 00      set /projects.active → another project
07 02 07 d0 00 00 active 00         readback
07 01 07 d0 {"active":3000} 00      set /projects.active → target project
07 02 07 d0 00 00 active 00         readback
07 01 0c 1c {"active":3200} 00      groups dir 3100 → active group A
07 02 0c 1c 00 00 active 00         readback
07 01 0c 80 {"active":3201} 00      group A 3200 → active pad 1
07 02 0c 80 00 00 active 00         readback
```

(Community RE also reports a dedicated project-switch command, byte 6 = `0x7C`;
not independently verified here.)

Practical timing note (live-verified on the EP-133, both 2.0.5-era and
2.5.1): byte-exact readback does not prove the
sequencer runtime is settled — starting transport immediately after a write
has produced MIDI note floods. Allow a settle window (~6 s) between
write/reload and playback.

## Metadata

`0x07 0x01` sets metadata as a **partial JSON merge** on a fid; `0x07 0x02`
reads it back as paginated JSON (page echo + text; last page NUL-terminated).

### Sample-slot metadata (fid = slot 1–999)

19 fields live on 2.5.1:

```text
channels, samplerate, format, crc        read-only, set at upload
name                                     ≤ 20 ASCII chars
sample.start, sample.end                 trim, in frames
sound.loopstart, sound.loopend           i32, −1 = none
sound.amplitude                          0–100
sound.playmode                           "oneshot" / "key" / "legato"
sound.pan                                −16..16
sound.pitch                              float semitones
sound.rootnote                           MIDI note
time.mode                                "off" / "bar" / "bpm"
sound.bpm                                1–200   (stretch: speed = project_bpm / sound.bpm)
sound.bars                               power-of-2 clamp
envelope.attack, envelope.release
```

These are the **sample-side defaults that seed a pad record on assignment**;
after assignment the per-project pad record owns the values
([project-format.md](project-format.md#scope-of-control)).

Write rules (learned the hard way):

- Enums (`sound.playmode`, `time.mode`) may be *emitted* as integers but must
  be **written as strings**.
- `sound.playmode` must be paired with `envelope.release` in the same write.
- Partial pad-metadata writes can make the device resync pad fields from the
  slot and clobber overrides.

### Pad metadata fields

```text
sym, sound.playmode, sample.start, sample.end, envelope.attack,
envelope.release, sound.pitch, sound.amplitude, sound.pan, sound.mutegroup,
time.mode, midi.channel
```

`sym` assigns a sample slot to a pad node; `sample.start`/`sample.end` are the
trim edit surface; `active` (on directory nodes) is the UI/tree navigation and
selection key.

## PLAY / audition (0x05 0x01)

`0x05 0x01 <fid>` on a **slot fid** auditions the raw sample file, applying
**no playback metadata** (envelope and `sample.start/end` trim verified
ignored via null sweeps). **Pad fids are accepted but silent** (a suffix form,
if any, is unknown). This is a file audition, not a sequencer transport
command.

## Beyond project I/O

The FILE layer reaches considerably more than the project archive. Summary of
the programmable control surface (all live-verified on OS 2.5.1 — sample
library and project flows on both the EP-133 and the EP-40 — unless noted):

| Domain | How | `epsysex` |
|---|---|---|
| **Sample library** (slots 1–999; shared across all projects, never in a project TAR) | create `0x02 0x00` flags `0x05` + paged data; metadata set (19-field JSON); delete `0x06 <fid>`; read `0x03`. Full upload + byte-exact readback + delete verified | `FileClient.upload_sound` / `read_sound` / `list_sounds` / `delete_sound` |
| **Per-sample sound defaults** (seed pad records on assignment) | metadata set on the slot fid | `get_sample_metadata` / `set_sample_metadata` |
| **Active project** (which project is loaded/played) | metadata set on node 2000: `{"active": project_fid}` | `reload_project` |
| **Active group / pad cursor** | metadata set on the group/pad fid: `{"active": <fid>}` | `set_metadata` |
| **Audition a sample** | PLAY `0x05 0x01 <slot fid>` | `audition` / `audition_sound` |
| **STAT any node** | `0x0b <fid>` | `stat` / `stat_sound` |
| **Whole project TAR** (pads/patterns/scenes/settings/fx at once) | FILE_PUT flow above | `read_project_archive` / `write_project_archive` |
| **Firmware identity** | standard MIDI Identity Request ([sysex-protocol.md](sysex-protocol.md#firmware-version--midi-universal-identity-request)) | `identity_from_device` |

### Sample-library tooling

`tools/sound_tool.py` wraps the sample-library surface as a CLI (one serialized
FILE session per invocation, behind the shared device lock):

```sh
python3 tools/sound_tool.py list                       # occupied slots + sizes
python3 tools/sound_tool.py stat 1
python3 tools/sound_tool.py get 1 --out slot001.pcm    # download raw PCM
python3 tools/sound_tool.py meta-get 1
python3 tools/sound_tool.py play 1                      # audition
python3 tools/sound_tool.py meta-set 1 --json '{"name":"kick"}'
python3 tools/sound_tool.py put 56 --in kick.pcm --name kick --verify
python3 tools/sound_tool.py delete 56 --acknowledge-delete "DELETE SOUND 56"
# EP-40: add --product ep40
```

`put` takes **raw interleaved PCM** (no WAV header) and writes
`samplerate`/`channels`/`format` (+ `name`) metadata after the data. Replacing
an occupied slot needs `--acknowledge-overwrite "OVERWRITE SOUND N"`; `delete`
needs `--acknowledge-delete "DELETE SOUND N"`. `set_sample_metadata` /
`meta-set` enforce the write rules below (string enums; `sound.playmode` paired
with `envelope.release`) before touching the wire.

Reachable via **plain MIDI**, not SysEx: pad triggering (note-on/off),
sequencer transport (clock + start/stop/continue), CC modulation, sound
selection via bank/program change — see [midi-reference.md](midi-reference.md).

**Not reachable at all**: every system-global setting (swing/timing, scale &
key, MIDI sync config, velocity curve, LED, rec-rate, metronome, scene-change
timing, song loop…). No known SysEx command sets any of these; they are
UI-only. A project generator must reproduce their *effect* (e.g. bake swing
into note ticks) — see the
[scope-of-control hierarchy](project-format.md#scope-of-control).
