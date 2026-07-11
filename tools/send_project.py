#!/usr/bin/env python3
"""Write an authored project to an EP device slot over SysEx.

    python3 tools/send_project.py --tar project.tar --project 1
    python3 tools/send_project.py --json project.json --project 1
    python3 tools/send_project.py --tar project.tar --project 2 --product ep40

Safety flow (do not shortcut it on a project you care about):

1. checkpoint: read the slot's current TAR to disk first (skip with
   --no-checkpoint at your own risk),
2. write the new archive (FILE_PUT open/data/close),
3. read back and verify byte-identical,
4. reload the project so the running firmware picks up the new bytes
   (skip with --no-reload).

All FILE traffic is serialized by the shared device lock. If the tool stops
with a "firmware/debug SysEx" error, power-cycle the device and restore the
checkpoint before anything else.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from epsysex import compile as epcompile
from epsysex.dependencies import ensure_sound_dependencies, load_document
from epsysex.fileclient import FileClient
from epsysex.products import product_profile
from epsysex.sysex import EP133_PRODUCT, EP40_PRODUCT

PRODUCTS = {"ep133": EP133_PRODUCT, "ep40": EP40_PRODUCT}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    source = ap.add_mutually_exclusive_group(required=True)
    source.add_argument("--tar", type=Path, help="precompiled project TAR to send")
    source.add_argument("--json", type=Path, help="project JSON to compile and send")
    ap.add_argument("--project", type=int, default=1, help="target project slot (1..99)")
    ap.add_argument("--product", choices=sorted(PRODUCTS), default="ep133")
    ap.add_argument("--port", help="MIDI port name substring (default: per-product hint)")
    ap.add_argument("--checkpoint-out", type=Path,
                    help="where to save the pre-write checkpoint TAR "
                         "(default: <tar>.checkpoint.tar next to --tar)")
    ap.add_argument("--no-checkpoint", action="store_true",
                    help="skip the pre-write checkpoint read (NOT recommended)")
    ap.add_argument("--no-reload", action="store_true",
                    help="skip the post-write reload/activation dance")
    ap.add_argument("--defer-verify", action="store_true",
                    help="skip immediate full readback; verify after the latency-critical window")
    ap.add_argument("--fast-live", action="store_true",
                    help="write+reload in one session; skips checkpoint and immediate readback")
    ap.add_argument("--trust-sound-slots", action="store_true",
                    help="skip live sound-library enumeration (use only for previously verified slots)")
    ap.add_argument("--put-page-delay-ms", type=float, default=6.0,
                    help="delay after each acknowledged FILE_PUT page (default: 6ms)")
    ap.add_argument("--unsafe-put-payload-bytes", type=int,
                    help="research only: override device-advertised raw payload size")
    ap.add_argument("--acknowledge-oversized-put",
                    help="must equal 'I ACCEPT OVERSIZED FILE_PUT' for an oversized payload")
    ap.add_argument(
        "--upload-missing-sounds", action="store_true",
        help="upload declared top-level sounds that are missing from the shared library",
    )
    args = ap.parse_args()

    if args.put_page_delay_ms < 0:
        raise SystemExit("--put-page-delay-ms must be nonnegative")
    if args.unsafe_put_payload_bytes is not None:
        if args.acknowledge_oversized_put != "I ACCEPT OVERSIZED FILE_PUT":
            raise SystemExit("oversized FILE_PUT requires --acknowledge-oversized-put "
                             "'I ACCEPT OVERSIZED FILE_PUT'")
    client = FileClient(product_byte=PRODUCTS[args.product], port_hint=args.port,
                        lock_owner="send_project",
                        put_page_delay=args.put_page_delay_ms / 1000.0,
                        put_payload_bytes=args.unsafe_put_payload_bytes)

    before = None
    if args.fast_live:
        args.no_checkpoint = True
        if args.no_reload:
            raise SystemExit("--fast-live conflicts with --no-reload")
        print("fast-live: using authored base; checkpoint and immediate readback are deferred")
    if not args.no_checkpoint:
        before, meta = client.read_project_archive(args.project)
        source_path = args.tar or args.json
        ckpt = args.checkpoint_out or source_path.with_suffix(source_path.suffix + ".checkpoint.tar")
        ckpt.parent.mkdir(parents=True, exist_ok=True)
        ckpt.write_bytes(before)
        print(f"checkpoint: saved project {args.project} ({len(before)} B, "
              f"name={meta.get('name')!r}) -> {ckpt}")

    if args.tar:
        tar_bytes = args.tar.read_bytes()
    else:
        doc = load_document(args.json)
        profile = product_profile(doc.get("product"))
        if profile.key not in ("ep", args.product):
            raise SystemExit(
                f"JSON product profile {profile.key!r} does not match --product {args.product!r}"
            )
        if doc.get("baseArchive") or doc.get("baseArchiveBase64"):
            tar_bytes = epcompile.compile_project_file(args.json)
        else:
            if before is None:
                raise SystemExit(
                    "JSON without baseArchive requires the normal checkpoint read so unknown "
                    "device members can be preserved"
                )
            tar_bytes = epcompile.compile_project(doc, base_archive=before)
        if args.trust_sound_slots:
            if args.upload_missing_sounds:
                raise SystemExit("--trust-sound-slots conflicts with --upload-missing-sounds")
            required = sorted({int(p["slot"]) for p in doc.get("pads", []) if "slot" in p})
            print(f"sound dependencies: trusted cached slots={required} (live enumeration skipped)")
        else:
            dependency_report = ensure_sound_dependencies(
                client,
                doc,
                base_dir=args.json.parent,
                upload_missing=args.upload_missing_sounds,
            )
            print(
                "sound dependencies: "
                f"required={dependency_report['requiredSlots']} "
                f"uploaded={[item['slot'] for item in dependency_report['uploads']]}"
            )

    print(f"writing {len(tar_bytes)} B to project {args.project} ...")
    if args.fast_live:
        result = client.write_project_archive_and_reload(args.project, tar_bytes)
        print(f"fast-live reload: {result}")
        return 0
    client.write_project_archive(args.project, tar_bytes)

    if args.defer_verify:
        verified = True
        print("readback deferred")
    else:
        time.sleep(0.25)
        after, _meta = client.read_project_archive(args.project)
        verified = after == tar_bytes
        print(f"readback {'VERIFIED byte-identical' if verified else 'MISMATCH'} ({len(after)} B)")

    if not args.no_reload:
        result = client.reload_project(args.project)
        print(f"reload: {result}")

    return 0 if verified else 1


if __name__ == "__main__":
    sys.exit(main())
