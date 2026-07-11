"""Compile a JSON project description into an EP project TAR.

The byte-level encoders here are a faithful port of the hardware-proven
scratch-archive builder from the koii research project (pad records, pattern
records, scenes member, decoded settings member, device TAR dialect). The
JSON contract mirrors the ``ep.project.v1`` document shape.

JSON document shape (all sections optional unless noted)::

    {
      "schema": "ep.project.v1",            # optional, informational
      "product": "ep",                     # ep / ep133 / ep1320 / ep40
      "metadata": { ... },                   # ignored by the compiler
      "baseArchive": "capture.tar",         # optional: preserve unknown members/bytes
      "settings": {                          # requires base/template unless explicitly unsafe
        "bpm": 120.0,
        "groupFaders": [ {"group": "B", "parameter": 4, "baseValue": 0.42} ]
      },
      "fxSettings": {                        # structured master/output/sidechain controls
        "type": "delay", "parameters": {"x": 0.5, "y": 0.5}
      },
      "includeDirectories": true,            # emit pads/, pads/a..d/, patterns/ dir entries
      "padMembers": "all",                   # "all" (48 members) or "used" (only pads listed below)
      "pads": [
        { "group": "A", "pad": 10,           # required; pad 1..12 (TAR bottom-up numbering)
          "slot": 421,                       # sample slot (0 = unassigned)
          "trimStart": 0, "trimEnd": 13699,  # frames
          "sampleBpm": 120.0,
          "amplitude": 100,                  # dB-domain fader, 100 = unity
          "pitch": 0, "pitchFraction": 0,    # semitones offset byte; cents/100 byte
          "pan": 0, "attack": 0,
          "release": 255,                    # 255 = play-through
          "timeMode": 0,                     # 0/1 bpm, 2 bar, 3 reverse
          "chokeGroup": 0,                   # nonzero = shared mute group
          "playMode": 0,                     # 0 one-shot, 1 key, 2 legato
          "rootNote": 60,
          "midiChannel": 0 }
      ],
      "patterns": [
        { "id": "A01", "bars": 2,
          "events": [ {"tick": 0, "pad": 10, "note": 60, "velocity": 112,
                       "duration": 36, "flag": 0} ],      # or "padByte" to preserve raw bytes
          "automation": [ {"tick": 0, "parameterID": 0, "value": 32000} ] }
      ],
      "scenes": [
        { "groupPatterns": [1, 0, 0, 0],     # 1-based pattern index per group A-D; 0 = none
          "timeSignature": [4, 4] }          # optional, default 4/4
      ],
      "song": [1],                           # 1-based scene indices
      "sounds": [ {"slot": 421, "path": "kick.wav"} ],
      "rawMembers": [ {"name": "future", "base64": "..."} ]
    }

Riddim-specific documents select ``"product": "ep40"`` and may additionally
use ``pads[].supertone``, loop play mode 3, and the 48-byte ``live`` member.

Safety invariants enforced here (each maps to an observed firmware failure):

* Pattern header byte 0 and pad record byte 0 are always zero. A nonzero
  pattern-header byte 0 is rejected by OS 2.5.1 with ``err pattern 71`` debug
  SysEx and wedges the FILE subsystem until power-cycle.
* ``scenes`` must not reference pattern members absent from the archive
  (``err pattern 187``).
* Fader automation values are validated to 0..32767 (``err fader 93``) and
  parameter ids to 0..11.
* Safe ``settings`` authoring patches a captured base member or an explicit
  template. Fabricating one requires ``unsafeFabricate`` because it has caused
  ``ERR CLOCK 43`` and a flash-format recovery.

Pure stdlib; no device I/O.
"""
from __future__ import annotations

import base64
import json
import struct
import time
import zipfile
from io import BytesIO
from pathlib import Path

from .products import product_profile
from .tar import build_archive, iter_members

PPQN = 96
TICKS_PER_BAR = PPQN * 4
FADER_AUTOMATION_MAX = 32767

PAD_RECORD_SIZE = 27  # generic Sample-Tool form; product profiles may override it
SETTINGS_SIZE = 222
FX_SETTINGS_SIZE = 160

SCENES_SIZE = 712
SCENE_HEADER_SIZE = 7
SCENE_CHUNK_SIZE = 6
# 1-based selected-scene cursor (EP-40-verified: external MIDI start plays
# exactly this scene; historically misread as a scene count because a device
# cursor naturally rests on the last-created scene).
SCENE_CURSOR_OFFSET = SCENE_HEADER_SIZE + 99 * SCENE_CHUNK_SIZE + 3
SONG_LENGTH_OFFSET = SCENE_HEADER_SIZE + 99 * SCENE_CHUNK_SIZE + 11
SONG_ENTRIES_OFFSET = SCENE_HEADER_SIZE + 99 * SCENE_CHUNK_SIZE + 12

GROUPS = "ABCD"

# Group-fader assignment parameter ids (settings bytes 216-219 and pattern
# automation records share this table).
FADER_PARAMETERS = (
    "level", "pitch", "time", "lowpass", "highpass", "fx",
    "attack", "release", "pan", "tune", "velocity", "modulation",
)

