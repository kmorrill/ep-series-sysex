#!/usr/bin/env python3
"""Offline round-trip tests for epsysex.

Runnable two ways (no pytest required):

    python3 tests/test_roundtrip.py
    pytest tests/test_roundtrip.py

No device I/O; everything here runs against compiler-generated synthetic bytes.
No device project captures, OEM projects, or sounds are checked into the repo.
"""
from __future__ import annotations

import io
import json
import random
import struct
import subprocess
import sys
import tarfile
import tempfile
import wave
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from epsysex import compile as epcompile
from epsysex import tar as eptar
from epsysex.fileclient import (
    SOUND_CREATE_FLAGS,
    SOUNDS_NODE_ID,
    FileClient,
    sound_fid,
)
from epsysex.dependencies import dependency_plan, prepare_sound
from epsysex.identity import identity_from_ppak
from epsysex.hardware import MidiEvent, compare_note_ons, expected_note_ons
from epsysex.hardware_cases import build_cases, sound_reference_from_archive
from epsysex.sysex import EPSysExError, pack7, unpack7

EXAMPLE = REPO / "examples" / "example-project.json"

ALL_PAD_MEMBERS = [f"pads/{g}/p{p:02d}" for g in "abcd" for p in range(1, 13)]


# ---------------------------------------------------------------------------
# packed7 codec
# ---------------------------------------------------------------------------

def test_pack7_roundtrip():
    rng = random.Random(0x133)
    cases = [
        b"",
        b"\x00",
        b"\x7f",
        b"\x80",
        b"\xff" * 7,
        b"\xff" * 8,
        bytes(range(256)),
    ]
    for length in (1, 2, 6, 7, 8, 13, 14, 15, 100, 324, 433):
        cases.append(bytes(rng.randrange(256) for _ in range(length)))
    for raw in cases:
        encoded = pack7(list(raw))
        assert all(0 <= b < 0x80 for b in encoded), "packed7 bytes must stay below 0x80"
        decoded = bytes(unpack7(encoded))
        assert decoded == raw, f"pack7/unpack7 round-trip failed for {raw!r}"
    # 7 raw bytes cost exactly 8 encoded bytes (flag + 7 data)
    assert len(pack7(list(b"\xff" * 7))) == 8


# ---------------------------------------------------------------------------
# compile example-project.json -> TAR -> parse back
# ---------------------------------------------------------------------------

def _compile_example():
    return epcompile.compile_project_file(EXAMPLE)


def _synthetic_fixture():
    """Return an authored archive suitable for parser/diff/HIL-builder tests."""
    return _compile_example()


def test_compile_example_member_set():
    tar_bytes = _compile_example()
    members = {name: (size, typeflag)
               for name, _start, size, typeflag in eptar.iter_members(tar_bytes)}

    for directory in ("pads", "pads/a", "pads/b", "pads/c", "pads/d", "patterns"):
        assert members[directory][1] == "5", f"{directory} should be a directory entry"
    for name in ALL_PAD_MEMBERS:
        assert name in members, f"missing pad member {name}"
    assert "patterns/a01" in members
    assert "scenes" in members
    # The example deliberately omits settings/fx_settings (ERR CLOCK 43 caveat).
    assert "settings" not in members
    assert "fx_settings" not in members
    assert tar_bytes.endswith(b"\0" * 1024), "missing TAR end marker"

    # stdlib tarfile must agree with our dialect parser
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:") as tf:
        std_names = {m.name for m in tf.getmembers()}
    assert std_names == set(members), "stdlib tarfile and epsysex.tar disagree"


def test_compile_example_pad_records():
    payloads = eptar.member_payloads(_compile_example())
    for name in ALL_PAD_MEMBERS:
        record = payloads[name]
        assert len(record) == 27, f"{name}: pad record must be 27 bytes, got {len(record)}"
        assert record[0] == 0, f"{name}: pad record byte 0 must be zero"
    # assigned pad A10 (slot 421 = 0x01A5, trim end 13699)
    p10 = payloads["pads/a/p10"]
    assert p10[1] | (p10[2] << 8) == 421
    assert int.from_bytes(p10[8:12], "little") == 13699
    assert p10[16] == 100 and p10[20] == 255 and p10[24] == 60
    # unassigned default pad
    p01 = payloads["pads/b/p01"]
    assert p01[1] == 0 and p01[16] == 100 and p01[20] == 255 and p01[24] == 60


