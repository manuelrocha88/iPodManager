"""Smoke tests for the iTunesDB binary writer (template splice mode)."""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

from ipod_manager import itunesdb


TEMPLATE_PATH = Path(__file__).resolve().parents[1] / "ipod_manager" / "templates" / "empty_nano1g.bin"


@pytest.fixture(scope="module")
def template() -> bytes:
    return TEMPLATE_PATH.read_bytes()


def _find_all(buf: bytes, magic: bytes) -> list[int]:
    out: list[int] = []
    i = 0
    while True:
        j = buf.find(magic, i)
        if j < 0:
            return out
        out.append(j)
        i = j + 1


def test_empty_splice_preserves_template(template: bytes):
    """Splicing zero tracks into the template must still produce a
    structurally-valid DB (same mhbd, same n_mhsds, zero tracks)."""
    data = itunesdb.build_itunesdb(template, tracks=[], ipod_name="Empty")

    assert data[:4] == b"mhbd"
    hdr_len, total_len = struct.unpack("<II", data[4:12])
    assert hdr_len == itunesdb.MHBD_HDR
    assert total_len == len(data)

    # Same number of mhsd sections as the template.
    n_mhsd_template = struct.unpack_from("<I", template, 0x14)[0]
    n_mhsd_result = struct.unpack_from("<I", data, 0x14)[0]
    assert n_mhsd_result == n_mhsd_template

    # No mhits anywhere.
    assert b"mhit" not in data

    # Master playlist name was replaced.
    assert b"\x45\x00\x6d\x00\x70\x00\x74\x00\x79\x00" in data  # "Empty" in utf-16-le


def test_single_track_splice(template: bytes):
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
    data = itunesdb.build_itunesdb(template, [track], ipod_name="MyPod")

    mhit = data.find(b"mhit")
    assert mhit > 0
    assert struct.unpack("<I", data[mhit + 0x10:mhit + 0x14])[0] == 1
    assert data[mhit + 0x18:mhit + 0x1C] == b"MPEG"
    assert struct.unpack("<I", data[mhit + 0x24:mhit + 0x28])[0] == 4_500_000
    assert struct.unpack("<I", data[mhit + 0x28:mhit + 0x2C])[0] == 180_000
    assert struct.unpack("<I", data[mhit + 0x38:mhit + 0x3C])[0] == 192
    # Samplerate is Hz << 16 (fixed-point 16.16).
    assert struct.unpack("<I", data[mhit + 0x3C:mhit + 0x40])[0] == 44100 << 16
    assert struct.unpack("<Q", data[mhit + 0x70:mhit + 0x78])[0] == 0x1122334455667788
    # Media type at 0xD0 must be 1 (audio).
    assert struct.unpack("<I", data[mhit + 0xD0:mhit + 0xD4])[0] == 1

    # Every master mhyp (template has several — podcasts/library in
    # both v1 and v2 playlist datasets) now contains an mhip pointing
    # at track 1.
    for mhyp_off in _find_all(data, b"mhyp"):
        is_master = struct.unpack_from("<I", data, mhyp_off + 0x14)[0]
        if not is_master:
            continue
        n_mhips = struct.unpack_from("<I", data, mhyp_off + 0x10)[0]
        assert n_mhips >= 1


def test_three_tracks_counts_are_consistent(template: bytes):
    tracks = [
        itunesdb.Track(track_id=i, dbid=i << 32,
                       ipod_path=f":iPod_Control:Music:F00:T{i:02d}.mp3",
                       title=f"Track {i}", artist="A", album="B",
                       file_ext="mp3", length_ms=1000, file_size=100,
                       bitrate_kbps=128, sample_rate_hz=44100)
        for i in (1, 2, 3)
    ]
    data = itunesdb.build_itunesdb(template, tracks, ipod_name="iPod")

    # mhlt reports 3 tracks.
    mhlt = data.find(b"mhlt")
    assert struct.unpack_from("<I", data, mhlt + 8)[0] == 3

    # Three mhit entries total.
    assert len(_find_all(data, b"mhit")) == 3


def test_mac_time_round_trip():
    assert itunesdb.unix_to_mac_time(0) == itunesdb.MAC_EPOCH_OFFSET
    assert itunesdb.unix_to_mac_time(1_700_000_000) == 1_700_000_000 + itunesdb.MAC_EPOCH_OFFSET