FX_TYPES = {
    "off": 0,
    "delay": 1,
    "reverb": 2,
    "distortion": 3,
    "chorus": 4,
    "filter": 5,
    "compressor": 6,
}


class CompileError(ValueError):
    """Invalid project document (caught before any bytes reach a device)."""


def _integer(value, minimum, maximum, label):
    value = int(value)
    if not (minimum <= value <= maximum):
        raise CompileError(f"{label} must be {minimum}..{maximum}")
    return value


# ---------------------------------------------------------------------------
# Pattern encoding
# ---------------------------------------------------------------------------

def encode_pattern(events, bars=1, automation_events=None, profile=None):
    """Encode a pattern member: header + interleaved 8-byte records.

    The header is profile-dependent (``profile.pattern_dialect``):

    * ``"ep"`` (default, EP-133-verified): 4 bytes ``[0, bars, count u8, 0]``
      — byte 0 MUST stay zero there (err pattern 71).
    * ``"ep40"`` (Riddim-native): 6 bytes ``[1, bars, 0xFF, 0xFF,
      count u16 LE]`` — matches every pattern member in every EP-40 device
      capture (e.g. ``[1, 3, 255, 255, 71, 0]`` for 71 records).

    Note events are
    ``[tick u16 LE, padByte, note, velocity, duration u16 LE, flag]``; fader
    automation records are ``[tick u16 LE, 0x01, parameterID, 0, value u16 LE
    (0..32767), 0]``. Records are interleaved sorted by tick, with automation
    records ahead of note events at the same tick (round-trip verified
    against device captures).
    """
    records = []
    for event in events:
        tick = _integer(event["tick"], 0, 65535, "pattern event tick")
        duration = _integer(event["duration"], 0, 65535, "pattern event duration")
        pad_byte = _integer(event["padByte"], 0, 255, "pattern event padByte")
        note = _integer(event["note"], 0, 127, "pattern event note")
        velocity = _integer(event["velocity"], 1, 127, "pattern event velocity")
        flag = _integer(event.get("flag", 0), 0, 255, "pattern event flag")
        records.append((tick, 1, 0, bytes([
            tick & 0xFF,
            (tick >> 8) & 0xFF,
            pad_byte,
            note,
            velocity,
            duration & 0xFF,
            (duration >> 8) & 0xFF,
            flag,
        ])))
    for event in automation_events or []:
        tick = _integer(event["tick"], 0, 65535, "automation tick")
        value = int(event["value"])
        parameter_id = int(event["parameterID"])
        if not (0 <= parameter_id <= 11):
            raise CompileError(f"automation parameterID must be 0..11: {event}")
        if not (0 <= value <= FADER_AUTOMATION_MAX):
            # err fader 93: the device range is 0..32767, not 0..65535.
            raise CompileError(f"automation value must be 0..{FADER_AUTOMATION_MAX}: {event}")
        records.append((tick, 0, parameter_id, bytes([
            tick & 0xFF,
            (tick >> 8) & 0xFF,
            1,
            parameter_id & 0xFF,
            0,
            value & 0xFF,
            (value >> 8) & 0xFF,
            0,
        ])))
    bars = _integer(bars, 1, 99, "pattern bars")
    records.sort(key=lambda item: (item[0], item[1], item[2]))
    body = b"".join(record for *_unused, record in records)
    return _pattern_header(bars, len(records), profile) + body


def _pattern_header(bars, count, profile=None):
    dialect = getattr(profile, "pattern_dialect", "ep")
    if dialect == "ep40":
        if count > 65535:
            raise CompileError(f"pattern has {count} records; the count field holds max 65535")
        return bytes([1, bars, 0xFF, 0xFF, count & 0xFF, (count >> 8) & 0xFF])
    if count > 255:
        raise CompileError(f"pattern has {count} records; the count byte holds max 255")
    return bytes([0, bars, count & 0xFF, 0])


def empty_pattern_payload(bars=1, profile=None):
    """A valid, empty pattern member (header only)."""
    return _pattern_header(_integer(bars, 1, 99, "pattern bars"), 0, profile)


def automation_value16(value):
    """Map a 0..1 float onto the device automation scale 0..32767."""
    return max(0, min(FADER_AUTOMATION_MAX, int(round(float(value) * FADER_AUTOMATION_MAX))))


# ---------------------------------------------------------------------------
# Scenes member
# ---------------------------------------------------------------------------

