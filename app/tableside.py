"""
Tableside - meeting notes for Zoom, Teams, or any call on your PC.

Records your speakers and microphone, works out who said what, and writes the
transcript and an optional AI summary to Word and Excel.
"""

from __future__ import annotations

import datetime as dt
import os
import queue
import socket
import sys
import tempfile
import threading
import time
import traceback
import wave
import webbrowser
from pathlib import Path

import tkinter as tk
import tkinter.font as tkfont
from tkinter import filedialog, messagebox, simpledialog, ttk

import numpy as np

import engine as E
import recorder as R
import watcher as W
import worker as WK

RES = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
IS_WINDOWS = sys.platform == "win32"

BRAND = "#E8503A"
BRAND_HOVER = "#CC3F2A"
STOP_RED = "#C62828"
STOP_HOVER = "#A11F1F"
ME_COLOR = "#E8503A"
SPEAKER_COLORS = ["#2F6FEB", "#1F9D74", "#9B51E0", "#D6336C", "#B07D00",
                  "#0E9AA7", "#7A5AF8", "#E8590C", "#5C7CFA", "#2B8A3E"]
PALETTE = {
    "light": dict(bg="#fafafa", side="#f1efee", surface="#ffffff", text="#1b1b1b",
                  muted="#6b6b6b", border="#e3e0de", banner="#fdeee8", sel="#f8d9cf"),
    "dark": dict(bg="#1c1c1c", side="#232120", surface="#242424", text="#f2f2f2",
                 muted="#a3a3a3", border="#3a3836", banner="#3b2620", sel="#5a2c22"),
}
DEFAULT_SETTINGS = {
    "speaker_device": "", "mic_device": "", "your_name": "",
    "whisper_model": "small.en", "detect_speakers": True,
    "theme": "system",
    "opt_audio": True, "opt_transcript": True, "opt_summary": False,
    "ai_provider": "local", "claude_model": E.CLAUDE_MODELS[0], "ollama_model": "llama3.1:8b",
    "guess_names": True,
    "watch_meetings": True, "watch_browsers": True, "auto_stop": True,
    "run_in_background": True, "start_with_windows": True,
    "auto_trim": True, "silence_stop": True, "silence_minutes": 10,
}
BUSY = ("queued", "processing", "summarizing")


def load_settings() -> dict:
    stored = E.load_json(E.SETTINGS_FILE, {})
    s = dict(DEFAULT_SETTINGS)
    s.update({k: v for k, v in stored.items() if k in DEFAULT_SETTINGS})
    if stored.get("settings_version", 1) < 2:   # v2: summaries run on this PC by default
        if s["ai_provider"] == "claude" and not E.get_api_key():
            s["ai_provider"] = "local"
        current = dict(stored, ai_provider=s["ai_provider"], settings_version=2)
        E.save_json(E.SETTINGS_FILE, current)
    return s


def save_settings(s: dict) -> None:
    current = E.load_json(E.SETTINGS_FILE, {})
    current.update(s)
    E.save_json(E.SETTINGS_FILE, current)


def open_path(path) -> None:
    try:
        if IS_WINDOWS:
            os.startfile(str(path))  # type: ignore[attr-defined]
        else:
            webbrowser.open(Path(path).as_uri())
    except Exception:
        pass


track_path = WK.track_path


def speaker_colors(meeting: dict) -> dict:
    colors, i = {E.ME: ME_COLOR}, 0
    for _s, _e, k, _t in meeting.get("segments", []):
        if k not in colors:
            colors[k] = SPEAKER_COLORS[i % len(SPEAKER_COLORS)]
            i += 1
    return colors


unnamed_speakers = WK.unnamed_speakers


# --------------------------------------------------------------------------- #
# Small widgets
# --------------------------------------------------------------------------- #
class PillButton(tk.Button):
    """A flat, coloured button with a hover state (ttk can't recolour buttons)."""
    instances: list = []

    def __init__(self, master, text, command, color=BRAND, hover=BRAND_HOVER, size=11, **kw):
        super().__init__(master, text=text, command=command, bg=color, fg="white",
                         activebackground=hover, activeforeground="white", relief="flat",
                         bd=0, cursor="hand2", font=("Segoe UI Semibold", size),
                         padx=18, pady=8, **kw)
        self._color, self._hover = color, hover
        PillButton.instances.append(self)
        self.bind("<Enter>", lambda e: self.configure(bg=self._hover) if self["state"] != "disabled" else None)
        self.bind("<Leave>", lambda e: self.configure(bg=self._color))
    def _repaint(self):
        try:
            self.configure(bg=self._color, fg="white", activebackground=self._hover,
                           activeforeground="white")
        except tk.TclError:
            pass

    def recolor(self, color, hover):
        self._color, self._hover = color, hover
        self.configure(bg=color, activebackground=hover)


class ReadOnlyText(tk.Text):
    def __init__(self, master, **kw):
        super().__init__(master, wrap="word", relief="flat", bd=0, padx=18, pady=14,
                         highlightthickness=0, cursor="arrow", **kw)
        self.configure(state="disabled")

    def write(self, chunks):
        """chunks: [(text, tags)]"""
        self.configure(state="normal")
        self.delete("1.0", "end")
        for text, tags in chunks:
            self.insert("end", text, tags)
        self.configure(state="disabled")


