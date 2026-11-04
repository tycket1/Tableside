"""
Tableside engine: transcription, speaker detection, voice memory and exports.
No GUI code lives here, so it can be tested on its own.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import difflib
import json
import os
import re
import tarfile
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import sys

import numpy as np

RES = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
APP_NAME = "Tableside"
APP_ID = "Tableside.MeetingNotes"
APP_DATA = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / ".local" / "share")) / APP_NAME
NOTES_ROOT = Path.home() / "Documents" / APP_NAME
MODEL_DIR = APP_DATA / "models"
VOICES_FILE = APP_DATA / "voices.json"
SETTINGS_FILE = APP_DATA / "settings.json"
MEETING_FILE = "meeting.json"

SR = 16000
ME = "me"            # key for the user's own microphone track
EXCEL_FONT = ("Times New Roman", 11)

WHISPER_MODELS = ["tiny.en", "base.en", "small.en", "medium.en", "large-v3"]
WHISPER_HINTS = {
    "tiny.en": "Fastest, rough",
    "base.en": "Fast, decent",
    "small.en": "Balanced (recommended)",
    "medium.en": "Accurate, slow",
    "large-v3": "Most accurate, slowest, any language",
}

_REL = "https://github.com/k2-fsa/sherpa-onnx/releases/download"
SEGMENTATION_URL = f"{_REL}/speaker-segmentation-models/sherpa-onnx-pyannote-segmentation-3-0.tar.bz2"
EMBEDDING_URL = f"{_REL}/speaker-recongition-models/wespeaker_en_voxceleb_resnet34_LM.onnx"
SEGMENTATION_MODEL = MODEL_DIR / "pyannote-segmentation-3-0.onnx"
EMBEDDING_MODEL = MODEL_DIR / "wespeaker_en_voxceleb_resnet34_LM.onnx"

VOICE_MATCH = 0.65       # cosine similarity needed to recognise a saved voice
VOICE_MARGIN = 0.08      # ...and how clearly it must beat the next-closest voice
CLUSTER_THRESHOLD = 0.5   # speaker-split sensitivity (higher = fewer, larger groups)
SAME_VOICE = 0.68         # groups whose fingerprints are this similar are merged as one person
TINY_TALK = 3.0           # seconds; groups smaller than this are folded into their closest voice

os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
@dataclass
class Segment:
    start: float
    end: float
    speaker: str   # a key: "me", "S1", "S2", ...
    text: str


def fmt_time(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def fmt_length(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h} hr {m} min"
    if m:
        return f"{m} min" if m >= 5 else f"{m} min {s} sec"
    return f"{s} sec"


def safe_name(text: str) -> str:
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", text).strip().rstrip(".")
    return text[:80] or "Meeting"


def load_json(path: Path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path: Path, data) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def unit(v) -> np.ndarray:
    v = np.asarray(v, dtype=np.float32)
    n = float(np.linalg.norm(v))
    return v / n if n > 0 else v


# --------------------------------------------------------------------------- #
# Transcript clean-up
# --------------------------------------------------------------------------- #
def remove_echo(mine: list[Segment], others: list[Segment]) -> list[Segment]:
    """Drop mic lines that are just the other side leaking from your speakers
    into your microphone (happens when you're not wearing headphones)."""
    kept = []
    for m in mine:
        a = m.text.lower()
        echo = False
        for o in others:
            if o.end < m.start - 2 or o.start > m.end + 2:
                continue
            if difflib.SequenceMatcher(None, a, o.text.lower()).ratio() > 0.6:
                echo = True
                break
        if not echo:
            kept.append(m)
    return kept


def group_turns(segments: list[Segment], max_gap: float = 3.0) -> list[Segment]:
    """Merge consecutive lines from the same speaker into one paragraph."""
    turns: list[Segment] = []
    for s in segments:
        last = turns[-1] if turns else None
        if last and last.speaker == s.speaker and s.start - last.end <= max_gap:
            turns[-1] = dataclasses.replace(
                last, end=max(last.end, s.end), text=f"{last.text} {s.text}")
        else:
            turns.append(dataclasses.replace(s))
    return turns


def speaker_for(t0: float, t1: float, turns: list[tuple]) -> str | None:
    """Speaker with the most overlap with [t0, t1]; nearest speaker if none overlap."""
    best, best_ov = None, 0.0
    nearest, nearest_d = None, 1e9
    for s, e, spk in turns:
        ov = min(t1, e) - max(t0, s)
        if ov > best_ov:
            best, best_ov = spk, ov
        d = max(s - t1, t0 - e, 0)
        if d < nearest_d:
            nearest, nearest_d = spk, d
    return best if best is not None else nearest


def assign_speakers(asr: list[dict], turns: list[tuple]) -> list[Segment]:
    """Split Whisper's lines at speaker changes, word by word.

    asr:   [{"start", "end", "text", "words": [(start, end, word), ...]}]
    turns: [(start, end, speaker_key)] from speaker detection
    """
    if not turns:
        return [Segment(a["start"], a["end"], "S1", a["text"].strip()) for a in asr]

    out: list[Segment] = []
    for a in asr:
        words = a.get("words") or []
        if not words:
            out.append(Segment(a["start"], a["end"],
                               speaker_for(a["start"], a["end"], turns), a["text"].strip()))
            continue
        labels = [speaker_for(w[0], w[1], turns) for w in words]
        # a single word flipping speaker between two words of the same speaker is noise
        for i in range(1, len(labels) - 1):
            if labels[i - 1] == labels[i + 1] != labels[i]:
                labels[i] = labels[i - 1]
        start = 0
        for i in range(1, len(words) + 1):
            if i == len(words) or labels[i] != labels[start]:
                chunk = words[start:i]
                text = "".join(w[2] for w in chunk).strip()
                if text:
                    out.append(Segment(chunk[0][0], chunk[-1][1], labels[start], text))
                start = i
    # Join neighbouring pieces from the same speaker (Whisper often splits mid-thought)
    merged: list[Segment] = []
    for s in out:
        if merged and merged[-1].speaker == s.speaker and s.start - merged[-1].end < 1.0 \
                and len(merged[-1].text) < 200:
            merged[-1] = dataclasses.replace(merged[-1], end=s.end, text=f"{merged[-1].text} {s.text}")
        else:
            merged.append(s)
    return merged


# --------------------------------------------------------------------------- #
# Trimming recordings (cut dead air at the start or end)
# --------------------------------------------------------------------------- #
TRIM_BLOCK = 0.5          # seconds per loudness block
ORIGINAL_DIR = ".original_audio"


def wav_seconds(path) -> float:
    import wave
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / float(w.getframerate())


def loudness(path, block: float = TRIM_BLOCK) -> np.ndarray:
    """RMS loudness of a WAV recording per `block` seconds (reads in chunks, any length)."""
    import wave
    out = []
    with wave.open(str(path), "rb") as w:
        rate, ch, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
        n = max(1, int(rate * block))
        while True:
            raw = w.readframes(n)
            if not raw:
                break
            x = np.frombuffer(raw, dtype=np.int16 if width == 2 else np.int32).astype(np.float32)
            x /= 32768.0 if width == 2 else 2147483648.0
            out.append(float(np.sqrt(np.mean(x * x))) if x.size else 0.0)
    return np.array(out, dtype=np.float32)


def combined_loudness(paths) -> np.ndarray:
    envs = [loudness(p) for p in paths if Path(p).exists()]
    if not envs:
        return np.zeros(0, dtype=np.float32)
    n = max(len(e) for e in envs)
    out = np.zeros(n, dtype=np.float32)
    for e in envs:
        floor = float(np.percentile(e, 10)) if len(e) else 0.0
        thresh = max(0.004, floor * 4)
        # scale each track so its speech threshold is 1.0 (mic and speakers have different noise)
        out[:len(e)] = np.maximum(out[:len(e)], e / thresh)
    return out


def speech_bounds(env: np.ndarray, block: float = TRIM_BLOCK) -> tuple[float, float] | None:
    """First and last moment with sustained sound (2 of 4 blocks above the speech threshold)."""
    if not len(env):
        return None
    active = (env >= 1.0).astype(np.int32)
    win = np.convolve(active, np.ones(4, dtype=np.int32), mode="same") >= 2
    idx = np.flatnonzero(win & (active > 0))
    if not len(idx):
        return None
    return float(idx[0] * block), float((idx[-1] + 1) * block)


def recorded_tracks(meeting: dict) -> list[Path]:
    """The WAV files Tableside recorded for this meeting (not imported files)."""
    if meeting.get("imported"):
        return []
    out = []
    for t in meeting.get("tracks", []):
        p = Path(t["file"])
        p = p if p.is_absolute() else Path(meeting["folder"]) / p
        if p.exists() and p.suffix.lower() == ".wav":
            out.append(p)
    return out


def _crop_wav(path: Path, start: float, end: float) -> None:
    import wave
    tmp = path.with_suffix(".trim.tmp")
    with wave.open(str(path), "rb") as src:
        rate, total = src.getframerate(), src.getnframes()
        a, b = max(0, int(start * rate)), min(total, int(end * rate))
        src.setpos(a)
        with wave.open(str(tmp), "wb") as dst:
            dst.setnchannels(src.getnchannels())
            dst.setsampwidth(src.getsampwidth())
            dst.setframerate(rate)
            left = b - a
            while left > 0:
                chunk = src.readframes(min(left, rate * 10))
                if not chunk:
                    break
                dst.writeframes(chunk)
                left -= len(chunk) // (src.getnchannels() * src.getsampwidth())
    os.replace(tmp, path)


def trim_meeting(meeting: dict, start: float, end: float, auto: bool = False) -> dict:
    """Keep only [start, end] seconds of the recording. Originals are kept for Undo."""
    import shutil
    tracks = recorded_tracks(meeting)
    if not tracks or end - start < 1:
        return meeting
    backup = Path(meeting["folder"]) / ORIGINAL_DIR
    backup.mkdir(exist_ok=True)
    if "untrimmed" not in meeting:
        meeting["untrimmed"] = {"duration": meeting.get("duration"),
                                "segments": meeting.get("segments")}
        for p in tracks:
            if not (backup / p.name).exists():
                shutil.copy2(p, backup / p.name)
    if os.name == "nt":
        try:  # hide the backup folder in Explorer
            import ctypes
            ctypes.windll.kernel32.SetFileAttributesW(str(backup), 0x02)
        except Exception:
            pass
    for p in tracks:
        _crop_wav(p, start, end)
    if "segments" in meeting:
        segs = []
        for s, e, k, t in meeting["segments"]:
            if e <= start or s >= end:
                continue
            segs.append([round(max(s, start) - start, 2), round(min(e, end) - start, 2), k, t])
        meeting["segments"] = segs
    old_len = meeting.get("duration") or max(wav_seconds(p) for p in tracks) + start
    meeting["duration"] = end - start
    cut = {"start": round(start, 1), "end": round(max(0.0, old_len - end), 1)}
    if auto:
        meeting["auto_trimmed"] = cut
    meeting["trimmed"] = cut
    return meeting


def undo_trim(meeting: dict) -> dict:
    import shutil
    backup = Path(meeting["folder"]) / ORIGINAL_DIR
    if "untrimmed" not in meeting or not backup.exists():
        return meeting
    for p in recorded_tracks(meeting):
        if (backup / p.name).exists():
            shutil.copy2(backup / p.name, p)
    u = meeting.pop("untrimmed")
    meeting["duration"] = u.get("duration")
    if u.get("segments") is not None:
        meeting["segments"] = u["segments"]
    else:
        meeting.pop("segments", None)
    for k in ("trimmed", "auto_trimmed", "auto_trim_seen"):
        meeting.pop(k, None)
    shutil.rmtree(backup, ignore_errors=True)
    return meeting


def auto_trim_bounds(meeting: dict, min_gap: float = 60.0, margin: float = 3.0):
    """(start, end) to cut long silence at the start/end, or None if not worth it."""
    tracks = recorded_tracks(meeting)
    if not tracks:
        return None
    env = combined_loudness(tracks)
    length = len(env) * TRIM_BLOCK
    b = speech_bounds(env)
    if b is None:
        return None
    first, last = b
    start = max(0.0, first - margin) if first > min_gap else 0.0
    end = min(length, last + margin) if length - last > min_gap else length
    if start == 0.0 and end == length:
        return None
    return start, end


def shrink_wav(path: Path) -> int:
    """Convert a large WAV (e.g. 48 kHz stereo) to 16 kHz mono in place. Returns bytes saved."""
    import wave
    with wave.open(str(path), "rb") as w:
        if w.getframerate() == SR and w.getnchannels() == 1:
            return 0
    before = path.stat().st_size
    audio = load_audio(path)
    tmp = path.with_suffix(".shrink.tmp")
    with wave.open(str(tmp), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes())
    os.replace(tmp, path)
    return before - path.stat().st_size


def shrink_all_recordings(log=print) -> int:
    """Shrink every saved recording (and trim backups) to 16 kHz mono. Returns bytes saved."""
    saved = 0
    for m in list_meetings():
        folder = Path(m["folder"])
        files = recorded_tracks(m) + sorted((folder / ORIGINAL_DIR).glob("*.wav"))
        for p in files:
            try:
                saved += shrink_wav(p)
                log(f"Shrinking recordings… saved {saved / 1e6:,.0f} MB so far")
            except Exception as ex:
                log(f"Skipped {p.name}: {ex}")
    return saved


def recordings_size() -> tuple[int, int]:
    """(total bytes of saved recordings, bytes that could be saved by shrinking)."""
    import wave
    total = possible = 0
    for m in list_meetings():
        folder = Path(m["folder"])
        for p in recorded_tracks(m) + sorted((folder / ORIGINAL_DIR).glob("*.wav")):
            try:
                size = p.stat().st_size
                total += size
                with wave.open(str(p), "rb") as w:
                    ratio = (w.getframerate() * w.getnchannels()) / SR
                if ratio > 1:
                    possible += int(size - size / ratio)
            except Exception:
                pass
    return total, possible


def read_mix(paths, t0: float, t1: float, rate: int = SR) -> np.ndarray:
    """A short mono mix of the recorded tracks between t0 and t1 (for previews)."""
    import wave
    mix = np.zeros(max(1, int((t1 - t0) * rate)), dtype=np.float32)
    for p in paths:
        with wave.open(str(p), "rb") as w:
            r, ch = w.getframerate(), w.getnchannels()
            w.setpos(min(w.getnframes(), int(t0 * r)))
            raw = w.readframes(int((t1 - t0) * r))
        x = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        if ch > 1 and x.size:
            x = x[: len(x) // ch * ch].reshape(-1, ch).mean(axis=1)
        if x.size:
            y = np.interp(np.linspace(0, len(x) - 1, len(mix)), np.arange(len(x)), x)
            mix += y.astype(np.float32)
    return np.clip(mix, -1, 1)


# --------------------------------------------------------------------------- #
# Model downloads
# --------------------------------------------------------------------------- #
def _download(url: str, dest: Path, log) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=dest.parent, suffix=".part")
    os.close(fd)
    try:
        with urllib.request.urlopen(url, timeout=60) as r, open(tmp, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            got, last = 0, -1
            while True:
                chunk = r.read(1 << 16)
                if not chunk:
                    break
                f.write(chunk)
                got += len(chunk)
                pct = int(got * 100 / total) if total else -1
                if pct >= 0 and pct // 25 != last:
                    last = pct // 25
                    log(f"Downloading {dest.name}… {pct}%")
        os.replace(tmp, dest)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def ensure_speaker_models(log=print) -> None:
    if not SEGMENTATION_MODEL.exists():
        log("Downloading the speaker-detection model (first time only)…")
        archive = MODEL_DIR / "segmentation.tar.bz2"
        _download(SEGMENTATION_URL, archive, log)
        with tarfile.open(archive) as tar:
            member = next(m for m in tar.getmembers() if m.name.endswith("/model.onnx"))
            with tar.extractfile(member) as src, open(SEGMENTATION_MODEL, "wb") as dst:
                dst.write(src.read())
        archive.unlink(missing_ok=True)
    if not EMBEDDING_MODEL.exists():
        log("Downloading the voice-recognition model (first time only)…")
        _download(EMBEDDING_URL, EMBEDDING_MODEL, log)


def load_audio(path) -> np.ndarray:
    """Any audio/video file -> mono float32 at 16 kHz."""
    from faster_whisper.audio import decode_audio
    return decode_audio(str(path), sampling_rate=SR)


# --------------------------------------------------------------------------- #
# Speaker detection + voice fingerprints
# --------------------------------------------------------------------------- #
class SpeakerEngine:
    def __init__(self, log=print):
        try:  # load the newer ONNX runtime first so sherpa-onnx shares it instead of clashing
            import onnxruntime  # noqa: F401
        except Exception:
            pass
        import sherpa_onnx
        ensure_speaker_models(log)
        self._so = sherpa_onnx
        threads = max(1, min(8, (os.cpu_count() or 4)))
        self.extractor = sherpa_onnx.SpeakerEmbeddingExtractor(
            sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(EMBEDDING_MODEL), num_threads=threads))
        self.threads = threads

    def diarize(self, audio: np.ndarray, num_speakers: int = 0, progress=None) -> list[tuple]:
        """Return [(start, end, "S1"|"S2"|...)] numbered in order of first appearance.
        num_speakers <= 0 means work it out automatically."""
        so = self._so
        config = so.OfflineSpeakerDiarizationConfig(
            segmentation=so.OfflineSpeakerSegmentationModelConfig(
                pyannote=so.OfflineSpeakerSegmentationPyannoteModelConfig(model=str(SEGMENTATION_MODEL)),
                num_threads=self.threads),
            embedding=so.SpeakerEmbeddingExtractorConfig(model=str(EMBEDDING_MODEL), num_threads=self.threads),
            clustering=so.FastClusteringConfig(
                num_clusters=num_speakers if num_speakers > 0 else -1, threshold=CLUSTER_THRESHOLD),
            min_duration_on=0.3,
            min_duration_off=0.5,
        )
        if not config.validate():
            raise RuntimeError("Speaker detection is misconfigured.")
        sd = so.OfflineSpeakerDiarization(config)

        def cb(done, total):
            if progress and total:
                progress(done / total)
            return 0

        result = sd.process(audio, callback=cb).sort_by_start_time()
        order: dict[int, str] = {}
        turns = []
        for r in result:
            if r.speaker not in order:
                order[r.speaker] = f"S{len(order) + 1}"
            turns.append((float(r.start), float(r.end), order[r.speaker]))
        return turns

    def embed(self, audio: np.ndarray) -> np.ndarray | None:
        if len(audio) < SR // 2:
            return None
        stream = self.extractor.create_stream()
        stream.accept_waveform(sample_rate=SR, waveform=audio)
        stream.input_finished()
        if not self.extractor.is_ready(stream):
            return None
        return unit(self.extractor.compute(stream))

    def speaker_fingerprints(self, audio: np.ndarray, turns: list[tuple],
                             max_seconds: float = 30.0) -> dict[str, list[float]]:
        """One voice fingerprint per detected speaker, from their longest stretches."""
        by_spk: dict[str, list[tuple]] = {}
        for s, e, k in turns:
            by_spk.setdefault(k, []).append((s, e))
        prints = {}
        for k, spans in by_spk.items():
            spans.sort(key=lambda x: x[0] - x[1])  # longest first
            pieces, total = [], 0.0
            for s, e in spans:
                if total >= max_seconds:
                    break
                pieces.append(audio[int(s * SR):int(e * SR)])
                total += e - s
            emb = self.embed(np.concatenate(pieces)) if pieces else None
            if emb is not None:
                prints[k] = emb.tolist()
        return prints


def _talk_time(turns) -> dict:
    out: dict = {}
    for s, e, k in turns:
        out[k] = out.get(k, 0.0) + (e - s)
    return out


def plan_merges(fingerprints: dict, talk: dict, target: int | None = None,
                same_voice: float = SAME_VOICE, tiny: float = TINY_TALK) -> dict:
    """Decide which speaker groups are really the same person.
    Returns {old_key: surviving_key}. With `target`, keeps merging the most similar
    voices until only that many remain."""
    groups = {k: {"emb": unit(fingerprints[k]), "talk": talk.get(k, 0.0)} for k in fingerprints}
    mapping = {k: k for k in talk}

    def absorb(small, big):
        g, h = groups[small], groups[big]
        h["emb"] = unit(g["emb"] * g["talk"] + h["emb"] * h["talk"]) if g["talk"] + h["talk"] else h["emb"]
        h["talk"] += g["talk"]
        del groups[small]
        for k, v in mapping.items():
            if v == small:
                mapping[k] = big

    while len(groups) > 1:
        keys = list(groups)
        best = max(((float(np.dot(groups[a]["emb"], groups[b]["emb"])), a, b)
                    for i, a in enumerate(keys) for b in keys[i + 1:]))
        sim, a, b = best
        if (target and len(groups) > target) or sim >= same_voice:
            small, big = (a, b) if groups[a]["talk"] < groups[b]["talk"] else (b, a)
            absorb(small, big)
        else:
            break
    # fold tiny groups (a cough, a one-word "yeah") into their closest voice
    for k in sorted(groups, key=lambda k: groups[k]["talk"]):
        if len(groups) <= (target or 1) or k not in groups or groups[k]["talk"] >= tiny:
            continue
        others = [o for o in groups if o != k]
        sim, near = max((float(np.dot(groups[k]["emb"], groups[o]["emb"])), o) for o in others)
        if sim >= 0.35 or groups[k]["talk"] < tiny / 2:
            absorb(k, near)
    # groups too short to fingerprint: give them to whoever spoke right before
    return mapping


def renumber(keys_in_order) -> dict:
    """Rename surviving groups S1, S2… by first appearance."""
    out, n = {}, 0
    for k in keys_in_order:
        if k.startswith("S") and k not in out:
            n += 1
            out[k] = f"S{n}"
    return out


def tidy_turns(turns: list, fingerprints: dict, target: int | None = None):
    """Merge over-split speakers in diarization output. Returns (turns, mapping)."""
    talk = _talk_time(turns)
    known = {k: v for k, v in fingerprints.items() if k in talk}
    mapping = plan_merges(known, talk, target) if known else {k: k for k in talk}
    last = None
    merged = []
    for s, e, k in turns:
        k2 = mapping.get(k, k)
        if k not in known and talk.get(k, 0) < TINY_TALK and last:   # unfingerprintable blip
            k2 = last
        merged.append((s, e, k2))
        last = k2
    order = renumber(k for _s, _e, k in merged)
    return [(s, e, order.get(k, k)) for s, e, k in merged], {k: order.get(v, v) for k, v in mapping.items()}


def merge_meeting_speakers(meeting: dict, target: int) -> dict:
    """Merge a finished meeting's voices down to `target` people (no re-transcribing)."""
    segs = meeting.get("segments", [])
    fps = meeting.get("fingerprints", {})
    talk = {}
    for s, e, k, _t in segs:
        if k.startswith("S"):
            talk[k] = talk.get(k, 0.0) + (e - s)
    with_fp = {k: fps[k] for k in talk if k in fps}
    if len(with_fp) < 2:
        return meeting
    mapping = plan_merges(with_fp, talk, target, same_voice=2.0, tiny=0.0)
    # keys without fingerprints join the closest-in-time surviving speaker
    names = meeting.get("names", {})
    new_segs = []
    for s, e, k, t in segs:
        new_segs.append([s, e, mapping.get(k, k), t])
    order = renumber(k for _s, _e, k, _t in new_segs)
    new_names, new_fps = {}, {}
    survivors_talk = {}
    for k, v in mapping.items():
        survivors_talk.setdefault(v, []).append(k)
    for v, members in survivors_talk.items():
        members.sort(key=lambda m: -talk.get(m, 0))
        named = next((names[m] for m in members if names.get(m)), None)
        if named:
            new_names[order.get(v, v)] = named
        embs = [unit(fps[m]) * talk.get(m, 0) for m in members if m in fps]
        if embs:
            new_fps[order.get(v, v)] = unit(np.sum(embs, axis=0)).tolist()
    for k, n in names.items():
        if not k.startswith("S"):
            new_names[k] = n
    meeting["segments"] = [[s, e, order.get(k, k), t] for s, e, k, t in new_segs]
    meeting["names"] = new_names
    meeting["fingerprints"] = new_fps
    meeting["guessed"] = {order.get(mapping.get(k, k), k): g for k, g in meeting.get("guessed", {}).items()
                          if mapping.get(k, k) == k}
    meeting["auto_named"] = sorted({order.get(mapping.get(k, k), k) for k in meeting.get("auto_named", [])})
    return meeting


# --------------------------------------------------------------------------- #
# Voice memory: remembers people you've named so they're labelled next time
# --------------------------------------------------------------------------- #
class VoiceBook:
    def __init__(self, path: Path = VOICES_FILE):
        self.path = path
        self.voices: dict = load_json(path, {})

    def names(self) -> list[str]:
        return sorted(self.voices, key=str.lower)

    def remember(self, name: str, fingerprint) -> None:
        emb = unit(fingerprint)
        v = self.voices.get(name)
        if v:
            n = v.get("n", 1)
            emb = unit((unit(v["emb"]) * n + emb) / (n + 1))
            self.voices[name] = {"emb": emb.tolist(), "n": min(n + 1, 20)}
        else:
            self.voices[name] = {"emb": emb.tolist(), "n": 1}

    def forget(self, name: str) -> None:
        self.voices.pop(name, None)

    def save(self) -> None:
        save_json(self.path, self.voices)

    def match(self, fingerprints: dict[str, list[float]]) -> dict[str, str]:
        """{speaker_key: saved name} for confident matches, each name used once."""
        if not self.voices or not fingerprints:
            return {}
        pairs = []
        for k, f in fingerprints.items():
            f = unit(f)
            sims = sorted(((float(np.dot(f, unit(v["emb"]))), name)
                           for name, v in self.voices.items()), reverse=True)
            runner_up = sims[1][0] if len(sims) > 1 else -1.0
            for sim, name in sims:
                if name != sims[0][1] or sim - runner_up >= VOICE_MARGIN:
                    pairs.append((sim, k, name))
        pairs.sort(reverse=True)
        used_k, used_n, out = set(), set(), {}
        for sim, k, name in pairs:
            if sim < VOICE_MATCH:
                break
            if k in used_k or name in used_n:
                continue
            out[k] = name
            used_k.add(k)
            used_n.add(name)
        return out


# --------------------------------------------------------------------------- #
# Working out names from how people address each other
#   "Alan, how's the grading plan?"  -> the next person to speak is probably Alan
#   "Thanks, Bob."                   -> the person who just spoke is probably Bob
#   "Hi, this is Amy."               -> the person speaking is Amy
# --------------------------------------------------------------------------- #
NOT_NAMES = set("""
a about after again all alright also although always am an and any anybody anyone anything anyway
are around as at awesome back basically be because before being best better both but by can
certainly cool correct could day definitely did do does done each easy either else even ever every
everybody everyone everything exactly fair fine first folks for from friday fun gentlemen get go god
good great guys had happy has have he hello her here hey hi him his hmm hold honestly hopefully how
however i if in indeed is it its just know last later let lets like look love may maybe me mine
monday more morning most much must my next nice no nobody none nope not nothing now of oh ok okay on
once one only or other our ours out over perfect please probably quick quickly real really right
saturday second see she should so some somebody someone something sorry still sunday super sure
team than thank thanks that the their them then there these they thing things think this those
though thursday to today together tomorrow too totally true tuesday uh um understood until up us
very wait we wednesday well were what whatever when where which while who whoever why will with
wonderful wow yeah yep yes yesterday yet you your yours
january february march april june july august september october november december
""".split())
_NAME = r"([A-Z][a-z]{1,15})"
_SELF_RE = re.compile(r"\b(?:I'm|I am|this is|This is|my name is|My name is|it's|It's)\s+" + _NAME + r"\b")
_HERE_RE = re.compile(r"^" + _NAME + r"\s+here\b")
_START_RE = re.compile(r"^(?:(?i:hey|hi|hello|okay|ok|so|and|yeah|yes|right|well|alright|great|perfect|"
                       r"thanks|thank you|thanks so much|thank you so much|sorry|morning|good morning)"
                       r",?\s+)?" + _NAME + r"(?:,|\?|!|\s*-)\s*")
_END_RE = re.compile(r",\s*" + _NAME + r"\s*[.?!]*$")
_THANKS_RE = re.compile(r"^(?i:thanks|thank you|thanks so much|thank you so much|good point|great point|"
                        r"agreed|i agree|exactly|right|good question|great question),?\s+" + _NAME + r"\b")
_MID_CAPS_RE = re.compile(r"(?<=[a-z,;:]\s)([A-Z][a-z]{1,15})\b")
_FIRST_NAMES: set | None = None


def first_names() -> set:
    global _FIRST_NAMES
    if _FIRST_NAMES is None:
        try:
            _FIRST_NAMES = set((RES / "first_names.txt").read_text(encoding="utf-8").split())
        except OSError:
            _FIRST_NAMES = set()
    return _FIRST_NAMES
_HANDOFF_RE = re.compile(r"\b(?:over to you|go ahead|you're up|your turn|what do you think|any thoughts|"
                         r"can you|could you|would you|will you|do you)\b", re.I)


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.?!])\s+", text.strip()) if s.strip()]


