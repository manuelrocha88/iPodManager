"""Pure-Python iTunesDB binary writer.

Targets the iTunesDB format used by iPods that do NOT require the
FirewireGUID-based hash (anything up to and including iPod Classic 6G
and all iPod Nanos through 4G). The format is little-endian throughout
and composed of nested "chunks" identified by 4-byte magic words.

Chunk reference (abbreviated):

  mhbd  database header         — root
  mhsd  dataset header          — one per section (tracks, playlists, ...)
  mhlt  track list header       — contains N mhit
  mhit  track item              — contains M mhod (strings)
  mhod  data object             — string / location / playlist index
  mhlp  playlist list header    — contains N mhyp
  mhyp  playlist                — contains M mhip + mhod children
  mhip  playlist item           — references a track by id

We generate a minimal but valid DB with:
    mhbd
      mhsd(type=1, tracks)
        mhlt (N tracks)
          mhit + 2-3 mhods each (location, title, [artist, album])
      mhsd(type=2, playlists v1)
        mhlp (1 playlist)
          mhyp "<ipod name>" master playlist
            mhods (name, "sort by title" index)
            mhip x N (one per track)
      mhsd(type=3, playlists v2)
        mhlp (1 playlist)
          mhyp "<ipod name>" master playlist   # duplicate, newer iPods expect it

The empty DB that ships blank on a Nano 1G has a few extra mhsd sections
(albums, podcast playlist, etc.). Those are optional and iPods happily
ignore their absence as long as the master playlist exists.
"""

from __future__ import annotations

import os
import struct
import time
from dataclasses import dataclass, field
from typing import List, Optional

# ------------------------------------------------------------------ #
# Chunk header sizes (bytes written for the fixed header portion).
# Chunk `total_length` includes the header plus all nested chunks.    #
# ------------------------------------------------------------------ #

MHBD_HDR = 0xF4
MHSD_HDR = 0x60
MHLT_HDR = 0x5C
MHLP_HDR = 0x5C
MHYP_HDR = 0xB8
MHIP_HDR = 0x4C
MHIT_HDR = 0xF4
MHOD_HDR = 0x18  # fixed header before string payload inside mhod

# mhod payload types (what the string/location block represents).
MHOD_TITLE = 1
MHOD_LOCATION = 2
MHOD_ALBUM = 3
MHOD_ARTIST = 4
MHOD_GENRE = 5
MHOD_FILETYPE = 6
MHOD_COMMENT = 8
MHOD_COMPOSER = 12
MHOD_GROUPING = 13
MHOD_PLAYLIST_INDEX = 100
MHOD_PLAYLIST_NAME = 101

# mhsd dataset types.
MHSD_TRACKS = 1
MHSD_PLAYLISTS_V1 = 2
MHSD_PLAYLISTS_V2 = 3

# iTunes DB version marker written into mhbd. 0x19 (= 25) matches
# iTunes 7.x which was contemporary with the Nano 1G and is accepted
# by every iPod from 4G onward.
ITUNESDB_VERSION = 0x19


# ------------------------------------------------------------------ #
# Public dataclass for a track entry.                                 #
# ------------------------------------------------------------------ #


@dataclass
class Track:
    track_id: int               # 1-based unique id, assigned by the library
    dbid: int                   # 64-bit persistent id, random per track
    ipod_path: str              # e.g. ":iPod_Control:Music:F00:ABCD.mp3"
    title: str
    artist: str = ""
    album: str = ""
    genre: str = ""
    composer: str = ""
    file_ext: str = "mp3"       # mp3 | m4a | aac | wav
    length_ms: int = 0
    file_size: int = 0
    bitrate_kbps: int = 128
    sample_rate_hz: int = 44100
    track_number: int = 0
    track_count: int = 0
    disc_number: int = 0
    disc_count: int = 0
    year: int = 0
    play_count: int = 0
    date_added: int = 0         # Mac HFS epoch seconds


# ------------------------------------------------------------------ #
# Low-level helpers                                                   #
# ------------------------------------------------------------------ #


# iPods store timestamps as "Mac/HFS time": seconds since 1904-01-01.
MAC_EPOCH_OFFSET = 2082844800


def unix_to_mac_time(ts: Optional[float] = None) -> int:
    if ts is None:
        ts = time.time()
    return int(ts) + MAC_EPOCH_OFFSET


