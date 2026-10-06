"""Microphone capture: a live level meter for lip-sync plus a 16 kHz feed for Whisper."""
from __future__ import annotations

import queue
import sys
import threading

import numpy as np

WHISPER_RATE = 16000


def list_input_devices() -> list[tuple[int, str]]:
    import sounddevice as sd

    out = []
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] > 0:
            out.append((i, d["name"]))
    return out


def _resample(x: np.ndarray, src: int, dst: int) -> np.ndarray:
    if src == dst or len(x) == 0:
        return x
    n = max(1, int(round(len(x) * dst / src)))
    return np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x).astype(np.float32)


class AudioEngine:
    """Opens the input device and fans audio out to subscribers.

    `level` is the RMS of the most recent block (0..1). Every subscriber queue
    receives mono float32 chunks at 16 kHz.
    """

    def __init__(self, device=None, blocksize_ms: int = 20):
        self.device = device
        self.blocksize_ms = blocksize_ms
        self.level = 0.0
        self.error: str | None = None
        self._subs: list[queue.Queue] = []
        self._lock = threading.Lock()
        self._stream = None
        self._rate = WHISPER_RATE

    def subscribe(self, maxsize: int = 500) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=maxsize)
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def _callback(self, indata, frames, time_info, status):
        mono = indata[:, 0] if indata.ndim > 1 else indata
        mono = np.asarray(mono, dtype=np.float32)
        self.level = float(np.sqrt(np.mean(mono * mono))) if len(mono) else 0.0
        chunk = _resample(mono, self._rate, WHISPER_RATE)
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(chunk.copy())
            except queue.Full:
                # consumer is behind: drop the oldest chunk rather than block the audio thread
                try:
                    q.get_nowait()
                    q.put_nowait(chunk.copy())
                except (queue.Empty, queue.Full):
                    pass

    def start(self) -> None:
        import sounddevice as sd

        device = self.device
        if isinstance(device, str) and device.isdigit():
            device = int(device)
        self.error = None
        # Ask for 16 kHz directly (CoreAudio / Pulse / PipeWire resample for us);
        # fall back to the device's native rate and resample ourselves.
        rates = [WHISPER_RATE]
        try:
            native = int(sd.query_devices(device, "input")["default_samplerate"])
            if native != WHISPER_RATE:
                rates.append(native)
        except Exception:
            pass
        last_err = None
        for rate in rates:
            try:
                self._rate = rate
                self._stream = sd.InputStream(
                    device=device,
                    channels=1,
                    samplerate=rate,
                    dtype="float32",
                    blocksize=int(rate * self.blocksize_ms / 1000),
                    callback=self._callback,
                )
                self._stream.start()
                return
            except Exception as e:  # PortAudioError, ValueError
                last_err = e
                self._stream = None
        self.error = f"Microphone unavailable: {last_err}"
        print(f"[onscreen] {self.error}", file=sys.stderr)

    def stop(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None
        self.level = 0.0