def _candidate(name: str, known: set, proper: set) -> str | None:
    """A word counts as a name if it's a known person, a common first name, or a word the
    transcript capitalises mid-sentence (a proper noun) - never an everyday word."""
    if not name:
        return None
    if name in known:
        return name
    if name.lower() in NOT_NAMES or len(name) < 2:
        return None
    if name in first_names() or name in proper:
        return name
    return None


def guess_names(segments: list[Segment], names: dict, known: set | None = None) -> dict:
    """Suggest names for unnamed speakers from forms of address in the transcript.

    segments: transcript lines (speaker = key), names: names already known by key.
    Returns {key: {"name": str, "score": float, "from": source_key, "quote": sentence}}.
    """
    known = set(known or ()) | {n for n in names.values() if n}
    turns = group_turns(segments, max_gap=30.0)
    proper = {w for t in turns for w in _MID_CAPS_RE.findall(t.text)}
    votes: dict[str, dict[str, float]] = {}
    proof: dict[tuple, tuple] = {}
    said_by: dict[str, dict[str, float]] = {}   # names a speaker uses for *other* people

    def vote(key, name, w, src, sentence):
        if key is None:
            return
        votes.setdefault(key, {})
        votes[key][name] = votes[key].get(name, 0) + w
        if w >= proof.get((key, name), (0,))[0]:
            proof[(key, name)] = (w, src, sentence)

    for i, t in enumerate(turns):
        prev_k = next((turns[j].speaker for j in range(i - 1, -1, -1) if turns[j].speaker != t.speaker), None)
        next_k = next((turns[j].speaker for j in range(i + 1, len(turns)) if turns[j].speaker != t.speaker), None)
        sents = _sentences(t.text)
        for si, s in enumerate(sents):
            first, last = si == 0, si == len(sents) - 1
            for rx in (_SELF_RE, _HERE_RE):
                m = rx.search(s)
                name = _candidate(m.group(1), known, proper) if m else None
                if name and (rx is _HERE_RE or first):
                    vote(t.speaker, name, 3.0, t.speaker, s)
            m = _THANKS_RE.match(s)
            name = _candidate(m.group(1), known, proper) if m else None
            if name:
                said_by.setdefault(t.speaker, {})[name] = 1
                if first:
                    vote(prev_k, name, 2.0, t.speaker, s)
                continue
            for rx in (_START_RE, _END_RE):
                m = rx.search(s)
                name = _candidate(m.group(1), known, proper) if m else None
                if not name:
                    continue
                said_by.setdefault(t.speaker, {})[name] = 1
                asks = s.endswith("?") or bool(_HANDOFF_RE.search(s))
                if first and last and not asks:   # a one-line reply: usually to whoever just spoke
                    vote(prev_k, name, 1.5, t.speaker, s)
                    vote(next_k, name, 1.0, t.speaker, s)
                elif last:    # end of a turn: talking to the next speaker
                    vote(next_k, name, 2.5 if asks else 1.5, t.speaker, s)
                elif first:   # start of a turn: replying to the last speaker
                    vote(prev_k, name, 1.5, t.speaker, s)
                else:
                    vote(next_k, name, 1.0, t.speaker, s)
                break

    taken = {n.lower() for n in names.values() if n}
    scored = []
    for key, cand in votes.items():
        if names.get(key) or not key.startswith("S"):
            continue
        ranked = []
        for name, v in cand.items():
            v -= 2.0 * said_by.get(key, {}).get(name, 0)  # people don't call themselves by name
            ranked.append((v, name))
        ranked.sort(reverse=True)
        if not ranked:
            continue
        best, runner = ranked[0][0], (ranked[1][0] if len(ranked) > 1 else 0.0)
        scored.append((best, best - runner, key, ranked[0][1]))
    scored.sort(reverse=True)
    out, used = {}, set(taken)
    for best, margin, key, name in scored:
        if best < 2.0 or margin < 1.0 or name.lower() in used:
            continue
        _w, src, sentence = proof[(key, name)]
        out[key] = {"name": name, "score": round(best, 1), "from": src, "quote": sentence}
        used.add(name.lower())
    return out


