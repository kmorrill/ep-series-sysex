# Error taxonomy — device errors, causes, recovery

Firmware error signatures observed while reverse-engineering project authoring
on the EP-133, what triggers each, and how to recover. **Firmware of record:
EP-133 OS 2.5.1** (each signature embeds the firmware it was seen on, e.g.
`2_5_1`). Derived from live incidents — every rule here was learned by
tripping it.

## Clean rejection vs debug frame

There are two very different failure surfaces
(see [sysex-protocol.md](sysex-protocol.md#response-structure-and-the-status-byte)):

- **Status-byte rejection** — a normal response with status ≠ 0 and an ASCII
  reason (`invalid id`, `invalid area`, `can only create files`, …). **Safe.**
  The device stays healthy; fix the request and continue.
- **Debug SysEx frame** — `F0 00 20 76 <product> 33 <ASCII log> F7`, e.g.
  `main[331]:    err pattern 187 2_5_1`. The debug marker is the **`0x33` in
  the protocol-byte position** (where a normal frame carries `0x40`) — on the
  EP-133 the frame reads `33 33`, on the EP-40 `3c 33`. Do not detect debug
  frames by "product byte repeated": that only matches the EP-133, where the
  product byte happens to equal the marker (an EP-40 incident went undetected
  mid-write because of exactly that check). **Stop immediately**:
  1. Halt all live FILE traffic.
  2. Passively capture the debug stream for the record (do not trigger more
     I/O).
  3. Do **not** use restore/readback as automatic recovery while the debug
     loop is active.
  4. **Power-cycle** before any further FILE work.

## Quick table

| Signature | Seen on | Cause | Confidence | Authoring rule |
|---|---|---|---|---|
| `err pattern 71` | 2.5.1 | **Nonzero pattern header byte 0** — firmware-validated; rejects the FILE write mid-stream and wedges the FILE subsystem | High (direct probe) | Pattern header byte 0 is always 0; never fuzz it |
| `err pattern 187` | EP-133 2.5.1 | Scene-validation failures: `scenes` referencing missing pattern members (original reproduction), and — **confirmed live 2026-07-11** — a defined scene with a **zero group-pattern reference** (`[1,0,0,0]`), i.e. the EP-133 spelling of the EP-40's `err pattern 213`. Historical "growth into blank projects" reports may be partly re-explained by partial-zero scenes | High for both confirmed triggers | Every referenced pattern present AND every defined scene fully referenced (all four groups); silence a group via an empty pattern member. The compiler enforces both on all profiles |
| `err pattern 213` | EP-40 2.5.1 | **A defined scene chunk with a zero group-pattern reference** (e.g. `[1,0,0,0]`). Fires at **write-commit for the active slot**, at **activation** otherwise (byte-exact readback ≠ loadable). Isolated by an in-place single-scene edit on otherwise native bytes; every native EP-40 scenes chunk in the corpus is all-nonzero. The EP-133's zero-means-silent convention does not port | **High** (single-variable in-place isolation + full-corpus scan + consistent with all six incident archives) | Reference a pattern for **all four groups** in every defined scene; silence a group via an empty pattern member (device idiom: `c02`/`d02`) or a phantom index (native scene 1 references nonexistent `c01`/`d01`). The `ep40` compile profile enforces this |
| `ERR PATTERN 189` | (UI error) | 27-byte Sample-Tool pad records at scene transitions | Medium | Prefer 26-byte native pad records |
| `ERR CLOCK 43` | (UI error, fatal) | A `settings` member **fabricated from scratch** in a generated archive | High | Never fabricate `settings`; transplant a device-written member or generate byte-exact from decoded rules ([project-format.md](project-format.md#settings-member-222-or-224-b--size-varies-across-captures)) — recovery required a **flash format** |
| `err fader 93` | 2.5.1 | Fader-automation values authored outside the device range (scaled 0…65535 instead of 0…32767) | High | Clamp automation values to `0…32767` |
| `err scene 109` | 2.0.5 | Scene/runtime state referencing an invalid or unsupported role/pad/pattern relationship (exact field not isolated) | Medium | Keep scene references, pad assignments, and event pad bytes mutually coherent |
| `err scene 156` | 2.5.1 | Zeroing the `04 04` time-signature bytes of **unused** scene chunks | High | Unused scene chunks must stay `00 00 00 00 04 04` |
| `err lfs 6327` | 2.0.5 | **Overlapping FILE sessions** (even two read-only clients) | High | The FILE subsystem is globally single-threaded — serialize everything behind one lock |
| `err 82` / `err 8200` | 2.0.5 | Client-side FILE state corruption: status byte decoded as packed7 payload, speculative reads past EOF, aggressive retries after partial streams, overlapping reads | High | Exact-size reads; strip the status byte before packed7 decode; no read-past-EOF; no overlap |
| no error, valid readback, **silent playback** | 2.5.1 | Project assigns a sound slot not present under `/sounds` | High | The project TAR and the sound store are separate deliverables — preflight every assigned slot |

## Notes per signature

### `err pattern 71`

Triggered by writing 3 into pattern header byte 0. The device emitted the
debug frame, rejected the FILE write mid-stream, and **wedged the FILE
subsystem until power-cycle**; in the originating incident the project stayed
broken until a flash format. This byte is the canonical example of a
firmware-validated must-be-zero field: bytes that are zero across every known
corpus member may be validated, so never fuzz them on a live device without a
backup plan.

### `err pattern 187`

Reproduced deterministically by writing a `scenes`-only archive whose scene 1
referenced a missing `patterns/a01`. Also seen when importing/growing pattern
members into blank post-format projects — current evidence says the target
project must already contain the relevant device-created members live, not
merely receive them via FILE_PUT. Negative controls that did **not** trigger
it: empty TAR, pattern-only, pad-only, pad+pattern, four groups of
pads+patterns with no `scenes`.

### `err pattern 213`

Incident (EP-40 OS 2.5.1, 2026-07-10, bench Riddim): a base-archive-mode
compile added three new pattern members (`a03`, `c01`, `d01`) to a captured
project; the merge appended them **after `scenes` and `settings`** at the TAR
tail. Scenes referenced those patterns. The FILE_PUT streamed all data pages
normally, then the **EOF-page ack never came**; the device dropped into a
`main[332]: err pattern 213 2_5_1` debug loop (one frame every ~1.03 s,
metronome-steady) with an error screen on the device. FILE_INIT then timed
out; the standard MIDI Identity Request got no real reply either — every
frame on the wire was the debug loop. Recovery: power-cycle, then restore the
checkpoint.

Two compounding client bugs made it worse than it needed to be:

1. The merge-ordering bug itself (new patterns must be *inserted into the
   patterns region*, not appended — fixed in `epsysex.compile`, which now also
   validates the invariant on every compile path).
2. `is_debug_frame` matched only the EP-133 frame shape (`33 33`), so the
   EP-40's `3c 33` debug frames were ignored instead of aborting the write at
   the first frame (fixed in `epsysex.sysex`; see the framing note at the top
   of this doc).

**Investigation ledger (all same-day, bench Riddim, one variable at a time):**

1. *Ordering-only theory — REFUTED.* Identical content with the merge-ordering
   fix (patterns correctly grouped before `scenes`) failed identically at the
   EOF/commit of a write to the **active** slot.
2. *Evidence review:* every historically successful EP-40 project write was
   **patternless** (loop-arm/supertone byte-edits of native captures) — no
   pattern member had ever been FILE-written to a Riddim in any dialect. The
   Riddim's own captures use a 6-byte pattern header
   `[1, bars, 0xFF, 0xFF, count u16 LE]`; the EP-133's is 4-byte
   `[0, bars, count u8, 0]` (an earlier "4 bytes confirmed" claim in ep40.md
   was contradicted by arithmetic on those captures: 254 B = 6 + 31×8).
3. *Native-dialect probe:* one authored pattern in the native 6-byte dialect
   written to an **inactive** slot **committed cleanly and read back
   byte-exact**, then hit the identical `err pattern 213` loop **at
   activation**. Two conclusions: (a) writes to the *active* slot are
   validated at commit, writes elsewhere at activation — so the earlier
   "dialect fixed the commit" observation is **confounded** with the slot
   change, and the 4-byte-dialect-rejection claim is unproven; (b) activation
   is a second validation boundary: byte-exact readback proves nothing about
   loadability.
4. *TAR serialization ruled out:* a zero-change base rebuild through the
   compiler is byte-identical to the device's own archive.

5. *Orphaned-pattern theory — REFUTED.* A compiler-merged archive whose
   scenes referenced **every** pattern member (no orphans, no phantom refs,
   native-dialect headers, correct ordering) still failed at activation.
6. *Positive controls that DO pass* (all on the same day, same device):
   byte-identical native archives (restore, repeatedly, including into
   blank post-format slots); native bytes + a 2-byte in-place **song/trailer
   patch** (write → activate → play state clean). So FILE-written projects
   activate fine per se, and scenes-member edits are legal.
7. *Scenes encoder verified byte-perfect:* the compiler's scenes builder
   reproduces the device's own 712-byte member **byte-identically** for the
   native semantics — encoding is not the problem.

8. *Final discrimination ladder (same device, one variable each):*
   a **one-byte edit inside a native pattern member** activated AND played
   (kills every content-integrity/checksum theory); a **compiler-authored
   replacement pattern** (different size, record style, native-dialect
   header) with native scenes activated cleanly (authored patterns are
   fine); an **in-place scenes edit on pure native bytes** — defining scene
   3 as `[3,0,0,0]` with matching count/song — reproduced the loop.
9. *Corpus confirmation:* every scenes chunk in every native EP-40 archive
   available (user captures + full factory content) is **all-nonzero**;
   partial-zero chunks appear only in our failing writes.

**ROOT CAUSE (confirmed): the Riddim loader rejects a defined scene whose
chunk contains a zero group-pattern reference.** The devices' own idiom for
a silent group is a reference to an **empty pattern member** (`c02`/`d02`)
or, on the Riddim, a **phantom index** with no member behind it
(`c01`/`d01` in native scene 1). All six earlier incident archives
contained partial-zero scenes; the theories they seeded (member ordering,
header dialect, orphaned members) were each refuted by single-variable
probes once the confound was controlled. Ordering and the native pattern
dialect remain enforced as device-native hygiene.

**Cross-device confirmation (2026-07-11): the rule is UNIVERSAL.** The
same probe pair on an EP-133 K.O. II OS 2.5.1 — identical scratch project,
scenes `[1,1,1,1]` vs `[1,0,0,0]` — activated cleanly for the all-nonzero
form and dropped into a `main[331]: err pattern 187 2_5_1` debug loop for
the partial-zero form (fired at write-commit: the slot was active, matching
the EP-40's commit-vs-activation semantics exactly). "0 = no pattern for
this group" was never a device convention on ANY product: no device-created
capture in the entire corpus (both devices, factory content included)
contains a partial-zero chunk — it existed only in this project's authored
examples. The compiler now rejects partial-zero scenes on **every**
profile, and the HIL case builder fills silent groups with empty patterns
on every product.

**Protocol discipline note.** One probe in this ledger had to be re-run: it
was launched while the device was still mid-debug-loop from the previous
failure (its "failure" was the *previous* incident's frames, uptime ~98 s).
Mutating `FileClient` sessions now run a ~1.2 s passive **preflight listen**
and refuse to start against a looping device, so this class of invalid
experiment (and recovery-rule violation) can't recur.

The device reboots into the loop (debug-frame log clock restarts at ~1.04 s),
and if the bad project is the persisted active one, the crash can recur on
boot — which is what makes a flash format necessary rather than a plain
power-cycle. Probing on a **non-active spare slot** keeps the blast radius to
one power-cycle in the persisted-inactive case and protects user content
either way.

### `ERR CLOCK 43`

Displayed on-device after loading an archive whose `settings` member was
fabricated rather than device-derived; fatal, required a flash format. See
safety invariant 3 in [project-format.md](project-format.md).

### `err fader 93`

An automation lane scaled across 0…65535 put the device into a repeating error
loop once transport was enabled. The field's device range is 0…32767
(cross-verified against the settings base floats).

### `err lfs 6327`

A protocol/state failure, not an authoring failure: `lfs` points at the
firmware filesystem layer. Strongest reproduction: two read-only tools
launched in parallel both reading the same project — the device entered the
debug loop before either completed. Rule: treat the FILE subsystem as
globally single-threaded and serialize all live FILE traffic behind one lock.

### `err 82` / `err 8200`

Client-caused FILE-state failures: decoding the response status byte as
packed7 payload (false page mismatches, mid-stream aborts), retrying
init/open after partial streams or timeouts, and overlapping reads. Rules:
exact-size reads from the open-response byte count, strip the status byte
before packed7 decode, no reads past EOF, no aggressive retries, no
overlapping sessions ([file-protocol.md](file-protocol.md)).

**Interrupted-GET aftermath (EP-40 2.5.1, 2026-07-11, with argument dump):**
killing a client mid-GET-stream (a large read-back batch) left device-side
session state that `FILE_INIT` did **not** clear. Subsequent `FILE_INIT`,
LIST, STAT and even metadata reads all succeeded — then the first
**FILE_PUT open** dropped the device into a debug loop:
`main[332]: err 8200 100f0696 10040a15 2c006008 2_5_1`. Rule: after any
abandoned/killed mid-stream FILE session, **power-cycle before the next
write** — successful reads are not evidence the session state is clean. (An
earlier apparent counter-example recovered only because the device dropped
off USB and re-enumerated, which is its own reset.)

### Silent playback (not an error)

A valid, byte-exact-readback project can still play nothing if any assigned
sound slot is missing from `/sounds` (e.g. after a device format). Preflight:
derive the nonzero slots from pad assignments, list `/sounds`, upload missing
samples (with `name`/`samplerate`/`channels`/`format` metadata), and only then
write the project. An empty live `/sounds` is a rebuild case — not proof that
cached samples are stale.

## Recovery ladder

1. **Clean rejection** → fix the request; no device action needed.
2. **Debug frame / error loop** → stop traffic, power-cycle, verify project
   integrity by read-only readback before writing anything.
3. **Wedged or corrupted project** (`err pattern 71` aftermath,
   `ERR CLOCK 43`) → device **flash format**, then restore from a `.pak`
   backup and re-upload sounds. Note a format also wipes device-global system
   settings (clock-in mode, etc.).
