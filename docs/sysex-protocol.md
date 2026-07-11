# SysEx wire protocol

EP devices speak a vendor SysEx
protocol over standard MIDI (USB or 5-pin DIN, equivalent transports). This doc
covers the frame format, the packed7 payload encoding, request/response
structure, and unsolicited events. **Firmware of record: EP-133 OS 2.5.1;
EP-40 OS 2.5.1** (protocol confirmed against both; see [ep40.md](ep40.md)).

The filesystem layer carried by the FILE command is documented separately in
[file-protocol.md](file-protocol.md).

## Frame shape

```text
F0 00 20 76 <product> 40 <flags|reqid_hi> <reqid_lo> <cmd> <payload...> F7
```

| Field | Value |
|---|---|
| SysEx manufacturer id | `00 20 76` (Teenage Engineering) |
| product byte | **`0x33` = EP-133 K.O. II · `0x3c` = EP-40** — a client hard-coded to `0x33` will not talk to an EP-40 |
| byte 5 | `0x40` (protocol marker; a `0x33` here instead means a **debug frame**, see below) |
| byte 6 | flag bits OR'd with the high 5 bits of the request id |
| byte 7 | low 7 bits of the request id |
| byte 8 | command |
| payload | packed7-encoded (requests); responses carry a raw status byte first, then packed7 |

Flag bits in byte 6:

```text
BIT_IS_REQUEST           = 0x40   set on requests, clear on responses
BIT_REQUEST_ID_AVAILABLE = 0x20   set when a request id is present
```

The **request id is a 12-bit value** split across the two bytes: the high 5
bits live in the low bits of byte 6, the low 7 bits in byte 7. A request sends
byte 6 = `0x60 | hi` (`0x40 | 0x20 | hi`); the matching response echoes
byte 6 = `0x20 | hi` and the same low byte, plus the same command byte.

## Top-level commands

```text
0x01  GREET
0x05  FILE   (all filesystem / project / metadata traffic — see file-protocol.md)
```

## Packed7 payload encoding

MIDI SysEx data bytes must stay below `0x80`, so raw 8-bit payloads are encoded
in groups of up to 8 encoded bytes:

```text
<flag byte> <up to 7 data bytes>
```

For each raw byte in a group of 7, the low 7 bits go into a data byte and the
high bit is stored in the flag byte — **bit 0 of the flag byte corresponds to
the first raw byte of the group**, bit 1 to the second, and so on (verified
against a working client implementation).

Worked example — raw bytes `07 D0 00 2C`:

```text
raw:    0x07      0xD0      0x00      0x2C
high:   0         1         0         0      → flag byte = 0b0000010 = 0x02
low7:   0x07      0x50      0x00      0x2C

encoded: 02 07 50 00 2C
```

Decoding reverses this: read a flag byte, then re-attach `0x80` to each of the
next (up to) 7 data bytes whose flag bit is set.

## Response structure and the status byte

**The response status byte sits outside the packed7 payload.** A response is:

```text
F0 00 20 76 <product> 40 <0x20|hi> <lo> <cmd> <status> <packed7 body...> F7
```

Parse the raw status byte first, then packed7-decode only the remaining bytes.
Including the status byte in packed7 decoding shifts every group and loses
high bits — a classic client bug that makes page echoes appear wrong and can
leave a file stream half-open on the device (see
[errors.md](errors.md#err-82--err-8200)).

- **status 0** — success; the packed7 body is the command-specific payload.
- **status ≠ 0** — clean rejection; the packed7 body decodes to an
  **ASCII reason string** (observed: `invalid id`, `invalid area`,
  `can only create files`, `failed to delete`, …). A clean rejection is safe —
  the device stays healthy. (`failed to delete` in particular is returned by the
  wrong-form delete `0x06 0x02`; the correct `0x06 <fid>` succeeds — see
  [file-protocol.md](file-protocol.md#sub-commands).)

## Debug frames — STOP

A frame whose byte 5 is `0x33` instead of `0x40` is a **firmware debug frame**:

```text
F0 00 20 76 33 33 <ASCII log text...> F7
```

e.g. `main[331]:    err pattern 187 2_5_1`. These are not normal responses —
they mean firmware-side validation failed or the device entered an error loop.
Unlike a status-byte rejection, a debug frame means **stop all FILE traffic
immediately**: the FILE subsystem may be wedged, restore/readback is not a safe
automatic recovery, and a power-cycle is required before further file work.
The known signatures and their causes are catalogued in [errors.md](errors.md).

## Unsolicited live FILE events

When a client subscribes via FILE init, the device sends unsolicited
filesystem-style events:

```text
0x03  METADATA_UPDATED
0x08  FILE_ADDED
0x09  FILE_UPDATED
0x0a  FILE_DELETED
0x0d  FILE_MOVED
```

Observed live metadata changes ride on `METADATA_UPDATED` and include `active`,
`sym`, `sample.start`, `sample.end`, `sound.*`, `envelope.*`, `time.mode`,
`name`, `channels`, `samplerate`, `format`, `crc`. Pressing a pad, for example,
emits a metadata event carrying the `active` pad file id (this is how the
[pad map](midi-reference.md#midi-note--pad-map) was calibrated).

These are generic file-system events. No semantic events such as
"scene changed" or "pattern changed" are known to exist; scene/pattern
performance state is not broadcast (inferable only from MIDI note output plus
clock counting).

## Firmware version — MIDI Universal Identity Request

Firmware identification uses the standard (non-TE) MIDI Universal Identity
Request, which is safe and does not touch the FILE subsystem:

```text
request: F0 7E 7F 06 01 F7
reply:   manufacturer 00 20 76, family, member, version bytes
```

Caveat (verified): **OS 2.5.x returns zeroed version bytes** in the reply, on
both the EP-133 and the EP-40 — the version must then be attested from the
device UI or a `.pak` backup's `meta.json` `device_version` field
(see [pak-container.md](pak-container.md)).

Byte layouts change across firmware releases (e.g. `fx_settings` was 144 B in
older public project TARs, 160 B on live 2.0.5+). Never diff captures across
firmware versions unless the diff *is* the firmware comparison.