def make_scenes_member(scenes, song, time_signatures=None, current_scene=None):
    """Build the 712-byte ``scenes`` member.

    Layout: 7-byte header, 99 x 6-byte scene chunks (four 1-based group
    pattern indices, then time-signature numerator/denominator), then the
    111-byte song trailer (**scene cursor** at trailer byte 3, song length at
    byte 11, 1-based scene entries from byte 12). Unused chunks and the
    header default to 4/4, matching device-written members.

    Trailer byte 3 is the 1-based **selected-scene cursor**, not a scene
    count (EP-40-verified live: external MIDI start plays exactly the scene
    this byte selects, looped; the song list is only followed by the
    device's own SONG-mode transport). Historical captures read as a "count"
    because a device cursor naturally sits on the last-created scene.
    ``current_scene`` defaults to ``len(scenes)`` to preserve that byte
    behavior; pass 1 to make external starts play scene 1.

    ``time_signatures`` is an optional parallel list of ``(num, den)`` per
    scene (default 4/4 each).
    """
    if not (1 <= len(scenes) <= 99):
        raise CompileError("scene count must be 1...99")
    if not (1 <= len(song) <= SCENES_SIZE - SONG_ENTRIES_OFFSET):
        raise CompileError("song length is out of range")
    member = bytearray(SCENES_SIZE)
    member[5] = 4
    member[6] = 4
    for index in range(99):
        offset = SCENE_HEADER_SIZE + index * SCENE_CHUNK_SIZE
        member[offset + 4] = 4
        member[offset + 5] = 4
    for index, scene in enumerate(scenes):
        if len(scene) != 4:
            raise CompileError("each scene must have four group pattern indices")
        offset = SCENE_HEADER_SIZE + index * SCENE_CHUNK_SIZE
        member[offset:offset + 4] = bytes(int(value) & 0xFF for value in scene)
        num, den = (4, 4)
        if time_signatures is not None and index < len(time_signatures) and time_signatures[index]:
            num, den = time_signatures[index]
        member[offset + 4] = _integer(num, 1, 255, "time-signature numerator")
        member[offset + 5] = _integer(den, 1, 255, "time-signature denominator")
    for entry in song:
        if not (1 <= int(entry) <= len(scenes)):
            raise CompileError(f"song entry {entry} does not reference a defined scene (1-based)")
    if current_scene is None:
        current_scene = len(scenes)
    if not (1 <= int(current_scene) <= len(scenes)):
        raise CompileError(
            f"currentScene {current_scene} does not reference a defined scene (1-based)"
        )
    member[SCENE_CURSOR_OFFSET] = int(current_scene)
    member[SONG_LENGTH_OFFSET] = len(song)
    member[SONG_ENTRIES_OFFSET:SONG_ENTRIES_OFFSET + len(song)] = bytes(int(v) for v in song)
    return bytes(member)


# ---------------------------------------------------------------------------
# Pad records
# ---------------------------------------------------------------------------

def base_pad_payload(record_size=PAD_RECORD_SIZE):
    """Default pad record: unassigned, 120 BPM, unity amp, play-through."""
    if record_size < 26:
        raise CompileError("pad record size must be at least 26 bytes")
    payload = bytearray(record_size)
    struct.pack_into("<f", payload, 12, 120.0)
    payload[16] = 100
    payload[20] = 255
    payload[21] = 0
    payload[23] = 0
    payload[24] = 60
    return payload


def encode_pad(pad, profile=None):
    """Encode one pad description onto the default record.

    Byte 0 is never written (must stay zero). Offsets follow the decoded pad
    record: slot u16 LE @1, MIDI channel @3, trim start/end u32 LE @4/@8,
    sample BPM f32 @12, amplitude @16, pitch @17, pan @18, attack @19,
    release @20, time mode @21, choke group @22, play mode @23, root note
    @24, pitch fraction (cents/100) @26. The source compiler wrote the
    slot/trim/bpm/amp/release/timeMode/playMode/rootNote subset; the
    remaining offsets follow the same published byte map.
    """
    profile = profile or product_profile("ep")
    supertone = pad.get("supertone")
    if supertone is not None and not profile.supports_supertone:
        raise CompileError("supertone pads require the ep40/Riddim product profile")
    payload = base_pad_payload(profile.pad_record_size)
    if supertone is not None:
        if not isinstance(supertone, dict):
            raise CompileError("pad supertone must be an object")
        engine = int(supertone.get("engine", -1))
        if not (0 <= engine <= 9):
            raise CompileError("supertone engine must be 0..9")
        if "slot" in pad:
            raise CompileError("a supertone pad derives its symbol; omit pad.slot")
        slot = 1000 + engine  # stored bytes are sym-1; sym is 1001..1010
        for knob in ("knobX", "knobY"):
            value = int(supertone.get(knob, 128))
            if not (0 <= value <= 254):
                raise CompileError(f"supertone {knob} must be 0..254")
        payload[27] = int(supertone.get("knobX", 128))
        payload[28] = int(supertone.get("knobY", 128))
    else:
        slot = int(pad.get("slot", 0))
        if not (0 <= slot <= 999):
            raise CompileError("sample slot must be 0..999")
    payload[1] = slot & 0xFF
    payload[2] = (slot >> 8) & 0xFF
    if "midiChannel" in pad:
        payload[3] = _integer(pad["midiChannel"], 0, 16, "pad midiChannel")
    struct.pack_into(
        "<I", payload, 4, _integer(pad.get("trimStart", 0), 0, 0xFFFFFFFF, "pad trimStart")
    )
    struct.pack_into(
        "<I", payload, 8, _integer(pad.get("trimEnd", 0), 0, 0xFFFFFFFF, "pad trimEnd")
    )
    sample_bpm = float(pad.get("sampleBpm", 120.0))
    if not (1.0 <= sample_bpm <= 399.0):
        raise CompileError("pad sampleBpm must be 1..399")
    struct.pack_into("<f", payload, 12, sample_bpm)
    payload[16] = _integer(pad.get("amplitude", 100), 0, 200, "pad amplitude")
    if "pitch" in pad:
        payload[17] = _integer(pad["pitch"], -12, 12, "pad pitch") & 0xFF
    if "pan" in pad:
        payload[18] = _integer(pad["pan"], -16, 16, "pad pan") & 0xFF
    if "attack" in pad:
        payload[19] = _integer(pad["attack"], 0, 255, "pad attack")
    payload[20] = _integer(pad.get("release", 255), 0, 255, "pad release")
    payload[21] = _integer(pad.get("timeMode", 0), 0, 3, "pad timeMode")
    if "chokeGroup" in pad:
        payload[22] = _integer(pad["chokeGroup"], 0, 255, "pad chokeGroup")
    play_mode = _integer(pad.get("playMode", 0), 0, 3, "pad playMode")
    payload[23] = play_mode
    payload[24] = _integer(pad.get("rootNote", 60), 0, 127, "pad rootNote")
    if "pitchFraction" in pad and len(payload) > 26:
        payload[26] = _integer(
            pad["pitchFraction"], -99, 99, "pad pitchFraction"
        ) & 0xFF
    if play_mode == 3 and not profile.supports_loop:
        raise CompileError("playMode 3 (loop) requires the ep40/Riddim product profile")
    return bytes(payload)


