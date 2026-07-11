#!/usr/bin/env python3
"""Diff two EP project archives member-by-member.

Accepts raw project ``.tar`` files or ``.pak`` / ``.ppak`` ZIP containers. For each
member present in both archives it prints the contiguous byte ranges that changed,
with old/new hex and a few interpretations (u16/u32 LE, f32 LE) to speed up field
localisation. Members that were added, removed, or resized are flagged.

Usage:
    diff_tars.py OLD NEW
    diff_tars.py OLD.ppak NEW.ppak --project P01
    diff_tars.py OLD NEW --only patterns/a01 --context 2

Pure stdlib.
"""
from __future__ import annotations

import argparse
import io
import struct
import sys
import tarfile
import zipfile
from pathlib import Path


def _is_zip(path: Path) -> bool:
    return path.suffix.lower() in {".pak", ".ppak", ".zip"} or zipfile.is_zipfile(path)


def _pick_tar_from_zip(zf: zipfile.ZipFile, project: "str | None") -> bytes:
    tars = [n for n in zf.namelist() if n.lower().endswith(".tar")]
    if not tars:
        raise SystemExit(f"no .tar members inside {zf.filename}")
    if project:
        want = project.lower()
        match = [n for n in tars if want in n.lower()]
        if not match:
            raise SystemExit(
                f"project {project!r} not found; available: {', '.join(tars)}"
            )
        chosen = match[0]
    elif len(tars) == 1:
        chosen = tars[0]
    else:
        raise SystemExit(
            "multiple projects in archive; pass --project. available: "
            + ", ".join(sorted(tars))
        )
    return zf.read(chosen)


def load_members(path: Path, project: "str | None") -> "dict[str, bytes]":
    """Return {member_name: raw_bytes} for every file entry in the archive."""
    if _is_zip(path):
        with zipfile.ZipFile(path) as zf:
            tar_bytes = _pick_tar_from_zip(zf, project)
        fileobj = io.BytesIO(tar_bytes)
    else:
        fileobj = open(path, "rb")
    members: "dict[str, bytes]" = {}
    with tarfile.open(fileobj=fileobj, mode="r:") as tf:
        for m in tf.getmembers():
            if m.isfile():
                f = tf.extractfile(m)
                members[m.name.lstrip("./")] = f.read() if f else b""
    return members


def changed_ranges(a: bytes, b: bytes, context: int) -> "list[tuple[int, int]]":
    """Contiguous [start, end) ranges where a and b differ, merged within context."""
    n = min(len(a), len(b))
    diffs = [i for i in range(n) if a[i] != b[i]]
    diffs += list(range(n, max(len(a), len(b))))  # length change tail
    if not diffs:
        return []
    ranges: "list[list[int]]" = []
    for i in diffs:
        if ranges and i - ranges[-1][1] <= context + 1:
            ranges[-1][1] = i + 1
        else:
            ranges.append([i, i + 1])
    return [(s, e) for s, e in ranges]


def interp(chunk: bytes) -> str:
    """Best-effort scalar interpretations of a short changed run."""
    out = []
    if len(chunk) >= 2:
        out.append(f"u16LE={struct.unpack_from('<H', chunk)[0]}")
    if len(chunk) >= 4:
        out.append(f"u32LE={struct.unpack_from('<I', chunk)[0]}")
        f = struct.unpack_from("<f", chunk)[0]
        if abs(f) < 1e12:
            out.append(f"f32LE={f:.4g}")
    return "  ".join(out)


def hexs(b: bytes) -> str:
    return " ".join(f"{x:02x}" for x in b)


def diff_member(name: str, a: bytes, b: bytes, context: int) -> "list[str]":
    lines: "list[str]" = []
    if len(a) != len(b):
        lines.append(f"  size {len(a)} -> {len(b)} bytes ({len(b) - len(a):+d})")
    for s, e in changed_ranges(a, b, context):
        lo = max(0, s - context)
        hi = min(max(len(a), len(b)), e + context)
        oa, ob = a[lo:hi], b[lo:hi]
        lines.append(f"  @{s}-{e - 1} (len {e - s})")
        lines.append(f"      old: {hexs(oa)}")
        lines.append(f"      new: {hexs(ob)}")
        ca, cb = a[s:e], b[s:e]
        ia, ib = interp(ca), interp(cb)
        if ia or ib:
            lines.append(f"      old~ {ia}")
            lines.append(f"      new~ {ib}")
    return lines


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("old", type=Path)
    ap.add_argument("new", type=Path)
    ap.add_argument("--project", help="project name inside a .pak/.ppak, e.g. P01")
    ap.add_argument("--only", action="append", help="restrict to member(s); repeatable")
    ap.add_argument("--context", type=int, default=1, help="bytes of context (default 1)")
    args = ap.parse_args()

    old = load_members(args.old, args.project)
    new = load_members(args.new, args.project)

    names = sorted(set(old) | set(new))
    if args.only:
        keep = set(args.only)
        names = [n for n in names if n in keep]

    any_change = False
    for name in names:
        if name not in old:
            print(f"+ {name}  (added, {len(new[name])} bytes)")
            any_change = True
            continue
        if name not in new:
            print(f"- {name}  (removed, {len(old[name])} bytes)")
            any_change = True
            continue
        a, b = old[name], new[name]
        if a == b:
            continue
        any_change = True
        print(f"~ {name}")
        for line in diff_member(name, a, b, args.context):
            print(line)

    if not any_change:
        print("no differences")
    return 0


if __name__ == "__main__":
    sys.exit(main())