# --------------------------------------------------------------------------- #
# Main window
# --------------------------------------------------------------------------- #
class App:
    def __init__(self, root: tk.Tk, background: bool = False):
        self.root = root
        self.hidden = background
        self.prev_apps: frozenset = frozenset()
        self.snoozed: set = set()
        self.toast = None
        self.tray = None
        self._bg_tip_shown = False
        self.settings = load_settings()
        self.msgs: queue.Queue = queue.Queue()
        self.jobs: WK.JobRunner | None = None   # started below
        self.meetings: dict[str, dict] = {}
        self.progress: dict[str, tuple] = {}
        self.errors: dict[str, str] = {}
        self.current: str | None = None
        self.session: dict | None = None
        self.recorders: list = []
        self.pa = None
        self._suppress_select = False
        self.dots: list = []   # coloured classic labels to repaint after theme changes

        root.title(E.APP_NAME)
        root.geometry("1060x700")
        root.minsize(900, 600)
        self._load_images()
        self.mode = self._apply_theme()
        self._build()
        self._restyle()
        root.bind("<<ThemeChanged>>", self._theme_changed, add="+")
        self._resume_interrupted()
        self.refresh_library()
        self.show_home()

        # transcription and summaries run in their own process so this window never freezes
        self.jobs = WK.JobRunner(self.post, lambda: self.settings)
        threading.Thread(target=self._watch_loop, daemon=True).start()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(120, self._poll)
        self._start_tray()
        if background:
            root.withdraw()
        else:
            root.after(400, self._first_run)


    # ------------------------------------------------------------------ setup
    def _load_images(self):
        self.img = {}
        for s in (24, 32, 64, 128):
            try:
                self.img[s] = tk.PhotoImage(file=str(RES / f"icon_{s}.png"))
            except tk.TclError:
                self.img[s] = None
        try:
            if IS_WINDOWS:
                self.root.iconbitmap(default=str(RES / "tableside.ico"))
            elif self.img[64]:
                self.root.iconphoto(True, self.img[64])
        except tk.TclError:
            pass

    def _apply_theme(self) -> str:
        mode = self.settings.get("theme", "system")
        if mode == "system":
            try:
                import darkdetect
                mode = "dark" if darkdetect.isDark() else "light"
            except Exception:
                mode = "light"
        try:
            import sv_ttk
            sv_ttk.set_theme(mode)
        except Exception:
            pass
        return mode

    def _restyle(self):
        p = PALETTE[self.mode]
        self.p = p
        st = ttk.Style()
        st.configure("Side.TFrame", background=p["side"])
        st.configure("Side.TLabel", background=p["side"], foreground=p["text"])
        st.configure("SideMuted.TLabel", background=p["side"], foreground=p["muted"], font=("Segoe UI", 9))
        st.configure("Brand.TLabel", background=p["side"], foreground=p["text"], font=("Segoe UI Semibold", 16))
        st.configure("Muted.TLabel", foreground=p["muted"])
        st.configure("Small.TLabel", foreground=p["muted"], font=("Segoe UI", 9))
        st.configure("H1.TLabel", font=("Segoe UI Semibold", 20))
        st.configure("H2.TLabel", font=("Segoe UI Semibold", 13))
        st.configure("Timer.TLabel", font=("Segoe UI Light", 44))
        st.configure("Banner.TFrame", background=p["banner"])
        st.configure("Banner.TLabel", background=p["banner"], foreground=p["text"])
        st.configure("Option.TCheckbutton", font=("Segoe UI Semibold", 11))
        st.configure("Library.Treeview", rowheight=34, background=p["side"],
                     fieldbackground=p["side"], foreground=p["text"], borderwidth=0)
        st.map("Library.Treeview", background=[("selected", p["sel"])],
               foreground=[("selected", p["text"])])
        st.configure("Brand.Horizontal.TProgressbar", background=BRAND)
        st.layout("Library.Treeview.Item", [("Treeitem.padding", {"sticky": "nswe", "children": [
            ("Treeitem.image", {"side": "left", "sticky": ""}),
            ("Treeitem.text", {"sticky": "nswe"})]})])
        st.configure("Library.Treeview", indent=0)
        self._recolor()
        if self.current:
            self.show_meeting(self.current)

    def _recolor(self, _e=None):
        """Colours for classic Tk widgets (the theme only styles ttk ones)."""
        self._recolor_pending = False
        p = self.p
        self.rec_dot.configure(bg=p["bg"])
        # the theme repaints classic Tk widgets when it loads; put our colours back
        for b in list(PillButton.instances):
            if b.winfo_exists():
                b._repaint()
            else:
                PillButton.instances.remove(b)
        for w, color in list(self.dots):
            if w.winfo_exists():
                w.configure(fg=color, bg=p["bg"])
            else:
                self.dots.remove((w, color))
        for t in (self.summary_text, self.transcript_text):
            t.configure(bg=p["surface"], fg=p["text"], selectbackground=p["sel"],
                        selectforeground=p["text"], font=("Segoe UI", 11))
            t.tag_configure("h", font=("Segoe UI Semibold", 13), spacing1=10, spacing3=6)
            t.tag_configure("muted", foreground=p["muted"], font=("Segoe UI", 9))
            t.tag_configure("body", spacing3=14, lmargin1=0, lmargin2=0)
            t.tag_configure("bullet", lmargin1=8, lmargin2=26, spacing3=4)
            t.tag_configure("owner", font=("Segoe UI Semibold", 11))
            t.tag_configure("placeholder", foreground=p["muted"], font=("Segoe UI", 11), spacing1=20)
        self.search.configure(foreground=p["muted"] if self._search_placeholder else p["text"])

    def _theme_changed(self, _e=None):
        if not getattr(self, "_recolor_pending", False):
            self._recolor_pending = True
            self.root.after(100, self._recolor)

    # ----------------------------------------------------------------- layout
    def _build(self):
        self.root.columnconfigure(1, weight=1)
        self.root.rowconfigure(0, weight=1)

        # Sidebar ------------------------------------------------------------
        side = ttk.Frame(self.root, style="Side.TFrame", width=290)
        side.grid(row=0, column=0, sticky="ns")
        side.grid_propagate(False)
        side.columnconfigure(0, weight=1)
        side.rowconfigure(4, weight=1)

        brand = ttk.Frame(side, style="Side.TFrame")
        brand.grid(row=0, column=0, sticky="ew", padx=18, pady=(18, 14))
        ttk.Label(brand, image=self.img[32], style="Side.TLabel").pack(side="left")
        ttk.Label(brand, text="  Tableside", style="Brand.TLabel").pack(side="left")

        self.new_btn = PillButton(side, "＋  New recording", self.on_new_button)
        self.new_btn.grid(row=1, column=0, sticky="ew", padx=16, pady=(0, 14))

        self._search_placeholder = True
        self.search_var = tk.StringVar(value="Search meetings")
        self.search = ttk.Entry(side, textvariable=self.search_var)
        self.search.grid(row=2, column=0, sticky="ew", padx=16, pady=(0, 8))
        self.search.bind("<FocusIn>", self._search_focus)
        self.search.bind("<FocusOut>", self._search_blur)
        self.search_var.trace_add("write", lambda *a: self.refresh_library())

        ttk.Label(side, text="MEETINGS", style="SideMuted.TLabel").grid(
            row=3, column=0, sticky="w", padx=20, pady=(6, 2))

        lib = ttk.Frame(side, style="Side.TFrame")
        lib.grid(row=4, column=0, sticky="nsew", padx=(10, 6))
        lib.rowconfigure(0, weight=1)
        lib.columnconfigure(0, weight=1)
        self.tree = ttk.Treeview(lib, columns=("when",), show="tree", selectmode="browse",
                                 style="Library.Treeview")
        self.tree.column("#0", width=170, stretch=True)
        self.tree.column("when", width=70, anchor="e", stretch=False)
        self.tree.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(lib, orient="vertical", command=self.tree.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)
        self.tree.bind("<Button-3>", self._tree_menu)

        bottom = ttk.Frame(side, style="Side.TFrame")
        bottom.grid(row=5, column=0, sticky="ew", padx=16, pady=14)
        ttk.Button(bottom, text="Import recording…", command=self.import_recording).pack(
            side="left", fill="x", expand=True)
        ttk.Button(bottom, text="⚙  Settings", command=self.open_settings).pack(side="left", padx=(8, 0))

        # Content views -------------------------------------------------------
        content = ttk.Frame(self.root)
        content.grid(row=0, column=1, sticky="nsew")
        content.rowconfigure(0, weight=1)
        content.columnconfigure(0, weight=1)
        self.home_view = self._build_home(content)
        self.record_view = self._build_record(content)
        self.meeting_view = self._build_meeting(content)
        for v in (self.home_view, self.record_view, self.meeting_view):
            v.grid(row=0, column=0, sticky="nsew")

    def _build_home(self, parent):
        outer = ttk.Frame(parent)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(0, weight=1)
        outer.rowconfigure(2, weight=1)
        f = ttk.Frame(outer)
        f.grid(row=1, column=0)
        f.columnconfigure(0, weight=1)

        ttk.Label(f, image=self.img[64]).grid(row=0, column=0, pady=(0, 10))
        ttk.Label(f, text="New meeting", style="H1.TLabel").grid(row=1, column=0)
        ttk.Label(f, text="Press record when your call starts. No bot joins the meeting.",
                  style="Muted.TLabel").grid(row=2, column=0, pady=(2, 18))

        ttk.Label(f, text="Meeting name").grid(row=3, column=0, sticky="w")
        self.title_var = tk.StringVar(value="")
        self.title_entry = ttk.Entry(f, textvariable=self.title_var, width=46, font=("Segoe UI", 11))
        self.title_entry.grid(row=4, column=0, sticky="ew", pady=(4, 18))

        ttk.Label(f, text="What should Tableside create?").grid(row=5, column=0, sticky="w", pady=(0, 6))
        self.opt_audio = tk.BooleanVar(value=self.settings["opt_audio"])
        self.opt_transcript = tk.BooleanVar(value=self.settings["opt_transcript"])
        self.opt_summary = tk.BooleanVar(value=self.settings["opt_summary"])
        rows = [
            (self.opt_audio, "Audio recording", "Keep the audio file of the meeting"),
            (self.opt_transcript, "Transcript", "Who said what, word for word, with timestamps"),
            (self.opt_summary, "AI summary", "Overview, decisions and action items"),
        ]
        opts = ttk.Frame(f)
        opts.grid(row=6, column=0, sticky="ew")
        self.summary_note = None
        for i, (var, label, hint) in enumerate(rows):
            ttk.Checkbutton(opts, text=label, variable=var, style="Option.TCheckbutton",
                            command=lambda v=var: self._options_changed(v)).grid(
                row=i * 2, column=0, sticky="w", pady=(6, 0))
            lab = ttk.Label(opts, text=hint, style="Small.TLabel")
            lab.grid(row=i * 2 + 1, column=0, sticky="w", padx=(30, 0))
            if var is self.opt_summary:
                self.summary_note = lab

        self.start_btn = PillButton(f, "●   Start recording", self.start_recording, size=13)
        self.start_btn.grid(row=7, column=0, sticky="ew", pady=(24, 8), ipady=4)
        self.device_hint = ttk.Label(f, text="", style="Small.TLabel", cursor="hand2")
        self.device_hint.grid(row=8, column=0)
        self.device_hint.bind("<Button-1>", lambda e: self.open_settings("Audio"))
        return outer

    def _build_record(self, parent):
        outer = ttk.Frame(parent)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(0, weight=1)
        outer.rowconfigure(2, weight=1)
        f = ttk.Frame(outer)
        f.grid(row=1, column=0)
        f.columnconfigure(1, weight=1)

        self.rec_dot = tk.Label(f, text="●", fg=STOP_RED, font=("Segoe UI", 14))
        self.rec_dot.grid(row=0, column=0, sticky="e")
        self.rec_title = ttk.Label(f, text="Recording", style="H2.TLabel")
        self.rec_title.grid(row=0, column=1, sticky="w", padx=6)
        self.timer_var = tk.StringVar(value="00:00:00")
        ttk.Label(f, textvariable=self.timer_var, style="Timer.TLabel").grid(
            row=1, column=0, columnspan=2, pady=(6, 18))

        ttk.Label(f, text="Meeting audio").grid(row=2, column=0, columnspan=2, sticky="w")
        self.spk_meter = ttk.Progressbar(f, maximum=100, length=420)
        self.spk_meter.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(4, 12))
        ttk.Label(f, text="Your microphone").grid(row=4, column=0, columnspan=2, sticky="w")
        self.mic_meter = ttk.Progressbar(f, maximum=100, length=420)
        self.mic_meter.grid(row=5, column=0, columnspan=2, sticky="ew", pady=(4, 12))
        self.rec_opts = ttk.Label(f, text="", style="Muted.TLabel")
        self.rec_opts.grid(row=6, column=0, columnspan=2, pady=(4, 18))

        self.stop_btn = PillButton(f, "■   Stop recording", self.stop_recording,
                                   color=STOP_RED, hover=STOP_HOVER, size=13)
        self.stop_btn.grid(row=7, column=0, columnspan=2, sticky="ew", ipady=4)
        ttk.Label(f, text="Tip: wear headphones for the cleanest transcript.",
                  style="Small.TLabel").grid(row=8, column=0, columnspan=2, pady=(12, 0))
        return outer

    def _build_meeting(self, parent):
        f = ttk.Frame(parent, padding=(26, 22, 26, 18))
        f.columnconfigure(0, weight=1)
        f.rowconfigure(4, weight=1)

        head = ttk.Frame(f)
        head.grid(row=0, column=0, sticky="ew")
        head.columnconfigure(0, weight=1)
        self.m_title = ttk.Label(head, text="", style="H1.TLabel")
        self.m_title.grid(row=0, column=0, sticky="w")
        ttk.Button(head, text="Rename", command=self.rename_meeting).grid(row=0, column=1, sticky="e")
        self.m_meta = ttk.Label(f, text="", style="Muted.TLabel")
        self.m_meta.grid(row=1, column=0, sticky="w", pady=(2, 10))

        self.banner = ttk.Frame(f, style="Banner.TFrame", padding=(14, 10))
        self.banner.columnconfigure(0, weight=1)
        self.banner_msg = ttk.Label(self.banner, text="", style="Banner.TLabel", wraplength=560)
        self.banner_msg.grid(row=0, column=0, sticky="w")
        self.banner_bar = ttk.Progressbar(self.banner, maximum=100)
        self.banner_btns = ttk.Frame(self.banner, style="Banner.TFrame")
        self.banner_btns.grid(row=0, column=1, rowspan=2, sticky="e", padx=(12, 0))

        bar = ttk.Frame(f)
        bar.grid(row=3, column=0, sticky="ew", pady=(10, 10))
        self.btn_names = ttk.Button(bar, text="Name speakers", command=self.name_speakers)
        self.btn_word = ttk.Button(bar, text="Open in Word", command=lambda: self.open_export("docx"))
        self.btn_excel = ttk.Button(bar, text="Open in Excel", command=lambda: self.open_export("xlsx"))
        self.btn_audio = ttk.Button(bar, text="▶  Play audio", command=self.play_audio)
        self.btn_trim = ttk.Button(bar, text="✂  Trim", command=self.open_trim)
        self.btn_folder = ttk.Button(bar, text="Show folder", command=lambda: open_path(self.current))
        self.btn_more = ttk.Button(bar, text="More ▾", command=self._more_menu)
        for b in (self.btn_names, self.btn_word, self.btn_excel, self.btn_audio, self.btn_trim, self.btn_folder):
            b.pack(side="left", padx=(0, 8))
        self.btn_more.pack(side="right")

        self.tabs = ttk.Notebook(f)
        self.tabs.grid(row=4, column=0, sticky="nsew")
        self.summary_tab = ttk.Frame(self.tabs)
        self.transcript_tab = ttk.Frame(self.tabs)
        self.tabs.add(self.summary_tab, text="  Summary  ")
        self.tabs.add(self.transcript_tab, text="  Transcript  ")
        self.summary_text = self._scrolled(self.summary_tab)
        self.transcript_text = self._scrolled(self.transcript_tab)
        return f

    def _scrolled(self, parent):
        parent.rowconfigure(0, weight=1)
        parent.columnconfigure(0, weight=1)
        t = ReadOnlyText(parent)
        t.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(parent, orient="vertical", command=t.yview)
        sb.grid(row=0, column=1, sticky="ns")
        t.configure(yscrollcommand=sb.set)
        return t

    # ------------------------------------------------------------ navigation
    def show_home(self):
        if self.session:
            self.record_view.tkraise()
            return
        self.current = None
        self._suppress_select = True
        self.tree.selection_remove(self.tree.selection())
        self._suppress_select = False
        if not self.title_var.get().strip():
            self.title_var.set(f"Meeting {dt.datetime.now():%b %d}")
        self._update_device_hint()
        self._options_changed()
        self.home_view.tkraise()
        self.title_entry.focus_set()
        self.title_entry.select_range(0, "end")

    def on_new_button(self):
        if self.session:
            self.record_view.tkraise()
            self.current = None
        else:
            self.show_home()

    def _search_focus(self, _e):
        if self._search_placeholder:
            self._search_placeholder = False
            self.search_var.set("")
            self.search.configure(foreground=self.p["text"])

    def _search_blur(self, _e):
        if not self.search_var.get():
            self._search_placeholder = True
            self.search_var.set("Search meetings")
            self.search.configure(foreground=self.p["muted"])

    def _options_changed(self, changed=None):
        # AI summary is written from the transcript, so it needs one
        if changed is self.opt_summary and self.opt_summary.get():
            if not self.opt_transcript.get():
                self.opt_transcript.set(True)
            if self.settings["ai_provider"] == "claude" and not E.get_api_key():
                if messagebox.askyesno(E.APP_NAME, "AI summaries use Claude, which needs an API key.\n\n"
                                                   "Add one now?", parent=self.root):
                    self.open_settings("AI summary")
                if not E.get_api_key() and self.settings["ai_provider"] == "claude":
                    self.opt_summary.set(False)
        if changed is self.opt_transcript and not self.opt_transcript.get():
            self.opt_summary.set(False)
        if not self.opt_audio.get() and not self.opt_transcript.get():
            if changed is self.opt_audio:
                self.opt_transcript.set(True)
            else:
                self.opt_audio.set(True)
        if self.summary_note is not None:
            where = {"claude": "sends the transcript text to Claude",
                     "ollama": "runs on this PC with Ollama"}.get(
                self.settings["ai_provider"],
                "made on this PC, free" + ("" if E.local_model_ready() else " (one-time download)"))
            self.summary_note.configure(text=f"Overview, decisions and action items - {where}")
        for k, var in (("opt_audio", self.opt_audio), ("opt_transcript", self.opt_transcript),
                       ("opt_summary", self.opt_summary)):
            self.settings[k] = var.get()
        save_settings(self.settings)

    def _update_device_hint(self):
        spk = self.settings.get("speaker_device") or "default speakers"
        mic = self.settings.get("mic_device") or "default microphone"
        if mic == R_NO_MIC:
            mic = "no microphone"
        self.device_hint.configure(text=f"Speakers: {spk}   ·   Mic: {mic}   ·   Change")

    # ---------------------------------------------------------------- library
    def refresh_library(self):
        self.meetings = {m["folder"]: m for m in E.list_meetings()}
        q = "" if self._search_placeholder else self.search_var.get().strip().lower()
        self._suppress_select = True
        self.tree.delete(*self.tree.get_children())
        today = dt.date.today()
        for folder, m in self.meetings.items():
            if q and q not in m.get("title", "").lower() and q not in E.transcript_text(m).lower():
                continue
            started = dt.datetime.fromisoformat(m["started"])
            when = (f"{started:%I:%M %p}".lstrip("0") if started.date() == today
                    else f"{started:%b %d}" if started.year == today.year else f"{started:%m/%d/%y}")
            status = m.get("status")
            mark = "●  " if status in BUSY else ("⚠  " if status in ("interrupted", "error") or folder in self.errors else "")
            self.tree.insert("", "end", iid=folder, text=f"  {mark}{m.get('title', 'Meeting')}", values=(when,))
        if self.current and self.tree.exists(self.current):
            self.tree.selection_set(self.current)
        self._suppress_select = False

    def _on_tree_select(self, _e):
        if self._suppress_select:
            return
        sel = self.tree.selection()
        if sel:
            self.show_meeting(sel[0])

    def _tree_menu(self, e):
        iid = self.tree.identify_row(e.y)
        if not iid:
            return
        self.tree.selection_set(iid)
        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label="Rename…", command=self.rename_meeting)
        menu.add_command(label="Show folder", command=lambda: open_path(iid))
        menu.add_separator()
        menu.add_command(label="Delete…", command=self.delete_meeting)
        menu.tk_popup(e.x_root, e.y_root)

    # ----------------------------------------------------------- meeting view
    def show_meeting(self, folder: str):
        m = E.read_meeting(Path(folder))
        if not m:
            return
        self.meetings[folder] = m
        self.current = folder
        opts = E.options(m)
        started = dt.datetime.fromisoformat(m["started"])
        names = m.get("names", {})
        speakers = list(dict.fromkeys(E.palette_name(k, names) for _s, _e, k, _t in m.get("segments", [])))
        meta = [f"{started:%a, %b %d, %Y  ·  %I:%M %p}".replace(" 0", " ")]
        if m.get("duration"):
            meta.append(E.fmt_length(m["duration"]))
        if speakers:
            meta.append(", ".join(speakers))
        self.m_title.configure(text=m.get("title", "Meeting"))
        self.m_meta.configure(text="   ·   ".join(meta))

        has_segments = "segments" in m
        has_files = bool(m.get("docx")) and (Path(folder) / m["docx"]).exists()
        audio = self._audio_file(m)
        self._enable(self.btn_names, has_segments and bool(m.get("segments")))
        self._enable(self.btn_word, has_files)
        self._enable(self.btn_excel, has_files)
        self._enable(self.btn_audio, audio is not None)
        self._enable(self.btn_trim, bool(E.recorded_tracks(m)) and m.get("status") not in BUSY + ("recording",))

        self._render_summary(m)
        self._render_transcript(m)
        if m.get("summary"):
            self.tabs.select(self.summary_tab)
        elif has_segments or not opts["summary"]:
            self.tabs.select(self.transcript_tab)
        self._render_banner(m)
        self.meeting_view.tkraise()

    @staticmethod
    def _enable(btn, on):
        btn.state(["!disabled"] if on else ["disabled"])

    def _audio_file(self, m) -> Path | None:
        for t in m.get("tracks", []):
            if t.get("kind") == "meeting":
                p = track_path(m, t)
                if p.exists():
                    return p
        for t in m.get("tracks", []):
            p = track_path(m, t)
            if p.exists():
                return p
        return None

    def _render_banner(self, m):
        folder = m["folder"]
        for w in self.banner_btns.winfo_children():
            w.destroy()
        self.banner_bar.grid_forget()
        msg, buttons, bar = None, [], None
        status = m.get("status")
        opts = E.options(m)
        if folder in self.progress and status in BUSY:
            pct, text = self.progress[folder]
            msg, bar = text, pct
        elif status == "queued":
            msg, bar = "Waiting to start…", 0
        elif status in BUSY:
            msg, bar = "Working…", 0
        elif folder in self.errors or status == "error":
            msg = self.errors.get(folder) or m.get("error") or "Something went wrong."
            buttons = [("Try again", lambda: self.retry(folder))]
        elif status == "interrupted":
            msg = "Tableside closed before this meeting was finished."
            buttons = [("Transcribe now", lambda: self.queue_process(folder))]
        elif unnamed_speakers(m):
            n = len(unnamed_speakers(m))
            msg = (f"{n} voice{'s' if n > 1 else ''} found. Name them so the notes say who said what."
                   + ("  The AI summary will be written after." if m.get("summary_pending") else ""))
            buttons = [("Name speakers", self.name_speakers)]
            if m.get("summary_pending"):
                buttons.append(("Summarize now", lambda: self.queue_summary(folder)))
        elif m.get("auto_trimmed") and not m.get("auto_trim_seen"):
            cut = m["auto_trimmed"]
            parts = [f"{E.fmt_length(cut[k])} of silence at the {k}" for k in ("start", "end") if cut.get(k, 0) >= 1]
            msg = "Tableside cut " + " and ".join(parts) + "."
            buttons = [("OK", lambda: self._ack_trim(folder)), ("Undo", lambda: self.undo_trim(folder))]
        elif m.get("guessed") and not m.get("names_confirmed"):
            who = ", ".join(g["name"] for g in m["guessed"].values())
            msg = f"Tableside worked out {who} from how people addressed each other. Check it's right."
            buttons = [("Check names", self.name_speakers)]
        if msg is None:
            self.banner.grid_forget()
            return
        self.banner.grid(row=2, column=0, sticky="ew", pady=(0, 4))
        self.banner_msg.configure(text=msg)
        if bar is not None:
            self.banner_bar.grid(row=1, column=0, sticky="ew", pady=(8, 0))
            self.banner_bar.configure(mode="determinate", value=bar)
        for text, cmd in buttons:
            ttk.Button(self.banner_btns, text=text, command=cmd,
                       style="Accent.TButton" if not self.banner_btns.winfo_children() else "TButton"
                       ).pack(side="left", padx=(6, 0))

    def _render_summary(self, m):
        s = m.get("summary")
        opts = E.options(m)
        if not s:
            if m.get("status") == "summarizing":
                text = "Writing the AI summary…"
            elif m.get("summary_pending"):
                text = "The AI summary will be written once the speakers are named."
            elif "segments" in m:
                text = "No AI summary for this meeting.\nUse  More ▾  →  Create AI summary  to make one."
            elif opts["summary"]:
                text = "The AI summary will appear here once the transcript is ready."
            else:
                text = "No AI summary for this meeting."
            self.summary_text.write([(text, "placeholder")])
            return
        chunks = []
        if s.get("note") or s.get("basic"):
            chunks.append(((s.get("note") or "Basic summary, made without AI.") + "\n", "muted"))
        chunks += [("Summary\n", "h"), (s.get("summary", "") + "\n", "body")]
        if s.get("action_items"):
            chunks.append(("Action items\n", "h"))
            for a in s["action_items"]:
                chunks += [("☐  ", "bullet"), (a["owner"], ("bullet", "owner")),
                           (f"  -  {a['task']}", "bullet")]
                if a.get("due"):
                    chunks.append((f"   ({a['due']})", ("bullet", "muted")))
                chunks.append(("\n", "bullet"))
        for key, heading in (("decisions", "Decisions"), ("open_questions", "Open questions")):
            if s.get(key):
                chunks.append((f"\n{heading}\n", "h"))
                chunks += [(f"•  {x}\n", "bullet") for x in s[key]]
        self.summary_text.write(chunks)

    def _render_transcript(self, m):
        t = self.transcript_text
        if "segments" not in m:
            opts = E.options(m)
            if m.get("status") in BUSY:
                text = "The transcript will appear here when it's ready."
            elif not opts["transcript"]:
                text = "This meeting was saved as audio only.\nUse  More ▾  →  Create transcript  to transcribe it."
            else:
                text = "No transcript yet."
            t.write([(text, "placeholder")])
            return
        segs = m["segments"]
        if not segs:
            t.write([("No speech was detected in this recording.", "placeholder")])
            return
        colors = speaker_colors(m)
        names = m.get("names", {})
        for k, c in colors.items():
            t.tag_configure(f"spk_{k}", foreground=c, font=("Segoe UI Semibold", 11))
        chunks = []
        raw = [E.Segment(s, e, k, txt) for s, e, k, txt in segs]
        for turn in E.group_turns(raw):
            chunks.append((E.palette_name(turn.speaker, names), f"spk_{turn.speaker}"))
            chunks.append((f"   {E.fmt_time(turn.start)}\n", "muted"))
            chunks.append((turn.text + "\n", "body"))
        t.write(chunks)

    def _more_menu(self):
        m = self.meetings.get(self.current)
        if not m:
            return
        busy = m.get("status") in BUSY
        menu = tk.Menu(self.root, tearoff=0)
        has_seg = "segments" in m
        menu.add_command(label="Create transcript" if not has_seg else "Transcribe again",
                         command=lambda: self.queue_process(self.current, force_transcript=True),
                         state="disabled" if busy or self._audio_file(m) is None else "normal")
        menu.add_command(label="Create AI summary" if not m.get("summary") else "Rewrite AI summary",
                         command=lambda: self.queue_summary(self.current),
                         state="normal" if has_seg and m.get("segments") and not busy else "disabled")
        menu.add_command(label="Trim recording…", command=self.open_trim,
                         state="normal" if E.recorded_tracks(m) and not busy else "disabled")
        if m.get("untrimmed"):
            menu.add_command(label="Undo trim (restore full recording)",
                             command=lambda: self.undo_trim(self.current),
                             state="disabled" if busy else "normal")
        menu.add_separator()
        menu.add_command(label="Rename…", command=self.rename_meeting)
        menu.add_command(label="Delete…", command=self.delete_meeting,
                         state="disabled" if busy else "normal")
        x = self.btn_more.winfo_rootx()
        y = self.btn_more.winfo_rooty() + self.btn_more.winfo_height()
        menu.tk_popup(x, y)

    def open_export(self, kind):
        m = self.meetings.get(self.current)
        if m and m.get(kind):
            open_path(Path(m["folder"]) / m[kind])

    def play_audio(self):
        m = self.meetings.get(self.current)
        p = self._audio_file(m) if m else None
        if p:
            open_path(p)

    def rename_meeting(self):
        folder = self.current or (self.tree.selection() or [None])[0]
        m = self.meetings.get(folder)
        if not m:
            return
        name = simpledialog.askstring(E.APP_NAME, "Meeting name:", initialvalue=m.get("title", ""),
                                      parent=self.root)
        if not name or not name.strip():
            return
        m = E.read_meeting(Path(folder))
        m["title"] = name.strip()
        try:
            E.export_files(m)
        except Exception as e:
            messagebox.showwarning(E.APP_NAME, f"Renamed, but the Word/Excel files couldn't be updated:\n{e}")
        E.write_meeting(m)
        self.refresh_library()
        self.show_meeting(folder)

    def delete_meeting(self):
        folder = self.current or (self.tree.selection() or [None])[0]
        m = self.meetings.get(folder)
        if not m:
            return
        if not messagebox.askyesno(E.APP_NAME, f"Delete “{m.get('title')}”?\n\nThe audio, transcript and "
                                               "notes go to the Recycle Bin.", icon="warning", parent=self.root):
            return
        try:
            from send2trash import send2trash
            send2trash(folder)
        except Exception as e:
            messagebox.showerror(E.APP_NAME, f"Couldn't delete it:\n{e}")
            return
        self.current = None
        self.refresh_library()
        self.show_home()

    # -------------------------------------------------------------- recording
    def _error(self, text):
        if self.hidden:
            self.show_window()
        messagebox.showerror(E.APP_NAME, text, parent=self.root)

    def start_recording(self):
        if R.pyaudio is None:
            self._error( "Recording needs Windows audio support, which isn't available.")
            return
        try:
            if self.pa is None:
                self.pa = R.pyaudio.PyAudio()
            loopbacks, mics, d_spk, d_mic = R.list_devices(self.pa)
        except Exception as e:
            self._error( f"Couldn't open your audio devices:\n{e}")
            return
        spk_name = self.settings.get("speaker_device") or d_spk
        spk = loopbacks.get(spk_name) or loopbacks.get(d_spk)
        if not spk:
            self._error( "Pick the speakers or headset your meeting plays through.")
            self.open_settings("Audio")
            return
        mic_name = self.settings.get("mic_device") or d_mic
        mic = None if mic_name == R_NO_MIC else (mics.get(mic_name) or mics.get(d_mic))

        title = self.title_var.get().strip() or f"Meeting {dt.datetime.now():%b %d}"
        opts = {"audio": self.opt_audio.get(), "transcript": self.opt_transcript.get(),
                "summary": self.opt_summary.get()}
        started = dt.datetime.now()
        folder = E.NOTES_ROOT / f"{started:%Y-%m-%d %H%M%S} {E.safe_name(title)}"
        folder.mkdir(parents=True, exist_ok=True)

        recs = [R.StreamRecorder(self.pa, spk, folder / "meeting audio.wav", "meeting")]
        if mic:
            recs.append(R.StreamRecorder(self.pa, mic, folder / "my mic.wav", "me"))
        try:
            for r in recs:
                r.open()
        except Exception as e:
            for r in recs:
                try:
                    r.stop(time.monotonic())
                except Exception:
                    pass
            self._error( f"Couldn't start recording:\n{e}")
            return
        t0 = time.monotonic()
        for r in recs:
            r.start(t0)
        self.recorders = recs
        meeting = {"folder": str(folder), "version": 1, "title": title, "started": started.isoformat(),
                   "options": opts, "status": "recording",
                   "tracks": [{"file": r.path.name, "kind": r.speaker} for r in recs]}
        E.write_meeting(meeting)
        self.session = {"folder": str(folder), "t0": t0, "title": title,
                        "meeting_seen": bool(self.prev_apps), "apps": set(self.prev_apps),
                        "quiet_since": None}
        self._close_toast()
        self._update_tray()
        keep = [n for n, on in (("audio", opts["audio"]), ("transcript", opts["transcript"]),
                                ("AI summary", opts["summary"])) if on]
        self.rec_title.configure(text=title)
        self.rec_opts.configure(text="Creating: " + ", ".join(keep))
        self.new_btn.configure(text="●  Recording…")
        self.new_btn.recolor(STOP_RED, STOP_HOVER)
        self.title_var.set("")
        self.current = None
        self.record_view.tkraise()

    def stop_recording(self):
        if not self.session:
            return
        stop = time.monotonic()
        for r in self.recorders:
            try:
                r.stop(stop)
            except Exception:
                traceback.print_exc()
        self.recorders = []
        folder = self.session["folder"]
        m = E.read_meeting(Path(folder))
        m["duration"] = stop - self.session["t0"]
        opts = E.options(m)
        m["status"] = "queued" if opts["transcript"] or opts["summary"] else "ready"
        E.write_meeting(m)
        self.session = None
        self.new_btn.configure(text="＋  New recording")
        self.new_btn.recolor(BRAND, BRAND_HOVER)
        self._close_toast()
        self._update_tray()
        self.spk_meter["value"] = self.mic_meter["value"] = 0
        if m["status"] == "queued":
            self.jobs.put(("process", folder, True))
        self.refresh_library()
        self.tree.selection_set(folder)
        self.show_meeting(folder)

    # ----------------------------------------------------------- job control
    def queue_process(self, folder, force_transcript=False):
        m = E.read_meeting(Path(folder))
        if not m or m.get("status") in BUSY:
            return
        if force_transcript:
            m.setdefault("options", {})["transcript"] = True
        m["status"] = "queued"
        m.pop("error", None)
        self.errors.pop(folder, None)
        E.write_meeting(m)
        self.jobs.put(("process", folder, False))
        self.refresh_library()
        self.show_meeting(folder)

    def queue_summary(self, folder):
        m = E.read_meeting(Path(folder))
        if not m or m.get("status") in BUSY:
            return
        if self.settings["ai_provider"] == "claude" and not E.get_api_key():
            if messagebox.askyesno(E.APP_NAME, "AI summaries need a Claude API key. Add one now?"):
                self.open_settings("AI summary")
            if not E.get_api_key():
                return
        m.setdefault("options", {})["summary"] = True
        m["status"] = "summarizing"
        m.pop("summary_pending", None)
        self.errors.pop(folder, None)
        E.write_meeting(m)
        self.jobs.put(("summary", folder, False))
        self.refresh_library()
        if self.current == folder:
            self.show_meeting(folder)

    def retry(self, folder):
        m = E.read_meeting(Path(folder))
        if m and "segments" in m and E.options(m)["summary"] and not m.get("summary"):
            self.queue_summary(folder)
        else:
            self.queue_process(folder)

    def import_recording(self):
        path = filedialog.askopenfilename(
            title="Choose a recording to transcribe",
            filetypes=[("Audio or video", "*.wav *.mp3 *.m4a *.mp4 *.aac *.flac *.ogg *.webm *.mkv *.mov *.wma"),
                       ("All files", "*.*")])
        if not path:
            return
        path = Path(path)
        title = simpledialog.askstring(E.APP_NAME, "Meeting name:", initialvalue=path.stem, parent=self.root)
        if title is None:
            return
        title = title.strip() or path.stem
        started = dt.datetime.fromtimestamp(path.stat().st_mtime)
        folder = E.NOTES_ROOT / f"{started:%Y-%m-%d %H%M%S} {E.safe_name(title)}"
        folder.mkdir(parents=True, exist_ok=True)
        opts = {"audio": True, "transcript": True, "summary": self.opt_summary.get()}
        m = {"folder": str(folder), "version": 1, "title": title, "started": started.isoformat(),
             "options": opts, "status": "queued", "imported": True,
             "tracks": [{"file": str(path), "kind": "meeting"}]}
        E.write_meeting(m)
        self.jobs.put(("process", str(folder), False))
        self.refresh_library()
        self.tree.selection_set(str(folder))
        self.show_meeting(str(folder))

    def _resume_interrupted(self):
        for m in E.list_meetings():
            if m.get("status") in BUSY + ("recording",):
                m["status"] = "interrupted"
                E.write_meeting(m)

    # --------------------------------------------------------- background work
    def post(self, *msg):
        self.msgs.put(msg)

    def _poll(self):
        changed = set()
        try:
            while True:
                msg = self.msgs.get_nowait()
                kind, folder = msg[0], msg[1]
                if kind == "progress":
                    self.progress[folder] = (msg[2], msg[3])
                    if folder == self.current:
                        m = self.meetings.get(folder)
                        if m:
                            m["status"] = m.get("status") if m.get("status") in BUSY else "processing"
                            self._render_banner(m)
                elif kind == "error":
                    self.errors[folder] = msg[2]
                    changed.add(folder)
                elif kind == "note":
                    print(msg[2])
                elif kind == "processed":
                    changed.add(folder)
                    if msg[2]:
                        self.root.after(300, lambda f=folder: self._prompt_names(f))
                elif kind == "refresh":
                    changed.add(folder)
                elif kind == "mics":
                    self._on_meeting_apps(msg[1])
                elif kind == "tray":
                    self._on_tray(msg[1])
                elif kind == "show":
                    self.show_window()
                elif kind == "call":     # work handed back from a background thread
                    try:
                        msg[1]()
                    except Exception:
                        traceback.print_exc()
        except queue.Empty:
            pass
        if changed:
            for f in changed:
                m = E.read_meeting(Path(f))
                if m and m.get("status") not in BUSY:
                    self.progress.pop(f, None)
            self.refresh_library()
            if self.current in changed:
                self.show_meeting(self.current)

        if self.session:
            secs = time.monotonic() - self.session["t0"]
            self.timer_var.set(E.fmt_time(secs))
            self.rec_dot.configure(fg=STOP_RED if int(secs * 2) % 2 == 0 else self.p["bg"])
            for meter in (self.spk_meter, self.mic_meter):
                meter["value"] = 0
            for r in self.recorders:
                meter = self.spk_meter if r.speaker == "meeting" else self.mic_meter
                meter["value"] = min(100, r.level * 140)
                r.level *= 0.5
            self._check_call_ended()
            self._check_silence()
        self.root.after(150, self._poll)

    def _prompt_names(self, folder):
        if self.session:  # don't interrupt a new recording; the banner will wait
            return
        self.show_window()
        self.refresh_library()
        self.tree.selection_set(folder)
        self.show_meeting(folder)
        self.name_speakers()

    # --------------------------------------------------- meeting detection
    def _watch_loop(self):
        last = None
        while True:
            try:
                s = self.settings
                apps = (W.meeting_apps_in_call(s.get("watch_browsers", True))
                        if s.get("watch_meetings", True) else frozenset())
            except Exception:
                apps = frozenset()
            if apps != last:
                last = apps
                self.post("mics", apps)
            time.sleep(2)

    def _on_meeting_apps(self, apps: frozenset):
        new = apps - self.prev_apps
        self.prev_apps = apps
        self.snoozed &= set(apps)          # "Not now" lasts until that call ends
        if self.session:
            if apps:
                self.session["meeting_seen"] = True
                self.session["apps"] |= set(apps)
                self.session["quiet_since"] = None
                if isinstance(self.toast, CallEndedToast):
                    self._close_toast()
            elif self.session.get("meeting_seen"):
                self.session["quiet_since"] = time.monotonic()
            return
        fresh = [a for a in new if a not in self.snoozed]
        if fresh and self.toast is None:
            self.toast = MeetingToast(self, fresh[0])
        if not apps and isinstance(self.toast, MeetingToast):
            self._close_toast()

    def _check_silence(self):
        s = self.session
        if not s or self.toast is not None or not self.settings.get("silence_stop", True):
            return
        if s.get("silence_ok_until", 0) > time.monotonic():
            return
        last = max((getattr(r, "last_loud", s["t0"]) for r in self.recorders), default=s["t0"])
        minutes = max(1, int(self.settings.get("silence_minutes", 10)))
        if time.monotonic() - last > minutes * 60:
            self.toast = CallEndedToast(self, None, title=f"It's been quiet for {minutes} minutes")

    def _check_call_ended(self):
        s = self.session
        if not s or not s.get("quiet_since") or not self.settings.get("auto_stop", True):
            return
        if time.monotonic() - s["quiet_since"] > 8 and self.toast is None:
            label = " / ".join(sorted(s["apps"])) or "meeting"
            self.toast = CallEndedToast(self, label)

    def toast_record(self, label, title, toast):
        self._close_toast()
        self.title_var.set(title)
        self.snoozed.discard(label)
        self.start_recording()

    def toast_dismiss(self, label):
        self.snoozed.add(label)
        self._close_toast()

    def toast_keep_recording(self):
        if self.session:
            self.session["meeting_seen"] = False
            self.session["quiet_since"] = None
            self.session["silence_ok_until"] = time.monotonic() + 10 * 60
        self._close_toast()

    def _close_toast(self):
        if self.toast is not None:
            try:
                self.toast.destroy()
            except tk.TclError:
                pass
            self.toast = None

    # ------------------------------------------------------------- tray icon
    def _start_tray(self):
        try:
            import pystray
            from PIL import Image, ImageDraw
        except Exception:
            return
        try:
            base = Image.open(RES / "tableside.png").convert("RGBA").resize((64, 64))
            rec = base.copy()
            d = ImageDraw.Draw(rec)
            d.ellipse((38, 38, 63, 63), fill=(255, 255, 255, 255))
            d.ellipse((42, 42, 59, 59), fill=(220, 30, 30, 255))
            self._tray_imgs = {"idle": base, "rec": rec}
            post = lambda a: (lambda icon, item: self.post("tray", a))
            menu = pystray.Menu(
                pystray.MenuItem("Open Tableside", post("open"), default=True),
                pystray.MenuItem(lambda item: "Stop recording" if self.session else "Start recording",
                                 post("record")),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Quit Tableside", post("quit")),
            )
            self.tray = pystray.Icon("Tableside", base, "Tableside", menu)
            self.tray.run_detached()
        except Exception:
            traceback.print_exc()
            self.tray = None

    def _update_tray(self):
        if self.tray is None:
            return
        try:
            rec = self.session is not None
            self.tray.icon = self._tray_imgs["rec" if rec else "idle"]
            self.tray.title = "Tableside - recording" if rec else "Tableside"
            self.tray.update_menu()
        except Exception:
            pass

    def _on_tray(self, action):
        if action == "open":
            self.show_window()
        elif action == "record":
            if self.session:
                self.stop_recording()
            else:
                self.show_window()
                self.show_home()
        elif action == "quit":
            self.quit_app()

    def _notify(self, text):
        if self.tray is not None:
            try:
                self.tray.notify(text, E.APP_NAME)
            except Exception:
                pass

    # ---------------------------------------------------------- trimming
    def open_trim(self):
        m = E.read_meeting(Path(self.current)) if self.current else None
        if m and E.recorded_tracks(m) and m.get("status") not in BUSY:
            TrimDialog(self, m)

    def trim_saved(self, folder, start, end):
        m = E.read_meeting(Path(folder))
        try:
            E.trim_meeting(m, start, end)
            if "segments" in m:
                E.export_files(m)
        except Exception as e:
            traceback.print_exc()
            self._error(f"Couldn't trim the recording:\n{e}")
            return
        m["auto_trim_seen"] = True
        E.write_meeting(m)
        self.refresh_library()
        self.show_meeting(folder)

    def undo_trim(self, folder):
        m = E.read_meeting(Path(folder))
        if not m or m.get("status") in BUSY:
            return
        # an automatic cut happens before transcribing, so the full recording needs a transcript
        needs_transcript = (m.get("untrimmed", {}).get("segments") is None and "segments" in m)
        E.undo_trim(m)
        if needs_transcript:
            E.write_meeting(m)
            self.queue_process(folder)
            return
        if "segments" in m:
            try:
                E.export_files(m)
            except Exception:
                traceback.print_exc()
        E.write_meeting(m)
        self.refresh_library()
        self.show_meeting(folder)

    def _ack_trim(self, folder):
        m = E.read_meeting(Path(folder))
        m["auto_trim_seen"] = True
        E.write_meeting(m)
        self.show_meeting(folder)

    # ---------------------------------------------------------- dialogs
    def name_speakers(self):
        m = E.read_meeting(Path(self.current)) if self.current else None
        if not m or not m.get("segments"):
            return
        SpeakerDialog(self, m)

    def speakers_saved(self, folder, names, remember):
        m = E.read_meeting(Path(folder))
        old = dict(m.get("names", {}))
        m["names"] = {k: v for k, v in names.items() if v}
        m["names_confirmed"] = True
        if remember:
            book = E.VoiceBook()
            for k, name in m["names"].items():
                fp = m.get("fingerprints", {}).get(k)
                if fp and k != E.ME and not name.startswith("Speaker "):
                    book.remember(name, fp)
            book.save()
        try:
            E.export_files(m)
        except Exception as e:
            messagebox.showwarning(E.APP_NAME, f"Names saved, but the Word/Excel files couldn't be updated:\n{e}")
        E.write_meeting(m)
        summary_wanted = m.get("summary_pending") or (m.get("summary") and old != m["names"])
        self.refresh_library()
        self.show_meeting(folder)
        if summary_wanted and E.options(m)["summary"]:
            self.queue_summary(folder)

    def merge_speakers(self, folder, target):
        m = E.read_meeting(Path(folder))
        if not m:
            return
        m = E.merge_meeting_speakers(m, target)
        try:
            E.export_files(m)
        except Exception:
            traceback.print_exc()
        E.write_meeting(m)
        self.refresh_library()
        self.show_meeting(folder)
        self.name_speakers()

    def speakers_skipped(self, folder):
        m = E.read_meeting(Path(folder))
        if m and m.get("summary_pending"):
            self.queue_summary(folder)

    def open_settings(self, tab=None):
        SettingsDialog(self, tab)

    def settings_saved(self):
        save_settings(self.settings)
        try:
            if W.IS_WINDOWS:
                W.set_start_with_windows(bool(self.settings.get("start_with_windows")))
        except Exception:
            traceback.print_exc()
        new_mode = self._apply_theme()
        if new_mode != self.mode:
            self.mode = new_mode
        self._restyle()
        self._update_device_hint()
        self._options_changed()

    def _first_run(self):
        if self.hidden:
            return
        if not self.settings.get("your_name"):
            guess = (os.environ.get("USERNAME") or os.environ.get("USER") or "").split(".")[0].title()
            name = simpledialog.askstring("Welcome to Tableside",
                                          "What's your name?\nIt labels your voice in transcripts.",
                                          initialvalue=guess, parent=self.root)
            self.settings["your_name"] = (name or guess or "Me").strip()
            save_settings(self.settings)

    def on_close(self):
        """Window X: keep running in the tray (to catch meetings) unless turned off."""
        if self.settings.get("run_in_background", True) and self.tray is not None:
            self.hide_window()
            if not self._bg_tip_shown:
                self._bg_tip_shown = True
                self._notify("Tableside is still running in the tray and will offer to record "
                             "when a meeting starts.")
            return
        self.quit_app()

    def hide_window(self):
        self.hidden = True
        self.root.withdraw()

    def show_window(self):
        self.hidden = False
        self.root.deiconify()
        self.root.lift()
        try:
            self.root.focus_force()
        except tk.TclError:
            pass
        if not self.settings.get("your_name"):
            self.root.after(300, self._first_run)

    def quit_app(self):
        if self.session:
            self.show_window()
            if not messagebox.askyesno(E.APP_NAME, "You're still recording. Stop and quit?\n\n"
                                                   "The audio is kept and you can transcribe it later."):
                return
            for r in self.recorders:
                try:
                    r.stop(time.monotonic())
                except Exception:
                    pass
            m = E.read_meeting(Path(self.session["folder"]))
            if m:
                m["status"] = "interrupted"
                m["duration"] = time.monotonic() - self.session["t0"]
                E.write_meeting(m)
        elif any(m.get("status") in BUSY for m in self.meetings.values()):
            if not messagebox.askyesno(E.APP_NAME, "Tableside is still working on a meeting. Quit anyway?\n\n"
                                                   "You can finish it next time with “Transcribe now”."):
                return
        if self.pa is not None:
            try:
                self.pa.terminate()
            except Exception:
                pass
        if self.tray is not None:
            try:
                self.tray.stop()
            except Exception:
                pass
        self.root.destroy()


