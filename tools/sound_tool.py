#!/usr/bin/env python3
"""Manage the EP shared sample library over SysEx FILE.

The sample library (slots 1..999) is global — shared across all projects and
never part of a project TAR — so it needs its own tool separate from the
project read/write flow. Every subcommand opens one serialized FILE session
behind the shared device lock.

Read-only:

    python3 tools/sound_tool.py list
    python3 tools/sound_tool.py stat 1
    python3 tools/sound_tool.py get 1 --out slot001.pcm
    python3 tools/sound_tool.py meta-get 1
    python3 tools/sound_tool.py play 1                 # audition (raw sample)

Writes (destructive ones require an explicit acknowledgement):

    python3 tools/sound_tool.py meta-set 1 --json '{"name":"kick"}'
    python3 tools/sound_tool.py put 56 --in kick.pcm --name kick --channels 1
    python3 tools/sound_tool.py put 1  --in kick.pcm --acknowledge-overwrite "OVERWRITE SOUND 1"
    python3 tools/sound_tool.py delete 56 --acknowledge-delete "DELETE SOUND 56"

Select the connected model's verified transport with ``--product``. ``put``
expects raw interleaved PCM (no WAV header); ``--format`` describes it
(default s16). Uploading past 999 slots or the device capacity is not enforced
here.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from epsysex.fileclient import FileClient, sound_fid
from epsysex.sysex import EP133_PRODUCT, EP40_PRODUCT

PRODUCTS = {"ep133": EP133_PRODUCT, "ep40": EP40_PRODUCT}


def make_client(args, op):
    return FileClient(product_byte=PRODUCTS[args.product], port_hint=args.port,
                      lock_owner=f"sound_tool:{op}")


def cmd_list(args):
    client = make_client(args, "list")
    sounds = client.list_sounds()
    if args.json:
        json.dump(sounds, sys.stdout, indent=2)
        print()
    else:
        total = sum(int(node["size"]) for node in sounds)
        for node in sorted(sounds, key=lambda n: int(n["id"])):
            print(f"{int(node['id']):>3}  {int(node['size']):>9} B  "
                  f"flags=0x{int(node['flags']):02x}  {node['name']}")
        print(f"\n{len(sounds)} slots, {total/1e6:.2f} MB")
    return 0


def cmd_stat(args):
    client = make_client(args, "stat")
    info = client.stat_sound(args.slot)
    json.dump(info, sys.stdout, indent=2)
    print()
    return 0


def cmd_get(args):
    client = make_client(args, "get")
    data, meta = client.read_sound(args.slot)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_bytes(data)
    print(f"read slot {args.slot} (name={meta.get('name')!r}): {len(data)} B -> {args.out}")
    return 0


def cmd_play(args):
    client = make_client(args, "play")
    client.audition_sound(args.slot)
    print(f"auditioned slot {args.slot}")
    return 0


def cmd_meta_get(args):
    client = make_client(args, "meta_get")
    meta = client.get_sample_metadata(args.slot, key=args.key)
    json.dump(meta, sys.stdout, indent=2)
    print()
    return 0


def cmd_meta_set(args):
    client = make_client(args, "meta_set")
    try:
        metadata = json.loads(args.json)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"--json is not valid JSON: {exc}")
    if not isinstance(metadata, dict):
        raise SystemExit("--json must be a JSON object")
    client.set_sample_metadata(args.slot, metadata)
    readback = client.get_sample_metadata(args.slot)
    print(f"set metadata on slot {args.slot}; readback:")
    json.dump(readback, sys.stdout, indent=2)
    print()
    return 0


def cmd_put(args):
    client = make_client(args, "put")
    pcm = args.infile.read_bytes()
    fid = sound_fid(args.slot)

    occupied = {int(node["id"]) for node in client.list_sounds()}
    if args.slot in occupied:
        required = f"OVERWRITE SOUND {args.slot}"
        if args.acknowledge_overwrite != required:
            raise SystemExit(
                f"slot {args.slot} is occupied (fid {fid}); to replace it pass "
                f"--acknowledge-overwrite {required!r}"
            )

    extra = None
    if args.metadata_json:
        extra = json.loads(args.metadata_json)
        if not isinstance(extra, dict):
            raise SystemExit("--metadata-json must be a JSON object")

    summary = client.upload_sound(
        args.slot, pcm,
        name=args.name,
        node_name=args.node_name,
        samplerate=args.samplerate,
        channels=args.channels,
        sample_format=args.format,
        extra_metadata=extra,
        write_metadata=not args.no_metadata,
    )
    print(f"uploaded slot {args.slot}: {summary['bytes']} B as {summary['nodeName']!r}")

    if args.verify:
        readback, _meta = client.read_sound(args.slot)
        if readback != pcm:
            raise SystemExit(
                f"readback mismatch: wrote {len(pcm)} B, read {len(readback)} B"
            )
        print(f"verified byte-exact readback ({len(readback)} B)")
    return 0


def cmd_delete(args):
    required = f"DELETE SOUND {args.slot}"
    if args.acknowledge_delete != required:
        raise SystemExit(f"delete is destructive; pass --acknowledge-delete {required!r}")
    client = make_client(args, "delete")
    client.delete_sound(args.slot)
    remaining = {int(node["id"]) for node in client.list_sounds()}
    if args.slot in remaining:
        raise SystemExit(f"slot {args.slot} still present after delete")
    print(f"deleted slot {args.slot} (confirmed absent)")
    return 0


def slot_arg(value):
    slot = int(value)
    if not 1 <= slot <= 999:
        raise argparse.ArgumentTypeError("slot must be 1..999")
    return slot


def build_parser():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--product", choices=sorted(PRODUCTS), default="ep133")
    ap.add_argument("--port", help="MIDI port name substring (default: per-product hint)")
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("list", help="list occupied sample slots")
    p.add_argument("--json", action="store_true", help="emit raw JSON")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("stat", help="STAT one sample slot")
    p.add_argument("slot", type=slot_arg)
    p.set_defaults(func=cmd_stat)

    p = sub.add_parser("get", help="download a slot's raw PCM to a file")
    p.add_argument("slot", type=slot_arg)
    p.add_argument("--out", type=Path, required=True)
    p.set_defaults(func=cmd_get)

    p = sub.add_parser("play", help="audition a slot's raw sample")
    p.add_argument("slot", type=slot_arg)
    p.set_defaults(func=cmd_play)

    p = sub.add_parser("meta-get", help="read a slot's metadata JSON")
    p.add_argument("slot", type=slot_arg)
    p.add_argument("--key", help="fetch a single metadata key")
    p.set_defaults(func=cmd_meta_get)

    p = sub.add_parser("meta-set", help="merge metadata onto a slot")
    p.add_argument("slot", type=slot_arg)
    p.add_argument("--json", required=True, dest="json", help="metadata object, e.g. '{\"name\":\"kick\"}'")
    p.set_defaults(func=cmd_meta_set)

    p = sub.add_parser("put", help="upload raw PCM to a slot (create/replace)")
    p.add_argument("slot", type=slot_arg)
    p.add_argument("--in", dest="infile", type=Path, required=True, help="raw PCM file (no WAV header)")
    p.add_argument("--name", help="display name (<=20 ASCII chars)")
    p.add_argument("--node-name", help="file node name (default NNN.pcm)")
    p.add_argument("--samplerate", type=int, default=46875)
    p.add_argument("--channels", type=int, default=1)
    p.add_argument("--format", default="s16", help="raw PCM format tag (default s16)")
    p.add_argument("--metadata-json", help="extra metadata object merged after the standard fields")
    p.add_argument("--no-metadata", action="store_true", help="skip the metadata write entirely")
    p.add_argument("--verify", action="store_true", help="read back and compare byte-for-byte")
    p.add_argument("--acknowledge-overwrite", help='required to replace an occupied slot: "OVERWRITE SOUND N"')
    p.set_defaults(func=cmd_put)

    p = sub.add_parser("delete", help="delete a slot (destructive)")
    p.add_argument("slot", type=slot_arg)
    p.add_argument("--acknowledge-delete", help='required: "DELETE SOUND N"')
    p.set_defaults(func=cmd_delete)

    return ap


def main():
    args = build_parser().parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