NAMES_SYSTEM = """You identify the real names of unnamed speakers in a meeting transcript.
Use only evidence in the transcript: people addressing each other by name, introductions,
handoffs ("Over to you, Sam"), thanks ("Thanks, Priya"), or someone speaking about
themselves. Only answer when the evidence is clear. Never guess from topic or role.
Return ONLY JSON: {"Speaker 2": {"name": "First name", "evidence": "short quote"}, ...}
Include only speakers you are confident about. Return {} if none."""


def ai_guess_names(meeting: dict, settings: dict, unnamed: list[str]) -> dict:
    """Ask the AI to work out names for still-unnamed speakers. Names must appear in the text."""
    if not unnamed:
        return {}
    text = transcript_text(meeting)
    if settings.get("ai_provider", "local") == "local":
        text = text[:12000]   # introductions happen early; keeps the on-PC model fast
    labels = {palette_name(k, {}): k for k in unnamed}
    reply = ask_ai(settings, NAMES_SYSTEM,
                   f"Unnamed speakers: {', '.join(labels)}\n\nTranscript:\n{text}", max_tokens=800)
    m = re.search(r"\{.*\}", reply or "", re.S)
    if not m:
        return {}
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}
    taken = {n.lower() for n in meeting.get("names", {}).values() if n}
    out = {}
    for label, v in data.items():
        key = labels.get(label)
        name = str((v or {}).get("name", "")).strip().split(" ")[0] if isinstance(v, dict) else ""
        if not key or not name or name.lower() in taken or name.lower() in NOT_NAMES:
            continue
        if not re.search(r"\b" + re.escape(name) + r"\b", text):   # must actually be said
            continue
        out[key] = {"name": name, "score": None, "from": "ai",
                    "quote": str(v.get("evidence", ""))[:160]}
        taken.add(name.lower())
    return out


