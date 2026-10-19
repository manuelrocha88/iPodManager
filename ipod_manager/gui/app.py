"""iPodManager — desktop UI (CustomTkinter).

Three panels side-by-side:

  +------------------------------------------------------------------+
  |  [iPodManager]           [ Refresh ]  [ Add… ]  [ Sync ]  [⏏]    |
  +----------------+-------------------------------------------------+
  |  Device        |                                                 |
  |  (card)        |   Library table                                 |
  |  name, size,   |   #  Title     Artist    Album    Len  [x]      |
  |  used, bars    |   ...                                           |
  |                |                                                 |
  |  Rename iPod…  |                                                 |
  +----------------+-------------------------------------------------+
  |  Status bar: "Added 3 files. iTunesDB written."                  |
  +------------------------------------------------------------------+
"""

from __future__ import annotations

import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox
from typing import Callable, List, Optional

import customtkinter as ctk

from .. import __version__
from ..device import (IPod, VolumeRenameError, eject, find_ipod, list_ipods,
                      rename_volume)
from ..library import Library, LibraryEntry

ctk.set_appearance_mode("System")   # follows OS light/dark setting
ctk.set_default_color_theme("blue")


AUDIO_EXTS = {".mp3", ".m4a", ".aac", ".wav", ".aif", ".aiff"}

# ------------------------------------------------------------------ #
# Formatting helpers                                                  #
# ------------------------------------------------------------------ #


def fmt_bytes(n: int) -> str:
    f = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if f < 1024:
            return f"{f:,.1f} {unit}"
        f /= 1024
    return f"{f:,.1f} PB"


def fmt_time(ms: int) -> str:
    s = ms // 1000
    return f"{s // 60}:{s % 60:02d}"


# ------------------------------------------------------------------ #
# Main window                                                         #
# ------------------------------------------------------------------ #


