#!/usr/bin/env python3
"""Read a project TAR from an EP device to a local file.

    python3 tools/read_project.py --project 1 --out project01.tar
    python3 tools/read_project.py --project 1 --out p1.tar --product ep40

Read-only against the project data, but it still opens a FILE session, so it
takes the shared device lock and must never run concurrently with other FILE
tools (overlapping sessions wedge the device).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from epsysex.fileclient import FileClient
from epsysex.sysex import EP133_PRODUCT, EP40_PRODUCT

PRODUCTS = {"ep133": EP133_PRODUCT, "ep40": EP40_PRODUCT}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--project", type=int, default=1, help="project slot (1..99)")
    ap.add_argument("--out", type=Path, required=True, help="output .tar path")
    ap.add_argument("--product", choices=sorted(PRODUCTS), default="ep133")
    ap.add_argument("--port", help="MIDI port name substring (default: per-product hint)")
    args = ap.parse_args()

    client = FileClient(product_byte=PRODUCTS[args.product], port_hint=args.port,
                        lock_owner="read_project")
    data, meta = client.read_project_archive(args.project)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_bytes(data)
    print(f"read project {args.project} (fid {meta['fid']}, name={meta.get('name')!r}): "
          f"{len(data)} B -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