# ---------------------------------------------------------------------------
# Settings / fx_settings members
# ---------------------------------------------------------------------------

def settings_payload(bpm, group_faders=None, template=None):
    """Build the 222-byte ``settings`` member from decoded field rules only.

    Captured healthy settings payloads have only a small amount of nonzero
    structure when fader bases are untouched: BPM f32 at bytes 4-7, the 4x12
    fader-base floats initialized to -1.0 ("untouched") across bytes 24-215,
    four fader-assignment bytes at 216-219, and a [0, 1] tail.

    Read the module docstring before emitting this into a from-scratch
    archive (ERR CLOCK 43 risk on some firmware/device states).
    """
    if template is None:
        payload = bytearray(SETTINGS_SIZE)
        for offset in range(24, 216, 4):
            struct.pack_into("<f", payload, offset, -1.0)
        payload[216:220] = bytes([0, 0, 0, 0])
        payload[220:222] = bytes([0, 1])
    else:
        payload = bytearray(template)
        if len(payload) not in (222, 224):
            raise CompileError("settings template must be a captured 222- or 224-byte member")
    if bpm is not None:
        bpm = float(bpm)
        if not (40.0 <= bpm <= 399.0):
            raise CompileError("settings bpm must be 40..399")
        struct.pack_into("<f", payload, 4, bpm)
    for setting in group_faders or []:
        group = str(setting["group"]).upper()
        if group not in GROUPS:
            raise CompileError(f"unknown settings group {group!r}")
        group_index = GROUPS.index(group)
        parameter_id = int(setting["parameter"])
        if not (0 <= parameter_id < 12):
            raise CompileError(f"settings parameter id out of range: {parameter_id}")
        payload[216 + group_index] = parameter_id & 0xFF
        if setting.get("baseValue") is not None:
            base = max(0.0, min(1.0, float(setting["baseValue"])))
            struct.pack_into("<f", payload, 24 + group_index * 48 + parameter_id * 4, base)
    return bytes(payload)


def _normalized_float(value, label):
    value = float(value)
    if not (0.0 <= value <= 1.0):
        raise CompileError(f"{label} must be 0..1")
    return value


def _decode_member_data(spec, label):
    if isinstance(spec, (bytes, bytearray)):
        return bytes(spec)
    if not isinstance(spec, dict):
        raise CompileError(f"{label} must be an object")
    if "base64" in spec:
        try:
            return base64.b64decode(spec["base64"], validate=True)
        except (ValueError, TypeError) as exc:
            raise CompileError(f"{label}.base64 is invalid") from exc
    if "hex" in spec:
        try:
            return bytes.fromhex(spec["hex"])
        except (ValueError, TypeError) as exc:
            raise CompileError(f"{label}.hex is invalid") from exc
    raise CompileError(f"{label} needs base64 or hex data")


def _fx_type(value):
    if isinstance(value, str):
        try:
            return FX_TYPES[value.lower()]
        except KeyError as exc:
            raise CompileError(f"unknown FX type {value!r}") from exc
    value = int(value)
    if not (0 <= value <= 6):
        raise CompileError("FX type must be 0..6")
    return value