# --------------------------------------------------------------------------- #
# Whisper
# --------------------------------------------------------------------------- #
WHISPER_DIR = MODEL_DIR / "whisper"


def load_whisper(name: str, log=print):
    """Load the speech model from Tableside's own folder (plain files, no shortcuts),
    downloading it first if needed. A damaged or half-finished copy is replaced."""
    import shutil
    import threading
    from faster_whisper import WhisperModel
    from faster_whisper.utils import download_model
    target = WHISPER_DIR / name
    threads = min(8, os.cpu_count() or 4)
    for attempt in (1, 2):
        complete = (target / "model.bin").exists() and (target / "config.json").exists() \
            and (target / "tokenizer.json").exists()
        if attempt == 2 or not complete:
            if attempt == 2:
                log("The speech model looks damaged - downloading a fresh copy…")
                shutil.rmtree(target, ignore_errors=True)
            target.mkdir(parents=True, exist_ok=True)
            log(f"Downloading the speech model “{name}” (first time only)…")
            done = threading.Event()

            def report():
                while not done.wait(4):
                    log(f"Downloading the speech model… {_folder_mb(target):,} MB")
            threading.Thread(target=report, daemon=True).start()
            try:
                download_model(name, output_dir=str(target))
            finally:
                done.set()
        try:
            return WhisperModel(str(target), device="cpu", compute_type="int8", cpu_threads=threads)
        except Exception:
            if attempt == 2:
                raise
    raise RuntimeError("Couldn't load the speech model.")