R_NO_MIC = "(Don't record my microphone)"


# --------------------------------------------------------------------------- #
# Name speakers dialog
# --------------------------------------------------------------------------- #
class SpeakerDialog(tk.Toplevel):
    def __init__(self, app: App, meeting: dict):
        super().__init__(app.root)
        self.app, self.m = app, meeting
        self.title("Name speakers")
        self.transient(app.root)
        self.resizable(False, False)
        self.configure(bg=app.p["bg"])
        self._audio_cache: dict = {}
        self._tmp = None

        f = ttk.Frame(self, padding=22)
        f.pack(fill="both", expand=True)
        ttk.Label(f, text="Who's who?", style="H1.TLabel").grid(row=0, column=0, columnspan=4, sticky="w")
        ttk.Label(f, text="Type a name for each voice. Press ▶ to hear them. Give two voices the same "
                          "name to merge them.", style="Muted.TLabel", wraplength=560).grid(
            row=1, column=0, columnspan=4, sticky="w", pady=(2, 8))
        voices = list(dict.fromkeys(k for _s, _e, k, _t in meeting["segments"]))
        has_me = E.ME in voices
        count_row = ttk.Frame(f)
        count_row.grid(row=2, column=0, columnspan=4, sticky="w", pady=(0, 8))
        ttk.Label(count_row, text="Too many voices? People in this meeting" +
                  (" (including you):" if has_me else ":")).pack(side="left")
        self.count_var = tk.IntVar(value=len(voices))
        ttk.Spinbox(count_row, from_=1 + int(has_me), to=max(len(voices), 2), width=4,
                    textvariable=self.count_var).pack(side="left", padx=8)
        ttk.Button(count_row, text="Apply", command=self.apply_count).pack(side="left")

        names = meeting.get("names", {})
        colors = speaker_colors(meeting)
        talk: dict[str, float] = {}
        sample: dict[str, list] = {}
        for s, e, k, t in meeting["segments"]:
            talk[k] = talk.get(k, 0) + (e - s)
            sample.setdefault(k, []).append((e - s, t))
        order = list(dict.fromkeys(k for _s, _e, k, _t in meeting["segments"]))
        keys = sorted(order, key=lambda k: (k != E.ME, order.index(k)))
        known = sorted(set(E.VoiceBook().names()) | {app.settings.get("your_name") or "Me"}, key=str.lower)
        auto = set(meeting.get("auto_named", []))
        guessed = meeting.get("guessed", {})

        self.vars: dict[str, tk.StringVar] = {}
        row = 3
        for k in keys:
            fallback = E.palette_name(k, {})
            ttk.Separator(f).grid(row=row, column=0, columnspan=4, sticky="ew", pady=6)
            row += 1
            dot = tk.Label(f, text="●", fg=colors.get(k, BRAND), bg=app.p["bg"], font=("Segoe UI", 16))
            app.dots.append((dot, colors.get(k, BRAND)))
            dot.grid(row=row, column=0, rowspan=2, sticky="n", padx=(0, 10))
            who = "You (microphone)" if k == E.ME else fallback
            badge = ("   ✓ recognised by voice" if k in auto
                     else "   ✓ from the conversation" if k in guessed else "")
            ttk.Label(f, text=f"{who}  ·  talked {E.fmt_length(talk[k])}{badge}",
                      style="H2.TLabel").grid(row=row, column=1, sticky="w")
            if k in guessed and guessed[k].get("quote"):
                g = guessed[k]
                src = "AI" if g.get("from") == "ai" else E.palette_name(g.get("from", ""), names)
                quote, prefix = g["quote"], f"{src} said: "
            else:
                quote, prefix = max(sample[k])[1], ""
            if len(quote) > 120:
                quote = quote[:117].rsplit(" ", 1)[0] + "…"
            ttk.Label(f, text=f"{prefix}“{quote}”", style="Muted.TLabel", wraplength=380).grid(
                row=row + 1, column=1, sticky="w")
            var = tk.StringVar(value=names.get(k, "" if k != E.ME else app.settings.get("your_name", "")))
            self.vars[k] = var
            box = ttk.Combobox(f, textvariable=var, values=known, width=18)
            box.grid(row=row, column=2, rowspan=2, padx=(12, 6))
            play = ttk.Button(f, text="▶", width=3, command=lambda key=k: self.play(key))
            play.grid(row=row, column=3, rowspan=2)
            if not self._source(k):
                play.state(["disabled"])
            row += 2

        self.remember = tk.BooleanVar(value=True)
        ttk.Checkbutton(f, text="Remember these voices and name them automatically next time",
                        variable=self.remember).grid(row=row, column=0, columnspan=4, sticky="w", pady=(16, 0))
        btns = ttk.Frame(f)
        btns.grid(row=row + 1, column=0, columnspan=4, sticky="e", pady=(16, 0))
        ttk.Button(btns, text="Skip", command=self.skip).pack(side="left", padx=(0, 8))
        ttk.Button(btns, text="Save names", style="Accent.TButton", command=self.save).pack(side="left")
        self.protocol("WM_DELETE_WINDOW", self.skip)
        self.bind("<Return>", lambda e: self.save())
        self.bind("<Escape>", lambda e: self.skip())
        self.update_idletasks()
        x = app.root.winfo_rootx() + (app.root.winfo_width() - self.winfo_width()) // 2
        y = app.root.winfo_rooty() + max(30, (app.root.winfo_height() - self.winfo_height()) // 3)
        self.geometry(f"+{x}+{y}")
        self.grab_set()
        self.focus_set()

    def _source(self, key) -> Path | None:
        kind = "me" if key == E.ME else "meeting"
        for t in self.m.get("tracks", []):
            if t["kind"] == kind:
                p = track_path(self.m, t)
                if p.exists():
                    return p
        return None

    def play(self, key):
        if not IS_WINDOWS:
            return
        src = self._source(key)
        spans = sorted(((e - s, s, e) for s, e, k, _t in self.m["segments"] if k == key), reverse=True)
        if not src or not spans:
            return
        _d, s, e = spans[0]

        def run():
            try:
                import winsound
                audio = self._audio_cache.get(src)
                if audio is None:
                    audio = self._audio_cache[src] = E.load_audio(src)
                clip = audio[int(s * E.SR):int(min(e, s + 8) * E.SR)]
                fd, path = tempfile.mkstemp(suffix=".wav")
                os.close(fd)
                with wave.open(path, "wb") as w:
                    w.setnchannels(1)
                    w.setsampwidth(2)
                    w.setframerate(E.SR)
                    w.writeframes((np.clip(clip, -1, 1) * 32000).astype(np.int16).tobytes())
                winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC)
                old, self._tmp = self._tmp, path
                if old:
                    try:
                        os.remove(old)
                    except OSError:
                        pass
            except Exception:
                traceback.print_exc()
        threading.Thread(target=run, daemon=True).start()

    def _close(self):
        if IS_WINDOWS:
            try:
                import winsound
                winsound.PlaySound(None, 0)
            except Exception:
                pass
        self.grab_release()
        self.destroy()

    def apply_count(self):
        try:
            n = int(self.count_var.get())
        except (tk.TclError, ValueError):
            return
        voices = list(dict.fromkeys(k for _s, _e, k, _t in self.m["segments"]))
        has_me = E.ME in voices
        others = sum(1 for k in voices if k.startswith("S"))
        target = n - int(has_me)
        if target < 1 or target >= others:
            return
        folder = self.m["folder"]
        self._close()
        self.app.merge_speakers(folder, target)

    def save(self):
        names = {k: v.get().strip() for k, v in self.vars.items()}
        self._close()
        self.app.speakers_saved(self.m["folder"], names, self.remember.get())

    def skip(self):
        self._close()
        self.app.speakers_skipped(self.m["folder"])


