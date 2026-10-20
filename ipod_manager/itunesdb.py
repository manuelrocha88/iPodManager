"""Pure-Python iTunesDB binary writer.

Targets the iTunesDB format used by iPods that do NOT require the
FirewireGUID-based hash (anything up to and including iPod Classic 6G
and all iPod Nanos through 4G). The format is little-endian throughout
and composed of nested "chunks" identified by 4-byte magic words.

Chunk reference (abbreviated):

  mhbd  database header         — root
  mhsd  dataset header          — one per section (tracks, playlists, ...)
  mhlt  track list header       — contains N mhit (as siblings in parent mhsd)
  mhit  track item              — contains M mhod (strings)
  mhod  data object             — string / location / playlist index
  mhlp  playlist list header    — contains N mhyp (as siblings in parent mhsd)
  mhyp  playlist                — contains M mhip + mhod children
  mhip  playlist item           — references a track by id

Writing strategy
================

Early versions of this module built the entire DB from scratch. That
turned out to be too fragile — the iPod firmware is picky about which
mhsd sections exist, several poorly-documented bytes in the mhbd
header, and a bunch of unused but required mhods on the master
playlist (sort indices). An empty DB written by iTunes contains all of
those as "magic bytes" we don't need to understand.

So the production path (:func:`build_itunesdb`) now does
*template splicing*: it loads the empty iTunesDB that shipped on the
iPod, parses its chunk tree, inserts our mhits into the track list and
our mhips into every master playlist, re-counts and resizes the parent
containers, and writes it back. Only the specific bytes we care about
change; everything else is preserved exactly as iTunes wrote it.
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


def _build_mhod_playlist_index(position: int) -> bytes:
    """mhod subtype 100 — playlist-item ordering slot, child of every mhip.

    Layout (44 bytes total, 24-byte header + 20-byte payload):
      0x00  "mhod"
      0x04  header_len (0x18)
      0x08  total_len  (0x2C)
      0x0C  type (100)
      0x10  unknown (0)
      0x14  unknown (0)
      0x18  position (1-based ordinal in the playlist)
      0x1C-0x2B  five uint32 zeroes (sort flags, firmware fills in)

    Every mhip from iTunes 4.9 (mhbd version >= 0x0d) onward carries
    exactly one of these. Omitting it, or giving it the wrong total
    length, causes Nano-era firmware to drop the playlist and show
    "0 Songs" even though the mhit chain is valid.
    """
    total_len = 0x2C
    buf = bytearray(total_len)
    buf[0:4] = b"mhod"
    struct.pack_into("<I", buf, 0x04, MHOD_HDR)
    struct.pack_into("<I", buf, 0x08, total_len)
    struct.pack_into("<I", buf, 0x0C, MHOD_PLAYLIST_INDEX)
    # 0x10, 0x14 left 0
    struct.pack_into("<I", buf, 0x18, position)
    return bytes(buf)


def _build_mhit(track: Track) -> bytes:
    """Build one mhit + its mhod children.

    Field layout follows the documented ipodlinux "mhit v1" structure
    (header length 0xf4). The important fields for playback are the id,
    the visible flag, the filetype magic, and the location mhod child —
    everything else is display metadata. We populate it anyway so the
    iPod shows accurate duration/bitrate/etc. in its UI.

    iTunes writes the Location mhod first, before Title/Artist/Album,
    and the iPod walks the children in order looking for Location.
    Mirroring that order avoids tripping older firmware.
    """
    mhods = bytearray()
    mhods += _build_mhod_string(MHOD_LOCATION, track.ipod_path)
    mhods += _build_mhod_string(MHOD_TITLE, track.title or "Untitled")
    if track.album:
        mhods += _build_mhod_string(MHOD_ALBUM, track.album)
    if track.artist:
        mhods += _build_mhod_string(MHOD_ARTIST, track.artist)
    if track.genre:
        mhods += _build_mhod_string(MHOD_GENRE, track.genre)
    if track.composer:
        mhods += _build_mhod_string(MHOD_COMPOSER, track.composer)

    n_children = 2 + sum(bool(x) for x in (track.album, track.artist,
                                           track.genre, track.composer))

    total_len = MHIT_HDR + len(mhods)
    added_mac = track.date_added or unix_to_mac_time()

    # If length is unknown (common for YouTube-sourced MP3s where mutagen
    # can't read a VBR Xing header), estimate it from file size and
    # bitrate. iPods will refuse to list tracks whose reported length is
    # zero, so this is load-bearing.
    length_ms = track.length_ms
    if length_ms == 0 and track.file_size and track.bitrate_kbps:
        length_ms = int(track.file_size * 8 / (track.bitrate_kbps * 1000) * 1000)

    # We build the header by writing into a pre-zeroed buffer at explicit
    # offsets, which is clearer than appending field-by-field and easier
    # to audit against the format spec.
    hdr = bytearray(MHIT_HDR)
    hdr[0x00:0x04] = b"mhit"
    struct.pack_into("<I", hdr, 0x04, MHIT_HDR)
    struct.pack_into("<I", hdr, 0x08, total_len)
    struct.pack_into("<I", hdr, 0x0C, n_children)         # num_strings
    struct.pack_into("<I", hdr, 0x10, track.track_id)
    struct.pack_into("<I", hdr, 0x14, 1)                   # visible
    hdr[0x18:0x1C] = _file_type_magic(track.file_ext)
    hdr[0x1C] = 0                                           # type1 (CBR flag)
    # type2 byte at 0x1D: 1 for MP3, 0 for AAC. Nano 1G firmware uses
    # this to pick a decoder; mislabelling it hides the track.
    hdr[0x1D] = 1 if track.file_ext.lower() == "mp3" else 0
    hdr[0x1E] = 0                                           # compilation
    hdr[0x1F] = 0                                           # rating (0-100)
    struct.pack_into("<I", hdr, 0x20, added_mac)           # last_modified
    struct.pack_into("<I", hdr, 0x24, track.file_size)
    struct.pack_into("<I", hdr, 0x28, length_ms)
    struct.pack_into("<I", hdr, 0x2C, track.track_number)
    struct.pack_into("<I", hdr, 0x30, track.track_count)
    struct.pack_into("<I", hdr, 0x34, track.year)
    struct.pack_into("<I", hdr, 0x38, track.bitrate_kbps)
    # Sample rate at 0x3C is stored as Hz << 16 (fixed-point 16.16),
    # NOT raw Hz. Writing 44100 instead of 44100 << 16 makes the iPod
    # treat the file as unplayable and show "0 songs".
    struct.pack_into("<I", hdr, 0x3C, track.sample_rate_hz << 16)
    struct.pack_into("<I", hdr, 0x40, 0)                   # volume adjust
    struct.pack_into("<I", hdr, 0x44, 0)                   # start_time
    struct.pack_into("<I", hdr, 0x48, 0)                   # stop_time
    struct.pack_into("<I", hdr, 0x4C, 0)                   # soundcheck
    struct.pack_into("<I", hdr, 0x50, track.play_count)
    struct.pack_into("<I", hdr, 0x54, track.play_count)
    struct.pack_into("<I", hdr, 0x58, 0)                   # last_played_time
    struct.pack_into("<I", hdr, 0x5C, track.disc_number)
    struct.pack_into("<I", hdr, 0x60, track.disc_count)
    struct.pack_into("<I", hdr, 0x64, 0)                   # user_id
    struct.pack_into("<I", hdr, 0x68, added_mac)           # date_added
    struct.pack_into("<I", hdr, 0x6C, 0)                   # bookmark_time
    struct.pack_into("<Q", hdr, 0x70, track.dbid)          # persistent_id (64-bit)
    # Media Type at 0xD0 must be 1 for audio. Firmware since Nano 1G
    # cross-checks this before including the track in Songs — leaving
    # it zero is what makes the device display "0 Songs".
    struct.pack_into("<I", hdr, 0xD0, 1)                   # media_type = audio
    # 0x78 .. 0xf3 are left zero (checked_flag, bpm, artwork_count, ...)

    return bytes(hdr) + bytes(mhods)


def _build_mhip(track_id: int, playlist_item_id: int) -> bytes:
    """mhip = playlist item.  Links a playlist (mhyp) to a specific track id.

    From iTunes 4.9 onward every mhip carries exactly one child mhod of
    type 100 holding the ordinal position of this item in its playlist.
    """
    sort_mhod = _build_mhod_playlist_index(playlist_item_id)
    total_len = MHIP_HDR + len(sort_mhod)

    header = bytearray(MHIP_HDR)
    header[0:4] = b"mhip"
    struct.pack_into("<I", header, 4, MHIP_HDR)
    struct.pack_into("<I", header, 8, total_len)
    struct.pack_into("<I", header, 0x0C, 1)             # num_mhods = 1
    struct.pack_into("<I", header, 0x10, playlist_item_id)
    struct.pack_into("<I", header, 0x14, 0)             # unknown
    struct.pack_into("<I", header, 0x18, 0)             # group id
    struct.pack_into("<I", header, 0x1C, track_id)
    struct.pack_into("<I", header, 0x20, 0)             # timestamp
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
# Template parser + splicer                                           #
# ------------------------------------------------------------------ #
#
# When the iPod ships (or after an iTunes "Restore"), it has an empty
# iTunesDB whose header carries a bunch of poorly-documented bytes the
# firmware checks before trusting the file. We keep those bytes intact
# by using that empty DB as a template and splicing in only the chunks
# we actually produce: the mhits (into the track mhlt) and the mhips
# (into every master mhyp). Every container's total_length and child
# count is re-counted on write.

# Chunk-type categories used by the parser.
# "Containers with total_length": header gives header_length AND the
#   total length of the chunk including its children. Children live
#   directly inside the chunk (within `total_length`).
# "List headers (count only)": header_length only; children are the
#   next siblings in the enclosing container's body, and the count is
#   stored at offset +0x08.
_CONTAINERS = {b"mhbd", b"mhsd", b"mhyp", b"mhit", b"mhip"}
_HAS_TOTAL_LEN = _CONTAINERS | {b"mhod"}
# mhla is theoretically a container (album list), but iTunes emits it
# with total_len = 0 in empty libraries, which would deadlock a naive
# parser. We treat it like mhlt/mhlp — a header-only list sibling.
_LIST_HEADER = {b"mhlt", b"mhlp", b"mhla"}


def _parse_chunk(buf: bytes, offset: int, end: int) -> Optional[dict]:
    """Parse one chunk starting at `offset`, bounded by `end` (parent limit).

    Returns a dict with keys:
      magic      : 4-byte identifier
      header     : the fixed-size header bytes (0 .. hdr_len)
      payload    : bytes between hdr_len and total_len that are NOT sub-chunks
                   (e.g. an mhod's string data). Containers leave this empty
                   and store their nested chunks under ``children``.
      hdr_len    : length of the fixed header
      total_len  : length of the whole chunk (header + payload + children)
      children   : list of parsed sub-chunk dicts (containers only)
    """
    if offset + 8 > end:
        return None
    magic = bytes(buf[offset:offset + 4])
    if not magic.isalpha():
        return None
    hdr_len = struct.unpack_from("<I", buf, offset + 4)[0]
    if offset + hdr_len > end:
        return None

    if magic in _HAS_TOTAL_LEN:
        total_len = struct.unpack_from("<I", buf, offset + 8)[0]
    else:                                 # mhlt, mhlp, mhla: no total_len
        total_len = hdr_len

    chunk = {
        "magic":    magic,
        "header":   bytearray(buf[offset:offset + hdr_len]),
        "payload":  b"",
        "hdr_len":  hdr_len,
        "total_len": total_len,
        "children": [],
    }

    if magic in _CONTAINERS:
        p = offset + hdr_len
        cend = offset + total_len
        while p < cend:
            child = _parse_chunk(buf, p, cend)
            if child is None:
                break
            chunk["children"].append(child)
            step = child["total_len"] if child["total_len"] > 0 else child["hdr_len"]
            p += step
    elif total_len > hdr_len:
        # Non-container with trailing data (an mhod's string block, etc.).
        chunk["payload"] = bytes(buf[offset + hdr_len:offset + total_len])
    return chunk


def _serialize_chunk(chunk: dict, out: bytearray) -> None:
    """Serialize a chunk subtree into `out`, recomputing sizes/counts."""
    magic = chunk["magic"]
    header = bytearray(chunk["header"])

    if chunk["children"]:
        # Container: fix up any list-header children (mhlt/mhlp/mhla)
        # with the current count of their matching sibling type before
        # serializing them.
        n_mhit = sum(1 for c in chunk["children"] if c["magic"] == b"mhit")
        n_mhyp = sum(1 for c in chunk["children"] if c["magic"] == b"mhyp")
        for c in chunk["children"]:
            if c["magic"] == b"mhlt":
                struct.pack_into("<I", c["header"], 8, n_mhit)
            elif c["magic"] == b"mhlp":
                struct.pack_into("<I", c["header"], 8, n_mhyp)
        body = bytearray()
        for c in chunk["children"]:
            _serialize_chunk(c, body)
    else:
        # Leaf chunk: payload is whatever came after the fixed header.
        body = bytearray(chunk["payload"])

    if magic in _HAS_TOTAL_LEN:
        struct.pack_into("<I", header, 8, chunk["hdr_len"] + len(body))

    out += bytes(header)
    out += bytes(body)


def _find_descendants(chunk: dict, magic: bytes) -> List[dict]:
    """DFS collect every descendant with the given magic."""
    out: List[dict] = []
    for c in chunk["children"]:
        if c["magic"] == magic:
            out.append(c)
        out.extend(_find_descendants(c, magic))
    return out


# ------------------------------------------------------------------ #
# High-level entry points                                             #
# ------------------------------------------------------------------ #


def build_itunesdb(template: bytes, tracks: List[Track],
                   ipod_name: str = "iPod") -> bytes:
    """Build an iTunesDB by splicing `tracks` into an empty-DB `template`.

    The template must be a valid iTunesDB produced by iTunes or the iPod
    itself — the function reuses its mhbd header, every mhsd section it
    contains, and the master playlist's sort-index mhods. We only insert
    our mhits/mhips and update size/count fields.

    If the template already has tracks in its mhlt or mhips in its master
    playlists, they are preserved and ours are appended.
    """
    root = _parse_chunk(template, 0, len(template))
    if root is None or root["magic"] != b"mhbd":
        raise ValueError("template is not a valid iTunesDB (no mhbd root)")

    # Find the track dataset (mhsd with type=1) and insert mhits as
    # children of that mhsd. The mhlt header sibling already exists.
    track_mhsds = [c for c in root["children"]
                   if c["magic"] == b"mhsd"
                   and struct.unpack_from("<I", c["header"], 12)[0] == MHSD_TRACKS]
    if not track_mhsds:
        raise ValueError("template has no track mhsd (type=1)")
    track_mhsd = track_mhsds[0]
    for t in tracks:
        mhit_bytes = _build_mhit(t)
        track_mhsd["children"].append(_parse_chunk(mhit_bytes, 0, len(mhit_bytes)))

    # Rename every master playlist to the chosen iPod name, and append
    # one mhip per track. playlist_item_id is unique within each mhyp,
    # we restart from (existing + 1).
    for mhyp in _find_descendants(root, b"mhyp"):
        is_master = struct.unpack_from("<I", mhyp["header"], 0x14)[0]
        if not is_master:
            continue

        _rename_master_playlist(mhyp, ipod_name)

        existing_mhips = sum(1 for c in mhyp["children"] if c["magic"] == b"mhip")
        for idx, t in enumerate(tracks, start=existing_mhips + 1):
            mhip_bytes = _build_mhip(t.track_id, playlist_item_id=idx)
            mhyp["children"].append(_parse_chunk(mhip_bytes, 0, len(mhip_bytes)))
        # mhyp.n_mhips at offset 0x10
        n_mhips = sum(1 for c in mhyp["children"] if c["magic"] == b"mhip")
        struct.pack_into("<I", mhyp["header"], 0x10, n_mhips)

    out = bytearray()
    _serialize_chunk(root, out)
    return bytes(out)


def _rename_master_playlist(mhyp: dict, new_name: str) -> None:
    """Replace the playlist-name mhod inside a mhyp.

    iTunes DBs use mhod type 1 for the playlist name on older iPods
    (Nano 1G/2G, Mini, Video 5G), and type 101 on newer firmware. We
    replace whichever one the template contains, preserving its type
    so the rest of the mhyp still matches what the iPod expects.
    """
    for i, c in enumerate(mhyp["children"]):
        if c["magic"] != b"mhod":
            continue
        mhod_type = struct.unpack_from("<I", c["header"], 12)[0]
        if mhod_type in (MHOD_TITLE, MHOD_PLAYLIST_NAME):
            new_bytes = _build_mhod_string(mhod_type, new_name)
            mhyp["children"][i] = _parse_chunk(new_bytes, 0, len(new_bytes))
            return


def write_itunesdb(path: str, tracks: List[Track], ipod_name: str = "iPod",
                   template: Optional[bytes] = None) -> None:
    """Build and atomically write the iTunesDB to `path`.

    `template` is the bytes of an empty iTunesDB that the iPod/iTunes
    produced. Callers typically keep a copy next to the DB and reuse
    it on every sync.
    """
    if template is None:
        raise ValueError("write_itunesdb requires a template DB")
    data = build_itunesdb(template, tracks, ipod_name)
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
