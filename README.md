# iPodManager

**Free, cross-platform music manager for classic iPods.**
Works with the iPods that modern iTunes / Finder / Music.app have dropped — iPod Nano 1G/2G, iPod Mini, iPod Video 5G, iPod Classic 6G/7G.

- **No account, no sync lock-in, no cloud.** Your music is just files.
- **Pure Python, no C deps.** Runs on macOS, Windows, and Linux.
- **Native desktop app** built with CustomTkinter — no browser, no server.
- **MIT licensed.**

---

## Why this exists

Modern macOS removed the ability to sync music to classic iPods, and iTunes on Windows barely limps along. If you've got a working Nano or a Classic in a drawer and want to put music on it, your options were: install an old iTunes in a VM, pay for a shareware "iPod transfer tool", or give up.

iPodManager writes the iPod's `iTunesDB` database directly — it's a well-documented binary format and this project is a pure-Python reimplementation of just the parts needed to put tracks on the device. No `libgpod`, no Qt bundle, no 500 MB download.

## Supported devices

Anything that uses the "classic" `iTunesDB` format without the FirewireGUID hash:

| Model | USB id suffix | Status |
|---|---|---|
| iPod Mini 1G/2G | A1051 / A1137 | ✅ |
| iPod Nano 1G | A1137 | ✅ (tested) |
| iPod Nano 2G | A1199 | ✅ |
| iPod Nano 3G/4G | A1236 / A1285 | ✅ |
| iPod Video 5G / 5.5G | A1136 | ✅ |
| iPod Classic 6G/6.5G/7G | A1238 | ⚠️ needs the hash on 6G+; untested |
| iPod Nano 5G/6G/7G | A1320 / A1366 / A1446 | ⚠️ newer DB format; untested |
| iPod Touch / iPhone | — | ❌ (uses a totally different sync stack) |

If you have a model that isn't listed, try it — the worst that can happen is the iPod ignores the DB until you plug it back into iTunes.

## Install

```bash
pip install git+https://github.com/manuelrocha88/iPodManager.git
```

Or clone and run from source:

```bash
git clone https://github.com/manuelrocha88/iPodManager.git
cd iPodManager
pip install -e .
```

Dependencies (installed automatically): `mutagen`, `customtkinter`, `pillow`, `psutil`.

Python 3.9+ required.

## Launch the app

```bash
ipodmanager        # desktop GUI
```

Or from Python:

```bash
python -m ipod_manager
```

Plug in your iPod, wait for it to mount, click **Add music…** or **Add folder…**, then hit **Sync iPod**. Eject with the ⏏ button when you're done.

## Command line

Everything the GUI does is also available on the CLI:

```bash
ipodmanager-cli devices                        # list connected iPods
ipodmanager-cli info                           # size + track count
ipodmanager-cli list                           # library contents
ipodmanager-cli add ~/Music/some-album         # add a file or whole folder
ipodmanager-cli remove 42                      # delete track by id
ipodmanager-cli rename "Manuel's iPod"         # change the library name
ipodmanager-cli rename "MYIPOD" --volume       # also rename the FAT32 volume
ipodmanager-cli sync                           # rewrite the iTunesDB
ipodmanager-cli eject
```

Pass `--mount /Volumes/IPOD` to any command if autodetection fails.

## How it works

iPodManager treats the iPod as a FAT32 drive. For every track you add:

1. The audio file is copied to `iPod_Control/Music/F##/XXXX.ext` with a random 4-char name.
2. A sidecar JSON (`iPod_Control/iTunes/ipodmanager.json`) records the title/artist/album/path.
3. On sync, the `iTunesDB` binary is regenerated from scratch from that sidecar.

This means the sidecar is the source of truth — unlike iTunes, iPodManager doesn't parse Apple's binary DB back into its model. The DB is always a build output. If anything goes wrong, delete the sidecar and the next sync starts fresh.

## FAQ

**Will this break tracks that are already on the iPod from iTunes?**
Yes, the first sync rewrites the `iTunesDB` based only on what iPodManager knows about. Back up the DB first if you care: `cp /Volumes/YOUR-IPOD/iPod_Control/iTunes/iTunesDB iTunesDB.bak`.

**Can it read / preserve existing tracks?**
Not yet. A one-time "import existing library" step is on the roadmap.

**Does it work on Windows / Linux?**
Yes, the whole thing is pure Python. The only platform-specific piece is `rename --volume` which uses `diskutil` on macOS and `label` on Windows; on Linux you need to unmount the iPod and run `mlabel` yourself.

**Why no iPod Touch / iPhone support?**
Those devices use the iOS sync protocol (`libimobiledevice`), which has nothing to do with the `iTunesDB` format. Different project entirely.

## Troubleshooting

**macOS: "macOS 26 (2601) or later required" when launching the GUI.**
Your Python was linked against an old Tcl/Tk. Install a Python version with modern Tk:
```bash
brew install python@3.11 python-tk@3.11
python3.11 -m venv .venv
./.venv/bin/pip install git+https://github.com/manuelrocha88/iPodManager.git
./.venv/bin/ipodmanager
```

**No iPod detected.**
Make sure the iPod is in **disk mode** and mounts as a drive (you should see it in Finder / Explorer). First-generation Nano and Mini mount automatically; some older firmwares need you to force disk mode by holding **Menu + Play/Pause** at boot.

**"Could not rename volume" on Linux.**
FAT32 label changes on Linux need the iPod unmounted and `mlabel` from the `mtools` package: `sudo mlabel -i /dev/sdX1 ::NEWNAME`.

**My iPod shows the songs but won't play them.**
Double-check that the iPod isn't in "WPD/MTP" mode (Windows) — it needs to appear as a plain USB mass-storage drive. Also make sure the audio format is one the iPod supports: MP3, AAC/M4A, WAV, or AIFF. Lossless/ALAC and FLAC are not supported on the Mini/Nano 1G.

## Development

```bash
pip install -e '.[dev]'
pytest
```

PRs welcome — especially:
- Importing existing on-device libraries into the sidecar
- Smart playlists / regular playlists
- Cover art (mhii / ArtworkDB)
- FirewireGUID hash for 6G+ Classic and Nano 5G+

## License

MIT — see [LICENSE](LICENSE).
