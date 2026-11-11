"""
Tableside background worker.

Transcribing, telling voices apart and writing summaries run in a separate process at
below-normal priority. Some of the speech libraries hold Python's interpreter lock for
many seconds at a time (speaker detection held it for 9 s on a 4.5-minute test call), so
running them on a thread froze the window and starved a new recording's audio callbacks.
A separate process can't block the window, and the lower priority means the window and
any recording in progress always get the CPU first.

`JobRunner` lives in the main process; `run()` is the worker process. They talk through
two queues. Messages from the worker are the same tuples `App._poll()` already handles
("progress", "refresh", "note", "error", "processed").

Keep this module free of GUI imports: it is imported by the worker process.
"""

from __future__ import annotations

import multiprocessing
import os
import queue
import sys
import threading
import time
import traceback
from pathlib import Path

import engine as E


def track_path(meeting: dict, track: dict) -> Path:
    p = Path(track["file"])
    return p if p.is_absolute() else Path(meeting["folder"]) / p


def unnamed_speakers(meeting: dict) -> list[str]:
    names = meeting.get("names", {})
    keys = dict.fromkeys(k for _s, _e, k, _t in meeting.get("segments", []))
    return [k for k in keys if k.startswith("S") and not names.get(k)]


# --------------------------------------------------------------------------- #
# Main-process side
# --------------------------------------------------------------------------- #
class JobRunner:
    """Starts the worker process, forwards its messages to `post`, and restarts it if it dies.

    `put(("process", folder, live))` / `put(("summary", folder, False))` queue a job, like
    the old thread queue did. The settings in effect at that moment go with the job."""

    def __init__(self, post, get_settings):
        self.post = post
        self.get_settings = get_settings
        self.ctx = multiprocessing.get_context("spawn")
        self.lock = threading.Lock()
        self.waiting: list[tuple] = []     # sent to the worker and not finished yet, oldest first
        self.stopping = False
        self._start()
        threading.Thread(target=self._pump, daemon=True, name="worker-messages").start()

    def _start(self):
        self.jobs = self.ctx.Queue()
        self.out = self.ctx.Queue()
        self.proc = self.ctx.Process(target=run, args=(self.jobs, self.out), daemon=True,
                                     name="TablesideWorker")
        self.proc.start()

    def put(self, job: tuple) -> None:
        kind, folder, live = job
        item = (kind, str(folder), bool(live), dict(self.get_settings()))
        with self.lock:
            self.waiting.append(item)
            self.jobs.put(item)

    def _pump(self):
        while not self.stopping:
            try:
                msg = self.out.get(timeout=1)
            except queue.Empty:
                if not self.proc.is_alive() and not self.stopping:
                    self._recover()
                continue
            except (EOFError, OSError):
                if self.stopping:
                    return
                time.sleep(1)
                continue
            if msg[0] == "finished":
                with self.lock:
                    for i, item in enumerate(self.waiting):
                        if item[0] == msg[1] and item[1] == msg[2]:
                            del self.waiting[i]
                            break
                continue
            self.post(*msg)

    def _recover(self):
        """The worker process died (crash or out of memory). Fail the job it was on,
        start a new worker and hand it the rest of the queue."""
        with self.lock:
            pending, self.waiting = self.waiting, []
            print(f"Background worker stopped (exit code {self.proc.exitcode}); restarting it.")
            if pending:
                kind, folder = pending[0][0], pending[0][1]
                text = (f"{'Transcription' if kind == 'process' else 'AI summary'} stopped unexpectedly "
                        f"(the PC may have run low on memory). Try again with a smaller speech model "
                        f"in Settings.")
                try:
                    m = E.read_meeting(Path(folder))
                    if m:   # a crash during the summary still leaves a good transcript
                        m["status"] = "ready" if m.get("status") == "summarizing" else "error"
                        m["error"] = text
                        E.write_meeting(m)
                except Exception:
                    traceback.print_exc()
                self.post("error", folder, text)
                self.post("refresh", folder)
            self._start()
            for item in pending[1:]:
                self.waiting.append(item)
                self.jobs.put(item)

    def stop(self):
        self.stopping = True
        try:
            self.jobs.put(None)
            self.proc.join(1.5)
        except Exception:
            pass
        if self.proc.is_alive():
            self.proc.terminate()       # quitting mid-job: the meeting is resumed next launch


# --------------------------------------------------------------------------- #
# Worker-process side
# --------------------------------------------------------------------------- #
def _lower_priority():
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes
        BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
        try:
            k32 = ctypes.WinDLL("kernel32")   # own copy, so these type settings stay local
            # without these, ctypes cuts the 64-bit process handle to 32 bits and the call fails
            k32.GetCurrentProcess.restype = wintypes.HANDLE
            k32.SetPriorityClass.argtypes = (wintypes.HANDLE, wintypes.DWORD)
            k32.SetPriorityClass(k32.GetCurrentProcess(), BELOW_NORMAL_PRIORITY_CLASS)
        except Exception:
            pass
    else:
        try:
            os.nice(5)
        except Exception:
            pass