def transcribe(model, audio_or_path, words: bool, progress=None) -> tuple[list[dict], float]:
    src = audio_or_path if isinstance(audio_or_path, np.ndarray) else str(audio_or_path)
    segments, info = model.transcribe(src, vad_filter=True, beam_size=5, word_timestamps=words)
    total = max(info.duration, 1e-6)
    out = []
    for s in segments:
        text = s.text.strip()
        if text:
            out.append({
                "start": s.start, "end": s.end, "text": text,
                "words": [(w.start, w.end, w.word) for w in (s.words or [])],
            })
        if progress:
            progress(min(1.0, s.end / total))
    return out, info.duration


# --------------------------------------------------------------------------- #
# Meeting records
# --------------------------------------------------------------------------- #
def palette_name(key: str, names: dict) -> str:
    if key in names and names[key]:
        return names[key]
    if key == ME:
        return "Me"
    if key.startswith("S") and key[1:].isdigit():
        return f"Speaker {key[1:]}"
    return key


def options(meeting: dict) -> dict:
    o = {"audio": True, "transcript": True, "summary": False}
    o.update(meeting.get("options") or {})
    return o


def resolved(meeting: dict) -> list[Segment]:
    names = meeting.get("names", {})
    return [Segment(s, e, palette_name(k, names), t) for s, e, k, t in meeting.get("segments", [])]


def read_meeting(folder: Path) -> dict | None:
    m = load_json(Path(folder) / MEETING_FILE, None)
    if m is not None:
        m["folder"] = str(folder)
    return m


def write_meeting(meeting: dict) -> None:
    data = {k: v for k, v in meeting.items() if k != "folder"}
    save_json(Path(meeting["folder"]) / MEETING_FILE, data)


def list_meetings(root: Path = NOTES_ROOT) -> list[dict]:
    out = []
    if root.exists():
        for d in root.iterdir():
            if d.is_dir() and (d / MEETING_FILE).exists():
                m = read_meeting(d)
                if m:
                    out.append(m)
    out.sort(key=lambda m: m.get("started", ""), reverse=True)
    return out



# --------------------------------------------------------------------------- #
# AI summary
# --------------------------------------------------------------------------- #
CLAUDE_MODELS = ["claude-sonnet-5-5", "claude-haiku-5-5", "claude-opus-5-5"]
CLAUDE_HINTS = {
    "claude-sonnet-5-5": "Sonnet 5.5 - best value (recommended)",
    "claude-haiku-5-5": "Haiku 5.5 - cheapest, fastest",
    "claude-opus-5-5": "Opus 5.5 - most thorough",
}
CLAUDE_KEYS_URL = "https://platform.claude.com/settings/keys"
KEYRING_SERVICE = "Tableside"

