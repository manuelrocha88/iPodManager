"""Entry point for `python -m ipod_manager` and the `ipodmanager` script.

Launches the desktop GUI. For command-line use, see `ipodmanager-cli`.
"""

from __future__ import annotations

import sys


def main() -> int:
    try:
        from .gui.app import run
    except ImportError as exc:  # pragma: no cover — surfaced to user
        print("The GUI needs the `customtkinter` package. Install it with:\n"
              "    pip install customtkinter pillow\n"
              f"({exc})", file=sys.stderr)
        return 2
    run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
