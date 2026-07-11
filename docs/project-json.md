# Project JSON — the epsysex compiler contract

The JSON authoring format lowers to an EP project TAR. `ep.project.v1` is the
canonical schema label; the older `koii.project.v1` label remains accepted as a
compatibility string because schema labels are informational in version 1.

```text
project.json → validated model → EP TAR
```

Compilation is offline. `tools/send_project.py --json` adds dependency
preflight, optional sound upload, project write/readback, reload, and checkpoint
handling.

## Product profiles

Use `"product": "ep"` for the shared format. Select a named model only when
the document uses a proven model-specific feature:

| value | purpose |
|---|---|
| `ep` | shared EP authoring; default |
| `ep133` | transport/artifact identity when targeting EP-133 |
| `ep1320` | artifact identity when targeting EP-1320 — **experimental: no EP-1320 has been probed; the shared-format assumption is unverified there** |
| `ep40` | Riddim-only 29-byte pads, loop mode, supertones, and `live` |

The authoring profile does not invent a MIDI product byte. Live tools currently
offer only transport product bytes that have been captured and verified.

## Safe base-archive mode

Use a captured project when editing a real project:

```json
{
  "schema": "ep.project.v1",
  "product": "ep",
  "baseArchive": "project-01-capture.tar",
  "settings": { "bpm": 128 },
  "pads": [{ "group": "A", "pad": 10, "slot": 421, "trimEnd": 13699 }]
}
```

In base mode, member order, unspecified members, unspecified pads/patterns,
and unknown bytes are preserved. Authored members are rebuilt and the TAR is
checksummed again. `baseArchive` accepts a relative `path` or base64 object;
`baseArchiveBase64` is also accepted by the in-memory API.

`tools/send_project.py --json` uses its just-read checkpoint as the base when
the document does not name one. This is the preferred live workflow.

## Safety guardrails

- Pattern and pad validity byte 0 always remains zero.
- Scene pattern references must resolve to members in the authored or base TAR.
- Automation parameter IDs are `0…11`; values are `0…32767`.
- Riddim-only fields require `product: ep40`.
- `live` with populated patterns and `live` with `fx_settings` are rejected
  because those combinations have not been decoded. Explicit `allowWith…`
  flags exist only for acknowledged research probes.
- `settings` patches a captured base or `templateBase64`. From-scratch
  fabrication is rejected unless `unsafeFabricate: true`; that path has caused
  `ERR CLOCK 43` and a flash-format recovery.
- Unknown material is preserved explicitly through a base archive or
  `rawMembers`.

## Shared pad, pattern, scene, and song fields

```json
{
  "schema": "ep.project.v1",
  "product": "ep",
  "metadata": { "title": "JSON smoke" },
  "includeDirectories": true,
  "padMembers": "all",
  "pads": [{
    "group": "A", "pad": 10, "slot": 421,
    "trimStart": 0, "trimEnd": 13699, "sampleBpm": 120,
    "amplitude": 100, "pitch": 0, "pitchFraction": 0,
    "pan": 0, "attack": 0, "release": 255,
    "timeMode": 0, "chokeGroup": 0, "playMode": 0,
    "rootNote": 60, "midiChannel": 0
  }],
  "patterns": [{
    "id": "A01", "bars": 2,
    "events": [
      {"tick": 0, "pad": 10, "note": 60, "velocity": 112, "duration": 18},
      {"tick": 72, "padByte": 72, "note": 60, "velocity": 96,
       "duration": 18, "flag": 0}
    ],
    "automation": [
      {"tick": 0, "parameterID": 4, "value": 32000}
    ]
  }],
  "scenes": [{"groupPatterns": [1, 0, 0, 0], "timeSignature": [4, 4]}],
  "song": [1]
}
```

