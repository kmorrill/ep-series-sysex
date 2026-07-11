"""Live FILE-protocol client for EP devices over MIDI SysEx.

Wraps the TE FILE command set (init, get, put, list, metadata get/set) as
verified against real EP-133 K.O. II and EP-40 hardware. The same protocol is
spoken by both devices; only the SysEx product byte differs (EP-133 = 0x33,
EP-40 = 0x3C).

Device-safety invariants baked in — do not weaken them:

* All FILE traffic is serialized through the cross-process flock in
  :mod:`epsysex.devicelock`. The device filesystem is globally
  single-threaded; overlapping sessions (even read-only) wedge it into a
  debug loop until power-cycle. NEVER parallelize FILE operations.
* A firmware debug SysEx frame raises :class:`~epsysex.sysex.DebugFrameError`
  immediately. Stop the batch, power-cycle, and restore before anything else.
* A nonzero response status raises
  :class:`~epsysex.sysex.StatusRejectionError` carrying the device's ASCII
  reason. A clean rejection is safe.
* Reads are exact-size (from the open-response byte count), never past EOF,
  and the response status byte is stripped before packed7 decode.

``mido`` (plus a backend such as python-rtmidi) is imported lazily inside the
device I/O paths only, so the rest of the library works without it.
"""
from __future__ import annotations

import json
import math
import time

from .devicelock import LOCK_PATH, acquire_device_lock
from .sysex import (
    EP133_PRODUCT,
    EP40_PRODUCT,
    DebugFrameError,
    EPSysExError,
    StatusRejectionError,
    build_file_request,
    debug_frame_text,
    is_debug_frame,
    match_response,
    status_reason,
)

SOUNDS_NODE_ID = 1000
PROJECTS_NODE_ID = 2000

FILE_CAPABILITY_READ = 0x04
FILE_TYPE_DIR = 0x02
FILE_TYPE_FILE = 0x01
# Sample slots are plain files: create with READ|FILE. A bare 0x04 is rejected
# ("can only create files"); 0x06 (READ|DIR) is the project-TAR create flag.
SOUND_CREATE_FLAGS = FILE_CAPABILITY_READ | FILE_TYPE_FILE  # 0x05

# Sample-slot metadata keys that must be written as strings even though the
# device may emit them as integers (see file-protocol.md write rules).
_STRING_ENUM_KEYS = ("sound.playmode", "time.mode")

# FILE_GET returns 324 raw bytes per page on both tested devices.
GET_PAGE_BYTES = 324
# FILE_PUT chunk fallback when the device reports an unusable init chunk size.
PUT_CHUNK_FALLBACK = 433

_PORT_HINTS = {
    EP133_PRODUCT: ("EP-133", "KO II", "K.O. II"),
    EP40_PRODUCT: ("EP-40",),
}


def project_fid(project_number):
    """File id of project slot ``P`` (1..99): ``3000 + (P-1)*1000``."""
    if not 1 <= project_number <= 99:
        raise EPSysExError(f"project number {project_number} is out of range")
    return 3000 + (project_number - 1) * 1000


def group_root_fid(project_number):
    """File id of a project's group root node (project fid + 100)."""
    return project_fid(project_number) + 100


def sound_fid(slot):
    """File id of a sample slot: the slot number itself (1..999).

    Sample slots live directly under ``/sounds`` (node 1000) and are addressed
    by their slot number — ``STAT``/``GET``/``metadata`` on fid ``N`` operate on
    slot ``N``. They are shared across all projects and never appear in a
    project TAR.
    """
    if not 1 <= slot <= 999:
        raise EPSysExError(f"sound slot {slot} is out of range (1..999)")
    return slot


def pad_fid(project_number, group, pad_number):
    """File id of one pad node: ``3200 + (P-1)*1000 + group*100 + pad``.

    ``group`` may be an index 0..3 or a letter A..D. Note the footgun: SysEx
    pad numbering counts top-down while TAR ``pNN`` filenames count bottom-up.
    """
    if isinstance(group, str):
        group = "ABCD".index(group.upper())
    if not 0 <= group <= 3:
        raise EPSysExError(f"group index {group} is out of range")
    return 3200 + (project_number - 1) * 1000 + group * 100 + pad_number