SUMMARY_SYSTEM = """You turn meeting transcripts into clear, accurate meeting notes.
Rules:
- Use the speaker names exactly as they appear in the transcript.
- Only include what was actually said. Never invent names, dates, numbers or tasks.
- An action item is something a person agreed or was asked to do. Owner is the person
  responsible ("Unassigned" if unclear). Due is the timeframe mentioned, or "".
- Be concise and concrete. Plain business English.
Return ONLY a JSON object, no other text, in this shape:
{"summary": "3-6 sentence overview",
 "decisions": ["decision", ...],
 "action_items": [{"owner": "Name", "task": "what to do", "due": "when or empty"}],
 "open_questions": ["unresolved question", ...]}"""


def transcript_text(meeting: dict) -> str:
    lines = []
    for t in group_turns(resolved(meeting)):
        lines.append(f"[{fmt_time(t.start)}] {t.speaker}: {t.text}")
    return "\n".join(lines)


def get_api_key() -> str:
    try:
        import keyring
        return keyring.get_password(KEYRING_SERVICE, "anthropic_api_key") or ""
    except Exception:
        return load_json(SETTINGS_FILE, {}).get("anthropic_api_key", "")


def set_api_key(key: str) -> None:
    try:
        import keyring
        if key:
            keyring.set_password(KEYRING_SERVICE, "anthropic_api_key", key)
        else:
            try:
                keyring.delete_password(KEYRING_SERVICE, "anthropic_api_key")
            except Exception:
                pass
    except Exception:  # no credential store: fall back to the settings file
        s = load_json(SETTINGS_FILE, {})
        s["anthropic_api_key"] = key
        save_json(SETTINGS_FILE, s)


def _post_json(url: str, body: dict, headers: dict, timeout: int = 300) -> dict:
    import urllib.error
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                 headers={"content-type": "application/json", **headers}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        try:
            detail = json.loads(detail)["error"]["message"]
        except Exception:
            pass
        if e.code == 401:
            raise RuntimeError("The Claude API key was rejected. Check it in Settings.") from None
        raise RuntimeError(f"AI request failed ({e.code}): {detail}") from None
    except urllib.error.URLError as e:
        raise RuntimeError(f"Couldn't reach the AI service: {e.reason}") from None


def ask_ai(settings: dict, system: str, user: str, max_tokens: int = 4000) -> str:
    provider = settings.get("ai_provider", "local")
    if provider == "local":
        return LocalAI.get().chat(system, user, max_new=min(max_tokens, 900))
    if provider == "ollama":
        model = settings.get("ollama_model") or "llama3.1:8b"
        r = _post_json("http://localhost:11434/api/chat", {
            "model": model, "stream": False, "format": "json",
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }, {}, timeout=1800)
        return r.get("message", {}).get("content", "")
    key = get_api_key()
    if not key:
        raise RuntimeError("Add your Claude API key in Settings to create AI summaries.")
    r = _post_json("https://api.anthropic.com/v1/messages", {
        "model": settings.get("claude_model") or CLAUDE_MODELS[0],
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }, {"x-api-key": key, "anthropic-version": "2023-06-01"})
    return "".join(b.get("text", "") for b in r.get("content", []) if b.get("type") == "text")


def parse_summary(text: str) -> dict:
    out = {"summary": "", "decisions": [], "action_items": [], "open_questions": []}
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        out["summary"] = (text or "").strip()
        return out
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        out["summary"] = text.strip()
        return out
    out["summary"] = str(data.get("summary") or "").strip()
    out["decisions"] = [str(x).strip() for x in data.get("decisions") or [] if str(x).strip()]
    out["open_questions"] = [str(x).strip() for x in data.get("open_questions") or [] if str(x).strip()]
    for a in data.get("action_items") or []:
        if isinstance(a, dict) and str(a.get("task", "")).strip():
            out["action_items"].append({
                "owner": str(a.get("owner") or "Unassigned").strip(),
                "task": str(a["task"]).strip(),
                "due": str(a.get("due") or "").strip(),
            })
        elif isinstance(a, str) and a.strip():
            out["action_items"].append({"owner": "Unassigned", "task": a.strip(), "due": ""})
    return out


def summarize(meeting: dict, settings: dict, log=print, progress=None) -> dict:
    text = transcript_text(meeting)
    if not text.strip():
        return {"summary": "No speech was detected, so there is nothing to summarize.",
                "decisions": [], "action_items": [], "open_questions": []}
    started = dt.datetime.fromisoformat(meeting["started"])
    header = f"Meeting: {meeting['title']}\nDate: {started:%A, %B %d, %Y}\n\n"
    if settings.get("ai_provider", "local") == "local":
        try:
            return summarize_local(header, text, log, progress)
        except Exception as e:
            log(f"Built-in AI unavailable ({e}); writing a basic summary instead.")
            s = basic_summary(meeting)
            s["note"] = f"Basic summary made without AI, because the built-in AI couldn't run: {e}"
            return s
    return parse_summary(ask_ai(settings, SUMMARY_SYSTEM, header + f"Transcript:\n{text}"))


# --------------------------------------------------------------------------- #
# Built-in AI: a small language model that runs on this PC (no key, no account)
# --------------------------------------------------------------------------- #
LOCAL_MODEL_REPOS = [("microsoft/Phi-3.5-mini-instruct-onnx", "Phi-3.5-mini"),
                     ("microsoft/Phi-4-mini-instruct-onnx", "Phi-4-mini")]
LOCAL_MODEL_DIR = MODEL_DIR / "summary-model"
CHUNK_TOKENS = 3500          # keeps memory use low on ordinary laptops


def local_model_ready() -> bool:
    return (LOCAL_MODEL_DIR / "genai_config.json").exists()


