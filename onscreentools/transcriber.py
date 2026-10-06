"""Live subtitles with Whisper.

Whisper isn't a streaming model, so we fake it: while you speak we re-run
it on the growing utterance every ~0.7 s (partial results), and once you
pause (or the utterance gets long) we run it a final time and commit.
"""
from __future__ import annotations

import queue
import re
import sys
import threading
import time

import numpy as np

from .audio import WHISPER_RATE
from .lipsync import rms_to_db

# Whisper's favourite things to say to silence
_HALLUCINATIONS = {
    "thank you.", "thanks for watching!", "thanks for watching.", "thank you for watching.",
    "you", "bye.", "subtitles by the amara.org community", "grazie a tutti.",
    "sottotitoli a cura di qtss", "sottotitoli creati dalla comunità amara.org", "♪",
}


# credits Whisper learned from subtitled videos and "hears" in silence
_HALLUCINATION_RE = re.compile(r"amara\.org|qtss|sottotitoli (creati|a cura|e revisione)|"
                               r"iscriviti al canale|subtitles by", re.I)

# Partials use greedy decoding only; finals may retry warmer, but not so hot
# that they drift into another language.
TEMPS_PARTIAL = (0.0,)
TEMPS_FINAL = (0.0, 0.2, 0.4)


def _clean(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    if text.lower() in _HALLUCINATIONS or _HALLUCINATION_RE.search(text):
        return ""
    return text


class FasterWhisperBackend:
    def __init__(self, model: str, language: str | None, translate: bool):
        from faster_whisper import WhisperModel

        device, compute = "cpu", "int8"
        try:
            import ctranslate2

            if ctranslate2.get_cuda_device_count() > 0:
                device, compute = "cuda", "float16"
        except Exception:
            pass
        self.model = WhisperModel(model, device=device, compute_type=compute)
        self.language = language or None
        self.task = "translate" if translate else "transcribe"

    def __call__(self, audio: np.ndarray, prompt: str | None, final: bool) -> str:
        segments, info = self.model.transcribe(
            audio,
            language=self.language,
            task=self.task,
            beam_size=5 if final else 1,
            temperature=TEMPS_FINAL if final else TEMPS_PARTIAL,
            condition_on_previous_text=False,
            initial_prompt=prompt or None,
            without_timestamps=True,
            vad_filter=False,
        )
        if self.language is None and getattr(info, "language_probability", 0) > 0.8:
            self.language = info.language  # lock in after a confident detection: faster + steadier
        parts = []
        for s in segments:
            if s.no_speech_prob > 0.6 and s.avg_logprob < -1.0:
                continue
            parts.append(s.text)
        return _clean("".join(parts))


class MLXWhisperBackend:
    """Apple Silicon: whisper on the GPU via MLX."""

    REPOS = {
        "tiny": "mlx-community/whisper-tiny-mlx", "base": "mlx-community/whisper-base-mlx",
        "small": "mlx-community/whisper-small-mlx", "medium": "mlx-community/whisper-medium-mlx",
        "large-v3": "mlx-community/whisper-large-v3-mlx",
        "large-v3-turbo": "mlx-community/whisper-large-v3-turbo",
        "turbo": "mlx-community/whisper-large-v3-turbo",
    }

    def __init__(self, model: str, language: str | None, translate: bool):
        import mlx_whisper

        self._t = mlx_whisper.transcribe
        self.repo = self.REPOS.get(model, model)
        self.language = language or None
        self.task = "translate" if translate else "transcribe"
        # warm up / download
        self._t(np.zeros(WHISPER_RATE, dtype=np.float32), path_or_hf_repo=self.repo, language=self.language)

    def __call__(self, audio: np.ndarray, prompt: str | None, final: bool) -> str:
        res = self._t(audio, path_or_hf_repo=self.repo, language=self.language, task=self.task,
                      initial_prompt=prompt or None, condition_on_previous_text=False,
                      temperature=TEMPS_FINAL if final else TEMPS_PARTIAL)
        segs = [s for s in res.get("segments", [])
                if not (s.get("no_speech_prob", 0) > 0.6 and s.get("avg_logprob", 0) < -1.0)]
        return _clean("".join(s.get("text", "") for s in segs))


def make_backend(kind: str, model: str, language: str | None, translate: bool):
    if kind in ("mlx", "auto") and sys.platform == "darwin":
        try:
            return MLXWhisperBackend(model, language, translate)
        except ImportError:
            if kind == "mlx":
                raise
    return FasterWhisperBackend(model, language, translate)


class Transcriber:
    """Consumes 16 kHz chunks from `audio_q`, emits ("partial"|"final", text) on `out`."""

    def __init__(self, audio_q: queue.Queue, backend_factory, gate_db: float = -50.0,
                 step: float = 0.7, silence_end: float = 0.65, max_utterance: float = 12.0):
        self.audio_q = audio_q
        self.backend_factory = backend_factory
        self.gate_db = gate_db
        self.step = step
        self.silence_end = silence_end
        self.max_utterance = max_utterance
        self.out: queue.Queue = queue.Queue()
        self.status = "loading"
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.backend = None

    def start(self):
        self._thread = threading.Thread(target=self._run, name="whisper", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=0.2)

    # Exposed for tests: feed audio and step the state machine without threads.
    def _reset(self):
        self.buf: list[np.ndarray] = []
        self.buf_len = 0
        self.in_speech = False
        self.silence = 0.0
        self.since_partial = 0.0
        self.last_partial = ""
        self.prompt = ""

    def feed(self, chunk: np.ndarray) -> None:
        dur = len(chunk) / WHISPER_RATE
        loud = rms_to_db(float(np.sqrt(np.mean(chunk * chunk))) if len(chunk) else 0.0) > self.gate_db + 4
        if loud:
            self.in_speech = True
            self.silence = 0.0
        elif self.in_speech:
            self.silence += dur
        self.buf.append(chunk)
        self.buf_len += len(chunk)
        if self.in_speech:
            self.since_partial += dur
        else:
            # keep ~0.3 s of pre-roll so the first syllable isn't clipped
            while self.buf and self.buf_len - len(self.buf[0]) > 0.3 * WHISPER_RATE:
                self.buf_len -= len(self.buf.pop(0))

    def tick(self) -> None:
        """Decide whether to run Whisper now."""
        if not self.in_speech:
            return
        seconds = self.buf_len / WHISPER_RATE
        if self.silence >= self.silence_end or seconds >= self.max_utterance:
            self._transcribe(final=True)
            self._reset_utterance()
        elif self.since_partial >= self.step and seconds >= 0.5:
            self._transcribe(final=False)
            self.since_partial = 0.0

    def _reset_utterance(self):
        self.buf, self.buf_len = [], 0
        self.in_speech, self.silence, self.since_partial = False, 0.0, 0.0
        self.last_partial = ""

    def _transcribe(self, final: bool):
        audio = np.concatenate(self.buf) if self.buf else np.zeros(0, np.float32)
        if len(audio) < 0.3 * WHISPER_RATE:
            return
        try:
            text = self.backend(audio, self.prompt[-200:], final)
        except Exception as e:
            print(f"[onscreen] whisper error: {e}", file=sys.stderr)
            return
        if final:
            if text:
                self.out.put(("final", text))
                self.prompt = (self.prompt + " " + text).strip()
            elif self.last_partial:
                self.out.put(("final", self.last_partial))
        elif text and text != self.last_partial:
            self.last_partial = text
            self.out.put(("partial", text))

    def _run(self):
        self._reset()
        try:
            self.backend = self.backend_factory()
        except Exception as e:
            self.status = f"error: {e}"
            print(f"[onscreen] could not load Whisper: {e}", file=sys.stderr)
            return
        self.status = "ready"
        # throw away audio that piled up while the model loaded
        while not self.audio_q.empty():
            try:
                self.audio_q.get_nowait()
            except queue.Empty:
                break
        while not self._stop.is_set():
            try:
                chunk = self.audio_q.get(timeout=0.1)
            except queue.Empty:
                continue
            self.feed(chunk)
            # drain everything already waiting (we may be behind real time)
            while True:
                try:
                    self.feed(self.audio_q.get_nowait())
                except queue.Empty:
                    break
            self.tick()


class SubtitleState:
    """What to show right now, with timing for fade-out."""

    def __init__(self, hide_after: float = 2.5, max_chars: int = 160):
        self.hide_after = hide_after
        self.max_chars = max_chars
        self.committed = ""      # finished sentences of the current "speech burst"
        self.partial = ""
        self.last_update = 0.0

    def push(self, kind: str, text: str, now: float | None = None):
        now = time.monotonic() if now is None else now
        if now - self.last_update > self.hide_after and not self.partial:
            self.committed = ""  # long pause: start a fresh caption
        if kind == "final":
            self.committed = (self.committed + " " + text).strip()
            self.partial = ""
        else:
            self.partial = text
        self.last_update = now

    def text(self) -> str:
        t = (self.committed + " " + self.partial).strip()
        if len(t) > self.max_chars:
            # keep the tail, cut on a word boundary
            t = t[-self.max_chars:]
            t = "…" + t[t.find(" ") + 1:] if " " in t else t
        return t

    def opacity(self, now: float | None = None, talking: bool = False) -> float:
        now = time.monotonic() if now is None else now
        if not self.text():
            return 0.0
        if talking or self.partial:
            return 1.0
        idle = now - self.last_update
        if idle < self.hide_after:
            return 1.0
        return max(0.0, 1.0 - (idle - self.hide_after) / 0.4)

    def clear(self):
        self.committed = self.partial = ""