def fx_settings_payload(spec=None, template=None):
    """Build/patch the decoded 160-byte FX/output/sidechain member."""
    if spec is True or spec is None:
        spec = {}
    if not isinstance(spec, dict):
        raise CompileError("fxSettings must be true, false, or an object")
    payload = bytearray(template if template is not None else bytes(FX_SETTINGS_SIZE))
    if len(payload) not in (144, 152, 160):
        raise CompileError("fx_settings template must be 144, 152, or 160 bytes")

    effect_type = None
    if "type" in spec:
        effect_type = _fx_type(spec["type"])
        payload[4] = effect_type
    elif len(payload) > 4:
        effect_type = payload[4]

    parameters = spec.get("parameters", {})
    if parameters is None:
        parameters = {}
    if not isinstance(parameters, dict):
        raise CompileError("fxSettings.parameters must be an object")
    parameter1 = spec.get("parameter1", parameters.get("x"))
    parameter2 = spec.get("parameter2", parameters.get("y"))
    if parameter1 is not None or parameter2 is not None:
        if not effect_type:
            raise CompileError("FX parameters require a non-off fxSettings.type")
        if parameter1 is not None:
            struct.pack_into(
                "<f", payload, 12 + (effect_type - 1) * 4,
                _normalized_float(parameter1, "FX parameter1"),
            )
        if parameter2 is not None:
            struct.pack_into(
                "<f", payload, 76 + (effect_type - 1) * 4,
                _normalized_float(parameter2, "FX parameter2"),
            )

    output = spec.get("outputCompressor")
    if output is not None:
        if not isinstance(output, dict):
            raise CompileError("fxSettings.outputCompressor must be an object")
        if len(payload) < 144:
            raise CompileError("output compressor needs a 144-byte or newer fx_settings member")
        for key, offset in (("drive", 136), ("speed", 140)):
            if key in output:
                struct.pack_into(
                    "<f", payload, offset,
                    _normalized_float(output[key], f"output compressor {key}"),
                )

    sidechain = spec.get("sidechain")
    if sidechain is not None:
        if not isinstance(sidechain, dict):
            raise CompileError("fxSettings.sidechain must be an object")
        if len(payload) < 160:
            raise CompileError("sidechain needs a 160-byte fx_settings member")
        for key, offset in (("length", 144), ("shape", 148)):
            if key in sidechain:
                struct.pack_into(
                    "<f", payload, offset,
                    _normalized_float(sidechain[key], f"sidechain {key}"),
                )
        routing = sidechain.get("routing", {})
        if not isinstance(routing, dict):
            raise CompileError("sidechain.routing must map groups A..D to route objects")
        for group, route in routing.items():
            group = str(group).upper()
            if group not in GROUPS or not isinstance(route, dict):
                raise CompileError("sidechain routing entries need group A..D objects")
            word = 0x8000 if route.get("destination", False) else 0
            for pad in route.get("sourcePads", []):
                pad = int(pad)
                if not (1 <= pad <= 12):
                    raise CompileError("sidechain source pads must be 1..12")
                word |= 1 << (pad - 1)
            struct.pack_into("<H", payload, 152 + GROUPS.index(group) * 2, word)
    return bytes(payload)


def make_live_member(spec):
    """Build Riddim's 48-byte Loop Startup Sequence bitmap."""
    if not isinstance(spec, dict):
        raise CompileError("live must be an object")
    payload = bytearray(48)
    for item in spec.get("armedPads", []):
        if not isinstance(item, dict):
            raise CompileError("live.armedPads entries must be objects")
        group = str(item.get("group", "")).upper()
        pad = int(item.get("pad", 0))
        if group not in GROUPS or not (1 <= pad <= 12):
            raise CompileError("live armed pads need group A..D and pad 1..12")
        payload[GROUPS.index(group) * 12 + pad - 1] = 1
    return bytes(payload)


# ---------------------------------------------------------------------------
# Document compiler
# ---------------------------------------------------------------------------

def _pattern_member_name(pattern_id):
    pattern_id = str(pattern_id)
    if (len(pattern_id) != 3 or pattern_id[0].upper() not in GROUPS
            or not pattern_id[1:].isdigit() or not (1 <= int(pattern_id[1:]) <= 99)):
        raise CompileError(f"pattern id must look like 'A01'..'D99', got {pattern_id!r}")
    return f"patterns/{pattern_id.lower()}"


def _event_pad_byte(event):
    if "padByte" in event:
        return int(event["padByte"])
    if "pad" in event:
        pad = int(event["pad"])
        if not (1 <= pad <= 12):
            raise CompileError(f"event pad must be 1..12, got {pad}")
        return (pad - 1) * 8
    raise CompileError(f"pattern event needs 'pad' or 'padByte': {event}")


def _archive_specs(tar_bytes):
    specs = []
    for name, start, size, typeflag in iter_members(tar_bytes):
        spec = {"name": name, "type": typeflag}
        if typeflag != "5":
            spec["payload"] = tar_bytes[start:start + size]
        specs.append(spec)
    return specs