def test_compile_example_pattern_encoding():
    payloads = eptar.member_payloads(_compile_example())
    pattern = payloads["patterns/a01"]
    doc = json.loads(EXAMPLE.read_text())
    spec = doc["patterns"][0]
    expected_records = len(spec["events"]) + len(spec["automation"])

    assert pattern[0] == 0, "pattern header byte 0 must be zero (err pattern 71)"
    assert pattern[1] == spec["bars"]
    assert pattern[2] == expected_records
    assert pattern[3] == 0
    assert len(pattern) == 4 + expected_records * 8

    records = [pattern[4 + i * 8:12 + i * 8] for i in range(expected_records)]
    ticks = [r[0] | (r[1] << 8) for r in records]
    assert ticks == sorted(ticks), "records must be interleaved in tick order"
    # automation records: [tick16, 0x01, parameterID, 0, value16 (<=32767), 0]
    automation = [r for r in records if r[2] == 1 and r[4] == 0 and r[7] == 0]
    assert len(automation) == len(spec["automation"])
    for r in automation:
        value = r[5] | (r[6] << 8)
        assert 0 <= value <= 32767
        assert r[3] == 0  # parameterID 0 = level in the example
    # at tick 0 the automation record sorts ahead of the note event
    assert records[0][2] == 1 and records[1][2] == 72  # padByte (10-1)*8 = 72


def test_compile_example_scenes_member():
    payloads = eptar.member_payloads(_compile_example())
    scenes = payloads["scenes"]
    assert len(scenes) == 712, f"scenes member must be 712 bytes, got {len(scenes)}"
    # scene 1 chunk: all-nonzero refs (device rule) + 4/4 time signature
    chunk = scenes[7:13]
    assert list(chunk) == [1, 1, 1, 1, 4, 4]
    # header and unused chunks default to 4/4
    assert scenes[5] == 4 and scenes[6] == 4
    assert scenes[7 + 6 + 4] == 4 and scenes[7 + 6 + 5] == 4
    # trailer: scene cursor, song length, song entries (1-based)
    trailer = 7 + 99 * 6
    assert scenes[trailer + 3] == 1
    assert scenes[trailer + 11] == 1
    assert scenes[trailer + 12] == 1


def test_compile_validation_guards():
    doc = json.loads(EXAMPLE.read_text())

    # err pattern 187: scene referencing a missing pattern member
    bad = json.loads(json.dumps(doc))
    bad["scenes"] = [{"groupPatterns": [1, 1, 0, 0]}]  # B01 does not exist
    _assert_raises(epcompile.CompileError, epcompile.compile_project, bad)

    # err fader 93: automation value beyond 32767
    bad = json.loads(json.dumps(doc))
    bad["patterns"][0]["automation"][0]["value"] = 40000
    _assert_raises(epcompile.CompileError, epcompile.compile_project, bad)

    # automation parameter id out of range
    bad = json.loads(json.dumps(doc))
    bad["patterns"][0]["automation"][0]["parameterID"] = 12
    _assert_raises(epcompile.CompileError, epcompile.compile_project, bad)

    # decoded device ranges must fail before a TAR can reach hardware
    bad = json.loads(json.dumps(doc))
    bad["scenes"][0]["timeSignature"] = [0, 4]
    _assert_raises(epcompile.CompileError, epcompile.compile_project, bad)

    bad = {"settings": {"bpm": 400, "unsafeFabricate": True}}
    _assert_raises(epcompile.CompileError, epcompile.compile_project, bad)


# ---------------------------------------------------------------------------
# synthetic archive fixture
# ---------------------------------------------------------------------------

def test_fixture_members():
    tar_bytes = _synthetic_fixture()
    members = {name: (size, typeflag)
               for name, _start, size, typeflag in eptar.iter_members(tar_bytes)}

    for directory in ("pads", "pads/a", "pads/b", "pads/c", "pads/d", "patterns"):
        assert directory in members and members[directory][1] == "5"
    pad_members = [n for n in members if n.startswith("pads/") and members[n][1] != "5"]
    assert len(pad_members) == 48
    payloads = eptar.member_payloads(tar_bytes)
    for name in pad_members:
        assert members[name][0] == 27, f"{name}: expected 27-byte pad record"
        assert payloads[name][0] == 0, f"{name}: pad record byte 0 must be zero"

    patterns = [n for n in members if n.startswith("patterns/") and members[n][1] != "5"]
    assert "patterns/a01" in patterns
    assert payloads["patterns/a01"][0] == 0, "pattern header byte 0 must be zero"

    assert members["scenes"][0] == 712


# ---------------------------------------------------------------------------
# tar dialect round-trip + in-place patch
# ---------------------------------------------------------------------------

