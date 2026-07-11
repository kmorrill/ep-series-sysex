"""EP SysEx framing.

Frame shape (observed from the official EP Sample Tool, verified live on
EP-133 K.O. II OS 2.0.5/2.5.1 and EP-40):

    F0 00 20 76 <product> 40 <0x60|reqid_hi5> <reqid_lo7> <command> <packed7...> F7

* ``product`` is 0x33 for the EP-133 K.O. II and 0x3C for the EP-40.
* Byte 5 carries ``BIT_IS_REQUEST`` (0x40) | ``BIT_REQUEST_ID_AVAILABLE``
  (0x20) | the high 5 bits of a 12-bit request id; byte 6 is the low 7 bits.
* Top-level commands: 0x01 GREET, 0x05 FILE.
* The payload is packed7-encoded: a flag byte followed by up to seven
  low-7-bit data bytes; each raw byte's high bit lives in the matching bit of
  the flag byte.

Responses echo the request id with 0x20 in place of 0x40+0x20:

    F0 00 20 76 <product> 40 <0x20|reqid_hi5> <reqid_lo7> <command> <status> <packed7...> F7

``status`` 0 is success; a nonzero status carries an ASCII reason in the
packed7 payload (a *clean rejection* — safe). A frame whose byte 4 repeats the
product byte instead of 0x40 is a firmware **debug/log frame** (ASCII text
follows). Debug frames mean the device is unhappy: stop the batch and
power-cycle before further FILE work.

This module is pure stdlib; nothing here touches MIDI I/O.
"""
from __future__ import annotations

TE_MANUFACTURER = (0x00, 0x20, 0x76)

# SysEx product bytes.
EP133_PRODUCT = 0x33  # EP-133 K.O. II
EP40_PRODUCT = 0x3C   # EP-40

CMD_GREET = 0x01
CMD_FILE = 0x05

BIT_IS_REQUEST = 0x40
BIT_REQUEST_ID_AVAILABLE = 0x20


class EPSysExError(RuntimeError):
    """Base error for EP SysEx/FILE failures."""


class DebugFrameError(EPSysExError):
    """The device emitted a firmware debug/log SysEx frame.

    Per the device-safety rules: stop all FILE traffic immediately and
    power-cycle the device before retrying. Do not use readback/restore as an
    automatic recovery path while a debug loop is active.
    """

    def __init__(self, text):
        super().__init__(f"device reported firmware/debug SysEx: {text}")
        self.text = text


class StatusRejectionError(EPSysExError):
    """The device returned a nonzero status (clean, safe rejection)."""

    def __init__(self, status, reason):
        super().__init__(f"SysEx status {status}: {reason!r}")
        self.status = status
        self.reason = reason


def pack7(raw):
    """Encode raw 8-bit bytes as packed7 (flag byte + 7 low-7-bit bytes)."""
    encoded = []
    for offset in range(0, len(raw), 7):
        chunk = raw[offset:offset + 7]
        flags = 0
        lows = []
        for bit, byte in enumerate(chunk):
            if byte & 0x80:
                flags |= 1 << bit
            lows.append(byte & 0x7F)
        encoded.append(flags)
        encoded.extend(lows)
    return encoded


def unpack7(encoded):
    """Decode packed7 back to raw 8-bit bytes (list of ints)."""
    raw = []
    index = 0
    while index < len(encoded):
        flags = encoded[index]
        index += 1
        for bit in range(7):
            if index >= len(encoded):
                break
            byte = encoded[index]
            index += 1
            if flags & (1 << bit):
                byte |= 0x80
            raw.append(byte)
    return raw


def build_file_request(product_byte, request_id, raw_payload, command=CMD_FILE):
    """Build the SysEx data body (without F0/F7) for a FILE request.

    ``raw_payload`` is the raw (un-packed) FILE payload; it is packed7-encoded
    here. Returns a list of ints suitable for ``mido.Message('sysex', data=...)``.
    """
    request_id &= 0x0FFF
    hi = (request_id >> 7) & 0x1F
    lo = request_id & 0x7F
    return (list(TE_MANUFACTURER)
            + [product_byte, 0x40, (BIT_IS_REQUEST | BIT_REQUEST_ID_AVAILABLE | hi) & 0x7F, lo, command]
            + pack7(raw_payload))


def is_te_frame(data):
    """True if the SysEx data body starts with the TE manufacturer prefix."""
    return list(data[:3]) == list(TE_MANUFACTURER) and len(data) >= 5


DEBUG_MARKER = 0x33  # in place of the 0x40 protocol marker


def is_debug_frame(data):
    """True if this TE frame is a firmware debug/log frame.

    Debug frames carry ``0x33`` where a normal frame carries the ``0x40``
    protocol marker — on EVERY product: ``00 20 76 33 33 <ascii>`` on the
    EP-133 and ``00 20 76 3c 33 <ascii>`` on the EP-40 (verified live). An
    earlier ``data[4] == data[3]`` check only matched the EP-133, where the
    product byte happens to equal the debug marker, and silently missed an
    EP-40 debug loop mid-write.
    """
    return is_te_frame(data) and data[4] == DEBUG_MARKER


def debug_frame_text(data):
    """ASCII text of a debug frame body."""
    return bytes(data[5:]).decode("utf-8", errors="replace").strip()


def match_response(data, request_id, command=CMD_FILE):
    """Match a SysEx data body against a pending request.

    Returns ``(status, raw_payload_list)`` when ``data`` is the response to
    ``request_id``/``command``; returns ``None`` for unrelated frames.
    Raises :class:`DebugFrameError` on a firmware debug frame — callers must
    stop the batch (device-safety rule).

    The status byte is stripped *before* packed7 decode; decoding it as
    payload causes false page mismatches and has wedged devices historically.
    """
    if not is_te_frame(data):
        return None
    if is_debug_frame(data):
        raise DebugFrameError(debug_frame_text(data))
    if len(data) < 9 or data[4] != 0x40:
        return None
    request_id &= 0x0FFF
    expected_hi = BIT_REQUEST_ID_AVAILABLE | ((request_id >> 7) & 0x1F)
    if data[5] != expected_hi or data[6] != (request_id & 0x7F) or data[7] != command:
        return None
    status = data[8]
    payload = unpack7(data[9:])
    return status, payload


def status_reason(payload):
    """Decode the ASCII reason carried by a nonzero-status response payload."""
    return bytes(payload).decode("utf-8", errors="replace").strip("\x00 ")