def _raw_member_specs(items):
    specs = []
    for index, item in enumerate(items or []):
        if not isinstance(item, dict) or not item.get("name"):
            raise CompileError(f"rawMembers[{index}] needs a name")
        typeflag = str(item.get("type", "0"))
        if typeflag not in ("0", "5"):
            raise CompileError("raw member type must be '0' or '5'")
        spec = {"name": str(item["name"]), "type": typeflag}
        if typeflag != "5":
            spec["payload"] = _decode_member_data(item, f"rawMembers[{index}]")
        specs.append(spec)
    return specs


def _merge_archive_specs(base_specs, replacements, additions, remove_names):
    """Merge authored members into a base archive's member list.

    Replacements land in the base member's original position. NEW members are
    inserted **inside their structural section**, not appended at the archive
    tail: firmware registers pattern members from the patterns region, and a
    pattern member introduced after ``scenes``/``settings`` is never seen by
    the loader — scenes then reference a "missing" pattern and the device
    drops into an ``err pattern`` debug loop (observed live as
    ``err pattern 213`` on EP-40 OS 2.5.1). New pads insert after the last
    existing member of their ``pads/<group>/`` directory; new patterns after
    the last existing ``patterns/`` member; anything else appends at the end.
    """
    output = []
    seen = set()
    for spec in base_specs:
        name = spec["name"]
        if name in remove_names:
            continue
        replacement = replacements.get(name, spec)
        output.append(replacement)
        seen.add(name)

    def section_key(name):
        if name.startswith("patterns/"):
            return "patterns/"
        if name.startswith("pads/"):
            return name.rsplit("/", 1)[0] + "/"
        return None

    def insert_in_section(spec):
        prefix = section_key(spec["name"])
        if prefix is not None:
            last = -1
            for index, existing in enumerate(output):
                existing_name = existing["name"]
                if existing_name.startswith(prefix) or existing_name == prefix.rstrip("/"):
                    last = index
            if last >= 0:
                # keep members within a section sorted by name for stable output
                insert_at = last + 1
                for index in range(last + 1):
                    existing_name = output[index]["name"]
                    if (existing_name.startswith(prefix)
                            and existing_name > spec["name"]):
                        insert_at = index
                        break
                output.insert(insert_at, spec)
                return
        output.append(spec)

    for spec in additions:
        name = spec["name"]
        if name in remove_names or name in seen:
            continue
        insert_in_section(replacements.get(name, spec))
        seen.add(name)
    return output


