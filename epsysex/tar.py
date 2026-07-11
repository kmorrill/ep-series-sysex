"""The EP project TAR dialect.

A project lives on the device as a single archive at file id
``3000 + (P-1)*1000``. It is *tar-like* but not POSIX ustar: uid/gid/mtime and
the ustar magic are null-filled, size and checksum are unpadded octal, and
directory entries use typeflag ``'5'``. Matching this stripped dialect keeps
generated archives byte-comparable with device captures and avoids exercising
firmware paths unrelated to project content. Python's ``tarfile`` reads these
archives fine; it does not *write* them byte-identically, hence this module.

Typical member layout::

    pads/{a,b,c,d}/p01..p12   27-byte pad records (byte 0 MUST be zero)
    patterns/{a,b,c,d}NN      pattern events (4-byte header, byte 0 MUST be zero)
    scenes                    712 bytes
    settings                  222 bytes
    fx_settings               160 bytes

Pure stdlib, no device I/O.
"""
from __future__ import annotations

BLOCK = 512


def tar_padding(size):
    """Zero-padding needed to round ``size`` up to a 512-byte block."""
    return (BLOCK - (size % BLOCK)) % BLOCK


def build_header(name, size, typeflag="0", mode=None, mtime=0):
    """Build the stripped-down 512-byte TAR header emitted by the device.

    uid/gid and magic fields are null-filled; size and checksum are unpadded
    octal; the checksum field is space-filled before summing per TAR rules.
    """
    raw_name = name.encode("utf-8")
    if len(raw_name) > 100:
        raise ValueError(f"TAR member name is too long: {name}")
    if mode is None:
        mode = 0o755 if typeflag == "5" else 0o644
    header = bytearray(BLOCK)
    header[0:len(raw_name)] = raw_name
    header[100:108] = f"{mode:07o}\0".encode("ascii")
    if size > 0:
        size_text = f"{size:o}".encode("ascii")
        header[124:124 + len(size_text)] = size_text
    if mtime:
        mtime_text = f"{mtime:o}".encode("ascii")
        header[136:136 + len(mtime_text)] = mtime_text
    header[148:156] = b"        "
    header[156:157] = typeflag.encode("ascii")
    checksum = sum(header)
    checksum_text = f"{checksum:o}".encode("ascii")
    header[148:148 + len(checksum_text)] = checksum_text
    header[148 + len(checksum_text):156] = b"\0   "[:8 - len(checksum_text)]
    return bytes(header)


def build_archive(member_specs):
    """Assemble a device-dialect TAR from member specs.

    Each spec is a dict: ``{"name": str, "payload": bytes, "type": "0"|"5",
    "mtime": int}`` (payload/type/mtime optional; type "5" = directory).
    Member order is preserved. Ends with the standard 1024-byte end marker.
    """
    output = bytearray()
    for spec in member_specs:
        name = spec["name"]
        payload = spec.get("payload", b"")
        typeflag = spec.get("type", "0")
        size = 0 if typeflag == "5" else len(payload)
        output.extend(build_header(name, size, typeflag=typeflag, mtime=spec.get("mtime", 0)))
        if typeflag != "5":
            output.extend(payload)
            output.extend(b"\0" * tar_padding(len(payload)))
    output.extend(b"\0" * 1024)
    return bytes(output)


def _member_size(header):
    size_text = header[124:136].split(b"\0", 1)[0].strip() or b"0"
    return int(size_text, 8)


def _member_name(header):
    name = header[0:100].split(b"\0", 1)[0].decode("utf-8", errors="replace")
    # Device archives null-fill the ustar prefix field, but parse it
    # defensively so foreign ustar archives still resolve to full paths.
    prefix = header[345:500].split(b"\0", 1)[0].decode("utf-8", errors="replace")
    return f"{prefix}/{name}" if prefix else name


def iter_members(tar_bytes):
    """Yield ``(name, content_start, size, typeflag)`` for every member.

    ``content_start`` is the absolute offset of the member payload within
    ``tar_bytes`` (useful for in-place patching). Directories have size 0 and
    typeflag ``'5'``.
    """
    offset = 0
    while offset + BLOCK <= len(tar_bytes):
        header = tar_bytes[offset:offset + BLOCK]
        if header == b"\0" * BLOCK:
            break
        size = _member_size(header)
        typeflag = chr(header[156]) if header[156] else "0"
        content_start = offset + BLOCK
        yield _member_name(header), content_start, size, typeflag
        offset = content_start + size + tar_padding(size)


def member_payloads(tar_bytes):
    """Return ``{name: payload_bytes}`` for every file (non-directory) member."""
    payloads = {}
    for name, content_start, size, typeflag in iter_members(tar_bytes):
        if typeflag != "5":
            payloads[name] = tar_bytes[content_start:content_start + size]
    return payloads


def read_member(tar_bytes, member_name):
    """Payload bytes of ``member_name``, or None if absent."""
    for name, content_start, size, typeflag in iter_members(tar_bytes):
        if name == member_name and typeflag != "5":
            return tar_bytes[content_start:content_start + size]
    return None


def patch_member_in_place(tar_bytes, member_name, member_bytes):
    """Overwrite one member's payload without restructuring the archive.

    The replacement must not be larger than the member's recorded size (TAR
    members cannot grow in place); shorter payloads are zero-padded to the
    original size. Raises ValueError if the member is missing or would grow —
    growing pattern members has correlated with ``err pattern`` debug loops
    on-device, so prefer capacity-preserving patches.
    """
    patched = bytearray(tar_bytes)
    for name, content_start, size, typeflag in iter_members(tar_bytes):
        if name != member_name:
            continue
        if len(member_bytes) > size:
            raise ValueError(
                f"{member_name} grew from {size} to {len(member_bytes)} bytes; "
                "in-place TAR patches cannot grow members"
            )
        patched[content_start:content_start + len(member_bytes)] = member_bytes
        patched[content_start + len(member_bytes):content_start + size] = (
            b"\0" * (size - len(member_bytes))
        )
        return bytes(patched)
    raise ValueError(f"{member_name} was not found in the archive")
