"""iPod device detection and raw filesystem operations.

An iPod exposed as USB mass storage looks like a FAT32 volume with a
top-level ``iPod_Control`` directory. We detect it by scanning the
mount points reported by the OS for that directory. Everything the
app writes goes under ``iPod_Control/Music`` and ``iPod_Control/iTunes``.
"""

from __future__ import annotations

import os
import platform
import random
import shutil
import string
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional

import psutil


# ------------------------------------------------------------------ #
# Device discovery                                                    #
# ------------------------------------------------------------------ #


@dataclass
class IPod:
    """Represents a mounted iPod."""
    mountpoint: Path
    name: str                   # volume label (e.g. "IPOD DE GER")
    total_bytes: int
    free_bytes: int

    @property
    def used_bytes(self) -> int:
        return self.total_bytes - self.free_bytes

    @property
    def control_dir(self) -> Path:
        return self.mountpoint / "iPod_Control"

    @property
    def music_dir(self) -> Path:
        return self.control_dir / "Music"

    @property
    def itunes_dir(self) -> Path:
        return self.control_dir / "iTunes"

    @property
    def itunesdb_path(self) -> Path:
        return self.itunes_dir / "iTunesDB"

    @property
    def managed_json_path(self) -> Path:
        # Our own sidecar — iPod firmware ignores unknown files here.
        return self.itunes_dir / "ipodmanager.json"


def _plausible_ipod_mount(mp: Path) -> bool:
    try:
        return (mp / "iPod_Control").is_dir()
    except (OSError, PermissionError):
        return False


def list_ipods() -> List[IPod]:
    """Return every currently-mounted iPod found on this machine."""
    seen: List[IPod] = []
    for part in psutil.disk_partitions(all=False):
        mp = Path(part.mountpoint)
        if not _plausible_ipod_mount(mp):
            continue
        try:
            usage = psutil.disk_usage(str(mp))
        except (OSError, PermissionError):
            continue
        seen.append(IPod(
            mountpoint=mp,
            name=mp.name or str(mp),
            total_bytes=usage.total,
            free_bytes=usage.free,
        ))
    return seen


def find_ipod() -> Optional[IPod]:
    """Return the first mounted iPod, or None."""
    pods = list_ipods()
    return pods[0] if pods else None


# ------------------------------------------------------------------ #
# Filesystem operations on the iPod                                   #
# ------------------------------------------------------------------ #


def ensure_structure(pod: IPod, n_music_folders: int = 20) -> None:
    """Make sure the iPod has the standard folder layout.

    iPods spread the music files across ``F00`` ... ``F##`` sub-folders
    (originally to work around filesystem limits on how many files
    could live in a single directory on the Mini). We keep that layout.
    """
    pod.itunes_dir.mkdir(parents=True, exist_ok=True)
    pod.music_dir.mkdir(parents=True, exist_ok=True)
    for i in range(n_music_folders):
        (pod.music_dir / f"F{i:02d}").mkdir(exist_ok=True)


def _random_name(ext: str, length: int = 4) -> str:
    alphabet = string.ascii_uppercase + string.digits
    return "".join(random.choice(alphabet) for _ in range(length)) + "." + ext.lstrip(".")


def assign_destination(pod: IPod, source_path: Path, n_music_folders: int = 20) -> Path:
    """Pick a free random ``F##/XXXX.ext`` path on the iPod for a new track."""
    ext = source_path.suffix.lstrip(".").lower() or "mp3"
    while True:
        folder = pod.music_dir / f"F{random.randrange(n_music_folders):02d}"
        folder.mkdir(exist_ok=True)
        candidate = folder / _random_name(ext)
        if not candidate.exists():
            return candidate


def to_itunes_path(pod: IPod, dest: Path) -> str:
    """Convert a filesystem path on the iPod to the Mac-colon form the
    iTunesDB stores (e.g. ``:iPod_Control:Music:F02:ABCD.mp3``)."""
    rel = dest.relative_to(pod.mountpoint)
    return ":" + ":".join(rel.parts)


def copy_track_to_ipod(pod: IPod, source: Path) -> Path:
    """Copy a track into the iPod's Music folder tree and return the
    chosen destination path. Does NOT touch the iTunesDB — the library
    layer handles that so the two always move together on sync."""
    ensure_structure(pod)
    dest = assign_destination(pod, source)
    shutil.copy2(source, dest)
    return dest


def remove_track_from_ipod(pod: IPod, ipod_path: str) -> None:
    """Delete the file referenced by an iTunesDB-style path."""
    rel = ipod_path.lstrip(":").replace(":", os.sep)
    full = pod.mountpoint / rel
    try:
        full.unlink()
    except FileNotFoundError:
        pass


# ------------------------------------------------------------------ #
# Volume label (iPod name) — OS-specific                              #
# ------------------------------------------------------------------ #


class VolumeRenameError(RuntimeError):
    pass


def rename_volume(pod: IPod, new_label: str) -> IPod:
    """Rename the iPod volume label at the OS level.

    This is the name macOS Finder / Windows Explorer displays. On FAT32
    it is limited to 11 ASCII characters, uppercase; longer labels get
    truncated or rejected depending on the OS.
    """
    label = new_label.strip().upper()[:11]
    if not label:
        raise VolumeRenameError("Label cannot be empty.")

    system = platform.system()
    try:
        if system == "Darwin":
            subprocess.run(
                ["diskutil", "renameVolume", str(pod.mountpoint), label],
                check=True, capture_output=True, text=True,
            )
            new_mp = Path("/Volumes") / label
        elif system == "Windows":
            drive_letter = str(pod.mountpoint).rstrip("\\/")[0:2]
            subprocess.run(
                ["cmd", "/c", "label", drive_letter, label],
                check=True, capture_output=True, text=True,
            )
            new_mp = pod.mountpoint
        else:
            # Linux — needs mtools (mlabel) because the volume is FAT32
            # and must be unmounted first for most label-change tools.
            raise VolumeRenameError(
                "Renaming the volume on Linux requires unmounting the "
                "iPod and running `sudo mlabel -i <device> ::NEWNAME`."
            )
    except subprocess.CalledProcessError as e:
        raise VolumeRenameError(e.stderr or str(e)) from e

    return IPod(
        mountpoint=new_mp,
        name=label,
        total_bytes=pod.total_bytes,
        free_bytes=pod.free_bytes,
    )


# ------------------------------------------------------------------ #
# Safe eject                                                          #
# ------------------------------------------------------------------ #


def eject(pod: IPod) -> None:
    """Flush writes and unmount the iPod so it is safe to disconnect."""
    system = platform.system()
    if system == "Darwin":
        subprocess.run(["diskutil", "eject", str(pod.mountpoint)], check=True)
    elif system == "Windows":
        # No clean built-in. Best effort: let user eject via Explorer.
        raise RuntimeError("On Windows, right-click the iPod drive and choose Eject.")
    else:
        subprocess.run(["umount", str(pod.mountpoint)], check=True)