def _folder_mb(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total // (1 << 20)


_CHOICE_CACHE: dict = {}


def local_model_choice() -> dict:
    """Find the smallest CPU 4-bit build of the supported models, using real file sizes.
    Returns {"repo", "label", "folder", "files": [(name, size)], "bytes"}."""
    if _CHOICE_CACHE:
        return _CHOICE_CACHE
    from huggingface_hub import HfApi
    api = HfApi()
    options, last_error = [], None
    for repo, label in LOCAL_MODEL_REPOS:
        try:
            info = api.model_info(repo, files_metadata=True)
        except Exception as e:
            last_error = e
            continue
        folders: dict = {}
        for s in info.siblings or []:
            name, size = s.rfilename, (s.size or 0)
            folder = name.rsplit("/", 1)[0] if "/" in name else ""
            folders.setdefault(folder, []).append((name, size))
        for folder, files in folders.items():
            low = folder.lower()
            if "cpu" in low and "int4" in low and any(n.endswith("genai_config.json") for n, _s in files):
                options.append({"repo": repo, "label": label, "folder": folder,
                                "files": files, "bytes": sum(sz for _n, sz in files)})
    if not options:
        raise RuntimeError(f"couldn't find the summary model ({last_error or 'not found'})")
    options.sort(key=lambda o: o["bytes"])          # smallest download wins
    _CHOICE_CACHE.update(options[0])
    return _CHOICE_CACHE


def local_model_size_text() -> str:
    try:
        return f"{local_model_choice()['bytes'] / 1e9:.1f} GB"
    except Exception:
        return "a few GB"


def ensure_local_model(log=print, progress=None) -> Path:
    """Download the built-in summary model once."""
    if local_model_ready():
        return LOCAL_MODEL_DIR
    import shutil
    import threading
    from huggingface_hub import snapshot_download
    choice = local_model_choice()
    total = max(1, choice["bytes"])
    tmp = MODEL_DIR / "summary-download"
    marker = tmp / "choice.txt"
    wanted = f"{choice['repo']}/{choice['folder']}"
    if tmp.exists() and (not marker.exists() or marker.read_text(encoding="utf-8").strip() != wanted):
        shutil.rmtree(tmp, ignore_errors=True)       # half-finished download of a different model
    tmp.mkdir(parents=True, exist_ok=True)
    marker.write_text(wanted, encoding="utf-8")
    log(f"Downloading the built-in summary AI ({choice['label']}, {total / 1e9:.1f} GB, first time only)…")
    done = threading.Event()

    def report():
        while not done.wait(3):
            mb = min(_folder_mb(tmp), total // (1 << 20))
            frac = mb * (1 << 20) / total
            log(f"Downloading the built-in summary AI… {mb:,} of {total // (1 << 20):,} MB ({frac:.0%})")
            if progress:
                progress(frac)
    threading.Thread(target=report, daemon=True).start()
    try:
        snapshot_download(choice["repo"], allow_patterns=[f"{choice['folder']}/*"], local_dir=str(tmp))
    finally:
        done.set()
    src = tmp / choice["folder"]
    if not (src / "genai_config.json").exists():
        raise RuntimeError("the summary model download was incomplete")
    if LOCAL_MODEL_DIR.exists():
        shutil.rmtree(LOCAL_MODEL_DIR, ignore_errors=True)
    shutil.move(str(src), str(LOCAL_MODEL_DIR))
    (LOCAL_MODEL_DIR / "source.txt").write_text(f"{choice['repo']}/{choice['folder']}\n", encoding="utf-8")
    shutil.rmtree(tmp, ignore_errors=True)
    return LOCAL_MODEL_DIR


class LocalAI:
    """Runs the on-PC model with ONNX Runtime GenAI. Loaded once, then reused."""
    _instance = None

    @classmethod
    def get(cls, log=print, progress=None) -> "LocalAI":
        if cls._instance is None:
            cls._instance = cls(log, progress)
        return cls._instance

    def __init__(self, log=print, progress=None):
        import onnxruntime_genai as og
        path = ensure_local_model(log, progress)
        log("Loading the built-in summary AI…")
        self.og = og
        self.model = og.Model(str(path))
        self.tok = og.Tokenizer(self.model)

    def tokens(self, text: str) -> int:
        return len(self.tok.encode(text))

    def _prompt(self, system: str, user: str) -> str:
        messages = json.dumps([{"role": "system", "content": system}, {"role": "user", "content": user}])
        try:
            return self.tok.apply_chat_template(messages, add_generation_prompt=True)
        except Exception:  # model without a stored template: Phi's format
            return f"<|system|>{system}<|end|><|user|>{user}<|end|><|assistant|>"

    def chat(self, system: str, user: str, max_new: int = 700, progress=None) -> str:
        og = self.og
        ids = self.tok.encode(self._prompt(system, user))
        params = og.GeneratorParams(self.model)
        params.set_search_options(max_length=len(ids) + max_new, do_sample=False)
        gen = og.Generator(self.model, params)
        gen.append_tokens(ids)
        stream = self.tok.create_stream()
        out, n = [], 0
        while not gen.is_done():
            gen.generate_next_token()
            out.append(stream.decode(gen.get_next_tokens()[0]))
            n += 1
            if progress and n % 10 == 0:
                progress(min(1.0, n / max_new))
        return "".join(out)


COMBINE_SYSTEM = """You merge partial meeting notes into one overview.
Write 3-6 sentences covering the whole meeting, in order, using people's names.
Return ONLY JSON: {"summary": "..."}"""


def _merge_unique(lists, key=lambda x: x):
    seen, out = set(), []
    for items in lists:
        for it in items:
            k = re.sub(r"\W+", " ", str(key(it)).lower()).strip()
            if k and k not in seen:
                seen.add(k)
                out.append(it)
    return out


def summarize_local(header: str, text: str, log=print, progress=None) -> dict:
    ai = LocalAI.get(log, progress)
    lines = text.split("\n")
    chunks, cur, cur_n = [], [], 0
    for line in lines:
        n = ai.tokens(line) + 1
        if cur and cur_n + n > CHUNK_TOKENS:
            chunks.append("\n".join(cur))
            cur, cur_n = [], 0
        cur.append(line)
        cur_n += n
    if cur:
        chunks.append("\n".join(cur))

    steps = len(chunks) + (1 if len(chunks) > 1 else 0)
    parts = []
    for i, chunk in enumerate(chunks):
        log(f"Writing the summary on this PC… part {i + 1} of {len(chunks)}"
            if len(chunks) > 1 else "Writing the summary on this PC…")
        part_note = f"(Part {i + 1} of {len(chunks)} of the meeting)\n" if len(chunks) > 1 else ""
        reply = ai.chat(SUMMARY_SYSTEM, header + part_note + f"Transcript:\n{chunk}",
                        progress=(lambda f, i=i: progress((i + f) / steps)) if progress else None)
        parts.append(parse_summary(reply))
    if len(parts) == 1:
        return parts[0]
    log("Pulling the parts together…")
    notes = "\n".join(f"Part {i + 1}: {p['summary']}" for i, p in enumerate(parts))
    overall = parse_summary(ai.chat(COMBINE_SYSTEM, header + notes, max_new=400))["summary"]
    return {
        "summary": overall or " ".join(p["summary"] for p in parts),
        "decisions": _merge_unique(p["decisions"] for p in parts),
        "action_items": _merge_unique((p["action_items"] for p in parts), key=lambda a: a["task"]),
        "open_questions": _merge_unique(p["open_questions"] for p in parts),
    }


# --------------------------------------------------------------------------- #
# Basic summary: rules only, no AI (used if the built-in AI can't run)
# --------------------------------------------------------------------------- #
_DUE_RE = re.compile(r"\b(today|tonight|tomorrow|this (?:morning|afternoon|evening|week)|next week|"
                     r"end of (?:the )?(?:day|week|month)|(?:by |on |before )?(?:monday|tuesday|wednesday|"
                     r"thursday|friday|saturday|sunday))\b", re.I)
_PROMISE_RE = re.compile(r"\b(I'll|I will|I can|I'm going to|I am going to|let me|I need to|I have to)\s+(.+)", re.I)
_ASK_RE = re.compile(r"\b(?:can you|could you|would you|will you|please|you need to)\s+(.+)", re.I)
_DECIDE_RE = re.compile(r"\b(we decided|we've decided|let's go with|we'll go with|we agreed|agreed to|"
                        r"the decision is|we're going to go|we are going to go)\b", re.I)
_TOPIC_STOP = NOT_NAMES | set("""i'm i'll it's that's don't can't we'll you're they're there's what's let's
need going want get got make think know see said say just really thing things kind sort lot little bit
yeah okay mean actually gonna wanna stuff guess put take look come back time way week next also""".split())


def _clean_task(text: str) -> str:
    text = re.split(r"(?<=[.?!])\s", text.strip())[0].rstrip(".?! ")
    text = re.sub(r",\s*[A-Z][a-z]+$", "", text)          # drop a trailing ", Amy"
    return text[:1].upper() + text[1:] if text else text


def basic_summary(meeting: dict) -> dict:
    segs = resolved(meeting)
    names = list(dict.fromkeys(s.speaker for s in segs))
    turns = group_turns(segs)
    actions, decisions, questions = [], [], []
    for i, t in enumerate(turns):
        nxt = next((x.speaker for x in turns[i + 1:] if x.speaker != t.speaker), None)
        for sent in _sentences(t.text):
            due = _DUE_RE.search(sent)
            m = _PROMISE_RE.search(sent)
            if m and len(m.group(2).split()) >= 2:
                actions.append({"owner": t.speaker, "task": _clean_task(m.group(2)),
                                "due": due.group(0) if due else ""})
                continue
            m = _ASK_RE.search(sent)
            if m and len(m.group(1).split()) >= 2:
                addressed = _START_RE.search(sent) or _END_RE.search(sent)
                owner = addressed.group(1) if addressed and addressed.group(1) in names else (nxt or "Unassigned")
                actions.append({"owner": owner, "task": _clean_task(m.group(1)),
                                "due": due.group(0) if due else ""})
                continue
            if _DECIDE_RE.search(sent) and len(sent.split()) >= 4:
                decisions.append(sent)
            elif sent.endswith("?") and len(sent.split()) >= 5 and i >= len(turns) - 6:
                questions.append(f"{t.speaker}: {sent}")
    # main topics: most repeated two-word phrases
    words = [w.lower() for w in re.findall(r"[A-Za-z][A-Za-z'-]+", " ".join(s.text for s in segs))]
    pairs = {}
    for a, b in zip(words, words[1:]):
        if a not in _TOPIC_STOP and b not in _TOPIC_STOP and len(a) > 2 and len(b) > 2:
            pairs[f"{a} {b}"] = pairs.get(f"{a} {b}", 0) + 1
    topics = [p for p, n in sorted(pairs.items(), key=lambda x: -x[1]) if n >= 2][:5]
    talk = {}
    for s in segs:
        talk[s.speaker] = talk.get(s.speaker, 0) + (s.end - s.start)
    who = ", ".join(sorted(talk, key=lambda k: -talk[k]))
    summary = f"{len(names)} {'person' if len(names) == 1 else 'people'} spoke ({who})."
    if topics:
        summary += " Main topics: " + ", ".join(topics) + "."
    if actions:
        summary += f" {len(actions)} possible action item{'s' if len(actions) != 1 else ''} were mentioned."
    return {"summary": summary, "decisions": decisions[:10], "action_items": actions[:20],
            "open_questions": questions[:6], "basic": True}


# --------------------------------------------------------------------------- #
# Word / Excel export
# --------------------------------------------------------------------------- #
def export_files(meeting: dict) -> list[Path]:
    """Write the Word and Excel notes for a meeting. Returns the files written."""
    folder = Path(meeting["folder"])
    opts = options(meeting)
    summary = meeting.get("summary")
    include_transcript = opts["transcript"] and "segments" in meeting
    if not include_transcript and not summary:
        return []
    started = dt.datetime.fromisoformat(meeting["started"])
    base = safe_name(f"{meeting['title']} {started:%Y-%m-%d}")
    segs = resolved(meeting) if include_transcript else []
    speakers = list(dict.fromkeys(s.speaker for s in resolved(meeting)))
    docx_path, xlsx_path = folder / f"{base}.docx", folder / f"{base}.xlsx"
    for old in (meeting.get("docx"), meeting.get("xlsx")):   # files from an old title
        if old and old not in (docx_path.name, xlsx_path.name):
            try:
                (folder / old).unlink()
            except OSError:
                pass
    duration = meeting.get("duration", 0)
    export_docx(docx_path, meeting["title"], started, duration, speakers, summary,
                segs if include_transcript else None)
    export_xlsx(xlsx_path, meeting["title"], started, duration, speakers, summary,
                segs if include_transcript else None)
    meeting["docx"], meeting["xlsx"] = docx_path.name, xlsx_path.name
    return [docx_path, xlsx_path]


def export_docx(path: Path, title: str, started: dt.datetime, duration: float,
                speakers: list[str], summary: dict | None, segments: list[Segment] | None) -> None:
    from docx import Document
    from docx.enum.table import WD_TABLE_ALIGNMENT
    from docx.shared import Pt, RGBColor

    doc = Document()
    doc.add_heading(title, level=0)
    info = doc.add_paragraph()
    info.add_run("Date: ").bold = True
    info.add_run(f"{started:%A, %B %d, %Y  %I:%M %p}\n")
    info.add_run("Length: ").bold = True
    info.add_run(f"{fmt_length(duration)}\n")
    info.add_run("Speakers: ").bold = True
    info.add_run(", ".join(speakers) if speakers else "-")

    if summary:
        doc.add_heading("Summary", level=1)
        if summary.get("note") or summary.get("basic"):
            note = doc.add_paragraph()
            r = note.add_run(summary.get("note") or "Basic summary, made without AI.")
            r.italic = True
        doc.add_paragraph(summary.get("summary") or "-")
        if summary.get("action_items"):
            doc.add_heading("Action Items", level=1)
            table = doc.add_table(rows=1, cols=3)
            table.style = "Light Grid Accent 2"
            table.alignment = WD_TABLE_ALIGNMENT.LEFT
            for cell, label in zip(table.rows[0].cells, ("Owner", "Task", "Due")):
                cell.text = label
            for a in summary["action_items"]:
                row = table.add_row().cells
                row[0].text, row[1].text, row[2].text = a["owner"], a["task"], a["due"]
        for key, heading in (("decisions", "Decisions"), ("open_questions", "Open Questions")):
            if summary.get(key):
                doc.add_heading(heading, level=1)
                for item in summary[key]:
                    doc.add_paragraph(item, style="List Bullet")

    if segments is not None:
        doc.add_heading("Transcript", level=1)
        if not segments:
            doc.add_paragraph("No speech was detected.")
        for t in group_turns(segments):
            p = doc.add_paragraph()
            p.paragraph_format.space_after = Pt(6)
            stamp = p.add_run(f"[{fmt_time(t.start)}]  ")
            stamp.font.color.rgb = RGBColor(0x80, 0x80, 0x80)
            stamp.font.size = Pt(9)
            p.add_run(f"{t.speaker}: ").bold = True
            p.add_run(t.text)
    doc.save(str(path))


def export_xlsx(path: Path, title: str, started: dt.datetime, duration: float,
                speakers: list[str], summary: dict | None, segments: list[Segment] | None) -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    normal = Font(name=EXCEL_FONT[0], size=EXCEL_FONT[1])
    bold = Font(name=EXCEL_FONT[0], size=EXCEL_FONT[1], bold=True)
    fill = PatternFill("solid", fgColor="FDE3D6")
    wrap_top = Alignment(vertical="top", wrap_text=True)

    def style(ws, header=True, wrap_cols=()):
        for row in ws.iter_rows():
            for cell in row:
                cell.font = normal
                cell.alignment = wrap_top if cell.column in wrap_cols else Alignment(vertical="top")
        if header:
            for cell in ws[1]:
                cell.font = bold
                cell.fill = fill

    wb = Workbook()
    first = wb.active
    sheets_made = 0

    if summary:
        ws = first
        ws.title = "Summary"
        ws.append(["Summary"])
        ws.append([summary.get("summary") or "-"])
        for key, heading in (("decisions", "Decisions"), ("open_questions", "Open Questions")):
            if summary.get(key):
                ws.append([])
                ws.append([heading])
                for item in summary[key]:
                    ws.append([f"- {item}"])
        style(ws, header=False, wrap_cols=(1,))
        for row in ws.iter_rows():
            if row[0].value in ("Summary", "Decisions", "Open Questions"):
                row[0].font = bold
        ws.column_dimensions["A"].width = 110
        sheets_made += 1

        ai = wb.create_sheet("Action Items")
        ai.append(["Owner", "Task", "Due", "Done"])
        for a in summary.get("action_items") or []:
            ai.append([a["owner"], a["task"], a["due"], ""])
        style(ai, wrap_cols=(2,))
        for col, width in zip("ABCD", (18, 80, 18, 8)):
            ai.column_dimensions[col].width = width
        ai.freeze_panes = "A2"
        sheets_made += 1

    if segments is not None:
        ws = first if not sheets_made else wb.create_sheet("Transcript")
        ws.title = "Transcript"
        ws.append(["Start", "End", "Speaker", "Text"])
        for s in segments:
            ws.append([fmt_time(s.start), fmt_time(s.end), s.speaker, s.text])
        style(ws, wrap_cols=(4,))
        for col, width in zip("ABCD", (10, 10, 18, 100)):
            ws.column_dimensions[col].width = width
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions

    info = wb.create_sheet("Meeting Info")
    for r in (("Title", title), ("Date", f"{started:%Y-%m-%d %I:%M %p}"),
              ("Length", fmt_time(duration)), ("Speakers", ", ".join(speakers) or "-")):
        info.append(list(r))
    style(info, header=False)
    for row in info.iter_rows():
        row[0].font = bold
    info.column_dimensions["A"].width = 12
    info.column_dimensions["B"].width = 60
    wb.save(str(path))