# --------------------------------------------------------------------------- #
# Settings dialog
# --------------------------------------------------------------------------- #
class SettingsDialog(tk.Toplevel):
    def __init__(self, app: App, tab=None):
        super().__init__(app.root)
        self.app = app
        s = app.settings
        self.title("Tableside settings")
        self.transient(app.root)
        self.resizable(False, False)
        self.configure(bg=app.p["bg"])
        outer = ttk.Frame(self, padding=18)
        outer.pack(fill="both", expand=True)
        nb = ttk.Notebook(outer)
        nb.pack(fill="both", expand=True)
        pages = {}
        for name in ("General", "Meetings", "Audio", "AI summary", "Voices"):
            page = ttk.Frame(nb, padding=18)
            page.columnconfigure(1, weight=1)
            nb.add(page, text=f"  {name}  ")
            pages[name] = page

        # General
        g = pages["General"]
        self.name_var = tk.StringVar(value=s.get("your_name", ""))
        self.model_var = tk.StringVar(value=self._model_label(s.get("whisper_model", "small.en")))
        self.detect_var = tk.BooleanVar(value=s.get("detect_speakers", True))
        self.theme_var = tk.StringVar(value=s.get("theme", "system"))
        self._row(g, 0, "Your name", ttk.Entry(g, textvariable=self.name_var, width=34))
        self._row(g, 1, "Transcript accuracy", ttk.Combobox(
            g, textvariable=self.model_var, state="readonly", width=40,
            values=[self._model_label(m) for m in E.WHISPER_MODELS]))
        ttk.Checkbutton(g, text="Tell different voices apart (who said what)",
                        variable=self.detect_var).grid(row=2, column=1, sticky="nw", pady=(6, 0))
        self.guess_var = tk.BooleanVar(value=s.get("guess_names", True))
        ttk.Checkbutton(g, text="Work out names when people call each other by name",
                        variable=self.guess_var).grid(row=2, column=1, sticky="sw", pady=(34, 6))
        themes = ttk.Frame(g)
        for val, label in (("system", "Match Windows"), ("light", "Light"), ("dark", "Dark")):
            ttk.Radiobutton(themes, text=label, value=val, variable=self.theme_var).pack(side="left", padx=(0, 12))
        self._row(g, 3, "Appearance", themes)
        shrink = ttk.Frame(g)
        shrink.grid(row=6, column=0, columnspan=2, sticky="w", pady=(14, 0))
        self.shrink_btn = ttk.Button(shrink, text="Shrink saved recordings", command=self._shrink)
        self.shrink_btn.pack(side="left")
        self.shrink_lbl = ttk.Label(shrink, text="Checking…", style="Small.TLabel")
        self.shrink_lbl.pack(side="left", padx=8)
        self.shrink_btn.state(["disabled"])

        def size_check():
            total, possible = E.recordings_size()
            def show():
                if not self.shrink_lbl.winfo_exists():
                    return
                if possible > 1e6:
                    self.shrink_lbl.configure(text=f"Older recordings use {total / 1e6:,.0f} MB - "
                                                   f"shrinking saves about {possible / 1e6:,.0f} MB.")
                    self.shrink_btn.state(["!disabled"])
                else:
                    self.shrink_lbl.configure(text=f"Recordings use {total / 1e6:,.0f} MB. Already compact.")
            self.app.post("call", show)
        threading.Thread(target=size_check, daemon=True).start()
        ttk.Label(g, text=f"Notes are saved in  {E.NOTES_ROOT}", style="Small.TLabel").grid(
            row=4, column=0, columnspan=2, sticky="w", pady=(18, 0))
        ttk.Button(g, text="Open notes folder",
                   command=lambda: (E.NOTES_ROOT.mkdir(parents=True, exist_ok=True), open_path(E.NOTES_ROOT))
                   ).grid(row=5, column=0, columnspan=2, sticky="w", pady=(6, 0))

        # Meetings (automatic pop-ups)
        mt = pages["Meetings"]
        self.watch_var = tk.BooleanVar(value=s.get("watch_meetings", True))
        self.browser_var = tk.BooleanVar(value=s.get("watch_browsers", True))
        self.autostop_var = tk.BooleanVar(value=s.get("auto_stop", True))
        self.bg_var = tk.BooleanVar(value=s.get("run_in_background", True))
        self.silence_var = tk.BooleanVar(value=s.get("silence_stop", True))
        self.trim_var = tk.BooleanVar(value=s.get("auto_trim", True))
        self.startup_var = tk.BooleanVar(value=W.starts_with_windows() if W.IS_WINDOWS
                                         else s.get("start_with_windows", True))
        items = [
            (self.watch_var, "Ask to record when a meeting starts",
             "Pops up when Zoom, Teams, Webex, Slack and similar apps start using your microphone."),
            (self.browser_var, "Include calls in a web browser",
             "Google Meet, Teams on the web, and so on."),
            (self.autostop_var, "Offer to stop when the call ends",
             "Stops and transcribes 30 seconds after the call app releases your microphone."),
            (self.bg_var, "Keep running in the system tray when the window is closed",
             "Needed for the pop-ups. Quit from the tray icon."),
            (self.startup_var, "Start Tableside when Windows starts",
             "Starts quietly in the tray so you never forget to turn it on."),
            (self.silence_var, f"Stop recording after {s.get('silence_minutes', 10)} minutes of silence",
             "For when you forget to press Stop. You get 30 seconds to keep recording."),
            (self.trim_var, "Cut long silences at the start and end automatically",
             "Removes more than a minute of dead air before transcribing. You can undo it."),
        ]
        for i, (var, label, hint) in enumerate(items):
            ttk.Checkbutton(mt, text=label, variable=var).grid(row=i * 2, column=0, sticky="w", pady=(8, 0))
            ttk.Label(mt, text=hint, style="Small.TLabel", wraplength=440).grid(
                row=i * 2 + 1, column=0, sticky="w", padx=(30, 0))

        # Audio
        a = pages["Audio"]
        self.spk_var = tk.StringVar(value=s.get("speaker_device", ""))
        self.mic_var = tk.StringVar(value=s.get("mic_device", ""))
        self.spk_box = ttk.Combobox(a, textvariable=self.spk_var, state="readonly", width=44)
        self.mic_box = ttk.Combobox(a, textvariable=self.mic_var, state="readonly", width=44)
        self._row(a, 0, "Meeting plays through", self.spk_box)
        self._row(a, 1, "Your microphone", self.mic_box)
        ttk.Button(a, text="Refresh devices", command=self._load_devices).grid(row=2, column=1, sticky="w", pady=6)
        ttk.Label(a, text="Pick the speakers or headset Zoom/Teams uses. Leave blank to follow "
                          "the Windows default.", style="Small.TLabel", wraplength=440).grid(
            row=3, column=0, columnspan=2, sticky="w", pady=(10, 0))
        self._load_devices()

        # AI summary
        ai = pages["AI summary"]
        self.provider_var = tk.StringVar(value=s.get("ai_provider", "local"))
        self.key_var = tk.StringVar(value=E.get_api_key())
        self.claude_var = tk.StringVar(value=E.CLAUDE_HINTS.get(s.get("claude_model"), E.CLAUDE_HINTS[E.CLAUDE_MODELS[0]]))
        self.ollama_var = tk.StringVar(value=s.get("ollama_model", "llama3.1:8b"))
        prov = ttk.Frame(ai)
        ttk.Radiobutton(prov, text="Built-in AI (free, no key, runs on this PC)", value="local",
                        variable=self.provider_var).pack(anchor="w")
        local_row = ttk.Frame(prov)
        local_row.pack(anchor="w", padx=(28, 0), pady=(2, 8))
        self.local_lbl = ttk.Label(local_row, style="Small.TLabel",
                                   text="Downloaded and ready ✓" if E.local_model_ready()
                                   else "One-time download (checking size…). Needs about 6 GB of free memory.")
        if not E.local_model_ready():
            def size_check():
                text = f"One-time {E.local_model_size_text()} download. Needs about 6 GB of free memory."
                self.app.post("call", lambda: self.local_lbl.winfo_exists() and self.local_lbl.configure(text=text))
            threading.Thread(target=size_check, daemon=True).start()
        self.local_lbl.pack(side="left")
        if not E.local_model_ready():
            self.dl_btn = ttk.Button(local_row, text="Download now", command=self._download_local)
            self.dl_btn.pack(side="left", padx=8)
        ttk.Radiobutton(prov, text="Claude (best quality, a few cents per meeting, needs a key)", value="claude",
                        variable=self.provider_var).pack(anchor="w")
        ttk.Radiobutton(prov, text="Ollama (free, runs on this PC, needs Ollama installed)", value="ollama",
                        variable=self.provider_var).pack(anchor="w", pady=(4, 0))
        self._row(ai, 0, "Summaries by", prov)
        key = ttk.Frame(ai)
        ttk.Entry(key, textvariable=self.key_var, show="•", width=34).pack(side="left")
        ttk.Button(key, text="Get a key", command=lambda: webbrowser.open(E.CLAUDE_KEYS_URL)).pack(side="left", padx=6)
        self._row(ai, 1, "Claude API key", key)
        self._row(ai, 2, "Claude model", ttk.Combobox(
            ai, textvariable=self.claude_var, state="readonly", width=40,
            values=[E.CLAUDE_HINTS[m] for m in E.CLAUDE_MODELS]))
        self._row(ai, 3, "Ollama model", ttk.Entry(ai, textvariable=self.ollama_var, width=24))
        test = ttk.Frame(ai)
        ttk.Button(test, text="Test connection", command=self._test_ai).pack(side="left")
        self.test_lbl = ttk.Label(test, text="", style="Small.TLabel")
        self.test_lbl.pack(side="left", padx=8)
        test.grid(row=4, column=1, sticky="w", pady=(10, 0))
        ttk.Label(ai, text="Built-in AI keeps everything on this PC. With Claude, only the transcript text "
                           "is sent - never the audio - and your key is stored in Windows Credential Manager.",
                  style="Small.TLabel", wraplength=440).grid(
            row=5, column=0, columnspan=2, sticky="w", pady=(14, 0))

        # Voices
        v = pages["Voices"]
        ttk.Label(v, text="People Tableside recognises automatically:").grid(row=0, column=0, sticky="w")
        self.voice_list = tk.Listbox(v, height=8, width=40, relief="flat", highlightthickness=1,
                                     bg=app.p["surface"], fg=app.p["text"], highlightbackground=app.p["border"],
                                     selectbackground=app.p["sel"], selectforeground=app.p["text"],
                                     font=("Segoe UI", 11))
        self.voice_list.grid(row=1, column=0, sticky="nsew", pady=(6, 6))
        ttk.Button(v, text="Forget selected voice", command=self._forget).grid(row=2, column=0, sticky="w")
        ttk.Label(v, text="Voices are learned when you name speakers after a meeting. They never "
                          "leave this PC.", style="Small.TLabel", wraplength=440).grid(
            row=3, column=0, sticky="w", pady=(12, 0))
        self.book = E.VoiceBook()
        for n in self.book.names():
            self.voice_list.insert("end", n)

        btns = ttk.Frame(outer)
        btns.pack(fill="x", pady=(14, 0))
        ttk.Button(btns, text="Save", style="Accent.TButton", command=self.save).pack(side="right")
        ttk.Button(btns, text="Cancel", command=self.destroy).pack(side="right", padx=8)
        if tab in pages:
            nb.select(pages[tab])
        self.bind("<Escape>", lambda e: self.destroy())
        self.update_idletasks()
        x = app.root.winfo_rootx() + (app.root.winfo_width() - self.winfo_width()) // 2
        y = app.root.winfo_rooty() + 60
        self.geometry(f"+{x}+{y}")
        self.grab_set()
        self.wait_window()

    @staticmethod
    def _row(parent, r, label, widget):
        ttk.Label(parent, text=label).grid(row=r, column=0, sticky="nw", padx=(0, 14), pady=8)
        widget.grid(row=r, column=1, sticky="w", pady=6)

    @staticmethod
    def _model_label(m):
        return f"{m}  -  {E.WHISPER_HINTS.get(m, '')}"

    def _load_devices(self):
        spk, mic = [""], ["", R_NO_MIC]
        if R.pyaudio is not None:
            try:
                if self.app.pa is not None and not self.app.session:
                    self.app.pa.terminate()
                    self.app.pa = None
                if self.app.pa is None:
                    self.app.pa = R.pyaudio.PyAudio()
                loopbacks, mics, _d1, _d2 = R.list_devices(self.app.pa)
                spk += list(loopbacks)
                mic += list(mics)
            except Exception:
                traceback.print_exc()
        self.spk_box["values"] = spk
        self.mic_box["values"] = mic

    def _test_ai(self):
        settings = dict(self.app.settings)
        settings.update(self._ai_values())
        if settings["ai_provider"] == "claude":
            E.set_api_key(self.key_var.get().strip())
        if settings["ai_provider"] == "local" and not E.local_model_ready():
            self.test_lbl.configure(text="Press “Download now” first.")
            return
        self.test_lbl.configure(text="Testing… (loading the AI takes a moment)"
                                if settings["ai_provider"] == "local" else "Testing…")

        def run():
            try:
                reply = E.ask_ai(settings, "Reply with the single word OK.", "Test", max_tokens=20)
                text = "Connected ✓" if reply else "Connected, but got an empty reply"
            except Exception as e:
                text = f"✗ {e}"
            self.app.post("call", lambda: self.test_lbl.configure(text=text[:80]))
        threading.Thread(target=run, daemon=True).start()

    def _shrink(self):
        self.shrink_btn.state(["disabled"])

        def log(text):
            self.app.post("call", lambda: self.shrink_lbl.winfo_exists() and self.shrink_lbl.configure(text=text))

        def run():
            saved = E.shrink_all_recordings(log)
            log(f"Done - freed {saved / 1e6:,.0f} MB.")
        threading.Thread(target=run, daemon=True).start()

    def _download_local(self):
        self.dl_btn.state(["disabled"])

        def log(text):
            self.app.post("call", lambda: self.local_lbl.winfo_exists() and self.local_lbl.configure(text=text[:70]))

        def run():
            try:
                E.ensure_local_model(log)
                log("Downloaded and ready ✓")
            except Exception as e:
                log(f"Download failed: {e}")
                self.app.post("call", lambda: self.dl_btn.winfo_exists() and self.dl_btn.state(["!disabled"]))
        threading.Thread(target=run, daemon=True).start()

    def _ai_values(self):
        claude = next((m for m, h in E.CLAUDE_HINTS.items() if h == self.claude_var.get()), E.CLAUDE_MODELS[0])
        return {"ai_provider": self.provider_var.get(), "claude_model": claude,
                "ollama_model": self.ollama_var.get().strip() or "llama3.1:8b"}

    def _forget(self):
        sel = self.voice_list.curselection()
        if not sel:
            return
        name = self.voice_list.get(sel[0])
        if messagebox.askyesno(E.APP_NAME, f"Forget {name}'s voice?", parent=self):
            self.book.forget(name)
            self.book.save()
            self.voice_list.delete(sel[0])

    def save(self):
        s = self.app.settings
        s["your_name"] = self.name_var.get().strip() or "Me"
        s["whisper_model"] = self.model_var.get().split()[0]
        s["detect_speakers"] = self.detect_var.get()
        s["guess_names"] = self.guess_var.get()
        s["watch_meetings"] = self.watch_var.get()
        s["watch_browsers"] = self.browser_var.get()
        s["auto_stop"] = self.autostop_var.get()
        s["run_in_background"] = self.bg_var.get()
        s["start_with_windows"] = self.startup_var.get()
        s["silence_stop"] = self.silence_var.get()
        s["auto_trim"] = self.trim_var.get()
        s["theme"] = self.theme_var.get()
        s["speaker_device"] = self.spk_var.get()
        s["mic_device"] = self.mic_var.get()
        s.update(self._ai_values())
        E.set_api_key(self.key_var.get().strip())
        self.grab_release()
        self.destroy()
        self.app.settings_saved()


