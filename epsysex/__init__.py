"""epsysex — EP SysEx protocol and project format.

A reference implementation for the shared EP format and verified transports:

* :mod:`epsysex.sysex` — TE SysEx framing, packed7 codec (pure stdlib)
* :mod:`epsysex.tar` — the device's stripped project-TAR dialect (pure stdlib)
* :mod:`epsysex.compile` — JSON project description -> project TAR (pure stdlib)
* :mod:`epsysex.identity` — firmware identity (live MIDI or offline .ppak)
* :mod:`epsysex.fileclient` — live FILE protocol client (projects, the shared
  sample library, node stat/delete/audition)
* :mod:`epsysex.devicelock` — cross-process device lock (FILE ops must be serialized)

Only the device-I/O code paths need ``mido`` (install ``epsysex[device]``);
everything else is stdlib-only, so compiling, parsing, and diffing work
without any MIDI stack.
"""
from __future__ import annotations

__version__ = "0.1.0"

from .sysex import (  # noqa: F401
    EP133_PRODUCT,
    EP40_PRODUCT,
    CMD_FILE,
    CMD_GREET,
    TE_MANUFACTURER,
    DebugFrameError,
    EPSysExError,
    StatusRejectionError,
    build_file_request,
    is_debug_frame,
    match_response,
    pack7,
    unpack7,
)
from .tar import (  # noqa: F401
    build_archive,
    build_header,
    iter_members,
    member_payloads,
    patch_member_in_place,
    read_member,
)
from .compile import (  # noqa: F401
    CompileError,
    build_ppak,
    compile_project,
    compile_project_file,
    encode_pattern,
    make_scenes_member,
)
from .devicelock import DeviceLock, DeviceLockError, LOCK_PATH, acquire_device_lock  # noqa: F401
from .fileclient import (  # noqa: F401
    PROJECTS_NODE_ID,
    SOUND_CREATE_FLAGS,
    SOUNDS_NODE_ID,
    FileClient,
    pad_fid,
    project_fid,
    sound_fid,
)
from .identity import identity_from_device, identity_from_ppak, parse_identity_reply  # noqa: F401
from .dependencies import (  # noqa: F401
    dependency_plan,
    dependency_specs,
    ensure_sound_dependencies,
    prepare_sound,
    project_sound_slots,
    wav_to_pcm16,
)
from .products import ProductProfile, product_profile  # noqa: F401