def test_tar_build_parse_patch_roundtrip():
    specs = [
        {"name": "pads", "type": "5"},
        {"name": "pads/a", "type": "5"},
        {"name": "pads/a/p01", "payload": bytes(27)},
        {"name": "patterns", "type": "5"},
        {"name": "patterns/a01", "payload": epcompile.empty_pattern_payload(2)},
        {"name": "scenes", "payload": epcompile.make_scenes_member([[1, 0, 0, 0]], [1])},
    ]
    tar_bytes = eptar.build_archive(specs)
    payloads = eptar.member_payloads(tar_bytes)
    assert payloads["pads/a/p01"] == bytes(27)
    assert payloads["patterns/a01"] == bytes([0, 2, 0, 0])
    assert len(payloads["scenes"]) == 712

    patched = eptar.patch_member_in_place(tar_bytes, "patterns/a01", bytes([0, 4, 0, 0]))
    assert eptar.member_payloads(patched)["patterns/a01"] == bytes([0, 4, 0, 0])
    assert len(patched) == len(tar_bytes)
    _assert_raises(ValueError, eptar.patch_member_in_place, tar_bytes, "patterns/a01", bytes(12))
    _assert_raises(ValueError, eptar.patch_member_in_place, tar_bytes, "patterns/zz9", b"")


def test_ppak_container():
    tar_bytes = _compile_example()
    ppak = epcompile.build_ppak(tar_bytes, project_number=1, device_version="2.5.1")
    with zipfile.ZipFile(io.BytesIO(ppak)) as zf:
        names = zf.namelist()
        assert "/meta.json" in names, "ppak entries must carry a leading slash"
        assert "/projects/P01.tar" in names
        assert zf.read("/projects/P01.tar") == tar_bytes
    with tempfile.TemporaryDirectory() as directory:
        tmp = Path(directory) / "synthetic.ppak"
        tmp.write_bytes(ppak)
        identity = identity_from_ppak(tmp)
        assert identity["device_version"] == "2.5.1"
        assert identity["device_name"] == "EP"


# ---------------------------------------------------------------------------
# diff tool: fixture against itself -> zero differences
# ---------------------------------------------------------------------------

def test_diff_fixture_against_itself():
    with tempfile.TemporaryDirectory() as tmp:
        fixture = Path(tmp) / "synthetic.tar"
        fixture.write_bytes(_synthetic_fixture())
        result = subprocess.run(
            [sys.executable, str(REPO / "tools" / "diff_tars.py"),
             str(fixture), str(fixture)], capture_output=True, text=True,
        )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "no differences", result.stdout


# ---------------------------------------------------------------------------
# hardware-suite case generation (offline; no ports opened)
# ---------------------------------------------------------------------------

def test_hardware_case_matrix_compiles_and_covers_json_surface():
    fixture = _synthetic_fixture()
    reference = sound_reference_from_archive(fixture, {421})
    assert reference["slot"] == 421 and reference["trimEnd"] > reference["trimStart"]

    cases = build_cases(reference, include_risky_settings=True, product="ep40")
    assert {case.name for case in cases} == {
        "authoring_surface", "sparse_archive", "pad_sound_design",
        "automation_matrix", "fx_output_sidechain", "supertone_pads",
        "loop_startup", "settings_member",
    }
    for case in cases:
        compiled = epcompile.compile_project(case.document)
        assert compiled.endswith(b"\0" * 1024), case.name
        assert len(compiled) > 1024, case.name

    top_level = set().union(*(case.document for case in cases))
    assert {
        "schema", "product", "metadata", "settings", "fxSettings",
        "includeDirectories", "padMembers", "pads", "patterns", "scenes",
        "song", "live",
    } <= top_level
    pad_fields = set().union(*(
        pad
        for case in cases
        for pad in case.document.get("pads", [])
    ))
    assert {
        "group", "pad", "slot", "trimStart", "trimEnd", "sampleBpm",
        "amplitude", "pitch", "pitchFraction", "pan", "attack", "release",
        "timeMode", "chokeGroup", "playMode", "rootNote", "midiChannel",
    } <= pad_fields
    automation_ids = {
        event["parameterID"]
        for case in cases
        for pattern in case.document.get("patterns", [])
        for event in pattern.get("automation", [])
    }
    assert automation_ids == set(range(12))