def _setup_log():
    # A windowed Tableside.exe has no console; write to the same log as the main process.
    if sys.stdout is None or sys.stderr is None or getattr(sys, "frozen", False):
        try:
            E.APP_DATA.mkdir(parents=True, exist_ok=True)
            log = open(E.APP_DATA / "tableside.log", "a", encoding="utf-8", buffering=1)
            sys.stdout = sys.stderr = log
        except OSError:
            sys.stdout = sys.stderr = open(os.devnull, "w")


def _watch_parent():
    """Quit if Tableside itself is gone, so a worker is never left running on its own."""
    parent = multiprocessing.parent_process()
    if parent is None:
        return

    def loop():
        while True:
            time.sleep(2)
            if not parent.is_alive():
                os._exit(0)
    threading.Thread(target=loop, daemon=True).start()


def run(jobs, out):
    _setup_log()
    _lower_priority()
    _watch_parent()
    w = Worker(out)
    while True:
        job = jobs.get()
        if job is None:
            return
        kind, folder, live, settings = job
        try:
            w.handle(kind, folder, live, settings)
        finally:
            out.put(("finished", kind, folder))


class Worker:
    def __init__(self, out):
        self.out = out
        self.pct: dict[str, float] = {}
        self._whisper = None
        self._whisper_name = None
        self._speakers = None

    def post(self, *msg):
        if msg[0] == "progress":
            self.pct[msg[1]] = msg[2]
        self.out.put(msg)

    def handle(self, kind, folder, live, settings):
        try:
            if kind == "process":
                self._process(folder, live, settings)
            else:
                self._summarize(folder, settings)
        except Exception as e:
            traceback.print_exc()
            m = E.read_meeting(Path(folder))
            if m:
                m["status"] = "error"
                m["error"] = str(e)
                E.write_meeting(m)
            self.post("error", folder, f"{'Transcription' if kind == 'process' else 'AI summary'} "
                                       f"failed: {e}")
        finally:
            self.pct.pop(folder, None)
            self.post("refresh", folder)

    def _get_whisper(self, name, prog):
        if self._whisper_name != name:
            prog(0.01, f"Loading the speech model “{name}”…")
            self._whisper = None          # free the old model before loading another
            self._whisper = E.load_whisper(name, log=lambda text: prog(None, text))
            self._whisper_name = name
        return self._whisper

    def _get_speakers(self, prog):
        if self._speakers is None:
            self._speakers = E.SpeakerEngine(log=lambda t: prog(None, t))
        return self._speakers

    def _process(self, folder: str, live: bool, settings: dict):
        m = E.read_meeting(Path(folder))
        m["status"] = "processing"
        m.pop("error", None)
        E.write_meeting(m)
        self.post("refresh", folder)
        opts = E.options(m)
        if live and settings.get("auto_trim", True) and "untrimmed" not in m:
            self.post("progress", folder, 1, "Checking for long silences…")
            try:   # cut dead air before transcribing it (saves time, too)
                bounds = E.auto_trim_bounds(m)
                if bounds:
                    E.trim_meeting(m, *bounds, auto=True)
                    E.write_meeting(m)
            except Exception:
                traceback.print_exc()

        state = {"base": 0.0, "span": 1.0}

        def prog(frac, text):
            if frac is not None:
                pct = 100 * (state["base"] + state["span"] * max(0.0, min(1.0, frac)))
            else:
                pct = self.pct.get(folder, 0)
            self.post("progress", folder, pct, text)

        def phase(base, span):
            state["base"], state["span"] = base, span

        meeting_tracks = [track_path(m, t) for t in m["tracks"] if t["kind"] == "meeting"]
        mic_tracks = [track_path(m, t) for t in m["tracks"] if t["kind"] == "me"]
        for p in meeting_tracks + mic_tracks:
            if not p.exists():
                raise RuntimeError(f"The audio file is missing: {p}")
        detect = settings.get("detect_speakers", True)
        mic_share = 0.3 if mic_tracks else 0.0
        phase(0, 0.05)
        whisper = self._get_whisper(settings["whisper_model"], prog)

        segments: list[E.Segment] = []
        names: dict = {}
        fingerprints: dict = {}
        duration = m.get("duration") or 0.0
        span_each = (0.95 - mic_share) / max(1, len(meeting_tracks))
        base = 0.05
        for path in meeting_tracks:
            phase(base, span_each * 0.1)
            prog(0, "Reading the meeting audio…")
            audio = E.load_audio(path)
            duration = max(duration, len(audio) / E.SR)
            turns = []
            if detect and len(audio) > E.SR:
                phase(base + span_each * 0.1, span_each * 0.3)
                try:
                    eng = self._get_speakers(prog)
                    prog(0, "Telling the voices apart…")
                    turns = eng.diarize(audio, 0, progress=lambda f: prog(f, "Telling the voices apart…"))
                    # merge groups that are really the same person (voices vary over a call)
                    turns, _ = E.tidy_turns(turns, eng.speaker_fingerprints(audio, turns))
                except Exception as e:
                    traceback.print_exc()
                    self.post("note", folder, f"Speaker detection was skipped: {e}")
                    turns = []
            phase(base + span_each * 0.4, span_each * 0.6)
            prog(0, "Transcribing the meeting…")
            asr, _ = E.transcribe(whisper, audio, words=bool(turns),
                                  progress=lambda f: prog(f, f"Transcribing the meeting… {int(f * 100)}%"))
            if turns:
                segs = E.assign_speakers(asr, turns)
                try:
                    fingerprints.update(self._get_speakers(prog).speaker_fingerprints(audio, turns))
                except Exception:
                    traceback.print_exc()
            else:
                segs = [E.Segment(a["start"], a["end"], "S1", a["text"]) for a in asr]
                names["S1"] = "Others" if mic_tracks else "Speaker"
            segments += segs
            base += span_each
            del audio

        mine: list[E.Segment] = []
        for path in mic_tracks:
            phase(0.95 - mic_share, mic_share)
            prog(0, "Transcribing your microphone…")
            asr, d = E.transcribe(whisper, path, words=False,
                                  progress=lambda f: prog(f, f"Transcribing your microphone… {int(f * 100)}%"))
            duration = max(duration, d)
            mine += [E.Segment(a["start"], a["end"], E.ME, a["text"]) for a in asr]
        if mic_tracks:
            names[E.ME] = settings.get("your_name") or "Me"
            mine = E.remove_echo(mine, segments)

        merged = sorted(segments + mine, key=lambda s: s.start)
        used = {s.speaker for s in merged}
        fingerprints = {k: v for k, v in fingerprints.items() if k in used}
        book = E.VoiceBook()
        matches = book.match(fingerprints)
        for k, n in matches.items():
            names.setdefault(k, n)

        # Work out names from how people address each other ("Alan, how's the grading plan?")
        guessed: dict = {}
        if settings.get("guess_names", True):
            prog(1.0, "Working out who is who…")
            for k, g in E.guess_names(merged, names, set(book.names())).items():
                names[k] = g["name"]
                guessed[k] = g
            remaining = [k for k in dict.fromkeys(s.speaker for s in merged)
                         if k.startswith("S") and not names.get(k)]
            # Only fast remote/Ollama AI double-checks names; the built-in model would add minutes.
            provider = settings.get("ai_provider", "local")
            ai_ready = provider == "ollama" or (provider == "claude" and bool(E.get_api_key()))
            if remaining and opts["summary"] and ai_ready:   # the transcript is going to the AI anyway
                tmp = {"title": m["title"], "started": m["started"], "names": names,
                       "segments": [[s.start, s.end, s.speaker, s.text] for s in merged]}
                try:
                    for k, g in E.ai_guess_names(tmp, settings, remaining).items():
                        names[k] = g["name"]
                        guessed[k] = g
                except Exception as e:
                    self.post("note", folder, f"AI name check skipped: {e}")

        m = E.read_meeting(Path(folder))
        m.update(segments=[[round(s.start, 2), round(s.end, 2), s.speaker, s.text] for s in merged],
                 names=names, fingerprints=fingerprints, auto_named=sorted(matches),
                 guessed=guessed, names_confirmed=False,
                 duration=duration, status="ready")
        m.pop("summary", None)
        need_names = bool(unnamed_speakers(m))
        if opts["summary"] and merged:
            if need_names and live:
                m["summary_pending"] = True
            else:
                m["status"] = "summarizing"
        E.write_meeting(m)
        prog(1.0, "Saving notes…")
        E.export_files(m)
        E.write_meeting(m)

        if not opts["audio"] and not m.get("imported"):
            for t in m["tracks"]:
                try:
                    track_path(m, t).unlink()
                except OSError:
                    pass
            m["audio_deleted"] = True
            E.write_meeting(m)

        self.post("processed", folder, need_names and live)
        if m["status"] == "summarizing":
            self._summarize(folder, settings)

    def _summarize(self, folder: str, settings: dict):
        m = E.read_meeting(Path(folder))
        m["status"] = "summarizing"
        E.write_meeting(m)
        self.post("progress", folder, 5, "Writing the AI summary…")
        self.post("refresh", folder)
        last = {"text": "Writing the AI summary…", "pct": 5.0}

        def log(text):
            last["text"] = text
            self.post("progress", folder, last["pct"], text)

        def progress(frac):
            last["pct"] = 5 + 95 * frac
            self.post("progress", folder, last["pct"], last["text"])
        try:
            summary = E.summarize(m, settings, log=log, progress=progress)
        except Exception as e:
            m = E.read_meeting(Path(folder))
            m["status"] = "ready"
            E.write_meeting(m)
            self.post("error", folder, f"The transcript is saved, but the AI summary failed: {e}")
            return
        m = E.read_meeting(Path(folder))
        m["summary"] = summary
        m["status"] = "ready"
        m.pop("summary_pending", None)
        E.export_files(m)
        E.write_meeting(m)
