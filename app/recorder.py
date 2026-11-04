"""Tableside recorder: captures speaker output (WASAPI loopback) and the microphone.

Audio is saved as 16 kHz mono WAV - the format the transcriber uses - which keeps
files about 7x smaller than raw sound-card audio (48 kHz stereo) with no loss in
transcript quality.
"""

from __future__ import annotations

import threading
import time
import wave
from pathlib import Path

import numpy as np

try:  # Windows-only fork of PyAudio that can record what your speakers play
    import pyaudiowpatch as pyaudio
except ImportError:
    pyaudio = None

OUT_RATE = 16000


class Downsampler:
    """Streaming mono resampler (keeps state between chunks, so no clicks)."""

    def __init__(self, in_rate: int, out_rate: int = OUT_RATE):
        self.in_rate, self.out_rate = in_rate, out_rate
        self._r = None
        if in_rate != out_rate:
            try:
                import av
                self._av = av
                self._r = av.AudioResampler(format="s16", layout="mono", rate=out_rate)
            except Exception:
                self._r = None
        self._pos = 0.0   # fallback: fractional read position

    def __call__(self, mono: np.ndarray) -> np.ndarray:
        """mono int16 at in_rate -> int16 at out_rate."""
        if self.in_rate == self.out_rate or mono.size == 0:
            return mono
        if self._r is not None:
            frame = self._av.AudioFrame.from_ndarray(mono.reshape(1, -1), format="s16", layout="mono")
            frame.sample_rate = self.in_rate
            outs = self._r.resample(frame)
            if not outs:
                return np.zeros(0, dtype=np.int16)
            return np.concatenate([o.to_ndarray().reshape(-1) for o in outs]).astype(np.int16)
        # simple fallback: linear interpolation with carried phase
        step = self.in_rate / self.out_rate
        idx = np.arange(self._pos, mono.size - 1, step)
        self._pos = (idx[-1] + step - mono.size) if idx.size else self._pos - mono.size
        return np.interp(idx, np.arange(mono.size), mono.astype(np.float32)).astype(np.int16)


class StreamRecorder:
    """Records one device (microphone or speaker loopback) to a 16 kHz mono WAV file.

    Windows loopback streams stop delivering audio while nothing is playing,
    so gaps are filled with silence based on the clock. That keeps the meeting
    track and the mic track lined up for correct timestamps."""

    def __init__(self, pa, device: dict, path: Path, speaker: str):
        self.pa = pa
        self.device = device
        self.path = path
        self.speaker = speaker
        self.rate = int(device["defaultSampleRate"])          # what the device delivers
        self.channels = max(1, int(device["maxInputChannels"]))
        self.out_rate = OUT_RATE                                # what we save
        self.frames_written = 0                                 # in saved samples
        self.level = 0.0
        self.t0: float | None = None
        self.last_loud = 0.0
        self.lock = threading.Lock()
        self.stream = None
        self.wav = None
        self.down = Downsampler(self.rate, self.out_rate)

    def open(self):
        self.wav = wave.open(str(self.path), "wb")
        self.wav.setnchannels(1)
        self.wav.setsampwidth(2)
        self.wav.setframerate(self.out_rate)
        self.stream = self.pa.open(
            format=pyaudio.paInt16, channels=self.channels, rate=self.rate,
            input=True, input_device_index=self.device["index"],
            frames_per_buffer=1024, stream_callback=self._callback, start=False,
        )

    def start(self, t0: float):
        self.t0 = t0
        self.last_loud = t0
        self.stream.start_stream()

    def _pad_to(self, now: float, extra_frames: int = 0):
        expected = int((now - self.t0) * self.out_rate) - extra_frames
        gap = expected - self.frames_written
        if gap > self.out_rate // 4:  # >250 ms missing: the device was silent
            self.wav.writeframes(b"\x00\x00" * gap)
            self.frames_written += gap

    def _callback(self, in_data, frame_count, time_info, status):
        now = time.monotonic()
        samples = np.frombuffer(in_data, dtype=np.int16)
        if self.channels > 1 and samples.size:
            samples = samples[: samples.size // self.channels * self.channels]
            mono = samples.reshape(-1, self.channels).astype(np.int32).mean(axis=1).astype(np.int16)
        else:
            mono = samples
        out = self.down(mono)
        with self.lock:
            if self.t0 is not None and self.wav is not None:
                self._pad_to(now, int(frame_count * self.out_rate / self.rate))
                self.wav.writeframes(out.tobytes())
                self.frames_written += out.size
        if samples.size:
            self.level = max(self.level, float(np.abs(samples).max()) / 32768.0)
            rms = float(np.sqrt(np.mean((mono.astype(np.float32) / 32768.0) ** 2)))
            if rms > 0.01:          # roughly a voice, not background hiss
                self.last_loud = now
        return (None, pyaudio.paContinue)

    def stop(self, stop_time: float):
        try:
            if self.stream is not None:
                self.stream.stop_stream()
                self.stream.close()
        finally:
            with self.lock:
                if self.wav is not None:
                    if self.t0 is not None:
                        self._pad_to(stop_time)
                    self.wav.close()
                    self.wav = None


def list_devices(pa):
    """Return ({label: loopback_device}, {label: mic_device}, default labels)."""
    wasapi = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
    loopbacks, mics = {}, {}

    def add(table, d):
        label = d["name"].replace(" [Loopback]", "").strip()
        base, n = label, 2
        while label in table:
            label = f"{base} ({n})"
            n += 1
        table[label] = d
        return label

    default_out = default_in = None
    if wasapi["defaultOutputDevice"] >= 0:
        default_out = pa.get_device_info_by_index(wasapi["defaultOutputDevice"])["name"]
    if wasapi["defaultInputDevice"] >= 0:
        default_in = pa.get_device_info_by_index(wasapi["defaultInputDevice"])["name"]

    default_spk_label = default_mic_label = None
    for i in range(pa.get_device_count()):
        d = pa.get_device_info_by_index(i)
        if d["hostApi"] != wasapi["index"] or d["maxInputChannels"] < 1:
            continue
        if d.get("isLoopbackDevice"):
            label = add(loopbacks, d)
            if default_out and default_out in d["name"] and not default_spk_label:
                default_spk_label = label
        else:
            label = add(mics, d)
            if default_in and d["name"] == default_in and not default_mic_label:
                default_mic_label = label
    return loopbacks, mics, default_spk_label, default_mic_label
