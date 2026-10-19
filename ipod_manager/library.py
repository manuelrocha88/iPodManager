"""Library management: the thing in between "a music file on disk" and
"an mhit entry in the iTunesDB".

The library layer owns:
  * a sidecar JSON file on the iPod (``iPod_Control/iTunes/ipodmanager.json``)
    that records what we put there — titles, artists, assigned filenames.
  * the sync step: regenerate the iTunesDB from scratch from the sidecar,
    which means the DB is always a faithful projection of what we know.

We never try to parse Apple's iTunesDB back into our model. The sidecar
is the source of truth; the iTunesDB is a build output. This is the
same strategy gtkpod and Rhythmbox used, and it dodges the complexity
of handling every DB version Apple ever shipped.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import mutagen

from . import itunesdb
from .device import (IPod, copy_track_to_ipod, ensure_structure,
                     remove_track_from_ipod, to_itunes_path)


# Default name the master playlist carries if the user hasn't set one.
DEFAULT_IPOD_NAME = "iPod"


# ------------------------------------------------------------------ #
# Entry record                                                        #
# ------------------------------------------------------------------ #


@dataclass
class LibraryEntry:
    """One track registered in the sidecar library."""
    track_id: int
    dbid: int
    ipod_path: str          # ":iPod_Control:Music:F00:ABCD.mp3"
    title: str
    artist: str = ""
    album: str = ""
    genre: str = ""
    composer: str = ""
    file_ext: str = "mp3"
    length_ms: int = 0
    file_size: int = 0
    bitrate_kbps: int = 128
    sample_rate_hz: int = 44100
    track_number: int = 0
    track_count: int = 0
    disc_number: int = 0
    disc_count: int = 0
    year: int = 0
    date_added: int = 0     # unix epoch seconds


# ------------------------------------------------------------------ #
# Audio metadata extraction                                           #
# ------------------------------------------------------------------ #


def _read_tags(path: Path) -> Dict[str, object]:
    """Best-effort metadata extraction with mutagen.

    Mutagen hides the per-format tag naming behind a lowest-common
    interface for the "EasyXxx" wrappers, but we need the audio info
    (length, bitrate, sample rate) which only the raw loader exposes.
    """
    try:
        audio = mutagen.File(path, easy=True)
    except Exception:
        audio = None

    def first(key: str) -> str:
        if not audio:
            return ""
        v = audio.get(key)
        if isinstance(v, list) and v:
            return str(v[0])
        return str(v) if v else ""

    length_ms = 0
    bitrate_kbps = 0
    sample_rate_hz = 44100
    if audio and getattr(audio, "info", None):
        info = audio.info
        length_ms = int(getattr(info, "length", 0) * 1000)
        bitrate_kbps = int(getattr(info, "bitrate", 0) // 1000) or 128
        sample_rate_hz = int(getattr(info, "sample_rate", 44100))

    def parse_int(s: str) -> int:
        if not s:
            return 0
        try:
            return int(s.split("/")[0])
        except ValueError:
            return 0

    track = parse_int(first("tracknumber"))
    track_total = 0
    if audio:
        raw = audio.get("tracknumber")
        if isinstance(raw, list) and raw and "/" in str(raw[0]):
            track_total = parse_int(str(raw[0]).split("/")[1])

    disc = parse_int(first("discnumber"))
    disc_total = 0
    if audio:
        raw = audio.get("discnumber")
        if isinstance(raw, list) and raw and "/" in str(raw[0]):
            disc_total = parse_int(str(raw[0]).split("/")[1])

    year = parse_int(first("date"))

    return {
        "title": first("title") or path.stem,
        "artist": first("artist"),
        "album": first("album"),
        "genre": first("genre"),
        "composer": first("composer"),
        "length_ms": length_ms,
        "bitrate_kbps": bitrate_kbps or 128,
        "sample_rate_hz": sample_rate_hz or 44100,
        "track_number": track,
        "track_count": track_total,
        "disc_number": disc,
        "disc_count": disc_total,
        "year": year,
    }


# ------------------------------------------------------------------ #
# Library                                                             #
# ------------------------------------------------------------------ #


SIDECAR_VERSION = 1


class Library:
    """Collection of LibraryEntry on a specific iPod, backed by a JSON sidecar."""

    def __init__(self, pod: IPod,
                 ipod_name: str = DEFAULT_IPOD_NAME,
                 entries: Optional[List[LibraryEntry]] = None,
                 next_track_id: int = 1,
                 library_persistent_id: int = 0):
        self.pod = pod
        self.ipod_name = ipod_name
        self.entries: List[LibraryEntry] = entries or []
        self._next_track_id = next_track_id
        self._library_pid = library_persistent_id or int.from_bytes(os.urandom(8), "little")

    # ---- (de)serialisation --------------------------------------- #

    def to_dict(self) -> dict:
        return {
            "version": SIDECAR_VERSION,
            "ipod_name": self.ipod_name,
            "library_persistent_id": self._library_pid,
            "next_track_id": self._next_track_id,
            "entries": [asdict(e) for e in self.entries],
        }

    @classmethod
    def load(cls, pod: IPod) -> "Library":
        """Load from sidecar if present; otherwise return an empty library."""
        try:
            raw = pod.managed_json_path.read_text(encoding="utf-8")
        except (FileNotFoundError, OSError):
            return cls(pod)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return cls(pod)

        entries = [LibraryEntry(**e) for e in data.get("entries", [])]
        return cls(
            pod=pod,
            ipod_name=data.get("ipod_name") or DEFAULT_IPOD_NAME,
            entries=entries,
            next_track_id=data.get("next_track_id", len(entries) + 1),
            library_persistent_id=data.get("library_persistent_id", 0),
        )

    def save_sidecar(self) -> None:
        ensure_structure(self.pod)
        tmp = self.pod.managed_json_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        os.replace(tmp, self.pod.managed_json_path)

    # ---- mutation ------------------------------------------------ #

    def add_track(self, source_path: Path) -> LibraryEntry:
        """Copy a track onto the iPod and register it in the library."""
        source_path = Path(source_path).expanduser().resolve()
        if not source_path.is_file():
            raise FileNotFoundError(source_path)

        tags = _read_tags(source_path)
        dest = copy_track_to_ipod(self.pod, source_path)

        entry = LibraryEntry(
            track_id=self._next_track_id,
            dbid=int.from_bytes(os.urandom(8), "little"),
            ipod_path=to_itunes_path(self.pod, dest),
            file_ext=source_path.suffix.lstrip(".").lower() or "mp3",
            file_size=source_path.stat().st_size,
            date_added=int(time.time()),
            **tags,
        )
        self._next_track_id += 1
        self.entries.append(entry)
        return entry

    def remove_track(self, track_id: int) -> bool:
        """Delete a track from the iPod. Returns True if it existed."""
        for i, e in enumerate(self.entries):
            if e.track_id == track_id:
                remove_track_from_ipod(self.pod, e.ipod_path)
                self.entries.pop(i)
                return True
        return False

    def rename(self, new_name: str) -> None:
        """Change the master-playlist name shown on the iPod."""
        self.ipod_name = new_name.strip() or DEFAULT_IPOD_NAME

    # ---- sync ---------------------------------------------------- #

    def sync(self) -> None:
        """Write the iTunesDB and sidecar to the iPod."""
        ensure_structure(self.pod)
        tracks = [self._to_itunesdb_track(e) for e in self.entries]
        itunesdb.write_itunesdb(
            str(self.pod.itunesdb_path),
            tracks,
            ipod_name=self.ipod_name,
            library_persistent_id=self._library_pid,
        )
        self.save_sidecar()

    @staticmethod
    def _to_itunesdb_track(e: LibraryEntry) -> itunesdb.Track:
        return itunesdb.Track(
            track_id=e.track_id,
            dbid=e.dbid,
            ipod_path=e.ipod_path,
            title=e.title,
            artist=e.artist,
            album=e.album,
            genre=e.genre,
            composer=e.composer,
            file_ext=e.file_ext,
            length_ms=e.length_ms,
            file_size=e.file_size,
            bitrate_kbps=e.bitrate_kbps,
            sample_rate_hz=e.sample_rate_hz,
            track_number=e.track_number,
            track_count=e.track_count,
            disc_number=e.disc_number,
            disc_count=e.disc_count,
            year=e.year,
            date_added=itunesdb.unix_to_mac_time(e.date_added) if e.date_added else 0,
        )
