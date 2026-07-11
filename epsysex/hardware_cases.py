"""Authored JSON cases for the EP hardware-in-the-loop suite.

Each case isolates a related part of the public ``ep.project.v1`` surface.
All cases use an existing sound dependency supplied by the runner.  The
compiler output is byte-verified after every device write; playback cases add
MIDI and/or USB-audio evidence on top.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

from . import tar as eptar


@dataclass
class HardwareCase:
    name: str
    description: str
    document: dict
    bars: int = 1
    expect_audio: bool = True
    risky: bool = False
    use_checkpoint_base: bool = True


def sound_reference_from_archive(tar_bytes, available_slots, requested_slot=None, trim_end=None):
    """Choose a known-playable sound and recover safe pad defaults.

    Auto mode selects an assigned pad in the checkpoint whose slot still
    exists under ``/sounds``.  An explicitly requested slot must either occur
    in that checkpoint or be accompanied by ``trim_end``.
    """
    payloads = eptar.member_payloads(tar_bytes)
    candidates = []
    for name, payload in payloads.items():
        if not name.startswith("pads/") or len(payload) < 27:
            continue
        slot = int.from_bytes(payload[1:3], "little")
        end = int.from_bytes(payload[8:12], "little")
        if slot and slot in available_slots and end:
            candidates.append((name, payload, slot, end))

    chosen = None
    if requested_slot is not None:
        for candidate in candidates:
            if candidate[2] == int(requested_slot):
                chosen = candidate
                break
        if chosen is None:
            if trim_end is None:
                raise RuntimeError(
                    f"sound slot {requested_slot} is not assigned in the checkpoint; "
                    "provide --trim-end so the suite does not guess its frame length"
                )
            if int(requested_slot) not in available_slots:
                raise RuntimeError(f"sound slot {requested_slot} is absent from /sounds")
            payload = bytearray(27)
            struct.pack_into("<f", payload, 12, 120.0)
            payload[16], payload[20], payload[24] = 100, 255, 60
            chosen = ("explicit", bytes(payload), int(requested_slot), int(trim_end))
    elif candidates:
        chosen = candidates[0]

    if chosen is None:
        raise RuntimeError(
            "the checkpoint has no assigned, present, non-empty sound; pass "
            "--sound-slot SLOT --trim-end FRAMES"
        )

    name, payload, slot, end = chosen
    signed = lambda value: value - 256 if value > 127 else value
    return {
        "sourcePad": name,
        "slot": slot,
        "trimStart": int.from_bytes(payload[4:8], "little"),
        "trimEnd": int(trim_end) if trim_end is not None else end,
        "sampleBpm": struct.unpack_from("<f", payload, 12)[0],
        "amplitude": payload[16],
        "pitch": signed(payload[17]),
        "pan": signed(payload[18]),
        "attack": payload[19],
        "release": payload[20],
        "timeMode": payload[21],
        "chokeGroup": payload[22],
        "playMode": payload[23],
        "rootNote": payload[24],
        "midiChannel": payload[3],
        "pitchFraction": signed(payload[26]),
    }


def _pad(reference, group, pad, **overrides):
    result = {
        "group": group,
        "pad": pad,
        "slot": reference["slot"],
        "trimStart": reference["trimStart"],
        "trimEnd": reference["trimEnd"],
        "sampleBpm": reference["sampleBpm"],
        "amplitude": 100,
        "pitch": 0,
        "pitchFraction": 0,
        "pan": 0,
        "attack": 0,
        "release": 255,
        "timeMode": 0,
        "chokeGroup": 0,
        "playMode": 0,
        "rootNote": 60,
        "midiChannel": 0,
    }
    result.update(overrides)
    return result


def _fill_silent_groups(doc):
    """Rewrite zero scene refs to point at empty pattern members.

    The Riddim loader rejects a defined scene with a zero group-pattern
    reference (err pattern 213, isolated live); the device's own idiom for a
    silent group is an empty pattern member (c02/d02 in native captures).
    Adds one empty pattern per silent group and rewrites the refs in place.
    """
    scenes = doc.get("scenes") or []
    if not any(0 in (scene.get("groupPatterns") or []) for scene in scenes):
        return doc
    patterns = doc.setdefault("patterns", [])
    used = {str(p["id"]).upper() for p in patterns}
    silent_ref = {}
    for group_index, group in enumerate("ABCD"):
        if not any((scene.get("groupPatterns") or [0] * 4)[group_index] == 0
                   for scene in scenes):
            continue
        number = 1
        while f"{group}{number:02d}" in used:
            number += 1
        used.add(f"{group}{number:02d}")
        patterns.append({"id": f"{group}{number:02d}", "bars": 1, "events": []})
        silent_ref[group_index] = number
    for scene in scenes:
        refs = scene.get("groupPatterns") or []
        scene["groupPatterns"] = [
            silent_ref[i] if ref == 0 else ref for i, ref in enumerate(refs)
        ]
    return doc


def build_cases(reference, include_risky_settings=False, product="ep40"):
    """Return the complete authored-JSON device matrix."""
    cases = []

    # Canonical archive plus every event spelling, every group, flags, bars,
    # multiple scenes/song entries, and the fx_settings switch.  Scene 2 is
    # deliberately identical because current firmware reloads the last-counted
    # scene rather than reliably following the authored song trailer.
    patterns = []
    pads = []
    for index, group in enumerate("ABCD"):
        pads.append(_pad(reference, group, 10, midiChannel=index + 1))
        patterns.append({
            "id": f"{group}01",
            "bars": 1,
            "events": [
                {
                    "tick": index * 24,
                    "pad": 10,
                    "note": 60 + index,
                    "velocity": 100 + index,
                    "duration": 18 + index,
                    "flag": 0,
                },
                {
                    "tick": 192 + index * 24,
                    "padByte": 72,
                    "note": 64 + index,
                    "velocity": 90 + index,
                    "duration": 24,
                    "flag": (0, 2, 6, 8)[index],
                },
            ],
        })
    refs = [1, 1, 1, 1]
    cases.append(HardwareCase(
        "authoring_surface",
        "canonical directories/pads, four groups, pad and padByte events, scenes/song, fx",
        {
            "schema": "ep.project.v1",
            "metadata": {"title": "HIL authoring surface", "ignoredByCompiler": True},
            "includeDirectories": True,
            "padMembers": "all",
            "pads": pads,
            "patterns": patterns,
            "scenes": [
                {"groupPatterns": refs, "timeSignature": [4, 4]},
                {"groupPatterns": refs, "timeSignature": [3, 4]},
            ],
            "song": [1, 2, 1],
            "fxSettings": True,
        },
        bars=1,
    ))

    # Sparse TAR controls are authoring features too.  This case proves the
    # firmware accepts the used-only/no-directory dialect and can play it.
    cases.append(HardwareCase(
        "sparse_archive",
        "padMembers=used and includeDirectories=false",
        {
            "schema": "ep.project.v1",
            "includeDirectories": False,
            "padMembers": "used",
            "pads": [_pad(reference, "A", 10)],
            "patterns": [{
                "id": "A01", "bars": 1,
                "events": [
                    {"tick": 0, "pad": 10, "note": 60, "velocity": 127, "duration": 24},
                    {"tick": 192, "pad": 10, "note": 60, "velocity": 96, "duration": 24},
                ],
            }],
            "scenes": [{"groupPatterns": [1, 0, 0, 0]}],
            "song": [1],
        },
        use_checkpoint_base=False,
    ))

    # One isolated hit per pad exposes every pad byte to readback, with broad
    # audio signatures for level, pan and pitch. Loop mode is Riddim-only.
    behavior_pads = [
        _pad(reference, "A", 10, midiChannel=2),
        _pad(reference, "A", 11, amplitude=60),
        _pad(reference, "A", 12, pan=-16),
        _pad(reference, "A", 9, pan=16),
        _pad(reference, "A", 8, pitch=12, pitchFraction=50, rootNote=60),
        _pad(reference, "A", 7, attack=160),
        _pad(reference, "A", 6, release=64, playMode=1),
        _pad(
            reference, "A", 5, trimStart=reference["trimStart"],
            trimEnd=max(
                reference["trimStart"] + 1,
                reference["trimStart"]
                + (reference["trimEnd"] - reference["trimStart"]) // 2,
            ),
            sampleBpm=max(40.0, min(399.0, float(reference["sampleBpm"]))),
            timeMode=1, chokeGroup=1, playMode=3 if product == "ep40" else 0,
        ),
    ]
    cases.append(HardwareCase(
        "pad_sound_design",
        "trim/BPM/amp/pitch/fraction/pan/attack/release/time/choke/play/root/channel fields",
        {
            "schema": "ep.project.v1",
            "pads": behavior_pads,
            "patterns": [{
                "id": "A01", "bars": 2,
                "events": [
                    {"tick": 0, "pad": 10, "note": 60, "velocity": 127, "duration": 48},
                    {"tick": 96, "pad": 11, "note": 60, "velocity": 127, "duration": 48},
                    {"tick": 192, "pad": 12, "note": 60, "velocity": 127, "duration": 48},
                    {"tick": 288, "pad": 9, "note": 60, "velocity": 127, "duration": 48},
                    {"tick": 384, "pad": 8, "note": 60, "velocity": 127, "duration": 48},
                    {"tick": 480, "pad": 7, "note": 60, "velocity": 127, "duration": 48},
                    {"tick": 576, "pad": 6, "note": 60, "velocity": 127, "duration": 48},
                    {"tick": 672, "pad": 5, "note": 60, "velocity": 127, "duration": 48},
                ],
            }],
            "scenes": [{"groupPatterns": [1, 0, 0, 0]}],
            "song": [1],
        },
        bars=2,
    ))

    # All fader parameter IDs, boundary values, same-tick interleaving, and
    # velocity range.  High neutral-ish values keep the sample audible across
    # disparate parameter laws while MIDI output remains the primary oracle.
    # Params 1 (pitch), 9 (tune) and 10 (velocity) modulate the EMITTED MIDI
    # (live-verified EP-133 2.5.1: pitch 32767 transposes note-out +12,
    # velocity 16384 halves velocity-out) — author them at neutral values so
    # the MIDI oracle stays exact while still proving the lanes are written.
    NEUTRAL = {1: 16384, 9: 16384, 10: 32767}
    automation = []
    for parameter in range(12):
        automation.append({
            "tick": (parameter // 4) * 96,
            "parameterID": parameter,
            "value": NEUTRAL.get(parameter, (32767, 16384, 0)[parameter % 3]),
        })
    cases.append(HardwareCase(
        "automation_matrix",
        "all 12 automation parameter IDs and 0/mid/max values",
        {
            "schema": "ep.project.v1",
            "pads": [_pad(reference, "A", 10)],
            "patterns": [{
                "id": "A01", "bars": 1,
                "events": [
                    {"tick": 0, "pad": 10, "note": 60, "velocity": 8, "duration": 12},
                    {"tick": 96, "pad": 10, "note": 61, "velocity": 64, "duration": 24},
                    {"tick": 192, "pad": 10, "note": 62, "velocity": 127, "duration": 36},
                ],
                "automation": automation,
            }],
            "scenes": [{"groupPatterns": [1, 0, 0, 0]}],
            "song": [1],
        },
    ))

    cases.append(HardwareCase(
        "fx_output_sidechain",
        "master FX parameters, output compressor, and per-pad sidechain routing",
        {
            "schema": "ep.project.v1",
            "fxSettings": {
                "type": "delay",
                "parameters": {"x": 0.25, "y": 0.75},
                "outputCompressor": {"drive": 0.2, "speed": 0.8},
                "sidechain": {
                    "length": 0.3,
                    "shape": 0.6,
                    "routing": {
                        "A": {"sourcePads": [10]},
                        "B": {"destination": True},
                    },
                },
            },
            # distinct MIDI channel on B so same-note voices pair with the
            # right note-offs in the oracle (both pads play note 60 at t0)
            "pads": [_pad(reference, "A", 10), _pad(reference, "B", 10, midiChannel=2)],
            "patterns": [
                {"id": "A01", "bars": 1, "events": [
                    {"tick": 0, "pad": 10, "note": 60, "velocity": 127, "duration": 24},
                    {"tick": 192, "pad": 10, "note": 60, "velocity": 127, "duration": 24},
                ]},
                {"id": "B01", "bars": 1, "events": [
                    {"tick": 0, "pad": 10, "note": 60, "velocity": 96, "duration": 288},
                ]},
            ],
            "scenes": [{"groupPatterns": [1, 1, 0, 0]}],
            "song": [1],
        },
    ))

    if product == "ep40":
        cases.append(HardwareCase(
            "supertone_pads",
            "Riddim supertone engines and both saved knob parameters",
            {
                "schema": "ep.project.v1",
                "pads": [
                    {"group": "A", "pad": 10, "supertone": {
                        "engine": 0, "knobX": 0, "knobY": 254,
                    }, "amplitude": 100, "rootNote": 60},
                    {"group": "A", "pad": 11, "supertone": {
                        "engine": 1, "knobX": 128, "knobY": 128,
                    }, "amplitude": 100, "rootNote": 60},
                    {"group": "A", "pad": 12, "supertone": {
                        "engine": 8, "knobX": 254, "knobY": 0,
                    }, "amplitude": 100, "rootNote": 60},
                ],
                "patterns": [{"id": "A01", "bars": 1, "events": [
                    {"tick": 0, "pad": 10, "note": 60, "velocity": 100, "duration": 48},
                    {"tick": 128, "pad": 11, "note": 60, "velocity": 100, "duration": 48},
                    {"tick": 256, "pad": 12, "note": 60, "velocity": 100, "duration": 48},
                ]}],
                "scenes": [{"groupPatterns": [1, 0, 0, 0]}],
                "song": [1],
            },
        ))
        cases.append(HardwareCase(
            "loop_startup",
            "Riddim loop mode and 48-byte Loop Startup Sequence member",
            {
                "schema": "ep.project.v1",
                "pads": [_pad(
                    reference, "A", 10, playMode=3, timeMode=1,
                    sampleBpm=max(40.0, min(399.0, float(reference["sampleBpm"]))),
                    chokeGroup=1,
                )],
                "live": {"armedPads": [{"group": "A", "pad": 10}]},
            },
            use_checkpoint_base=False,
        ))

    if include_risky_settings:
        group_faders = []
        for parameter in range(12):
            group_faders.append({
                "group": "A",
                "parameter": parameter,
                "baseValue": 1.0 if parameter == 0 else 0.75,
            })
        # Additional groups cover assignment addressing.  A's final assignment
        # is parameter 11, while all twelve A base floats remain encoded.
        group_faders.extend([
            {"group": "B", "parameter": 3, "baseValue": -1},
            {"group": "C", "parameter": 5, "baseValue": 0.5},
            {"group": "D", "parameter": 8, "baseValue": 2},
        ])
        cases.append(HardwareCase(
            "settings_member",
            "BPM plus all group-fader bases/assignments, including clamp boundaries",
            {
                "schema": "ep.project.v1",
                "settings": {"bpm": 123.5, "groupFaders": group_faders},
                "pads": [_pad(reference, "A", 10)],
                "patterns": [{
                    "id": "A01", "bars": 1,
                    "events": [{
                        "tick": 0, "pad": 10, "note": 60, "velocity": 127, "duration": 48,
                    }],
                }],
                "scenes": [{"groupPatterns": [1, 0, 0, 0]}],
                "song": [1],
            },
            risky=True,
            use_checkpoint_base=False,
        ))

    for case in cases:
        case.document.setdefault("product", product)
        if case.risky and "settings" in case.document:
            case.document["settings"]["unsafeFabricate"] = True
        # every EP device rejects partial-zero scenes (187/213)
        _fill_silent_groups(case.document)
    return cases