# --------------------------------------------------------------------------- #
# Trim recording dialog
# --------------------------------------------------------------------------- #
class TrimDialog(tk.Toplevel):
    W, H, PAD = 720, 150, 14

    def __init__(self, app: App, meeting: dict):
        super().__init__(app.root)
        self.app, self.m = app, meeting
        self.title("Trim recording")
        self.transient(app.root)
        self.resizable(False, False)
        self.configure(bg=app.p["bg"])
        self.tracks = E.recorded_tracks(meeting)
        self.length = max(E.wav_seconds(p) for p in self.tracks)
        self.start, self.end = 0.0, self.length
        self.env = None
        self.drag = None
        self._tmp = None
        p = app.p

        f = ttk.Frame(self, padding=22)
        f.pack(fill="both", expand=True)
        ttk.Label(f, text="Trim recording", style="H1.TLabel").grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(f, text="Drag the handles to keep only the part you want. Everything outside is cut "
                          "from the audio and the transcript.", style="Muted.TLabel", wraplength=700).grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(2, 12))
        self.canvas = tk.Canvas(f, width=self.W, height=self.H, bg=p["surface"], highlightthickness=1,
                                highlightbackground=p["border"], cursor="sb_h_double_arrow")
        self.canvas.grid(row=2, column=0, columnspan=2)
        self.canvas.bind("<Button-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._move)
        self.canvas.bind("<ButtonRelease-1>", lambda e: setattr(self, "drag", None))
        self.info = ttk.Label(f, text="", style="Muted.TLabel")
        self.info.grid(row=3, column=0, columnspan=2, sticky="w", pady=(8, 12))

        left = ttk.Frame(f)
        left.grid(row=4, column=0, sticky="w")
        ttk.Button(left, text="▶  Play start", command=lambda: self.play(self.start, self.start + 8)).pack(side="left")
        ttk.Button(left, text="▶  Play end", command=lambda: self.play(max(self.start, self.end - 8), self.end)
                   ).pack(side="left", padx=6)
        self.auto_btn = ttk.Button(left, text="Cut silence automatically", command=self.auto)
        self.auto_btn.pack(side="left", padx=6)
        right = ttk.Frame(f)
        right.grid(row=4, column=1, sticky="e")
        ttk.Button(right, text="Cancel", command=self._close).pack(side="left", padx=(0, 8))
        ttk.Button(right, text="Trim", style="Accent.TButton", command=self.save).pack(side="left")
        f.columnconfigure(1, weight=1)

        self.canvas.create_text(self.W // 2, self.H // 2, text="Reading the recording…", fill=p["muted"])
        self._update_info()
        threading.Thread(target=self._load, daemon=True).start()
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.bind("<Escape>", lambda e: self._close())
        self.update_idletasks()
        x = app.root.winfo_rootx() + (app.root.winfo_width() - self.winfo_width()) // 2
        y = app.root.winfo_rooty() + 80
        self.geometry(f"+{x}+{y}")
        self.grab_set()

    # -- data
    def _load(self):
        try:
            env = E.combined_loudness(self.tracks)
        except Exception:
            traceback.print_exc()
            env = np.zeros(1, dtype=np.float32)
        self.app.post("call", lambda: self._loaded(env))

    def _loaded(self, env):
        if not self.winfo_exists():
            return
        self.env = env
        self.draw()

    # -- geometry
    def x_of(self, t):
        return self.PAD + (self.W - 2 * self.PAD) * (t / max(self.length, 1e-6))

    def t_of(self, x):
        return min(self.length, max(0.0, (x - self.PAD) / (self.W - 2 * self.PAD) * self.length))

    def draw(self):
        c, p = self.canvas, self.app.p
        c.delete("all")
        top, bottom = 14, self.H - 24
        mid = (top + bottom) / 2
        if self.env is not None and len(self.env):
            cols = self.W - 2 * self.PAD
            env = self.env
            per = max(1, len(env) / cols)
            for i in range(cols):
                a, b = int(i * per), max(int(i * per) + 1, int((i + 1) * per))
                v = float(env[a:b].max()) if a < len(env) else 0.0
                h = min(1.0, np.log1p(v) / np.log1p(25)) * (bottom - top) / 2
                x = self.PAD + i
                t = (i + 0.5) / cols * self.length
                color = BRAND if self.start <= t <= self.end else p["border"]
                c.create_line(x, mid - max(h, 0.5), x, mid + max(h, 0.5), fill=color)
        # dim the parts being cut
        for a, b in ((0, self.start), (self.end, self.length)):
            if b > a:
                c.create_rectangle(self.x_of(a), top - 6, self.x_of(b), bottom + 6,
                                   fill=p["bg"], stipple="gray50", outline="")
        # handles
        for t in (self.start, self.end):
            x = self.x_of(t)
            c.create_line(x, top - 8, x, bottom + 8, fill=p["text"], width=2)
            c.create_rectangle(x - 6, top - 10, x + 6, top + 2, fill=p["text"], outline="")
        # time ruler
        step = next(s for s in (5, 10, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200)
                    if self.length / s <= 8)
        t = 0
        while t <= self.length + 0.1:
            x = self.x_of(t)
            c.create_line(x, bottom + 10, x, bottom + 14, fill=p["muted"])
            label = E.fmt_time(t)[3:] if self.length < 3600 else E.fmt_time(t)
            c.create_text(x, bottom + 20, text=label, fill=p["muted"], font=("Segoe UI", 8))
            t += step
        self._update_info()

    def _update_info(self):
        cut = self.start + (self.length - self.end)
        self.info.configure(text=f"Keep  {E.fmt_time(self.start)}  to  {E.fmt_time(self.end)}"
                                 f"     ·     new length {E.fmt_length(self.end - self.start)}"
                                 + (f"     ·     cutting {E.fmt_length(cut)}" if cut >= 1 else ""))

    # -- interaction
    def _press(self, e):
        xs, xe = self.x_of(self.start), self.x_of(self.end)
        self.drag = "start" if abs(e.x - xs) <= abs(e.x - xe) else "end"
        self._move(e)

    def _move(self, e):
        if not self.drag:
            return
        t = self.t_of(e.x)
        if self.drag == "start":
            self.start = min(t, self.end - 1)
        else:
            self.end = max(t, self.start + 1)
        self.draw()

    def auto(self):
        if self.env is None:
            return
        b = E.speech_bounds(self.env)
        if not b:
            self.info.configure(text="No speech found in this recording.")
            return
        self.start, self.end = max(0.0, b[0] - 3), min(self.length, b[1] + 3)
        self.draw()

    def play(self, t0, t1):
        if not IS_WINDOWS:
            return

        def run():
            try:
                import winsound
                clip = E.read_mix(self.tracks, t0, max(t0 + 0.5, t1))
                fd, path = tempfile.mkstemp(suffix=".wav")
                os.close(fd)
                with wave.open(path, "wb") as w:
                    w.setnchannels(1)
                    w.setsampwidth(2)
                    w.setframerate(E.SR)
                    w.writeframes((clip * 32000).astype(np.int16).tobytes())
                winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC)
                old, self._tmp = self._tmp, path
                if old:
                    try:
                        os.remove(old)
                    except OSError:
                        pass
            except Exception:
                traceback.print_exc()
        threading.Thread(target=run, daemon=True).start()

    def _close(self):
        if IS_WINDOWS:
            try:
                import winsound
                winsound.PlaySound(None, 0)
            except Exception:
                pass
        self.grab_release()
        self.destroy()

    def save(self):
        if self.start <= 0.5 and self.end >= self.length - 0.5:
            self._close()
            return
        start, end, folder = self.start, self.end, self.m["folder"]
        self._close()
        self.app.trim_saved(folder, start, end)


# --------------------------------------------------------------------------- #
# Corner pop-ups ("Zoom meeting started - record it?")
# --------------------------------------------------------------------------- #
def work_area(widget) -> tuple[int, int, int, int]:
    if IS_WINDOWS:
        try:
            import ctypes
            from ctypes import wintypes
            r = wintypes.RECT()
            if ctypes.windll.user32.SystemParametersInfoW(0x30, 0, ctypes.byref(r), 0):
                return r.left, r.top, r.right, r.bottom
        except Exception:
            pass
    return 0, 0, widget.winfo_screenwidth(), widget.winfo_screenheight()


class Toast(tk.Toplevel):
    def __init__(self, app: App):
        super().__init__(app.root)
        self.app = app
        self.withdraw()
        self.overrideredirect(True)
        try:
            self.attributes("-topmost", True)
        except tk.TclError:
            pass
        self.configure(bg=BRAND)
        self.body = ttk.Frame(self, padding=(18, 14, 18, 16))
        self.body.pack(fill="both", expand=True, padx=(4, 1), pady=1)
        self.body.columnconfigure(1, weight=1)

    def header(self, title):
        ttk.Label(self.body, image=self.app.img[32]).grid(row=0, column=0, rowspan=2, sticky="nw", padx=(0, 12))
        ttk.Label(self.body, text=title, style="H2.TLabel").grid(row=0, column=1, sticky="w")
        ttk.Button(self.body, text="✕", width=3, command=self.on_close).grid(row=0, column=2, sticky="ne")

    def show(self):
        self.update_idletasks()
        w, h = self.winfo_reqwidth(), self.winfo_reqheight()
        _x0, _y0, x1, y1 = work_area(self)
        self.geometry(f"+{x1 - w - 18}+{y1 - h - 18}")
        self.deiconify()
        self.lift()
        if IS_WINDOWS:
            try:
                import winsound
                winsound.MessageBeep(0x40)
            except Exception:
                pass

    def on_close(self):
        self.app._close_toast()


class MeetingToast(Toast):
    def __init__(self, app: App, label: str):
        super().__init__(app)
        self.label = label
        browser = label == "your browser"
        self.header("Call started in your browser" if browser else f"{label} meeting started")
        ttk.Label(self.body, text="Record it with Tableside?", style="Muted.TLabel").grid(
            row=1, column=1, columnspan=2, sticky="w")
        self.title_var = tk.StringVar(
            value=f"{'Meeting' if browser else label + ' meeting'} {dt.datetime.now():%b %d}".replace(" 0", " "))
        ttk.Entry(self.body, textvariable=self.title_var, width=34).grid(
            row=2, column=0, columnspan=3, sticky="ew", pady=(12, 8))
        opts = ttk.Frame(self.body)
        opts.grid(row=3, column=0, columnspan=3, sticky="w")
        for var, text in ((app.opt_audio, "Audio"), (app.opt_transcript, "Transcript"),
                          (app.opt_summary, "AI summary")):
            ttk.Checkbutton(opts, text=text, variable=var,
                            command=lambda v=var: app._options_changed(v)).pack(side="left", padx=(0, 12))
        btns = ttk.Frame(self.body)
        btns.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(14, 0))
        PillButton(btns, "●  Record", self.record, size=11).pack(side="left", fill="x", expand=True)
        ttk.Button(btns, text="Not now", command=self.on_close).pack(side="left", padx=(8, 0))
        self.after(120_000, self._timeout)
        self.show()

    def record(self):
        self.app.toast_record(self.label, self.title_var.get().strip(), self)

    def on_close(self):
        self.app.toast_dismiss(self.label)

    def _timeout(self):
        if self.app.toast is self:
            self.on_close()


