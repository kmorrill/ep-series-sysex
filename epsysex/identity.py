"""Firmware/device identity for EP devices.

Two paths:

* :func:`identity_from_device` sends a standard MIDI Universal Identity
  Request (``F0 7E 7F 06 01 F7``). This does NOT touch the TE FILE subsystem
  and is safe to run anytime, even mid-session.
* :func:`identity_from_ppak` reads ``device_version`` from a ``.ppak``/``.pak``
  export's ``meta.json`` — fully offline.

The TE identity reply is parsed defensively and raw bytes are always included
so an unexpected layout can still be decoded by hand. Note: EP-133 OS 2.5.1
replies with zeroed version bytes (a behavior change vs <=2.0.5), so a live
identity read alone may not resolve the firmware version on newer OSes.
"""
from __future__ import annotations

import json
import time
import zipfile
from pathlib import Path

from .sysex import TE_MANUFACTURER

IDENTITY_REQUEST = [0xF0, 0x7E, 0x7F, 0x06, 0x01, 0xF7]
PORT_HINTS = ("ep-133", "ep133", "k.o", "ko ii", "koii", "ep-40", "ep40", "riddim", "teenage")


def identity_from_ppak(path):
    """Read device identity fields from a .ppak/.pak meta.json (offline)."""
    path = Path(path)
    with zipfile.ZipFile(path) as zf:
        meta_name = next(
            (n for n in zf.namelist() if n.lstrip("/").lower() == "meta.json"), None
        )
        if meta_name is None:
            raise ValueError(f"no meta.json in {path}")
        meta = json.loads(zf.read(meta_name))
    return {
        "device_version": meta.get("device_version"),
        "device_name": meta.get("device_name"),
        "device_sku": meta.get("device_sku"),
        "source": "ppak_meta",
        "ppak": str(path),
    }


def parse_identity_reply(data):
    """Best-effort parse of an Identity Reply (F0 7E cc 06 02 ...)."""
    out = {"source": "identity_request", "raw": " ".join(f"{b:02x}" for b in data)}
    body = data[1:-1] if data and data[0] == 0xF0 else data
    if len(body) >= 5 and body[0] == 0x7E and body[2] == 0x06 and body[3] == 0x02:
        payload = body[4:]
        if tuple(payload[:3]) == TE_MANUFACTURER:
            out["manufacturer"] = "teenage engineering"
            rest = payload[3:]
            # family(2 LE) member(2 LE) version(commonly 4 bytes)
            if len(rest) >= 8:
                out["family"] = rest[0] | (rest[1] << 8)
                out["member"] = rest[2] | (rest[3] << 8)
                ver = rest[4:8]
                out["version_bytes"] = list(ver)
                # TE tends to pack version as ASCII-ish or nibble digits.
                out["device_version"] = ".".join(str(b) for b in ver if b) or None
        else:
            out["manufacturer_id"] = [f"{b:02x}" for b in payload[:3]]
    return out


def identity_from_device(port_substr=None, timeout=3.0):
    """Send a MIDI Universal Identity Request and parse the reply.

    Auto-detects an EP-looking port unless ``port_substr`` is given.
    Requires ``mido`` (installed with the ``epsysex[device]`` extra).
    """
    import mido  # lazy: device I/O only

    ins = mido.get_input_names()
    outs = mido.get_output_names()

    def pick(names):
        if port_substr:
            matches = [n for n in names if port_substr.lower() in n.lower()]
            if matches:
                return matches[0]
            raise ValueError(f"no MIDI port matching {port_substr!r} in {names}")
        matches = [n for n in names if any(h in n.lower() for h in PORT_HINTS)]
        if matches:
            return matches[0]
        raise ValueError(f"no EP-like MIDI port auto-detected. ports: {names}")

    from .sysex import debug_frame_text, is_debug_frame

    in_name, out_name = pick(ins), pick(outs)
    debug_seen = None
    with mido.open_output(out_name) as out, mido.open_input(in_name) as inp:
        out.send(mido.Message("sysex", data=IDENTITY_REQUEST[1:-1]))
        deadline = time.time() + timeout
        while time.time() < deadline:
            for msg in inp.iter_pending():
                if msg.type != "sysex":
                    continue
                body = list(msg.data)
                # A device stuck in a firmware error loop floods the wire with
                # debug frames; never parse one as the identity reply.
                if is_debug_frame(body):
                    debug_seen = debug_frame_text(body)
                    continue
                # Only a Universal Non-Realtime Identity Reply counts.
                if len(body) >= 4 and body[0] == 0x7E and body[2] == 0x06 and body[3] == 0x02:
                    result = parse_identity_reply([0xF0, *body, 0xF7])
                    result["midi_in"], result["midi_out"] = in_name, out_name
                    return result
            time.sleep(0.01)
    if debug_seen is not None:
        raise RuntimeError(
            f"no identity reply on {in_name!r}; the device is emitting firmware "
            f"debug frames instead ({debug_seen!r}) — power-cycle before further work"
        )
    raise TimeoutError(f"no identity reply within {timeout}s on {in_name!r}")
