#!/usr/bin/env python3
"""Print EP device/firmware identity as JSON.

    python3 tools/identity.py                   # live: MIDI Universal Identity Request
    python3 tools/identity.py --port "EP-40"    # live: pick port by substring
    python3 tools/identity.py --ppak backup.ppak  # offline: from meta.json

The Identity Request (F0 7E 7F 06 01 F7) is a standard MIDI message and does
NOT touch the FILE subsystem, so it is safe to run anytime. Note that EP-133
OS 2.5.1 returns zeroed version bytes; the .ppak path may be the only way to
resolve an exact version on newer firmware.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from epsysex.identity import identity_from_device, identity_from_ppak


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--ppak", type=Path, help="read device_version from a .ppak/.pak meta.json")
    ap.add_argument("--port", help="MIDI port name substring (default: auto-detect)")
    ap.add_argument("--timeout", type=float, default=3.0)
    args = ap.parse_args()

    result = identity_from_ppak(args.ppak) if args.ppak else identity_from_device(
        args.port, args.timeout
    )
    json.dump(result, sys.stdout, indent=2)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