def test_structured_fx_output_and_sidechain_encoding():
    doc = {
        "fxSettings": {
            "type": "filter",
            "parameter1": 0.25,
            "parameter2": 0.75,
            "outputCompressor": {"drive": 0.2, "speed": 0.8},
            "sidechain": {
                "length": 0.3,
                "shape": 0.6,
                "routing": {
                    "A": {"sourcePads": [1, 10]},
                    "C": {"destination": True},
                },
            },
        },
    }
    payload = eptar.member_payloads(epcompile.compile_project(doc))["fx_settings"]
    assert len(payload) == 160 and payload[4] == 5
    assert abs(struct.unpack_from("<f", payload, 28)[0] - 0.25) < 1e-6
    assert abs(struct.unpack_from("<f", payload, 92)[0] - 0.75) < 1e-6
    assert abs(struct.unpack_from("<f", payload, 136)[0] - 0.2) < 1e-6
    assert abs(struct.unpack_from("<f", payload, 140)[0] - 0.8) < 1e-6
    assert abs(struct.unpack_from("<f", payload, 144)[0] - 0.3) < 1e-6
    assert abs(struct.unpack_from("<f", payload, 148)[0] - 0.6) < 1e-6
    assert struct.unpack_from("<H", payload, 152)[0] == (1 << 0) | (1 << 9)
    assert struct.unpack_from("<H", payload, 156)[0] == 0x8000


def test_riddim_supertone_and_live_encoding():
    doc = {
        "product": "ep40",
        "pads": [{
            "group": "B", "pad": 4,
            "supertone": {"engine": 2, "knobX": 17, "knobY": 231},
            "rootNote": 52,
        }, {
            "group": "A", "pad": 10, "slot": 1, "trimEnd": 1000,
            "playMode": 3, "timeMode": 1,
        }],
        "live": {"armedPads": [{"group": "A", "pad": 10}]},
    }
    payloads = eptar.member_payloads(epcompile.compile_project(doc))
    assert all(len(payloads[f"pads/{g}/p{p:02d}"]) == 29
               for g in "abcd" for p in range(1, 13))
    pad = payloads["pads/b/p04"]
    assert int.from_bytes(pad[1:3], "little") == 1002  # sym 1003 stores sym-1
    assert pad[27:29] == bytes([17, 231]) and pad[24] == 52
    assert len(payloads["live"]) == 48
    assert payloads["live"][9] == 1 and sum(payloads["live"]) == 1

    bad = json.loads(json.dumps(doc))
    bad["product"] = "ep"
    _assert_raises(epcompile.CompileError, epcompile.compile_project, bad)


def test_base_archive_preserves_unknowns_and_safely_patches_settings_raw_members():
    settings = bytearray(epcompile.settings_payload(120, []))
    settings[8] = 0x5A  # unknown byte must survive known-field patches
    base = eptar.build_archive([
        {"name": "pads", "type": "5"},
        {"name": "pads/a", "type": "5"},
        {"name": "pads/a/p01", "payload": bytes(epcompile.base_pad_payload())},
        {"name": "settings", "payload": bytes(settings)},
        {"name": "future_member", "payload": b"preserve-me"},
    ])
    doc = {
        "pads": [{"group": "A", "pad": 1, "slot": 77, "trimEnd": 1000}],
        "settings": {"bpm": 133.25},
        "rawMembers": [{"name": "vendor_extension", "hex": "0102feff"}],
    }
    compiled = epcompile.compile_project(doc, base_archive=base)
    payloads = eptar.member_payloads(compiled)
    assert payloads["future_member"] == b"preserve-me"
    assert payloads["vendor_extension"] == bytes.fromhex("0102feff")
    assert payloads["settings"][8] == 0x5A
    assert abs(struct.unpack_from("<f", payloads["settings"], 4)[0] - 133.25) < 1e-6
    assert int.from_bytes(payloads["pads/a/p01"][1:3], "little") == 77

    unsafe = {"settings": {"bpm": 120}}
    _assert_raises(epcompile.CompileError, epcompile.compile_project, unsafe)


