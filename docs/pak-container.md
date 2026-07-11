# `.pak` / `.ppak` containers

The outer archive format used for EP backups and project exports (the
files the official EP Sample Tool reads and writes). **Firmware of record:
EP-133 OS 2.5.1**; the container itself has been stable across releases, with
`meta.json` recording the producing firmware.

## Outer archive

`.pak` and `.ppak` files are **ZIP archives** with **leading-slash entry
names**:

```text
/meta.json
/projects/P01.tar
/projects/P02.tar
...
/sounds/001 sample name.wav
/sounds/002 another sample.wav
...
```

The leading `/` is significant for files intended for the official Sample
Tool — missing leading slashes can produce `PAK FILE IS EMPTY`.

Each `/projects/PNN.tar` entry is a project archive in the EP TAR dialect —
see [project-format.md](project-format.md).

## PAK vs PPAK

Observed conventions:

```text
.ppak   single project pack
.pak    user/factory backup, often multiple projects
```

Common `pak_type` values: `project`, `user`, `factory`. Not every import path
accepts every `pak_type` (exact acceptance rules unmapped — see Confidence).

## `meta.json`

Modern project-style metadata:

```json
{
  "info": "teenage engineering - pak file",
  "pak_version": 1,
  "pak_type": "project",
  "pak_release": "1.2.0",
  "device_name": "EP",
  "device_sku": "TE032AS001",
  "device_version": "2.5.1",
  "generated_at": "2026-07-09T20:17:02.000Z",
  "author": "computer",
  "base_sku": "TE032AS001"
}
```

(Field inventory first documented against Sample Tool exports on OS 2.0.5 by
[ep133-ppak](https://github.com/ZacharySBrown/ep133-ppak) (MIT); the example
above is the generic identity the `epsysex` compiler emits by default, matching
that shape. Set the device identity fields explicitly for a model-specific
artifact.)

`device_version` is the reliable firmware provenance for a capture (useful
because OS 2.5.x zeroes the version bytes in the MIDI Identity Reply — see
[sysex-protocol.md](sysex-protocol.md#firmware-version--midi-universal-identity-request)).

Factory content differs: the official EP-133 factory `.pak` inspected used
`pak_type: factory`, `pak_release: 1.1.0`, `device_version: 1.1.0`
(generated Nov 24, 2023) and had no `base_sku`.

## Sound entries

Named with a zero-padded slot number, a literal space, then the WAV name:

```text
/sounds/001 micro kick.wav
/sounds/100 100_my_sample.wav
```

- The slot prefix maps the file to the sample-library slot (1–999).
- Entries without the slot-plus-space convention may not be picked up.
- WAVs may carry Teenage Engineering metadata in a `LIST/INFO/TNGE` chunk.

Factory/native sample WAVs:

```text
mono · 16-bit PCM · 46875 Hz (device native rate)
smpl chunk · LIST/INFO/TNGE metadata chunk
chunk order: fmt -> smpl -> LIST -> data
```

Import-path compatibility fork (unresolved): some docs and generators disagree
on whether Sample-Tool project imports want 44.1 kHz stereo WAVs or native
46875 Hz mono WAVs. Factory/user backup data uses native TE WAVs; the
user-facing Sample Tool import may transcode ordinary WAVs; generated `.ppak`
files are safest when based on a real reference/template.

## Confidence

The ZIP container is mostly solved. Remaining uncertainty is acceptance rules:

- exact `pak_type` behavior per import path,
- tolerance for compression method and entry order,
- timestamp strictness,
- device SKU / `base_sku` validation,
- the WAV import-path fork above.