class CallEndedToast(Toast):
    SECONDS = 30

    def __init__(self, app: App, label: str | None, title: str | None = None):
        super().__init__(app)
        self.left = self.SECONDS
        self.header(title or f"Your {label} call ended")
        self.msg = ttk.Label(self.body, text="", style="Muted.TLabel")
        self.msg.grid(row=1, column=1, columnspan=2, sticky="w")
        btns = ttk.Frame(self.body)
        btns.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(14, 0))
        PillButton(btns, "■  Stop now", self.stop, color=STOP_RED, hover=STOP_HOVER).pack(
            side="left", fill="x", expand=True)
        ttk.Button(btns, text="Keep recording", command=self.on_close).pack(side="left", padx=(8, 0))
        self._tick()
        self.show()

    def _tick(self):
        if self.app.toast is not self and self.left < self.SECONDS:
            return
        if not self.app.session:
            self.app._close_toast()
            return
        if self.left <= 0:
            self.stop()
            return
        self.msg.configure(text=f"Stopping and transcribing in {self.left} seconds…")
        self.left -= 1
        self.after(1000, self._tick)

    def stop(self):
        self.app._close_toast()
        self.app.stop_recording()

    def on_close(self):
        self.app.toast_keep_recording()


# --------------------------------------------------------------------------- #
# Start-up
# --------------------------------------------------------------------------- #
PORT_FILE = E.APP_DATA / "instance.port"