def _utf16(s: str) -> bytes:
    """UTF-16-LE encode a string. All on-disk strings in mhod use this."""
    return s.encode("utf-16-le")


def _pad(buf: bytearray, size: int) -> None:
    """Pad `buf` with zero bytes until it reaches `size` bytes."""
    if len(buf) < size:
        buf.extend(b"\x00" * (size - len(buf)))


def _file_type_magic(ext: str) -> bytes:
    """Four-byte 'file kind' tag that iTunes writes to mhit offset 0x18.

    The constant we pick is only cosmetic (iTunes displays "MPEG audio
    file" vs "AAC audio file" based on it) but it needs to match what the
    iPod firmware recognises for the given extension.
    """
    ext = ext.lower().lstrip(".")
    table = {
        "mp3": b"MPEG",
        "m4a": b"M4A ",
        "m4b": b"M4B ",
        "aac": b"M4A ",
        "wav": b"WAVE",
        "aif": b"AIFF",
        "aiff": b"AIFF",
    }
    return table.get(ext, b"MPEG")


# ------------------------------------------------------------------ #
# Chunk builders                                                      #
# ------------------------------------------------------------------ #


def _build_mhod_string(mhod_type: int, value: str) -> bytes:
    """Build an mhod of one of the "string" subtypes (title, artist, ...).

    Layout:
      0x00  "mhod"
      0x04  header length (0x18)
      0x08  total length (0x18 + 0x10 + 2*len(utf16))
      0x0c  mhod type
      0x10  (unknown, 0)
      0x14  (unknown, 0)
      0x18  string header:
             0x00  position (always 1)
             0x04  byte length of string
             0x08  (unknown, 0)
             0x0c  (unknown, 0)
             0x10  utf-16-le string bytes
    """
    text = _utf16(value)
    string_block = struct.pack("<IIII", 1, len(text), 0, 0) + text
    total_len = MHOD_HDR + len(string_block)

    buf = bytearray()
    buf += b"mhod"
    buf += struct.pack("<III", MHOD_HDR, total_len, mhod_type)
    buf += struct.pack("<II", 0, 0)
    buf += string_block
    return bytes(buf)


def _build_mhod_playlist_index() -> bytes:
    """mhod subtype 100 — "sort order" index attached to the master playlist.

    The payload is a 0x60-byte block that would normally hold a sorted
    index of track positions; the iPod firmware regenerates it when it
    first mounts the DB, so we emit an empty one.
    """
    header = bytearray()
    header += b"mhod"
    header += struct.pack("<III", MHOD_HDR, MHOD_HDR + 0x60, MHOD_PLAYLIST_INDEX)
    header += struct.pack("<II", 0, 0)
    header += b"\x00" * 0x60
    return bytes(header)


