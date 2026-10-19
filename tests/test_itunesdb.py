"""Smoke tests for the iTunesDB binary writer."""

from __future__ import annotations

import struct

import pytest

from ipod_manager import itunesdb


def _find_all(buf: bytes, magic: bytes) -> list[int]:
    out: list[int] = []
    i = 0
    while True:
        j = buf.find(magic, i)
        if j < 0:
            return out
        out.append(j)
        i = j + 1


def test_empty_db_is_valid():
    data = itunesdb.build_itunesdb(tracks=[], ipod_name="Empty")

    # Overall mhbd fields should be internally consistent.
    assert data[:4] == b"mhbd"
    hdr_len, total_len = struct.unpack("<II", data[4:12])
    assert hdr_len == itunesdb.MHBD_HDR
    assert total_len == len(data)

    # Three datasets expected: tracks + 2 playlist versions.
    n_children = struct.unpack("<I", data[0x14:0x18])[0]
    assert n_children == 3

    # Even an empty library has a master playlist with the name mhod.
    assert b"mhyp" in data
    assert b"\x45\x00\x6d\x00\x70\x00\x74\x00\x79\x00" in data  # "Empty" in utf-16-le


def test_single_track_roundtrip():
    track = itunesdb.Track(
        track_id=1,
        dbid=0x1122334455667788,
        ipod_path=":iPod_Control:Music:F00:ABCD.mp3",
        title="Hello World",
        artist="Testing",
        album="UnitTest",
        file_ext="mp3",
        length_ms=180_000,
        file_size=4_500_000,
        bitrate_kbps=192,
        sample_rate_hz=44100,
    )
    data = itunesdb.build_itunesdb([track], ipod_name="MyPod")

    # mhit should be present, with correct id and filetype.
    mhit = data.find(b"mhit")
    assert mhit > 0
    assert struct.unpack("<I", data[mhit + 0x10:mhit + 0x14])[0] == 1
    assert data[mhit + 0x18:mhit + 0x1C] == b"MPEG"

    # Numeric fields land at the documented offsets.
    assert struct.unpack("<I", data[mhit + 0x24:mhit + 0x28])[0] == 4_500_000
    assert struct.unpack("<I", data[mhit + 0x28:mhit + 0x2C])[0] == 180_000
    assert struct.unpack("<I", data[mhit + 0x38:mhit + 0x3C])[0] == 192
    assert struct.unpack("<H", data[mhit + 0x3E:mhit + 0x40])[0] == 44100
    assert struct.unpack("<Q", data[mhit + 0x70:mhit + 0x78])[0] == 0x1122334455667788

    # Both playlist datasets reference the track id.
    mhips = _find_all(data, b"mhip")
    assert len(mhips) == 2
    for off in mhips:
        tid = struct.unpack("<I", data[off + 0x1C:off + 0x20])[0]
        assert tid == 1


def test_three_tracks_counts_are_consistent():
    tracks = [
        itunesdb.Track(track_id=i, dbid=i << 32,
                       ipod_path=f":iPod_Control:Music:F00:T{i:02d}.mp3",
                       title=f"Track {i}", artist="A", album="B",
                       file_ext="mp3", length_ms=1000, file_size=100,
                       bitrate_kbps=128, sample_rate_hz=44100)
        for i in (1, 2, 3)
    ]
    data = itunesdb.build_itunesdb(tracks, ipod_name="iPod")

    # mhlt reports 3 tracks.
    mhlt = data.find(b"mhlt")
    assert struct.unpack("<I", data[mhlt + 8:mhlt + 12])[0] == 3

    # Each master mhyp has 3 mhips.
    mhyp_offsets = _find_all(data, b"mhyp")
    assert len(mhyp_offsets) == 2
    for off in mhyp_offsets:
        n_mhips = struct.unpack("<I", data[off + 0x10:off + 0x14])[0]
        is_master = struct.unpack("<I", data[off + 0x14:off + 0x18])[0]
        assert n_mhips == 3
        assert is_master == 1


def test_mac_time_round_trip():
    assert itunesdb.unix_to_mac_time(0) == itunesdb.MAC_EPOCH_OFFSET
    assert itunesdb.unix_to_mac_time(1_700_000_000) == 1_700_000_000 + itunesdb.MAC_EPOCH_OFFSET
