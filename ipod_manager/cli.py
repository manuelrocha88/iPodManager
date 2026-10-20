"""Command-line interface for iPodManager.

Everything the GUI does is also available here, which makes it easy to
script bulk imports or automate backups.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Optional

from . import __version__
from .device import IPod, VolumeRenameError, eject, find_ipod, list_ipods, rename_volume
from .library import Library


def _fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:,.1f} {unit}"
        n /= 1024
    return f"{n:,.1f} PB"


def _fmt_ms(ms: int) -> str:
    s = ms // 1000
    return f"{s // 60}:{s % 60:02d}"


def _resolve_pod(mount: Optional[str]) -> IPod:
    if mount:
        from .device import IPod as _IPod  # for the direct-mount case
        import psutil
        usage = psutil.disk_usage(mount)
        return _IPod(mountpoint=Path(mount), name=Path(mount).name,
                     total_bytes=usage.total, free_bytes=usage.free)
    pod = find_ipod()
    if not pod:
        print("No iPod detected. Plug one in and try again, or pass --mount PATH.",
              file=sys.stderr)
        sys.exit(2)
    return pod


# ------------------------------------------------------------------ #
# Commands                                                            #
# ------------------------------------------------------------------ #


def cmd_devices(args: argparse.Namespace) -> int:
    pods = list_ipods()
    if not pods:
        print("No iPods mounted.")
        return 0
    for p in pods:
        print(f"- {p.name}  @ {p.mountpoint}")
        print(f"    size: {_fmt_bytes(p.total_bytes)}, "
              f"used: {_fmt_bytes(p.used_bytes)}, "
              f"free: {_fmt_bytes(p.free_bytes)}")
    return 0


def cmd_info(args: argparse.Namespace) -> int:
    pod = _resolve_pod(args.mount)
    lib = Library.load(pod)
    print(f"iPod:         {lib.ipod_name}  ({pod.name} @ {pod.mountpoint})")
    print(f"Capacity:     {_fmt_bytes(pod.total_bytes)}")
    print(f"Used:         {_fmt_bytes(pod.used_bytes)}")
    print(f"Free:         {_fmt_bytes(pod.free_bytes)}")
    print(f"Tracks:       {len(lib.entries)}")
    total_ms = sum(e.length_ms for e in lib.entries)
    if total_ms:
        print(f"Total time:   {total_ms // 3_600_000}h "
              f"{(total_ms // 60_000) % 60}m")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    pod = _resolve_pod(args.mount)
    lib = Library.load(pod)
    if not lib.entries:
        print("Library is empty.")
        return 0
    print(f"{'ID':>4}  {'Length':>7}  {'Title':<40}  {'Artist':<30}  Album")
    print("-" * 110)
    for e in lib.entries:
        print(f"{e.track_id:>4}  {_fmt_ms(e.length_ms):>7}  "
              f"{(e.title or '')[:40]:<40}  "
              f"{(e.artist or '')[:30]:<30}  "
              f"{e.album or ''}")
    return 0


def cmd_add(args: argparse.Namespace) -> int:
    pod = _resolve_pod(args.mount)
    lib = Library.load(pod)
    added = 0
    for raw in args.files:
        p = Path(raw).expanduser()
        if p.is_dir():
            paths = sorted(q for q in p.rglob("*")
                           if q.suffix.lower() in {".mp3", ".m4a", ".aac", ".wav", ".aif", ".aiff"})
        else:
            paths = [p]
        for path in paths:
            try:
                entry = lib.add_track(path)
                added += 1
                print(f"  + [{entry.track_id}] {entry.title} — {entry.artist or '?'}")
            except FileNotFoundError:
                print(f"  ! not found: {path}", file=sys.stderr)
            except Exception as exc:
                print(f"  ! {path.name}: {exc}", file=sys.stderr)
    if added:
        if args.no_sync:
            lib.save_sidecar()
            print(f"Staged {added} track(s). Run `ipodmanager-cli sync` to write iTunesDB.")
        else:
            lib.sync()
            print(f"Added {added} track(s) and wrote iTunesDB.")
    return 0 if added else 1


def cmd_remove(args: argparse.Namespace) -> int:
    pod = _resolve_pod(args.mount)
    lib = Library.load(pod)
    removed = 0
    for tid in args.ids:
        if lib.remove_track(int(tid)):
            removed += 1
            print(f"  - removed track {tid}")
        else:
            print(f"  ! no track with id {tid}", file=sys.stderr)
    if removed:
        lib.sync()
        print(f"Removed {removed} track(s) and wrote iTunesDB.")
    return 0 if removed else 1


def cmd_rename(args: argparse.Namespace) -> int:
    pod = _resolve_pod(args.mount)
    lib = Library.load(pod)
    lib.rename(args.name)
    lib.sync()
    print(f"iPod library name set to: {lib.ipod_name}")
    if args.volume:
        try:
            new_pod = rename_volume(pod, args.name)
            print(f"Volume label changed to: {new_pod.name}")
        except VolumeRenameError as exc:
            print(f"Could not rename volume: {exc}", file=sys.stderr)
            return 1
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    pod = _resolve_pod(args.mount)
    lib = Library.load(pod)
    lib.sync()
    print(f"iTunesDB written ({len(lib.entries)} tracks).")
    return 0


def cmd_prune(args: argparse.Namespace) -> int:
    pod = _resolve_pod(args.mount)
    lib = Library.load(pod)
    pruned = lib.prune_missing()
    if not pruned:
        print("No ghost entries — library is clean.")
        return 0
    for e in pruned:
        print(f"  - pruned [{e.track_id}] {e.title}  ({e.ipod_path})")
    lib.sync()
    print(f"Pruned {len(pruned)} missing track(s) and rewrote iTunesDB.")
    return 0


def cmd_eject(args: argparse.Namespace) -> int:
    pod = _resolve_pod(args.mount)
    eject(pod)
    print(f"Ejected {pod.name}.")
    return 0


# ------------------------------------------------------------------ #
# Argument parser                                                     #
# ------------------------------------------------------------------ #


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ipodmanager-cli",
        description="Manage music on classic iPods (Nano, Mini, Video, Classic).",
    )
    p.add_argument("--version", action="version", version=f"iPodManager {__version__}")
    p.add_argument("--mount", help="Explicit iPod mountpoint (skip autodetection).")

    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("devices", help="List connected iPods").set_defaults(func=cmd_devices)
    sub.add_parser("info", help="Show iPod capacity + track count").set_defaults(func=cmd_info)
    sub.add_parser("list", help="List tracks on the iPod").set_defaults(func=cmd_list)

    sp = sub.add_parser("add", help="Copy audio files onto the iPod")
    sp.add_argument("files", nargs="+", help="Files or directories to add")
    sp.add_argument("--no-sync", action="store_true",
                    help="Stage files but don't rewrite the iTunesDB yet")
    sp.set_defaults(func=cmd_add)

    sp = sub.add_parser("remove", help="Remove tracks by id (see `list`)")
    sp.add_argument("ids", nargs="+", help="Numeric track IDs")
    sp.set_defaults(func=cmd_remove)

    sp = sub.add_parser("rename", help="Rename the iPod")
    sp.add_argument("name", help="New iPod name (shown in the library)")
    sp.add_argument("--volume", action="store_true",
                    help="Also rename the FAT32 volume label (<= 11 ASCII chars)")
    sp.set_defaults(func=cmd_rename)

    sub.add_parser("sync", help="Rewrite iTunesDB from the sidecar").set_defaults(func=cmd_sync)
    sub.add_parser("prune", help="Drop sidecar entries whose files are missing").set_defaults(func=cmd_prune)
    sub.add_parser("eject", help="Flush writes and safely unmount").set_defaults(func=cmd_eject)

    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