def _build_mhit(track: Track) -> bytes:
    """Build one mhit + its mhod children.

    Only a handful of the ~60 integer fields matter for playback; the rest
    are left at zero and the iPod fills them in from the audio file at
    first play.
    """
    mhods = bytearray()
    mhods += _build_mhod_string(MHOD_TITLE, track.title or "Untitled")
    mhods += _build_mhod_string(MHOD_LOCATION, track.ipod_path)
    if track.album:
        mhods += _build_mhod_string(MHOD_ALBUM, track.album)
    if track.artist:
        mhods += _build_mhod_string(MHOD_ARTIST, track.artist)
    if track.genre:
        mhods += _build_mhod_string(MHOD_GENRE, track.genre)
    if track.composer:
        mhods += _build_mhod_string(MHOD_COMPOSER, track.composer)

    # mhod children count. Required children are title + location; the
    # others are added conditionally above.
    n_children = 2 + sum(bool(x) for x in (track.album, track.artist,
                                           track.genre, track.composer))

    total_len = MHIT_HDR + len(mhods)
    added_mac = track.date_added or unix_to_mac_time()

    header = bytearray()
    header += b"mhit"
    header += struct.pack("<III", MHIT_HDR, total_len, n_children)
    header += struct.pack("<I", track.track_id)              # 0x10 unique id
    header += struct.pack("<I", 1)                            # 0x14 visible
    header += _file_type_magic(track.file_ext)                # 0x18 file kind
    header += struct.pack("<BBBB", 0, 1, 0, 0)                # 0x1c flags
    header += struct.pack("<II", 0, 0)                        # 0x20 date added2, 0x24 ???
    # mhit from here follows the field order documented on the ipodlinux wiki.
    header += struct.pack("<I", track.file_size)              # 0x28 file size
    header += struct.pack("<I", track.length_ms)              # 0x2c length ms
    header += struct.pack("<I", track.track_number)           # 0x30
    header += struct.pack("<I", track.track_count)            # 0x34
    header += struct.pack("<I", track.year)                   # 0x38
    header += struct.pack("<I", track.bitrate_kbps)           # 0x3c
    header += struct.pack("<HH", 0, track.sample_rate_hz)     # 0x40 sample rate hi/lo
    header += struct.pack("<I", 0)                            # 0x44 volume
    header += struct.pack("<I", 0)                            # 0x48 start_time
    header += struct.pack("<I", 0)                            # 0x4c stop_time
    header += struct.pack("<I", 0)                            # 0x50 soundcheck
    header += struct.pack("<I", track.play_count)             # 0x54 play count
    header += struct.pack("<I", track.play_count)             # 0x58 play count2
    header += struct.pack("<I", 0)                            # 0x5c last_played_time
    header += struct.pack("<I", track.disc_number)            # 0x60
    header += struct.pack("<I", track.disc_count)             # 0x64
    header += struct.pack("<I", 0)                            # 0x68 user_id
    header += struct.pack("<I", added_mac)                    # 0x6c date_added
    header += struct.pack("<I", 0)                            # 0x70 bookmark
    header += struct.pack("<II", track.dbid & 0xFFFFFFFF,
                          (track.dbid >> 32) & 0xFFFFFFFF)    # 0x74 persistent id
    _pad(header, MHIT_HDR)

    return bytes(header) + bytes(mhods)


def _build_mhip(track_id: int, playlist_item_id: int) -> bytes:
    """mhip = playlist item.  Links a playlist (mhyp) to a specific track id.

    The mhip has one mhod child of subtype 100 (empty "sort index" block)
    for every playlist item on recent firmware. On older iPods it is
    optional but harmless to emit.
    """
    sort_mhod = _build_mhod_playlist_index()
    total_len = MHIP_HDR + len(sort_mhod)

    header = bytearray()
    header += b"mhip"
    header += struct.pack("<III", MHIP_HDR, total_len, 1)
    header += struct.pack("<I", playlist_item_id)   # 0x10 playlist item id
    header += struct.pack("<I", 0)                   # 0x14 unknown
    header += struct.pack("<I", 0)                   # 0x18 group id
    header += struct.pack("<I", track_id)            # 0x1c track id
    header += struct.pack("<I", 0)                   # 0x20 timestamp
    _pad(header, MHIP_HDR)
    return bytes(header) + sort_mhod


def _build_mhyp(name: str, is_master: bool, track_ids: List[int]) -> bytes:
    """mhyp = playlist header + N mhips.

    `is_master` tags this as the main library playlist (which must be the
    first mhyp in the file and is always marked visible).
    """
    mhod_chunks = [_build_mhod_string(MHOD_PLAYLIST_NAME, name)]
    mhip_chunks = [
        _build_mhip(tid, playlist_item_id=idx)
        for idx, tid in enumerate(track_ids, start=1)
    ]

    body = b"".join(mhod_chunks) + b"".join(mhip_chunks)
    total_len = MHYP_HDR + len(body)
    playlist_pid = int.from_bytes(os.urandom(8), "little")

    header = bytearray()
    header += b"mhyp"
    header += struct.pack("<III", MHYP_HDR, total_len, len(mhod_chunks))
    header += struct.pack("<I", len(mhip_chunks))               # 0x10 n_mhip
    header += struct.pack("<I", 1 if is_master else 0)          # 0x14 is_master
    header += struct.pack("<I", unix_to_mac_time())             # 0x18 timestamp
    header += struct.pack("<Q", playlist_pid)                   # 0x1c persistent id
    _pad(header, MHYP_HDR)
    return bytes(header) + body