def compile_project(doc, base_archive=None):
    """Compile a project document into an EP project TAR.

    When ``base_archive`` is supplied, unspecified members and unknown bytes
    are preserved and only explicitly authored members are rebuilt. Scratch
    compilation remains available when no base is supplied.
    """
    if not isinstance(doc, dict):
        raise CompileError("project document must be a JSON object")

    try:
        profile = product_profile(doc.get("product"))
    except ValueError as exc:
        raise CompileError(str(exc)) from exc
    if base_archive is None and doc.get("baseArchiveBase64"):
        try:
            base_archive = base64.b64decode(doc["baseArchiveBase64"], validate=True)
        except (ValueError, TypeError) as exc:
            raise CompileError("baseArchiveBase64 is invalid") from exc
    base_specs = _archive_specs(base_archive) if base_archive is not None else []
    base_by_name = {spec["name"]: spec for spec in base_specs}
    patch_mode = base_archive is not None

    include_directories = bool(doc.get("includeDirectories", True))
    pad_members_mode = doc.get("padMembers", "all")
    if pad_members_mode not in ("all", "used"):
        raise CompileError('padMembers must be "all" or "used"')

    # Pads: scratch archives materialize defaults; patch archives preserve
    # every unspecified pad and replace only pads present in JSON.
    pad_payloads = {}
    if not patch_mode:
        for group in "abcd":
            for pad in range(1, 13):
                pad_payloads[f"pads/{group}/p{pad:02d}"] = bytes(
                    base_pad_payload(profile.pad_record_size)
                )
    used_pads = []
    for pad in doc.get("pads", []):
        group = str(pad.get("group", "")).upper()
        if group not in GROUPS:
            raise CompileError(f"pad group must be A..D: {pad}")
        number = int(pad.get("pad", 0))
        if not (1 <= number <= 12):
            raise CompileError(f"pad number must be 1..12: {pad}")
        name = f"pads/{group.lower()}/p{number:02d}"
        pad_payloads[name] = encode_pad(pad, profile=profile)
        used_pads.append(name)

    # Patterns.
    pattern_payloads = {}
    for pattern in doc.get("patterns", []):
        name = _pattern_member_name(pattern.get("id"))
        if name in pattern_payloads:
            raise CompileError(f"duplicate pattern id {pattern.get('id')!r}")
        events = []
        for event in pattern.get("events", []):
            events.append({
                "tick": event["tick"],
                "padByte": _event_pad_byte(event),
                "note": event.get("note", 60),
                "velocity": event.get("velocity", 100),
                "duration": event["duration"],
                "flag": event.get("flag", 0),
            })
        pattern_payloads[name] = encode_pattern(
            events,
            bars=int(pattern.get("bars", 1)),
            automation_events=pattern.get("automation"),
            profile=profile,
        )

    # Scenes + song, with the err-pattern-187 preflight: every nonzero group
    # pattern reference must resolve to a pattern member in this archive.
    scenes = []
    time_signatures = []
    for scene in doc.get("scenes", []):
        refs = scene.get("groupPatterns")
        if not isinstance(refs, list) or len(refs) != 4:
            raise CompileError(f"scene needs a 4-entry groupPatterns list: {scene}")
        if getattr(profile, "requires_full_scene_refs", True) and 0 in [int(v) for v in refs]:
            # Universal EP rule (isolated live on both devices): a defined
            # scene must reference a pattern for all four groups. EP-40
            # rejects with err pattern 213, EP-133 with err pattern 187 —
            # both are power-cycle debug loops. Silence a group by
            # referencing an empty pattern member instead.
            raise CompileError(
                f"scene {refs} has a zero group-pattern reference; EP devices "
                "reject partial-zero scenes with a debug loop (err pattern 187 "
                "on EP-133, 213 on EP-40) — reference an empty pattern member "
                "for a silent group (author one with no events)"
            )
        for group_index, ref in enumerate(refs):
            ref = int(ref)
            if ref < 0 or ref > 99:
                raise CompileError(f"scene pattern index out of range: {scene}")
            if ref:
                member = f"patterns/{'abcd'[group_index]}{ref:02d}"
                available_patterns = set(pattern_payloads)
                if patch_mode:
                    available_patterns.update(
                        name for name in base_by_name if name.startswith("patterns/")
                    )
                if member not in available_patterns:
                    raise CompileError(
                        f"scene references missing {member}; the device rejects this "
                        "with an 'err pattern 187' debug loop — include the pattern "
                        "member or drop the reference"
                    )
        scenes.append([int(v) for v in refs])
        ts = scene.get("timeSignature")
        if ts is not None and (not isinstance(ts, list) or len(ts) != 2):
            raise CompileError(f"timeSignature must be [numerator, denominator]: {scene}")
        time_signatures.append(tuple(int(v) for v in ts) if ts else None)
    song = [int(v) for v in doc.get("song", [1] if scenes else [])]

    # Assemble newly authored member specs in canonical order.
    additions = []
    replacements = {}
    if include_directories:
        additions.append({"name": "pads", "type": "5"})
    emitted_pads = set(used_pads) if pad_members_mode == "used" else None
    for group in "abcd":
        if include_directories:
            additions.append({"name": f"pads/{group}", "type": "5"})
        for pad in range(1, 13):
            name = f"pads/{group}/p{pad:02d}"
            if name in pad_payloads and (emitted_pads is None or name in emitted_pads):
                spec = {"name": name, "payload": pad_payloads[name]}
                additions.append(spec)
                replacements[name] = spec
    if include_directories:
        additions.append({"name": "patterns", "type": "5"})
    for member in sorted(pattern_payloads, key=lambda name: (name[9], name[-2:])):
        spec = {"name": member, "payload": pattern_payloads[member]}
        additions.append(spec)
        replacements[member] = spec
    if "scenes" in doc and scenes:
        spec = {
            "name": "scenes",
            "payload": make_scenes_member(
                scenes, song, time_signatures=time_signatures,
                current_scene=doc.get("currentScene"),
            ),
        }
        additions.append(spec)
        replacements["scenes"] = spec

    settings = doc.get("settings")
    if settings is not None:
        if not isinstance(settings, dict):
            raise CompileError("settings must be an object")
        template = base_by_name.get("settings", {}).get("payload")
        if settings.get("templateBase64") is not None:
            template = _decode_member_data(
                {"base64": settings["templateBase64"]}, "settings.templateBase64"
            )
        if template is None and not settings.get("unsafeFabricate", False):
            raise CompileError(
                "safe settings authoring needs a base archive or templateBase64; "
                "set unsafeFabricate only for an acknowledged disposable-slot probe"
            )
        bpm = settings.get("bpm", None if template is not None else 120.0)
        spec = {
            "name": "settings",
            "payload": settings_payload(
                bpm, settings.get("groupFaders"), template=template
            ),
        }
        additions.append(spec)
        replacements["settings"] = spec
    if doc.get("fxSettings"):
        fx_spec = doc["fxSettings"]
        template = base_by_name.get("fx_settings", {}).get("payload")
        if isinstance(fx_spec, dict) and fx_spec.get("templateBase64") is not None:
            template = _decode_member_data(
                {"base64": fx_spec["templateBase64"]}, "fxSettings.templateBase64"
            )
        spec = {
            "name": "fx_settings",
            "payload": fx_settings_payload(fx_spec, template=template),
        }
        additions.append(spec)
        replacements["fx_settings"] = spec

    live = doc.get("live")
    if live is not None:
        if not isinstance(live, dict):
            raise CompileError("live must be an object")
        if not profile.supports_live:
            raise CompileError("the live member requires the ep40/Riddim product profile")
        has_patterns = bool(pattern_payloads) or any(
            name.startswith("patterns/") and base_by_name[name].get("type") != "5"
            for name in base_by_name
        )
        if has_patterns and not bool(live.get("allowWithPatterns", False)):
            raise CompileError(
                "live + populated patterns has not been decoded; omit patterns or set "
                "live.allowWithPatterns only for an explicit research probe"
            )
        if doc.get("fxSettings") and not bool(live.get("allowWithFxSettings", False)):
            raise CompileError(
                "live and fx_settings have not been observed together on Riddim"
            )
        authored_pads = {
            (str(pad.get("group", "")).upper(), int(pad.get("pad", 0))): pad
            for pad in doc.get("pads", [])
        }
        for armed in live.get("armedPads", []):
            key = (str(armed.get("group", "")).upper(), int(armed.get("pad", 0)))
            pad = authored_pads.get(key)
            if pad is not None and int(pad.get("playMode", 0)) != 3:
                raise CompileError(f"live armed pad {key[0]}{key[1]:02d} is not in loop mode")
            if not patch_mode and pad is None:
                raise CompileError(
                    f"scratch live armed pad {key[0]}{key[1]:02d} needs a matching loop pad"
                )
        spec = {"name": "live", "payload": make_live_member(live)}
        additions.append(spec)
        replacements["live"] = spec

    for raw_spec in _raw_member_specs(doc.get("rawMembers")):
        name = raw_spec["name"]
        if name in replacements:
            raise CompileError(f"raw member {name!r} conflicts with an authored section")
        additions.append(raw_spec)
        replacements[name] = raw_spec

    remove_items = doc.get("removeMembers", [])
    if not isinstance(remove_items, list):
        raise CompileError("removeMembers must be a list of member names")
    remove_names = {str(name) for name in remove_items}
    if patch_mode:
        specs = _merge_archive_specs(base_specs, replacements, additions, remove_names)
    else:
        specs = [spec for spec in additions if spec["name"] not in remove_names]

    names = [spec["name"] for spec in specs]
    if len(names) != len(set(names)):
        duplicates = sorted({name for name in names if names.count(name) > 1})
        raise CompileError(f"duplicate TAR members after merge: {duplicates}")

    # Structural ordering invariant (err pattern 213): the firmware loader
    # registers pattern members from the patterns region only — a pattern
    # member serialized after `scenes` is never seen, and scenes referencing
    # it throw the device into a debug loop. Enforce on every compile path
    # (scratch, base-merge, rawMembers) rather than trusting any one merge.
    if "scenes" in names:
        scenes_position = names.index("scenes")
        stragglers = [
            name for index, name in enumerate(names)
            if name.startswith("patterns/") and index > scenes_position
        ]
        if stragglers:
            raise CompileError(
                f"pattern members {stragglers} would be serialized after `scenes`; "
                "the device loader never registers patterns there (err pattern 213) "
                "— this is a compiler ordering bug or a conflicting rawMembers layout"
            )

    return build_archive(specs)