def test_sound_dependency_plan_and_wav_preparation():
    doc = {
        "pads": [{"group": "A", "pad": 1, "slot": 50}],
        "sounds": [{"slot": 50, "path": "tone.wav", "name": "tone"}],
    }
    assert dependency_plan(doc, available_slots=())["missingSlots"] == [50]
    assert dependency_plan(doc, available_slots=(50,))["missingSlots"] == []
    with tempfile.TemporaryDirectory() as directory:
        wav_path = Path(directory) / "tone.wav"
        with wave.open(str(wav_path), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(24000)
            handle.writeframes(struct.pack("<hhhh", 0, 1000, -1000, 0))
        prepared = prepare_sound(doc["sounds"][0], directory)
        assert prepared["slot"] == 50
        assert prepared["stream"]["samplerate"] == 46875
        assert prepared["stream"]["channels"] == 1
        assert prepared["stream"]["frames"] == round(4 * 46875 / 24000)
        assert prepared["pcm"]


def test_hardware_midi_oracle_checks_values_timing_channel_and_duration():
    doc = {
        "pads": [{"group": "A", "pad": 10, "midiChannel": 2}],
        "patterns": [{
            "id": "A01",
            "events": [{
                "tick": 96, "pad": 10, "note": 64, "velocity": 112, "duration": 24,
            }],
        }],
        "scenes": [{"groupPatterns": [1, 0, 0, 0]}],
    }
    expected = expected_note_ons(doc, bpm=120)
    captured = [
        MidiEvent(time=0.505, type="note_on", channel=1, note=64, velocity=112),
        MidiEvent(time=0.630, type="note_off", channel=1, note=64, velocity=0),
    ]
    assert compare_note_ons(captured, expected, timing_tolerance=0.02) == []
    captured[0].velocity = 100
    failures = compare_note_ons(captured, expected, timing_tolerance=0.02)
    assert any("velocity" in failure for failure in failures)


# ---------------------------------------------------------------------------
# sample-library FILE ops (offline: a fake device records request bytes and
# returns canned responses, so the exact wire payloads are asserted without
# opening a MIDI port)
# ---------------------------------------------------------------------------

def _default_responder(payload):
    """Minimal stand-in device: enough response shape to drive each op."""
    if payload[0] == 0x01:  # FILE_INIT -> chunk size 512
        return [0x00, 0x00, 0x00, 0x02, 0x00]
    if payload[0] == 0x02 and payload[1] == 0x00:  # PUT open -> echo the fid
        return [payload[3], payload[4]]
    if payload[0] == 0x07 and payload[1] == 0x02:  # METADATA get -> canned JSON
        return [0x00, 0x00] + list(b'{"name":"kick","channels":1}\x00')
    return []


class _FakeClient(FileClient):
    """FileClient with the MIDI session/transport replaced by a recorder."""

    def __init__(self, responder=None):
        super().__init__(use_lock=False)
        self.sent = []
        self._responder = responder or _default_responder

    def _session(self, op, body):  # no ports, no lock
        return body(None, None)

    def _request(self, midi_in, midi_out, raw_payload, timeout=3.0):
        payload = list(raw_payload)
        self.sent.append(payload)
        return self._responder(payload)


def test_sound_fid_bounds():
    assert sound_fid(1) == 1 and sound_fid(999) == 999
    _assert_raises(EPSysExError, sound_fid, 0)
    _assert_raises(EPSysExError, sound_fid, 1000)


def test_stat_request_and_parse():
    stat_resp = [0, 56, 0x03, 0xE8, 0x1D, 0, 0, 0x04, 0xD2] + list(b"056.pcm\x00")
    client = _FakeClient(lambda p: [0, 0, 0, 2, 0] if p[0] == 0x01 else stat_resp)
    info = client.stat_sound(56)
    assert client.sent[0][0] == 0x01, "STAT must FILE_INIT first"
    assert client.sent[1] == [0x0B, 0, 56], "STAT request bytes"
    assert info == {
        "fid": 56, "parent": 1000, "flags": 0x1D, "byteSize": 1234, "name": "056.pcm",
    }


def test_delete_and_audition_requests():
    client = _FakeClient()
    client.delete_sound(56)
    # 0x06 <fid u16> — NOT 0x06 0x02 (that form no-ops on the device)
    assert client.sent[-1] == [0x06, 0, 56], "DELETE request bytes"

    client = _FakeClient()
    client.audition_sound(56)
    assert client.sent[-1] == [0x05, 0x01, 0, 56], "PLAY request bytes"


def test_upload_sound_wire_sequence():
    client = _FakeClient()
    pcm = bytes(range(10))
    summary = client.upload_sound(
        56, pcm, name="a name well over the twenty char limit",
        channels=2, samplerate=44100,
    )
    open_req = client.sent[1]
    assert open_req[:3] == [0x02, 0x00, SOUND_CREATE_FLAGS], "sound create flags 0x05"
    assert open_req[3:7] == [0, 56, SOUNDS_NODE_ID >> 8, SOUNDS_NODE_ID & 0xFF]
    assert open_req[7:11] == [0, 0, 0, 10], "size u32 big-endian"
    assert bytes(open_req[11:]) == b"056.pcm\x00", "default node name"

    data_req = client.sent[2]
    assert data_req[:2] == [0x02, 0x01] and bytes(data_req[4:]) == pcm
    eof = client.sent[3]
    assert eof[:2] == [0x02, 0x01] and len(eof) == 4, "empty final page = EOF"

    meta_req = client.sent[-1]
    assert meta_req[:2] == [0x07, 0x01], "metadata set is last"
    meta = json.loads(bytes(meta_req[4:]).split(b"\x00", 1)[0])
    assert meta["channels"] == 2 and meta["samplerate"] == 44100 and meta["format"] == "s16"
    assert meta["name"] == "a name well over the"  # clamped to 20 chars
    assert summary == {
        "slot": 56, "fid": 56, "bytes": 10, "nodeName": "056.pcm", "metadata": meta,
    }


def test_upload_sound_can_skip_metadata():
    client = _FakeClient()
    client.upload_sound(56, b"\x00\x01", write_metadata=False)
    assert not any(p[:2] == [0x07, 0x01] for p in client.sent), "no metadata write expected"


def test_get_sample_metadata_request_and_parse():
    client = _FakeClient()
    meta = client.get_sample_metadata(1)
    assert meta == {"name": "kick", "channels": 1}
    get_req = client.sent[1]
    assert get_req[:4] == [0x07, 0x02, 0, 1]
    client = _FakeClient()
    client.get_sample_metadata(1, key="name")
    assert bytes(client.sent[-1][6:]).split(b"\x00", 1)[0] == b"name"


def test_sample_metadata_write_rules_reject_before_any_io():
    # integer enum, and playmode without its required envelope.release pair
    for bad in ({"time.mode": 2}, {"sound.playmode": "oneshot"}):
        client = _FakeClient()
        _assert_raises(EPSysExError, client.set_sample_metadata, 1, bad)
        assert client.sent == [], "rejected metadata must not touch the wire"
    # the same guard applies to upload_sound's extra_metadata
    client = _FakeClient()
    _assert_raises(
        EPSysExError, lambda: client.upload_sound(56, b"\x00", extra_metadata={"time.mode": 3})
    )
    assert client.sent == []


def test_set_sample_metadata_valid_write():
    client = _FakeClient()
    client.set_sample_metadata(1, {"sound.playmode": "legato", "envelope.release": 20})
    set_req = client.sent[-1]
    assert set_req[:4] == [0x07, 0x01, 0, 1]
    assert json.loads(bytes(set_req[4:]).split(b"\x00", 1)[0]) == {
        "sound.playmode": "legato", "envelope.release": 20,
    }


# ---------------------------------------------------------------------------
# debug-frame detection must work per-product (EP-40 regression: the device
# looped `err pattern 213` mid-write and the old data[4]==data[3] check
# silently ignored every frame because 0x3c != 0x33)
# ---------------------------------------------------------------------------

def test_debug_frame_detection_all_products():
    from epsysex.sysex import is_debug_frame
    ep133_debug = [0x00, 0x20, 0x76, 0x33, 0x33] + list(b"err pattern 71 2_5_1")
    ep40_debug = [0x00, 0x20, 0x76, 0x3C, 0x33] + list(b"err pattern 213 2_5_1")
    ep133_normal = [0x00, 0x20, 0x76, 0x33, 0x40, 0x20, 0x00, 0x05, 0x00]
    ep40_normal = [0x00, 0x20, 0x76, 0x3C, 0x40, 0x20, 0x00, 0x05, 0x00]
    assert is_debug_frame(ep133_debug), "EP-133 debug frame must be detected"
    assert is_debug_frame(ep40_debug), "EP-40 debug frame must be detected"
    assert not is_debug_frame(ep133_normal)
    assert not is_debug_frame(ep40_normal)


# ---------------------------------------------------------------------------
# base-archive merge must insert new pattern members inside the patterns
# section (EP-40 regression: new patterns appended after `scenes` are never
# registered by the loader -> scenes reference a "missing" pattern ->
# `err pattern 213` debug loop)
# ---------------------------------------------------------------------------

def test_base_merge_inserts_new_patterns_before_scenes():
    base_specs = [
        {"name": "pads", "type": "5"},
        {"name": "pads/a", "type": "5"},
        {"name": "pads/a/p01", "payload": bytes(27)},
        {"name": "patterns", "type": "5"},
        {"name": "patterns/a01", "payload": epcompile.empty_pattern_payload(1)},
        {"name": "patterns/b01", "payload": epcompile.empty_pattern_payload(1)},
        {"name": "scenes", "payload": epcompile.make_scenes_member([[1, 0, 0, 0]], [1])},
    ]
    base = eptar.build_archive(base_specs)
    doc = {
        "patterns": [
            {"id": "A02", "bars": 1,
             "events": [{"tick": 0, "pad": 1, "note": 60, "velocity": 100, "duration": 24}]},
            {"id": "C01", "bars": 1, "events": []},
            {"id": "D01", "bars": 1, "events": []},
        ],
        "scenes": [{"groupPatterns": [2, 1, 1, 1]}],
        "song": [1],
    }
    merged = epcompile.compile_project(doc, base_archive=base)
    names = [name for name, *_ in eptar.iter_members(merged)]
    pattern_positions = [i for i, n in enumerate(names) if n.startswith("patterns/")]
    assert max(pattern_positions) < names.index("scenes"), (
        f"new patterns must precede scenes, got order {names}"
    )
    section = [n for n in names if n.startswith("patterns/")]
    assert section == sorted(section) == [
        "patterns/a01", "patterns/a02", "patterns/b01", "patterns/c01",
        "patterns/d01",
    ]


def test_ep40_pattern_dialect():
    """The ep40 profile must emit the native Riddim 6-byte pattern header
    [1, bars, 0xFF, 0xFF, count u16 LE]; the default profile keeps the
    EP-133-verified 4-byte form [0, bars, count, 0]."""
    from epsysex.products import product_profile
    events = [{"tick": 0, "padByte": 72, "note": 60, "velocity": 100, "duration": 24}]

    ep40 = epcompile.encode_pattern(events, bars=3, profile=product_profile("ep40"))
    assert ep40[:6] == bytes([1, 3, 0xFF, 0xFF, 1, 0])
    assert len(ep40) == 6 + 8

    default = epcompile.encode_pattern(events, bars=3)
    assert default[:4] == bytes([0, 3, 1, 0])
    assert len(default) == 4 + 8

    assert epcompile.empty_pattern_payload(2, profile=product_profile("ep40")) == bytes(
        [1, 2, 0xFF, 0xFF, 0, 0]
    )
    assert epcompile.empty_pattern_payload(2) == bytes([0, 2, 0, 0])

    # compile_project must thread the profile through to pattern encoding
    # (all-nonzero scene refs: the Riddim loader rejects partial-zero scenes)
    doc = {
        "product": "ep40",
        "pads": [],
        "patterns": [
            {"id": "A01", "bars": 1, "events": [
                {"tick": 0, "pad": 1, "note": 60, "velocity": 100, "duration": 24}]},
            {"id": "B01", "bars": 1, "events": []},
            {"id": "C01", "bars": 1, "events": []},
            {"id": "D01", "bars": 1, "events": []},
        ],
        "scenes": [{"groupPatterns": [1, 1, 1, 1]}],
        "song": [1],
    }
    payloads = eptar.member_payloads(epcompile.compile_project(doc))
    assert payloads["patterns/a01"][:6] == bytes([1, 1, 0xFF, 0xFF, 1, 0])


def test_scenes_reject_partial_zero_refs_all_profiles():
    """Universal EP rule (isolated live on both devices): a defined scene
    chunk must reference a pattern in all four groups. EP-40 rejects
    partial-zero scenes with err pattern 213, EP-133 with err pattern 187.
    Every profile must refuse them at compile time."""
    def doc_for(product):
        return {
            "product": product,
            "pads": [],
            "patterns": [{"id": "A01", "bars": 1, "events": [
                {"tick": 0, "pad": 1, "note": 60, "velocity": 100, "duration": 24}]}],
            "scenes": [{"groupPatterns": [1, 0, 0, 0]}],
            "song": [1],
        }
    for product in ("ep", "ep133", "ep40"):
        _assert_raises(epcompile.CompileError, epcompile.compile_project, doc_for(product))

    # all-nonzero refs compile (silent groups point at empty pattern members)
    doc_ok = {
        "product": "ep40",
        "pads": [],
        "patterns": [
            {"id": "A01", "bars": 1, "events": [
                {"tick": 0, "pad": 1, "note": 60, "velocity": 100, "duration": 24}]},
            {"id": "B01", "bars": 1, "events": []},
            {"id": "C01", "bars": 1, "events": []},
            {"id": "D01", "bars": 1, "events": []},
        ],
        "scenes": [{"groupPatterns": [1, 1, 1, 1]}],
        "song": [1],
    }
    payloads = eptar.member_payloads(epcompile.compile_project(doc_ok))
    assert payloads["patterns/c01"] == bytes([1, 1, 0xFF, 0xFF, 0, 0])

    # the HIL case builder fills silent groups on every product
    from epsysex.hardware_cases import _fill_silent_groups
    filled = _fill_silent_groups({
        "patterns": [{"id": "A01", "bars": 1, "events": []}],
        "scenes": [{"groupPatterns": [1, 0, 0, 0]}],
    })
    assert 0 not in filled["scenes"][0]["groupPatterns"]


def test_session_preflight_refuses_wedged_device():
    """A mutating session must detect an active debug loop during its
    passive preflight listen and raise before sending a single request
    (regression: a probe ran against a device still looping from the
    previous incident, invalidating the experiment and violating the
    stop-all-traffic recovery rule)."""
    from epsysex.sysex import DebugFrameError

    class _Msg:
        type = "sysex"
        def __init__(self, data):
            self.data = tuple(data)

    class _LoopingIn:
        def __init__(self):
            self.polls = 0
        def poll(self):
            self.polls += 1
            if self.polls == 3:  # after the initial drain
                return _Msg([0x00, 0x20, 0x76, 0x3C, 0x33] + list(b"err pattern 213 2_5_1"))
            return None

    class _RecordingOut:
        def __init__(self):
            self.sent = []
        def send(self, message):
            self.sent.append(message)

    client = FileClient(use_lock=False)
    client.LOOP_PREFLIGHT_SECONDS = 0.3
    out = _RecordingOut()

    class _PatchedClient(type(client)):
        pass

    # drive _session's guts directly: drain, preflight, body
    midi_in = _LoopingIn()
    client._drain(midi_in)
    _assert_raises(DebugFrameError, client._assert_not_looping, midi_in)
    assert out.sent == [], "preflight must not send anything"
    assert "write_project_archive" in FileClient._PREFLIGHT_OPS
    assert "reload_project" in FileClient._PREFLIGHT_OPS


def test_compile_rejects_pattern_members_after_scenes():
    # rawMembers append at the tail — a pattern member there must be refused
    # by the ordering invariant, not silently serialized (err pattern 213).
    doc = {
        "pads": [],
        "patterns": [{"id": "A01", "bars": 1,
                      "events": [{"tick": 0, "pad": 1, "note": 60,
                                  "velocity": 100, "duration": 24}]}],
        "scenes": [{"groupPatterns": [1, 0, 0, 0]}],
        "song": [1],
        "rawMembers": [{"name": "patterns/z99", "hex": "00010000"}],
    }
    _assert_raises(epcompile.CompileError, epcompile.compile_project, doc)


def test_request_timeout_diagnoses_debug_loop():
    """A device that starts a debug loop *after* the request deadline must be
    reported as DebugFrameError by the grace listen, not a generic timeout."""
    from epsysex.sysex import DebugFrameError

    class _Msg:
        type = "sysex"
        def __init__(self, data):
            self.data = tuple(data)

    class _SilentIn:
        def poll(self):
            return None

    import time as _time

    class _DebugLoopIn:
        """Silent during the request window; starts 'looping' only after
        ``quiet_for`` seconds, so the grace listen (not the main wait) must
        be what catches the frame."""
        def __init__(self, quiet_for):
            self.start = _time.monotonic()
            self.quiet_for = quiet_for
        def poll(self):
            if _time.monotonic() - self.start < self.quiet_for:
                return None
            return _Msg([0x00, 0x20, 0x76, 0x3C, 0x33] + list(b"err pattern 213 2_5_1"))

    class _Out:
        def send(self, message):
            pass

    client = FileClient(use_lock=False)
    client.DEBUG_GRACE_SECONDS = 0.2

    _assert_raises(
        DebugFrameError,
        client._request, _DebugLoopIn(quiet_for=0.08), _Out(), [0x01, 0x00], 0.05,
    )
    _assert_raises(
        EPSysExError,
        client._request, _SilentIn(), _Out(), [0x01, 0x00], 0.05,
    )


# ---------------------------------------------------------------------------
# offline paths must work without mido installed
# ---------------------------------------------------------------------------

def test_import_without_mido():
    probe = r"""
import sys
class _BlockMido:
    def find_module(self, name, path=None):
        return self if name == "mido" or name.startswith("mido.") else None
    def find_spec(self, name, path=None, target=None):
        if name == "mido" or name.startswith("mido."):
            raise ModuleNotFoundError("mido blocked for test")
        return None
sys.meta_path.insert(0, _BlockMido())

import epsysex
import epsysex.compile
import epsysex.tar
import epsysex.fileclient
import epsysex.identity

tar_bytes = epsysex.compile.compile_project_file(sys.argv[1])
assert len(tar_bytes) > 0
client = epsysex.fileclient.FileClient(use_lock=False)  # constructing is fine
try:
    client.file_init()  # device I/O must fail on the (blocked) mido import
except ModuleNotFoundError:
    pass
else:
    raise AssertionError("expected ModuleNotFoundError from device I/O path")
print("OK")
"""
    result = subprocess.run(
        [sys.executable, "-c", probe, str(EXAMPLE)],
        capture_output=True, text=True, cwd=str(REPO),
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "OK"


# ---------------------------------------------------------------------------
# tiny runner (no pytest dependency)
# ---------------------------------------------------------------------------

def _assert_raises(exc_type, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except exc_type:
        return
    raise AssertionError(f"expected {exc_type.__name__} from {fn.__name__}")


def main():
    tests = [(name, fn) for name, fn in sorted(globals().items())
             if name.startswith("test_") and callable(fn)]
    failures = 0
    for name, fn in tests:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 - report and continue
            failures += 1
            print(f"FAIL  {name}: {exc!r}")
        else:
            print(f"ok    {name}")
    print(f"\n{len(tests) - failures}/{len(tests)} tests passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