def _build_mhlt(tracks: List[Track]) -> bytes:
    """mhlt = track list header, children = N mhit."""
    body = b"".join(_build_mhit(t) for t in tracks)
    header = bytearray()
    header += b"mhlt"
    header += struct.pack("<II", MHLT_HDR, len(tracks))
    _pad(header, MHLT_HDR)
    return bytes(header) + body


def _build_mhlp(playlists: List[bytes], n_playlists: int) -> bytes:
    """mhlp = playlist list header, children = N mhyp (already-built bytes)."""
    body = b"".join(playlists)
    header = bytearray()
    header += b"mhlp"
    header += struct.pack("<II", MHLP_HDR, n_playlists)
    _pad(header, MHLP_HDR)
    return bytes(header) + body


def _build_mhsd(dataset_type: int, body: bytes) -> bytes:
    """mhsd = dataset header, wraps a mhlt/mhlp/mhla."""
    total_len = MHSD_HDR + len(body)
    header = bytearray()
    header += b"mhsd"
    header += struct.pack("<III", MHSD_HDR, total_len, dataset_type)
    _pad(header, MHSD_HDR)
    return bytes(header) + body


def _build_mhbd(datasets: List[bytes], library_persistent_id: int) -> bytes:
    """mhbd = top-level database header. Wraps every dataset in the file."""
    body = b"".join(datasets)
    total_len = MHBD_HDR + len(body)

    header = bytearray()
    header += b"mhbd"
    header += struct.pack("<III", MHBD_HDR, total_len, 1)       # 0x0c unknown=1
    header += struct.pack("<II", ITUNESDB_VERSION, len(datasets))  # 0x10 ver, 0x14 nchildren
    header += struct.pack("<Q", library_persistent_id)          # 0x18 persistent id
    header += struct.pack("<H", 2)                               # 0x20 unknown (= 2 on all recent DBs)
    header += struct.pack("<H", 0)                               # 0x22 unknown
    header += struct.pack("<Q", 0)                               # 0x24 unknown
    header += struct.pack("<Q", 0)                               # 0x2c unknown
    header += struct.pack("<H", 2)                               # 0x34 hashing version
    header += b"\x00" * 20                                       # 0x36 hash1 (zero on <=5G)
    header += b"pt"                                              # 0x4a language code
    header += b"\x00" * 20                                       # 0x4c unknown block
    _pad(header, MHBD_HDR)
    return bytes(header) + body


# ------------------------------------------------------------------ #
# High-level entry point                                              #
# ------------------------------------------------------------------ #


def build_itunesdb(tracks: List[Track], ipod_name: str = "iPod",
                   library_persistent_id: Optional[int] = None) -> bytes:
    """Build and return a complete iTunesDB file as bytes.

    `tracks` must have unique .track_id values (1-based). `ipod_name` is
    the label of the master playlist, which is what the iPod displays as
    the device name under Settings → About.
    """
    if library_persistent_id is None:
        library_persistent_id = int.from_bytes(os.urandom(8), "little")

    track_ids = [t.track_id for t in tracks]

    # Dataset 1: tracks.
    tracks_mhlt = _build_mhlt(tracks)
    tracks_mhsd = _build_mhsd(MHSD_TRACKS, tracks_mhlt)

    # Dataset 2: playlists (v1, older-style). Must include the master
    # playlist. We emit a single master playlist here.
    master_v1 = _build_mhyp(ipod_name, is_master=True, track_ids=track_ids)
    playlists_v1 = _build_mhlp([master_v1], n_playlists=1)
    playlists_v1_mhsd = _build_mhsd(MHSD_PLAYLISTS_V1, playlists_v1)

    # Dataset 3: playlists (v2). Newer firmware reads this preferentially.
    master_v2 = _build_mhyp(ipod_name, is_master=True, track_ids=track_ids)
    playlists_v2 = _build_mhlp([master_v2], n_playlists=1)
    playlists_v2_mhsd = _build_mhsd(MHSD_PLAYLISTS_V2, playlists_v2)

    return _build_mhbd(
        [tracks_mhsd, playlists_v1_mhsd, playlists_v2_mhsd],
        library_persistent_id,
    )


def write_itunesdb(path: str, tracks: List[Track], ipod_name: str = "iPod",
                   library_persistent_id: Optional[int] = None) -> None:
    """Build and atomically write the iTunesDB to `path`."""
    data = build_itunesdb(tracks, ipod_name, library_persistent_id)
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