def ping_running_copy(show: bool) -> bool:
    """True if Tableside is already running (and asks it to show its window if `show`)."""
    try:
        port = int(PORT_FILE.read_text().strip())
        with socket.create_connection(("127.0.0.1", port), timeout=1.5) as s:
            s.sendall(b"show\n" if show else b"ping\n")
            return s.recv(16).startswith(b"tableside")
    except (OSError, ValueError):
        return False


def serve_instance(app: App) -> None:
    """Listen (on this PC only) for a second launch asking us to show the window."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(4)
    PORT_FILE.write_text(str(srv.getsockname()[1]))

    def loop():
        while True:
            try:
                conn, _ = srv.accept()
                with conn:
                    conn.settimeout(2)
                    data = conn.recv(16)
                    conn.sendall(b"tableside")
                    if data.startswith(b"show"):
                        app.post("show", None)
            except OSError:
                continue
    threading.Thread(target=loop, daemon=True).start()
def selftest() -> int:
    """`Tableside.exe --selftest`: checks the bundled parts load. Writes selftest.txt."""
    out = []
    try:
        import av, ctranslate2, docx, openpyxl, sv_ttk, keyring, send2trash  # noqa: F401
        import faster_whisper, onnxruntime, sherpa_onnx, PIL, onnxruntime_genai  # noqa: F401
        from faster_whisper.vad import VadOptions, get_speech_timestamps
        tone = (0.3 * np.sin(np.linspace(0, 2 * np.pi * 440 * 2, E.SR * 2))).astype(np.float32)
        get_speech_timestamps(tone, VadOptions())
        fd, wav = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        with wave.open(wav, "wb") as w:
            w.setnchannels(2)
            w.setsampwidth(2)
            w.setframerate(48000)
            w.writeframes((np.repeat(tone, 6) * 32000).astype(np.int16).tobytes())
        audio = E.load_audio(wav)
        os.remove(wav)
        out.append(f"decode ok {len(audio)}")
        out.append(f"sherpa {sherpa_onnx.__version__} ctranslate2 {ctranslate2.__version__} av {av.__version__}")
        out.append("audio capture " + ("ok" if R.pyaudio is not None or not IS_WINDOWS else "MISSING"))
        out.append("PASS")
        code = 0
    except Exception:
        out.append(traceback.format_exc())
        out.append("FAIL")
        code = 1
    (E.APP_DATA / "selftest.txt").write_text("\n".join(out), encoding="utf-8")
    return code


def main():
    import multiprocessing
    multiprocessing.freeze_support()   # Tableside.exe also runs the background worker
    E.APP_DATA.mkdir(parents=True, exist_ok=True)
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    E.NOTES_ROOT.mkdir(parents=True, exist_ok=True)
    # Windowed apps have no console: send output to a log file instead of crashing.
    if sys.stdout is None or sys.stderr is None or getattr(sys, "frozen", False):
        log = open(E.APP_DATA / "tableside.log", "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stdout if sys.stdout is not None and not getattr(sys, "frozen", False) else log
        sys.stderr = sys.stderr if sys.stderr is not None and not getattr(sys, "frozen", False) else log
        print(f"--- Tableside started {dt.datetime.now():%Y-%m-%d %H:%M:%S}")
    background = "--background" in sys.argv
    if ping_running_copy(show=not background):
        return
    if IS_WINDOWS:
        import ctypes
        try:  # own taskbar button and icon, and lets you pin it
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(E.APP_ID)
        except Exception:
            pass
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass

    root = tk.Tk()
    for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont"):
        try:
            tkfont.nametofont(name).configure(family="Segoe UI", size=10)
        except tk.TclError:
            pass
    app = App(root, background=background)
    try:
        serve_instance(app)
    except OSError:
        traceback.print_exc()

    def report(exc, val, tb):
        traceback.print_exception(exc, val, tb)
        messagebox.showerror(E.APP_NAME, f"Something went wrong:\n{val}")

    root.report_callback_exception = report
    root.mainloop()
    if app.jobs is not None:
        app.jobs.stop()
    try:
        PORT_FILE.unlink()
    except OSError:
        pass
    os._exit(0)   # make sure no background thread (tray, watcher) keeps the process alive


if __name__ == "__main__":
    main()