def compile_project_file(path):
    """Load a JSON document from ``path`` and compile it to TAR bytes."""
    path = Path(path)
    with path.open("r", encoding="utf-8") as handle:
        doc = json.load(handle)
    base_archive = None
    base_spec = doc.get("baseArchive")
    if isinstance(base_spec, str):
        base_archive = (path.parent / base_spec).resolve().read_bytes()
    elif isinstance(base_spec, dict):
        if "path" in base_spec:
            base_archive = (path.parent / base_spec["path"]).resolve().read_bytes()
        elif "base64" in base_spec:
            base_archive = _decode_member_data(base_spec, "baseArchive")
        else:
            raise CompileError("baseArchive needs path or base64")
    return compile_project(doc, base_archive=base_archive)


# ---------------------------------------------------------------------------
# .ppak container
# ---------------------------------------------------------------------------

def build_ppak(tar_bytes, project_number=1, device_name="EP",
               device_sku="TE032AS001", device_version="2.5.1", author="computer"):
    """Wrap a project TAR in a ``.ppak`` ZIP container.

    Entries carry a leading slash (``/meta.json``, ``/projects/PNN.tar``) —
    the leading slash is significant to the official Sample Tool (missing it
    produces "PAK FILE IS EMPTY"). ``device_version`` should match the
    target device's firmware; import acceptance rules (pak_type, SKU,
    compression tolerance) are not fully mapped, so treat generated ppaks as
    Sample-Tool import candidates, not guaranteed-accepted artifacts.
    """
    if not (1 <= int(project_number) <= 99):
        raise CompileError(f"project number {project_number} out of range")
    meta = {
        "info": "teenage engineering - pak file",
        "pak_version": 1,
        "pak_type": "project",
        "pak_release": "1.2.0",
        "device_name": device_name,
        "device_sku": device_sku,
        "device_version": device_version,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
        "author": author,
        "base_sku": device_sku,
    }
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("/meta.json", json.dumps(meta, indent=1))
        zf.writestr(f"/projects/P{int(project_number):02d}.tar", tar_bytes)
    return buffer.getvalue()
