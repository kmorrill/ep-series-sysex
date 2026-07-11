#!/usr/bin/env python3
"""Compile a JSON project description into an EP project TAR.

    python3 tools/compile_project.py examples/example-project.json --out project.tar
    python3 tools/compile_project.py project.json --out project.tar --ppak project.ppak

The JSON contract is documented in epsysex/compile.py (and docs/). Offline
only: never touches a device. Prints a small JSON report (members, sha256).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from epsysex import compile
from epsysex.dependencies import dependency_plan, load_document
from epsysex.tar import iter_members


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("project_json", type=Path, help="project description JSON")
    ap.add_argument("--out", type=Path, required=True, help="output .tar path")
    ap.add_argument("--ppak", type=Path,
                    help="also wrap the TAR in a .ppak ZIP container at this path")
    ap.add_argument("--project-number", type=int, default=1,
                    help="project slot used for the ppak member name PNN.tar (default 1)")
    ap.add_argument("--device-name", default="EP", help="ppak meta.json device_name")
    ap.add_argument("--device-sku", default="TE032AS001", help="ppak meta.json device_sku")
    ap.add_argument("--device-version", default="2.5.1",
                    help="ppak meta.json device_version (should match target firmware)")
    args = ap.parse_args()

    tar_bytes = compile.compile_project_file(args.project_json)
    document = load_document(args.project_json)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_bytes(tar_bytes)

    report = {
        "archive": str(args.out),
        "archiveBytes": len(tar_bytes),
        "sha256": hashlib.sha256(tar_bytes).hexdigest(),
        "productProfile": document.get("product", "ep"),
        "soundDependencies": dependency_plan(document),
        "members": [
            {"name": name, "size": size, "type": typeflag}
            for name, _start, size, typeflag in iter_members(tar_bytes)
        ],
    }

    if args.ppak:
        ppak_bytes = compile.build_ppak(
            tar_bytes,
            project_number=args.project_number,
            device_name=args.device_name,
            device_sku=args.device_sku,
            device_version=args.device_version,
        )
        args.ppak.parent.mkdir(parents=True, exist_ok=True)
        args.ppak.write_bytes(ppak_bytes)
        report["ppak"] = {"path": str(args.ppak), "bytes": len(ppak_bytes)}

    json.dump(report, sys.stdout, indent=2)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
