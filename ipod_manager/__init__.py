"""iPodManager — a free, cross-platform music manager for classic iPods.

Works with iPods that modern iTunes/Finder has dropped:
  - iPod Nano 1G/2G (A1137 / A1199)
  - iPod Video 5G (A1136)
  - iPod Mini
  - iPod Classic 6G/7G

Design: tracks are copied into /iPod_Control/Music/F##/XXXX.<ext>, the
iTunesDB is regenerated from scratch each sync, and a sidecar JSON on
the iPod records our view of the library so future sessions can resume.
"""

__version__ = "0.1.0"