def calculate_put_payload_bytes(device_chunk_size):
    """How many raw TAR bytes fit in one packed FILE_PUT data page."""
    max_packed_body = max(0, device_chunk_size - 6)
    framing_overhead = 8 + 2 + 1
    if max_packed_body <= framing_overhead:
        return PUT_CHUNK_FALLBACK
    available = max_packed_body - 1 - framing_overhead
    return max(1, available - available // 8)


def _validate_sample_metadata(metadata):
    """Guard the two hard-won sample-metadata write rules before sending.

    Raises :class:`EPSysExError` when a string-enum key is written as a
    non-string, or when ``sound.playmode`` is written without the paired
    ``envelope.release`` (a partial write can make the device resync the pad
    from the slot and clobber overrides — see file-protocol.md).
    """
    if not isinstance(metadata, dict):
        raise EPSysExError("sample metadata must be a JSON object (dict)")
    for key in _STRING_ENUM_KEYS:
        if key in metadata and not isinstance(metadata[key], str):
            raise EPSysExError(
                f"metadata {key!r} must be written as a string, got "
                f"{type(metadata[key]).__name__} {metadata[key]!r}"
            )
    if "sound.playmode" in metadata and "envelope.release" not in metadata:
        raise EPSysExError(
            "writing 'sound.playmode' must be paired with 'envelope.release' in "
            "the same metadata write"
        )


class FileClient:
    """One EP device endpoint.

    Every public method acquires the device lock, opens the MIDI ports, runs
    a complete FILE session, and releases everything — so calls are safe to
    issue back-to-back but must never overlap from concurrent
    threads/processes (the lock enforces the latter).
    """

    def __init__(self, input_name=None, output_name=None,
                 product_byte=EP133_PRODUCT, port_hint=None,
                 lock_owner="epsysex", lock_timeout=15,
                 lock_path=LOCK_PATH, use_lock=True,
                 put_page_delay=0.006, put_payload_bytes=None):
        self.input_name = input_name
        self.output_name = output_name
        self.product_byte = product_byte
        self.port_hint = port_hint
        self.lock_owner = lock_owner
        self.lock_timeout = lock_timeout
        self.lock_path = lock_path
        self.use_lock = use_lock
        self.request_id = 0x300
        self.put_page_delay = max(0.0, float(put_page_delay))
        self.put_payload_bytes = (None if put_payload_bytes is None
                                  else max(1, int(put_payload_bytes)))

    # -- port / lock plumbing -------------------------------------------------

    def _hints(self):
        if self.port_hint:
            return (self.port_hint,)
        return _PORT_HINTS.get(self.product_byte, ("EP-",))

    def _resolve_ports(self):
        import mido  # lazy: device I/O only

        def pick(names, direction):
            for hint in self._hints():
                for name in names:
                    if hint.lower() in name.lower():
                        return name
            raise EPSysExError(
                f"no MIDI {direction} port matching {self._hints()!r}; available: {names}"
            )

        if self.input_name is None:
            self.input_name = pick(mido.get_input_names(), "input")
        if self.output_name is None:
            self.output_name = pick(mido.get_output_names(), "output")
        return self.input_name, self.output_name

    def _locked(self, op):
        if not self.use_lock:
            class _NullLock:
                def release(self):
                    pass
            return _NullLock()
        return acquire_device_lock(f"{self.lock_owner}:{op}",
                                   timeout=self.lock_timeout, path=self.lock_path)

    # A wedged device announces itself roughly every 1.03 s. Listening for a
    # bit over one period before a session decides whether the device is
    # mid-debug-loop; sessions must never start against a wedged device
    # (recovery rule: power-cycle first). Write sessions always pay this
    # ~1.2 s preflight — it is noise next to a multi-second write, and it
    # converts "our write caused an error" misdiagnoses into the correct
    # "the device was already looping".
    LOOP_PREFLIGHT_SECONDS = 1.2
    _PREFLIGHT_OPS = ("write_project_archive", "write_file", "upload_sound",
                      "delete_file", "set_metadata", "reload_project",
                      "set_sample_metadata")

    def _session(self, op, body):
        """Lock the device, open ports, drain stale input, run ``body``.

        Mutating sessions first listen ``LOOP_PREFLIGHT_SECONDS`` for an
        active firmware debug loop and raise :class:`DebugFrameError` without
        sending anything if one is detected.
        """
        import mido  # lazy: device I/O only

        in_name, out_name = self._resolve_ports()
        lock = self._locked(op)
        try:
            with mido.open_input(in_name) as midi_in, mido.open_output(out_name) as midi_out:
                self._drain(midi_in)
                if op in self._PREFLIGHT_OPS:
                    self._assert_not_looping(midi_in)
                return body(midi_in, midi_out)
        finally:
            lock.release()

    def _assert_not_looping(self, midi_in):
        """Passively listen for an active debug loop; raise before any I/O."""
        deadline = time.time() + self.LOOP_PREFLIGHT_SECONDS
        while time.time() < deadline:
            message = midi_in.poll()
            if message is None:
                time.sleep(0.02)
                continue
            if message.type == "sysex":
                data = list(message.data)
                if is_debug_frame(data):
                    raise DebugFrameError(
                        f"(preflight, no request sent) {debug_frame_text(data)}"
                    )

    @staticmethod
    def _drain(midi_in):
        while midi_in.poll():
            pass

    # -- request/response core -------------------------------------------------

    # Debug loops emit one frame every ~1.03 s (measured, err pattern 213
    # incident); listen a little over two periods before diagnosing a plain
    # timeout so a looping device is reported as what it is.
    DEBUG_GRACE_SECONDS = 2.5

    def _request(self, midi_in, midi_out, raw_payload, timeout=3.0):
        """Send one FILE request and wait for its matching response.

        Raises DebugFrameError on a firmware debug frame (stop the batch!),
        StatusRejectionError on a nonzero status, EPSysExError on timeout.
        A timeout is only reported after a grace window confirms the device
        is silent rather than mid-debug-loop — a firmware error loop can start
        *after* the request that triggered it, and the loop (not the timeout)
        is the actionable diagnosis.
        """
        import mido  # lazy: device I/O only

        request_id = self.request_id & 0x0FFF
        self.request_id += 1
        data = build_file_request(self.product_byte, request_id, raw_payload)
        midi_out.send(mido.Message("sysex", data=data))

        deadline = time.time() + timeout
        while time.time() < deadline:
            message = midi_in.poll()
            if not message:
                time.sleep(0.002)
                continue
            if message.type != "sysex":
                continue
            matched = match_response(list(message.data), request_id)  # raises on debug frame
            if matched is None:
                continue
            status, payload = matched
            if status != 0:
                raise StatusRejectionError(status, status_reason(payload))
            return payload

        # Timed out: distinguish "device busy/looping" from "device silent".
        grace_deadline = time.time() + self.DEBUG_GRACE_SECONDS
        while time.time() < grace_deadline:
            message = midi_in.poll()
            if message is None:
                time.sleep(0.02)
                continue
            if message.type == "sysex":
                match_response(list(message.data), request_id)  # raises on debug frame
        raise EPSysExError(f"timed out waiting for response to request id {request_id}")

    def _file_init(self, midi_in, midi_out, flags=0x01, max_response_length=4 * 1024 * 1024):
        """FILE_INIT: open/reset the file session; returns the device chunk size."""
        payload = self._request(
            midi_in,
            midi_out,
            [
                0x01,
                flags,
                (max_response_length >> 24) & 0xFF,
                (max_response_length >> 16) & 0xFF,
                (max_response_length >> 8) & 0xFF,
                max_response_length & 0xFF,
            ],
            timeout=3.0,
        )
        if len(payload) >= 5:
            chunk_size = (payload[1] << 24) | (payload[2] << 16) | (payload[3] << 8) | payload[4]
            if chunk_size > 0:
                return chunk_size
        return 512

    # -- session-scoped primitives ---------------------------------------------

    def _list_nodes(self, midi_in, midi_out, node_id):
        nodes = []
        page = 0
        while True:
            payload = self._request(
                midi_in,
                midi_out,
                [0x04, (page >> 8) & 0xFF, page & 0xFF, (node_id >> 8) & 0xFF, node_id & 0xFF],
                timeout=3.0,
            )
            if len(payload) <= 2:
                break
            echoed_page = (payload[0] << 8) | payload[1]
            if echoed_page != page:
                raise EPSysExError(f"unexpected list page {echoed_page}, expected {page}")
            offset = 2
            while offset + 7 <= len(payload):
                listed_id = (payload[offset] << 8) | payload[offset + 1]
                flags = payload[offset + 2]
                size = ((payload[offset + 3] << 24) | (payload[offset + 4] << 16)
                        | (payload[offset + 5] << 8) | payload[offset + 6])
                name_start = offset + 7
                name_end = name_start
                while name_end < len(payload) and payload[name_end] != 0:
                    name_end += 1
                name = bytes(payload[name_start:name_end]).decode("utf-8", errors="replace")
                nodes.append({"id": listed_id, "flags": flags, "size": size, "name": name})
                offset = name_end + 1
            page += 1
        return nodes

    def _get_metadata(self, midi_in, midi_out, file_id, key=None):
        metadata = ""
        page = 0
        while True:
            request = bytearray(
                [0x07, 0x02, (file_id >> 8) & 0xFF, file_id & 0xFF, (page >> 8) & 0xFF, page & 0xFF]
            )
            if key:
                request.extend(key.encode("utf-8"))
                request.append(0)
            payload = self._request(midi_in, midi_out, request, timeout=3.0)
            if len(payload) <= 2:
                break
            echoed_page = (payload[0] << 8) | payload[1]
            if echoed_page != page:
                raise EPSysExError(f"unexpected metadata page {echoed_page}, expected {page}")
            page_text = bytes(payload[2:]).split(b"\0", 1)[0].decode("utf-8", errors="replace")
            metadata += page_text
            if payload[-1] == 0:
                break
            page += 1
        if not metadata:
            return {}
        try:
            return json.loads(metadata)
        except json.JSONDecodeError:
            return {"_raw": metadata}

    def _set_metadata(self, midi_in, midi_out, file_id, metadata):
        encoded = json.dumps(metadata, separators=(",", ":")).encode("utf-8")
        payload = bytearray([0x07, 0x01, (file_id >> 8) & 0xFF, file_id & 0xFF])
        payload.extend(encoded)
        payload.append(0)
        self._request(midi_in, midi_out, payload, timeout=3.0)

    def _read_file(self, midi_in, midi_out, file_id):
        """FILE_GET open + exact-size paged read. Returns (bytes, meta dict)."""
        open_payload = self._request(
            midi_in,
            midi_out,
            [0x03, 0x00, (file_id >> 8) & 0xFF, file_id & 0xFF, 0x00, 0x00, 0x00, 0x00],
            timeout=3.0,
        )
        if len(open_payload) < 7:
            raise EPSysExError(f"unexpected file open response for {file_id}: {open_payload[:16]}")
        flags = open_payload[2]
        size = ((open_payload[3] << 24) | (open_payload[4] << 16)
                | (open_payload[5] << 8) | open_payload[6])
        name_bytes = []
        for byte in open_payload[7:]:
            if byte == 0:
                break
            name_bytes.append(byte)
        name = bytes(name_bytes).decode("utf-8", errors="replace")

        page_count = math.ceil(size / GET_PAGE_BYTES)
        data = bytearray()
        for page in range(page_count):
            payload = self._request(
                midi_in, midi_out, [0x03, 0x01, (page >> 8) & 0xFF, page & 0xFF], timeout=3.0
            )
            if len(payload) < 2:
                raise EPSysExError(f"short page {page} for file {file_id}")
            echoed_page = (payload[0] << 8) | payload[1]
            if echoed_page != page:
                raise EPSysExError(
                    f"unexpected data page {echoed_page}, expected {page}, file {file_id}"
                )
            data.extend(payload[2:])
            if len(data) >= size:
                break
            time.sleep(0.006)
        return bytes(data[:size]), {"fid": file_id, "flags": flags, "byteSize": size, "name": name}

    def _write_file(self, midi_in, midi_out, file_id, data, parent_id, name,
                    flags=FILE_CAPABILITY_READ | FILE_TYPE_DIR):
        """FILE_PUT open + paged data + empty-final-page EOF + session re-init."""
        chunk_size = self._file_init(midi_in, midi_out)
        advertised_payload_bytes = calculate_put_payload_bytes(chunk_size)
        chunk_payload_bytes = self.put_payload_bytes or advertised_payload_bytes

        put_init = bytearray([
            0x02, 0x00, flags,
            (file_id >> 8) & 0xFF, file_id & 0xFF,
            (parent_id >> 8) & 0xFF, parent_id & 0xFF,
        ])
        put_init.extend(len(data).to_bytes(4, "big"))
        put_init.extend(name.encode("ascii"))
        put_init.append(0)
        response = self._request(midi_in, midi_out, put_init, timeout=15.0)
        if len(response) >= 2:
            returned_fid = (response[0] << 8) | response[1]
            if returned_fid != file_id:
                raise EPSysExError(f"write returned file id {returned_fid}, expected {file_id}")

        page = 0
        offset = 0
        while offset < len(data):
            chunk = data[offset:offset + chunk_payload_bytes]
            payload = bytearray([0x02, 0x01, (page >> 8) & 0xFF, page & 0xFF])
            payload.extend(chunk)
            self._request(midi_in, midi_out, payload, timeout=15.0)
            offset += len(chunk)
            page += 1
            if self.put_page_delay:
                time.sleep(self.put_page_delay)

        # An empty final data page signals EOF.
        self._request(midi_in, midi_out, [0x02, 0x01, (page >> 8) & 0xFF, page & 0xFF], timeout=15.0)
        time.sleep(0.05)
        self._file_init(midi_in, midi_out)

    def _alternate_project_fid(self, midi_in, midi_out, fid):
        for node in self._list_nodes(midi_in, midi_out, PROJECTS_NODE_ID):
            if node["id"] != fid:
                return node["id"]
        return None

    def _stat(self, midi_in, midi_out, file_id):
        """FILE_STAT (0x0b): one node's fid/parent/flags/size/name.

        Response payload (post status, packed7-decoded, big-endian ints):
        ``<fid u16> <parent u16> <flags u8> <size u32> <name> 00`` — verified
        live on EP-40 OS 2.5.1 (a sound slot reports its exact PCM byte size).
        """
        payload = self._request(
            midi_in, midi_out, [0x0b, (file_id >> 8) & 0xFF, file_id & 0xFF], timeout=3.0
        )
        if len(payload) < 9:
            raise EPSysExError(f"unexpected STAT response for {file_id}: {payload[:16]}")
        name = bytes(payload[9:]).split(b"\0", 1)[0].decode("utf-8", errors="replace")
        return {
            "fid": (payload[0] << 8) | payload[1],
            "parent": (payload[2] << 8) | payload[3],
            "flags": payload[4],
            "byteSize": (payload[5] << 24) | (payload[6] << 16) | (payload[7] << 8) | payload[8],
            "name": name,
        }

    def _delete(self, midi_in, midi_out, file_id):
        """FILE_DELETE (``0x06 <fid u16>``): destructive removal of one node.

        The payload is the bare command byte plus the big-endian fid — there is
        **no** ``0x02`` sub-command (some community docs show ``0x06 0x02``;
        that form returns ``failed to delete`` / a no-op status and leaves the
        slot in place — verified on EP-40 OS 2.5.1). This 3-byte form returns
        status 0 and the slot disappears from the /sounds LIST.
        """
        return self._request(
            midi_in, midi_out, [0x06, (file_id >> 8) & 0xFF, file_id & 0xFF], timeout=10.0
        )

    def _play(self, midi_in, midi_out, file_id):
        """FILE_PLAY (0x05 0x01): audition a file node.

        On a slot fid this auditions the raw sample with NO playback metadata
        applied (envelope/trim ignored). Pad fids are accepted but silent.
        """
        return self._request(
            midi_in, midi_out, [0x05, 0x01, (file_id >> 8) & 0xFF, file_id & 0xFF], timeout=3.0
        )

    # -- public API --------------------------------------------------------------

    def file_init(self):
        """Probe the FILE session; returns the device chunk size."""
        return self._session("file_init", lambda mi, mo: self._file_init(mi, mo))

    def list_nodes(self, node_id):
        """List children of a node (e.g. SOUNDS_NODE_ID=1000, PROJECTS_NODE_ID=2000)."""
        def body(mi, mo):
            self._file_init(mi, mo)
            return self._list_nodes(mi, mo, node_id)
        return self._session("list_nodes", body)

    def get_metadata(self, file_id, key=None):
        """FILE_METADATA get for a node; returns the parsed JSON dict."""
        def body(mi, mo):
            self._file_init(mi, mo)
            return self._get_metadata(mi, mo, file_id, key=key)
        return self._session("get_metadata", body)

    def set_metadata(self, file_id, metadata):
        """FILE_METADATA set (JSON dict) on a node."""
        def body(mi, mo):
            self._file_init(mi, mo)
            self._set_metadata(mi, mo, file_id, metadata)
        return self._session("set_metadata", body)

    def read_file(self, file_id):
        """Read one file node. Returns ``(bytes, meta)``."""
        def body(mi, mo):
            self._file_init(mi, mo)
            return self._read_file(mi, mo, file_id)
        return self._session("read_file", body)

    def write_file(self, file_id, data, parent_id, name,
                   flags=FILE_CAPABILITY_READ | FILE_TYPE_DIR):
        """Write one file node under ``parent_id`` via FILE_PUT."""
        def body(mi, mo):
            self._write_file(mi, mo, file_id, data, parent_id, name, flags=flags)
        return self._session("write_file", body)

    def stat(self, file_id):
        """FILE_STAT one node. Returns ``{fid, parent, flags, byteSize, name}``."""
        def body(mi, mo):
            self._file_init(mi, mo)
            return self._stat(mi, mo, file_id)
        return self._session("stat", body)

    def delete_file(self, file_id):
        """Delete one file node (**destructive**, no undo).

        Removes a sample slot (fid = slot number) or other deletable node.
        There is no confirmation on the wire — a nonzero status is the only
        rejection. Serialized behind the device lock like every FILE op.
        """
        def body(mi, mo):
            self._file_init(mi, mo)
            self._delete(mi, mo, file_id)
        return self._session("delete_file", body)

    def audition(self, file_id):
        """Audition a file node via FILE_PLAY (0x05 0x01).

        A slot fid plays the raw sample immediately with no envelope/trim; pad
        fids are accepted but produce no sound. This is a file audition, not a
        sequencer transport command.
        """
        def body(mi, mo):
            self._file_init(mi, mo)
            self._play(mi, mo, file_id)
        return self._session("audition", body)

    def read_project_archive(self, project_number):
        """Read a project slot's TAR. Returns ``(tar_bytes, meta)``."""
        fid = project_fid(project_number)
        def body(mi, mo):
            self._file_init(mi, mo)
            data, meta = self._read_file(mi, mo, fid)
            meta["project"] = project_number
            return data, meta
        return self._session("read_project_archive", body)

    def write_project_archive(self, project_number, tar_bytes):
        """Replace a project slot's TAR (FILE_PUT open/data/close + re-init).

        The archive name is the zero-padded project number, parent is the
        /projects node (2000), and flags are
        FILE_CAPABILITY_READ|FILE_TYPE_DIR (0x06) — matching the official
        Sample Tool flow. Follow up with :meth:`reload_project` so the
        running firmware picks up the new bytes.
        """
        fid = project_fid(project_number)
        name = f"{project_number:02d}"
        def body(mi, mo):
            self._write_file(mi, mo, fid, tar_bytes, PROJECTS_NODE_ID, name)
        return self._session("write_project_archive", body)

    def reload_project(self, project_number, cycle=True):
        """Force the firmware to reload a project slot from storage.

        The activation dance (verified on EP-133 OS 2.0.5/2.5.1): remember
        the active group/pad, optionally select another project (``cycle``)
        so re-selecting the target forces a real reload, then re-activate the
        project, group, and pad via metadata ``{"active": fid}`` writes with
        readback verification at each step.
        """
        fid = project_fid(project_number)
        group_root = group_root_fid(project_number)

        def body(mi, mo):
            self._file_init(mi, mo)
            active_group = self._get_metadata(mi, mo, group_root, key="active").get("active")
            active_pad = (self._get_metadata(mi, mo, active_group, key="active").get("active")
                          if active_group else None)

            cycled_project = None
            if cycle:
                cycled_project = self._alternate_project_fid(mi, mo, fid)
                if cycled_project:
                    self._set_metadata(mi, mo, PROJECTS_NODE_ID, {"active": cycled_project})
                    readback = self._get_metadata(mi, mo, PROJECTS_NODE_ID, key="active").get("active")
                    if readback != cycled_project:
                        raise EPSysExError(
                            f"project cycle readback active={readback!r}, expected {cycled_project}"
                        )
                    time.sleep(0.2)

            self._set_metadata(mi, mo, PROJECTS_NODE_ID, {"active": fid})
            active = self._get_metadata(mi, mo, PROJECTS_NODE_ID, key="active").get("active")
            if active != fid:
                raise EPSysExError(f"project reload readback active={active!r}, expected {fid}")

            if isinstance(active_group, int) and active_group > 0:
                self._set_metadata(mi, mo, group_root, {"active": active_group})
                group_readback = self._get_metadata(mi, mo, group_root, key="active").get("active")
                if group_readback != active_group:
                    raise EPSysExError(
                        f"group reload readback active={group_readback!r}, expected {active_group}"
                    )
            else:
                group_readback = None

            if isinstance(active_pad, int) and active_pad > 0:
                self._set_metadata(mi, mo, active_group, {"active": active_pad})
                pad_readback = self._get_metadata(mi, mo, active_group, key="active").get("active")
                if pad_readback != active_pad:
                    raise EPSysExError(
                        f"pad reload readback active={pad_readback!r}, expected {active_pad}"
                    )
            else:
                pad_readback = None

            return {
                "activeProjectFid": active,
                "activeGroupFid": group_readback,
                "activePadFid": pad_readback,
                "cycledProjectFid": cycled_project,
            }

        return self._session("reload_project", body)

    def write_project_archive_and_reload(self, project_number, tar_bytes, cycle=True):
        """Fast live path: replace a project and reload it in one guarded session.

        This avoids reopening the MIDI ports and repeating the passive debug-loop
        preflight between FILE_PUT and activation. Callers remain responsible for
        checkpointing and readback verification according to their risk policy.
        """
        fid = project_fid(project_number)
        name = f"{project_number:02d}"
        group_root = group_root_fid(project_number)

        def body(mi, mo):
            self._write_file(mi, mo, fid, tar_bytes, PROJECTS_NODE_ID, name)
            active_group = self._get_metadata(mi, mo, group_root, key="active").get("active")
            active_pad = (self._get_metadata(mi, mo, active_group, key="active").get("active")
                          if active_group else None)
            cycled_project = None
            if cycle:
                cycled_project = self._alternate_project_fid(mi, mo, fid)
                if cycled_project:
                    self._set_metadata(mi, mo, PROJECTS_NODE_ID, {"active": cycled_project})
                    time.sleep(0.2)
            self._set_metadata(mi, mo, PROJECTS_NODE_ID, {"active": fid})
            active = self._get_metadata(mi, mo, PROJECTS_NODE_ID, key="active").get("active")
            if active != fid:
                raise EPSysExError(f"project reload readback active={active!r}, expected {fid}")
            if isinstance(active_group, int) and active_group > 0:
                self._set_metadata(mi, mo, group_root, {"active": active_group})
            if isinstance(active_pad, int) and active_pad > 0:
                self._set_metadata(mi, mo, active_group, {"active": active_pad})
            return {
                "activeProjectFid": active,
                "activeGroupFid": active_group,
                "activePadFid": active_pad,
                "cycledProjectFid": cycled_project,
            }

        return self._session("write_project_archive", body)

    # -- sample library (global; shared across projects, never in a TAR) --------

    def list_sounds(self):
        """List occupied sample slots under /sounds.

        Returns the LIST node dicts (``{id, flags, size, name}``); ``id`` is the
        slot number (1..999) and ``size`` is the raw PCM byte count.
        """
        def body(mi, mo):
            self._file_init(mi, mo)
            return [node for node in self._list_nodes(mi, mo, SOUNDS_NODE_ID)
                    if 1 <= int(node["id"]) <= 999]
        return self._session("list_sounds", body)

    def read_sound(self, slot):
        """Read a sample slot's raw PCM bytes. Returns ``(bytes, meta)``."""
        fid = sound_fid(slot)
        def body(mi, mo):
            self._file_init(mi, mo)
            data, meta = self._read_file(mi, mo, fid)
            meta["slot"] = slot
            return data, meta
        return self._session("read_sound", body)

    def stat_sound(self, slot):
        """STAT a sample slot. Returns ``{fid, parent, flags, byteSize, name}``."""
        return self.stat(sound_fid(slot))

    def get_sample_metadata(self, slot, key=None):
        """Read a sample slot's metadata JSON (optionally one key)."""
        fid = sound_fid(slot)
        def body(mi, mo):
            self._file_init(mi, mo)
            return self._get_metadata(mi, mo, fid, key=key)
        return self._session("get_sample_metadata", body)

    def set_sample_metadata(self, slot, metadata):
        """Set a sample slot's metadata (partial JSON merge), with write-rule
        guards from file-protocol.md:

        * ``sound.playmode`` / ``time.mode`` must be strings (the device emits
          them as integers but rejects/ignores integer writes).
        * ``sound.playmode`` must be written together with ``envelope.release``.

        Violations raise :class:`~epsysex.sysex.EPSysExError` before any write.
        """
        _validate_sample_metadata(metadata)
        fid = sound_fid(slot)
        def body(mi, mo):
            self._file_init(mi, mo)
            self._set_metadata(mi, mo, fid, metadata)
        return self._session("set_sample_metadata", body)

    def upload_sound(self, slot, pcm_bytes, *, name=None, node_name=None,
                     samplerate=46875, channels=1, sample_format="s16",
                     extra_metadata=None, write_metadata=True):
        """Create/replace a sample slot from raw PCM bytes.

        Uploads ``pcm_bytes`` to slot ``slot`` under /sounds (create flags
        ``0x05`` = READ|FILE), then, unless ``write_metadata=False``, sets
        ``samplerate``/``channels``/``format`` (and ``name`` when given, clamped
        to 20 ASCII chars). ``extra_metadata`` is merged last. Returns a summary
        dict. The write replaces any existing slot of the same number.

        ``pcm_bytes`` must already be the device's raw interleaved PCM (no WAV
        header); ``sample_format`` describes it (``"s16"`` is the common case).
        """
        fid = sound_fid(slot)
        pcm_bytes = bytes(pcm_bytes)
        node_name = node_name or f"{slot:03d}.pcm"
        meta = None
        if write_metadata:
            meta = {
                "samplerate": int(samplerate),
                "channels": int(channels),
                "format": sample_format,
            }
            if name is not None:
                meta["name"] = str(name)[:20]
            if extra_metadata:
                meta.update(extra_metadata)
            _validate_sample_metadata(meta)

        def body(mi, mo):
            self._write_file(mi, mo, fid, pcm_bytes, SOUNDS_NODE_ID, node_name,
                             flags=SOUND_CREATE_FLAGS)
            if meta is not None:
                self._set_metadata(mi, mo, fid, meta)
            return {
                "slot": slot, "fid": fid, "bytes": len(pcm_bytes),
                "nodeName": node_name, "metadata": meta,
            }
        return self._session("upload_sound", body)

    def delete_sound(self, slot):
        """Delete a sample slot (**destructive**). See :meth:`delete_file`."""
        return self.delete_file(sound_fid(slot))

    def audition_sound(self, slot):
        """Audition a sample slot's raw sample. See :meth:`audition`."""
        return self.audition(sound_fid(slot))
