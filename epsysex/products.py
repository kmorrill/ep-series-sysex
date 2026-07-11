"""Authoring profiles for the EP family.

Most project members share one format and should be described simply as EP.
Profiles exist only for proven model-specific differences, currently Riddim's
29-byte pad records, supertone engines, loop mode, and ``live`` member.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProductProfile:
    key: str
    pad_record_size: int
    supports_loop: bool = False
    supports_supertone: bool = False
    supports_live: bool = False
    # Pattern-member header dialect. "ep": 4-byte ``[0, bars, count u8, 0]``
    # (EP-133-verified; byte 0 MUST stay 0 there — err pattern 71). "ep40":
    # native Riddim 6-byte ``[1, bars, 0xFF, 0xFF, count u16 LE]`` — every
    # pattern member in every EP-40 device capture uses this form, and all
    # historically successful EP-40 writes contained no pattern members at
    # all, so the 4-byte form was never proven there.
    pattern_dialect: str = "ep"
    # A DEFINED scene chunk with a zero group-pattern ref is rejected by
    # EVERY EP device tested: EP-40 OS 2.5.1 answers ``err pattern 213``,
    # EP-133 OS 2.5.1 answers ``err pattern 187`` (both isolated live with
    # single-variable probes; no device-created capture on either product
    # contains a partial-zero chunk). Express silence by referencing an
    # empty pattern member (the devices' own idiom) — see errors.md.
    requires_full_scene_refs: bool = True


PROFILES = {
    "ep": ProductProfile("ep", 27),
    "ep133": ProductProfile("ep133", 27),
    # EXPERIMENTAL: pure identity mapping — no EP-1320 has ever been probed
    # by this project; the shared-format assumption is unverified there.
    "ep1320": ProductProfile("ep1320", 27),
    "ep40": ProductProfile(
        "ep40", 29, supports_loop=True, supports_supertone=True,
        supports_live=True, pattern_dialect="ep40",
    ),
}

ALIASES = {
    "ep": "ep",
    "generic": "ep",
    "ep133": "ep133",
    "ep-133": "ep133",
    "koii": "ep133",
    "k.o. ii": "ep133",
    "ep1320": "ep1320",
    "ep-1320": "ep1320",
    "medieval": "ep1320",
    "ep40": "ep40",
    "ep-40": "ep40",
    "riddim": "ep40",
}


def product_profile(value=None):
    """Resolve a JSON/CLI authoring profile name.

    The generic ``ep`` profile is the default. Model aliases are accepted at
    the boundary but canonical keys are returned in reports and artifacts.
    """
    key = str(value or "ep").strip().lower()
    canonical = ALIASES.get(key)
    if canonical is None:
        raise ValueError(
            f"unknown EP product profile {value!r}; expected ep, ep133, ep1320, or ep40"
        )
    return PROFILES[canonical]