class App(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()
        self.title("iPodManager")
        self.geometry("1100x700")
        self.minsize(900, 580)

        self.pod: Optional[IPod] = None
        self.library: Optional[Library] = None
        self._dirty = False     # true when sidecar/db differs from on-disk

        self._build_layout()
        self._refresh_device()

        # Watch for iPod mount/unmount in the background.
        self._watch_stop = threading.Event()
        threading.Thread(target=self._watch_loop, daemon=True).start()

    # ------------------------------------------------------------- #
    # Layout                                                         #
    # ------------------------------------------------------------- #

    def _build_layout(self) -> None:
        self.grid_rowconfigure(1, weight=1)
        self.grid_columnconfigure(1, weight=1)

        # ---- Toolbar ---------------------------------------------- #
        toolbar = ctk.CTkFrame(self, corner_radius=0, height=56)
        toolbar.grid(row=0, column=0, columnspan=2, sticky="ew")
        toolbar.grid_columnconfigure(1, weight=1)

        title = ctk.CTkLabel(
            toolbar, text="  iPodManager",
            font=ctk.CTkFont(size=18, weight="bold"),
        )
        title.grid(row=0, column=0, padx=16, pady=10, sticky="w")

        btn_row = ctk.CTkFrame(toolbar, fg_color="transparent")
        btn_row.grid(row=0, column=2, padx=12, pady=8, sticky="e")

        self.refresh_btn = ctk.CTkButton(
            btn_row, text="Refresh", width=92, command=self._refresh_device,
        )
        self.refresh_btn.grid(row=0, column=0, padx=4)

        self.add_btn = ctk.CTkButton(
            btn_row, text="Add music…", width=120,
            command=self._on_add_files,
        )
        self.add_btn.grid(row=0, column=1, padx=4)

        self.folder_btn = ctk.CTkButton(
            btn_row, text="Add folder…", width=120,
            command=self._on_add_folder,
        )
        self.folder_btn.grid(row=0, column=2, padx=4)

        self.sync_btn = ctk.CTkButton(
            btn_row, text="Sync iPod", width=110,
            fg_color="#2fa65c", hover_color="#258f4e",
            command=self._on_sync,
        )
        self.sync_btn.grid(row=0, column=3, padx=4)

        self.eject_btn = ctk.CTkButton(
            btn_row, text="⏏  Eject", width=92,
            fg_color="#8e8e93", hover_color="#6e6e73",
            command=self._on_eject,
        )
        self.eject_btn.grid(row=0, column=4, padx=4)

        # ---- Device panel (left) ---------------------------------- #
        self.side = ctk.CTkFrame(self, width=260, corner_radius=0)
        self.side.grid(row=1, column=0, sticky="nsw")
        self.side.grid_propagate(False)
        self.side.grid_rowconfigure(99, weight=1)

        self.device_card = ctk.CTkFrame(self.side, corner_radius=12)
        self.device_card.grid(row=0, column=0, padx=16, pady=16, sticky="new")

        self.device_icon = ctk.CTkLabel(
            self.device_card, text="🎧",
            font=ctk.CTkFont(size=42),
        )
        self.device_icon.grid(row=0, column=0, padx=16, pady=(14, 2))

        self.device_name = ctk.CTkLabel(
            self.device_card, text="No iPod",
            font=ctk.CTkFont(size=16, weight="bold"),
        )
        self.device_name.grid(row=1, column=0, padx=16, pady=(0, 2))

        self.device_mount = ctk.CTkLabel(
            self.device_card, text="",
            font=ctk.CTkFont(size=11), text_color="gray",
        )
        self.device_mount.grid(row=2, column=0, padx=16, pady=(0, 10))

        self.capacity_bar = ctk.CTkProgressBar(self.device_card, width=200, height=10)
        self.capacity_bar.set(0)
        self.capacity_bar.grid(row=3, column=0, padx=16, pady=(2, 6))

        self.capacity_label = ctk.CTkLabel(
            self.device_card, text="—",
            font=ctk.CTkFont(size=12),
        )
        self.capacity_label.grid(row=4, column=0, padx=16, pady=(0, 14))

        rename_card = ctk.CTkFrame(self.side, corner_radius=12)
        rename_card.grid(row=1, column=0, padx=16, pady=(0, 16), sticky="new")

        ctk.CTkLabel(
            rename_card, text="Rename iPod",
            font=ctk.CTkFont(size=12, weight="bold"),
        ).grid(row=0, column=0, padx=12, pady=(10, 2), sticky="w")

        self.name_entry = ctk.CTkEntry(rename_card, placeholder_text="e.g. Manuel's iPod", width=210)
        self.name_entry.grid(row=1, column=0, padx=12, pady=(2, 6))

        self.volume_check = ctk.CTkCheckBox(
            rename_card, text="Also rename volume label",
            font=ctk.CTkFont(size=11),
        )
        self.volume_check.grid(row=2, column=0, padx=12, pady=(0, 4), sticky="w")

        ctk.CTkButton(
            rename_card, text="Apply", width=90, command=self._on_rename,
        ).grid(row=3, column=0, padx=12, pady=(2, 12), sticky="e")

        self.footer = ctk.CTkLabel(
            self.side, text=f"v{__version__}  ·  free & open-source",
            font=ctk.CTkFont(size=10), text_color="gray",
        )
        self.footer.grid(row=100, column=0, padx=16, pady=10, sticky="sw")

        # ---- Library panel (right) -------------------------------- #
        self.main = ctk.CTkFrame(self, corner_radius=0)
        self.main.grid(row=1, column=1, sticky="nsew")
        self.main.grid_rowconfigure(1, weight=1)
        self.main.grid_columnconfigure(0, weight=1)

        hdr = ctk.CTkFrame(self.main, fg_color="transparent", height=36)
        hdr.grid(row=0, column=0, sticky="ew", padx=16, pady=(14, 6))
        hdr.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(
            hdr, text="Library", font=ctk.CTkFont(size=15, weight="bold"),
        ).grid(row=0, column=0, sticky="w")
        self.track_count_label = ctk.CTkLabel(
            hdr, text="0 tracks", font=ctk.CTkFont(size=12), text_color="gray",
        )
        self.track_count_label.grid(row=0, column=2, sticky="e")

        self.table = LibraryTable(self.main, on_remove=self._on_remove_track)
        self.table.grid(row=1, column=0, sticky="nsew", padx=16, pady=(0, 10))

        # ---- Status bar ------------------------------------------- #
        self.statusbar = ctk.CTkLabel(
            self, text="Ready.", anchor="w", height=24,
            font=ctk.CTkFont(size=11), text_color="gray",
        )
        self.statusbar.grid(row=2, column=0, columnspan=2, sticky="ew",
                            padx=16, pady=(0, 8))

    # ------------------------------------------------------------- #
    # Device state                                                   #
    # ------------------------------------------------------------- #

    def _refresh_device(self) -> None:
        pod = find_ipod()
        self.pod = pod
        if pod is None:
            self.library = None
            self._render_no_device()
            return
        self.library = Library.load(pod)
        self._render_device()
        self._render_library()
        self._status(f"Loaded library ({len(self.library.entries)} tracks).")

    def _render_no_device(self) -> None:
        self.device_name.configure(text="No iPod")
        self.device_mount.configure(text="Plug one in to begin.")
        self.capacity_label.configure(text="—")
        self.capacity_bar.set(0)
        self.name_entry.configure(state="disabled")
        self.add_btn.configure(state="disabled")
        self.folder_btn.configure(state="disabled")
        self.sync_btn.configure(state="disabled")
        self.eject_btn.configure(state="disabled")
        self.table.set_rows([])
        self.track_count_label.configure(text="")

    def _render_device(self) -> None:
        assert self.pod and self.library
        self.device_name.configure(text=self.library.ipod_name)
        self.device_mount.configure(text=str(self.pod.mountpoint))
        used = self.pod.used_bytes
        total = max(self.pod.total_bytes, 1)
        self.capacity_bar.set(used / total)
        self.capacity_label.configure(
            text=f"{fmt_bytes(used)} / {fmt_bytes(total)} used",
        )
        self.name_entry.configure(state="normal")
        self.name_entry.delete(0, tk.END)
        self.name_entry.insert(0, self.library.ipod_name)
        for b in (self.add_btn, self.folder_btn, self.sync_btn, self.eject_btn):
            b.configure(state="normal")

    def _render_library(self) -> None:
        if not self.library:
            return
        self.table.set_rows(self.library.entries)
        n = len(self.library.entries)
        self.track_count_label.configure(text=f"{n} track{'s' if n != 1 else ''}")

    # ------------------------------------------------------------- #
    # Actions                                                        #
    # ------------------------------------------------------------- #

    def _on_add_files(self) -> None:
        if not self.library:
            return
        paths = filedialog.askopenfilenames(
            title="Add music to iPod",
            filetypes=[
                ("Audio files", " ".join(f"*{e}" for e in AUDIO_EXTS)),
                ("All files", "*.*"),
            ],
        )
        if not paths:
            return
        self._add_paths([Path(p) for p in paths])

    def _on_add_folder(self) -> None:
        if not self.library:
            return
        folder = filedialog.askdirectory(title="Add every song in a folder")
        if not folder:
            return
        root = Path(folder)
        files = sorted(p for p in root.rglob("*") if p.suffix.lower() in AUDIO_EXTS)
        if not files:
            self._status("No audio files found in that folder.")
            return
        self._add_paths(files)

    def _add_paths(self, paths: List[Path]) -> None:
        assert self.library
        self._set_busy(True, f"Adding {len(paths)} file(s)…")

        def worker() -> None:
            added, failed = 0, 0
            for p in paths:
                try:
                    self.library.add_track(p)        # type: ignore[union-attr]
                    added += 1
                except Exception as exc:
                    failed += 1
                    self.after(0, lambda e=exc, pp=p: self._status(
                        f"Failed: {pp.name} ({e})"))
            try:
                self.library.sync()                   # type: ignore[union-attr]
            except Exception as exc:
                self.after(0, lambda e=exc: messagebox.showerror(
                    "Sync failed", str(e)))
            self.after(0, self._render_library)
            self.after(0, lambda: self._set_busy(False,
                f"Added {added}, failed {failed}. iTunesDB written."))

        threading.Thread(target=worker, daemon=True).start()

    def _on_remove_track(self, entry: LibraryEntry) -> None:
        if not self.library:
            return
        if not messagebox.askyesno(
            "Remove track",
            f"Remove '{entry.title}' from the iPod?",
        ):
            return
        self.library.remove_track(entry.track_id)
        try:
            self.library.sync()
        except Exception as exc:
            messagebox.showerror("Sync failed", str(exc))
            return
        self._render_library()
        self._status(f"Removed '{entry.title}'.")

    def _on_sync(self) -> None:
        if not self.library:
            return
        self._set_busy(True, "Writing iTunesDB…")

        def worker() -> None:
            try:
                self.library.sync()        # type: ignore[union-attr]
                self.after(0, lambda: self._set_busy(False,
                    f"iTunesDB written ({len(self.library.entries)} tracks).")
                )
            except Exception as exc:
                self.after(0, lambda e=exc: self._set_busy(False, f"Sync failed: {e}"))

        threading.Thread(target=worker, daemon=True).start()

    def _on_rename(self) -> None:
        if not self.library or not self.pod:
            return
        name = self.name_entry.get().strip()
        if not name:
            self._status("Enter a name first.")
            return
        self.library.rename(name)
        try:
            self.library.sync()
        except Exception as exc:
            messagebox.showerror("Sync failed", str(exc))
            return
        if self.volume_check.get():
            try:
                self.pod = rename_volume(self.pod, name)
                self.library.pod = self.pod
            except VolumeRenameError as exc:
                messagebox.showerror("Rename volume failed", str(exc))
        self._render_device()
        self._status(f"iPod renamed to '{name}'.")

    def _on_eject(self) -> None:
        if not self.pod:
            return
        try:
            eject(self.pod)
            self._status(f"Ejected {self.pod.name}.")
            self.pod = None
            self.library = None
            self._render_no_device()
        except Exception as exc:
            messagebox.showerror("Eject failed", str(exc))

    # ------------------------------------------------------------- #
    # Background watchdog + helpers                                  #
    # ------------------------------------------------------------- #

    def _watch_loop(self) -> None:
        last_mount = self.pod.mountpoint if self.pod else None
        while not self._watch_stop.is_set():
            time.sleep(2.0)
            pods = list_ipods()
            current = pods[0].mountpoint if pods else None
            if current != last_mount:
                last_mount = current
                self.after(0, self._refresh_device)

    def _set_busy(self, busy: bool, message: str = "") -> None:
        state = "disabled" if busy else "normal"
        for b in (self.add_btn, self.folder_btn, self.sync_btn,
                  self.eject_btn, self.refresh_btn):
            b.configure(state=state)
        if message:
            self._status(message)

    def _status(self, text: str) -> None:
        self.statusbar.configure(text=text)

    def destroy(self) -> None:
        self._watch_stop.set()
        super().destroy()


# ------------------------------------------------------------------ #
# Library table — a tk.Treeview styled to match CustomTkinter        #
# ------------------------------------------------------------------ #


from tkinter import ttk


class LibraryTable(ctk.CTkFrame):
    """A sortable, scrollable library view with a right-click 'remove'."""

    COLS = (
        ("id",     "#",       48,  "center"),
        ("title",  "Title",   260, "w"),
        ("artist", "Artist",  180, "w"),
        ("album",  "Album",   180, "w"),
        ("length", "Length",   72, "center"),
        ("size",   "Size",     88, "center"),
    )

    def __init__(self, parent: tk.Misc,
                 on_remove: Callable[[LibraryEntry], None]) -> None:
        super().__init__(parent, corner_radius=12)
        self._on_remove = on_remove
        self._entries: List[LibraryEntry] = []

        self._apply_treeview_style()

        self.tree = ttk.Treeview(
            self,
            columns=[c[0] for c in self.COLS],
            show="headings",
            selectmode="browse",
            style="iPod.Treeview",
        )
        for key, title, width, anchor in self.COLS:
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, anchor=anchor, stretch=(key in ("title", "artist", "album")))

        self.scroll = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=self.scroll.set)

        self.tree.grid(row=0, column=0, sticky="nsew", padx=(8, 0), pady=8)
        self.scroll.grid(row=0, column=1, sticky="ns", padx=(0, 8), pady=8)
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)

        self.tree.bind("<Delete>", self._handle_delete_key)
        self.tree.bind("<BackSpace>", self._handle_delete_key)
        self.tree.bind("<Button-3>", self._handle_right_click)      # Linux / Windows
        self.tree.bind("<Button-2>", self._handle_right_click)      # macOS

    def _apply_treeview_style(self) -> None:
        style = ttk.Style()
        bg = self._apply_appearance_mode(ctk.ThemeManager.theme["CTkFrame"]["fg_color"])
        text = self._apply_appearance_mode(ctk.ThemeManager.theme["CTkLabel"]["text_color"])
        sel = self._apply_appearance_mode(ctk.ThemeManager.theme["CTkButton"]["fg_color"])
        style.theme_use("default")
        style.configure(
            "iPod.Treeview",
            background=bg, fieldbackground=bg, foreground=text,
            rowheight=26, borderwidth=0,
        )
        style.configure("iPod.Treeview.Heading", font=("", 11, "bold"))
        style.map("iPod.Treeview",
                  background=[("selected", sel)],
                  foreground=[("selected", "white")])

    def set_rows(self, entries: List[LibraryEntry]) -> None:
        self._entries = list(entries)
        self.tree.delete(*self.tree.get_children())
        for e in self._entries:
            self.tree.insert(
                "", "end", iid=str(e.track_id),
                values=(
                    e.track_id,
                    e.title or "(untitled)",
                    e.artist or "",
                    e.album or "",
                    fmt_time(e.length_ms) if e.length_ms else "—",
                    fmt_bytes(e.file_size) if e.file_size else "—",
                ),
            )

    def _selected_entry(self) -> Optional[LibraryEntry]:
        sel = self.tree.selection()
        if not sel:
            return None
        tid = int(sel[0])
        for e in self._entries:
            if e.track_id == tid:
                return e
        return None

    def _handle_delete_key(self, _event) -> None:
        e = self._selected_entry()
        if e:
            self._on_remove(e)

    def _handle_right_click(self, event) -> None:
        iid = self.tree.identify_row(event.y)
        if not iid:
            return
        self.tree.selection_set(iid)
        e = self._selected_entry()
        if not e:
            return
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label=f"Remove '{e.title}'", command=lambda: self._on_remove(e))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()


# ------------------------------------------------------------------ #
# Entry point                                                         #
# ------------------------------------------------------------------ #


def run() -> None:
    app = App()
    app.mainloop()


if __name__ == "__main__":
    run()