- `pad` is the archive `pNN` number; see the numbering warning in
  [midi-reference.md](midi-reference.md#pad-numbering-warning).
- `trimEnd` is the stored native-frame length, not an absolute endpoint.
- `tick` and `duration` use 96 PPQN.
- Event `note` is musical pitch relative to the pad root and is also emitted
  over MIDI.
- Automation records are interleaved with note records and latch.
- Scene pattern indices and song entries are 1-based. **A defined scene must
  reference a pattern in all four groups on every EP device** (EP-133
  rejects partial-zero scenes with `err pattern 187`, EP-40 with
  `err pattern 213` — both power-cycle debug loops; live-verified on both).
  Reference an empty pattern member (one with no events) for a silent group;
  the compiler rejects zero references on all profiles.
- `currentScene` (1-based, default = last defined scene) sets the scenes
  trailer's **selected-scene cursor** — the scene external MIDI start plays,
  looped. The song list is followed only by the device's own SONG-mode
  transport (EP-40-verified).

## Safe settings authoring

```json
{
  "baseArchive": "captured.tar",
  "settings": {
    "bpm": 128,
    "groupFaders": [
      {"group": "A", "parameter": 0, "baseValue": 1.0},
      {"group": "B", "parameter": 5, "baseValue": 0.42}
    ]
  }
}
```

Parameters are `0 level, 1 pitch, 2 time, 3 lowpass, 4 highpass, 5 fx,
6 attack, 7 release, 8 pan, 9 tune, 10 velocity, 11 modulation`. Base values
are clamped to `0…1`; unspecified and unknown settings bytes survive base mode.

An explicit captured payload may be supplied as `settings.templateBase64`.

## FX, output compressor, and sidechain

```json
{
  "fxSettings": {
    "type": "delay",
    "parameters": {"x": 0.25, "y": 0.75},
    "outputCompressor": {"drive": 0.2, "speed": 0.8},
    "sidechain": {
      "length": 0.3,
      "shape": 0.6,
      "routing": {
        "A": {"sourcePads": [1, 10]},
        "B": {"destination": true}
      }
    }
  }
}
```

FX types are `off`, `delay`, `reverb`, `distortion`, `chorus`, `filter`, and
`compressor`, or numeric `0…6`. All continuous values are normalized `0…1`.
Routing writes a 12-bit source-pad mask plus destination bit 15 for each group.
`templateBase64` may preserve an existing 144/152/160-byte member; otherwise a
160-byte member is generated.

Unknown punch-in bank slots and sidechain enable/mode semantics are not
authored. They survive when a template/base member is used.

## Riddim supertones and loop startup

These fields are product-specific and require `"product": "ep40"`.

```json
{
  "schema": "ep.project.v1",
  "product": "ep40",
  "pads": [{
    "group": "B", "pad": 4,
    "supertone": {"engine": 2, "knobX": 17, "knobY": 231},
    "amplitude": 100, "rootNote": 52
  }, {
    "group": "A", "pad": 10, "slot": 421,
    "trimEnd": 187500, "sampleBpm": 120,
    "timeMode": 1, "playMode": 3, "chokeGroup": 1
  }],
  "live": {
    "armedPads": [{"group": "A", "pad": 10}]
  }
}
```

Supertone engine `0…9` stores symbol `1001…1010` as `sym−1`; knobs are raw
`0…254`. The profile emits 29-byte records for all 48 pads. `live` emits the
48-byte A→D, p01→p12 Loop Startup Sequence bitmap. The compiler currently
allows `live` only in patternless/no-`fx_settings` projects because no captured
project establishes those coexistence rules.

## Sound dependencies and upload

Project TARs reference the shared sound library but do not contain samples.
Declare upload material beside the project:

```json
{
  "sounds": [{
    "slot": 421,
    "path": "audio/kick.wav",
    "name": "JSON kick",
    "samplerate": 46875,
    "channels": 1,
    "bpm": 120,
    "timeMode": "bpm",
    "playMode": "oneshot",
    "release": 255,
    "metadata": {"sound.rootnote": 60}
  }]
}
```

PCM16 WAV input is converted to the requested rate/channels with the bundled
offline converter. Non-WAV paths are treated as raw interleaved PCM using the
declared `samplerate`, `channels`, and `format`.

`tools/send_project.py --json` always preflights referenced slots. Missing,
undeclared slots stop the run; declared missing slots stop unless
`--upload-missing-sounds` is present. Uploads are byte-verified by default and
do not overwrite occupied slots.

## Raw members and removal

```json
{
  "rawMembers": [
    {"name": "future_member", "base64": "AQID"},
    {"name": "empty_dir", "type": "5"}
  ],
  "removeMembers": ["obsolete_member"]
}
```

Raw members can use `base64` or `hex`. They cannot conflict with a structured
section authored in the same document. Base-archive unknown members are
preserved automatically; removal must be explicit.

## Still unresolved

The compiler intentionally does not guess at runtime song activation,
arpeggiator persistence, unknown settings bytes, punch-in FX banks, Riddim
automation flag meanings, or `live`/pattern coexistence. System-global swing,
scale/key, MIDI sync, and similar settings are not project members; reproduce
their effects in authored events where appropriate.
