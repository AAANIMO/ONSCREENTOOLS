"""Webcam capture with green-screen keying or AI background removal.

Runs in its own thread and keeps the latest RGBA frame ready for the UI.
"""
from __future__ import annotations

import sys
import threading
import time
import urllib.request

import numpy as np

from .config import cache_dir

SEG_MODEL_URL = ("https://storage.googleapis.com/mediapipe-models/image_segmenter/"
                 "selfie_segmenter/float16/latest/selfie_segmenter.tflite")


# --------------------------------------------------------------------------- keying
def chroma_key(rgb: np.ndarray, key_rgb, tolerance: float = 40, softness: float = 25,
               spill: float = 0.8) -> np.ndarray:
    """Key out `key_rgb` from an RGB uint8 frame. Returns RGBA uint8.

    Distance is measured in chroma (CbCr) only, so shadows on the screen
    still key out. Pixels within `tolerance` are transparent, fading to
    opaque over `softness`. Spill suppression pulls the key's dominant
    channel down on the edges and on light reflected onto you.
    """
    import cv2

    ycc = cv2.cvtColor(rgb, cv2.COLOR_RGB2YCrCb)
    key = np.asarray(key_rgb, dtype=np.uint8).reshape(1, 1, 3)
    kcc = cv2.cvtColor(key, cv2.COLOR_RGB2YCrCb)[0, 0].astype(np.float32)
    cr = ycc[..., 1].astype(np.float32) - kcc[1]
    cb = ycc[..., 2].astype(np.float32) - kcc[2]
    d = cv2.magnitude(cr, cb)
    alpha = cv2.convertScaleAbs(d, alpha=255.0 / max(softness, 1e-3),
                                beta=-255.0 * tolerance / max(softness, 1e-3))
    alpha[d < tolerance] = 0

    out = cv2.cvtColor(rgb, cv2.COLOR_RGB2RGBA)
    if spill > 0:
        ch = int(np.argmax(key))  # 1 = green screen, 2 = blue screen
        a, b = [out[..., i] for i in range(3) if i != ch]
        excess = cv2.subtract(out[..., ch], cv2.max(a, b))
        out[..., ch] = cv2.addWeighted(out[..., ch], 1.0, excess, -float(spill), 0)
    out[..., 3] = alpha
    return out


class AISegmenter:
    """MediaPipe selfie segmentation: person vs. background, no green screen needed."""

    def __init__(self):
        import mediapipe as mp
        from mediapipe.tasks.python import BaseOptions, vision

        self._mp = mp
        model = self._model_path()
        opts = vision.ImageSegmenterOptions(
            base_options=BaseOptions(model_asset_path=str(model)),
            running_mode=vision.RunningMode.VIDEO,
            output_confidence_masks=True,
        )
        self._seg = vision.ImageSegmenter.create_from_options(opts)
        self._prev = None
        self._t0 = time.monotonic()
        self._last_ts = -1

    @staticmethod
    def _model_path():
        path = cache_dir() / "selfie_segmenter.tflite"
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            print("[onscreen] downloading selfie segmentation model…", file=sys.stderr)
            tmp = path.with_suffix(".part")
            urllib.request.urlretrieve(SEG_MODEL_URL, tmp)
            tmp.replace(path)
        return path

    def mask(self, rgb: np.ndarray) -> np.ndarray:
        import cv2

        ts = int((time.monotonic() - self._t0) * 1000)
        ts = max(ts, self._last_ts + 1)  # must be strictly increasing
        self._last_ts = ts
        image = self._mp.Image(image_format=self._mp.ImageFormat.SRGB, data=np.ascontiguousarray(rgb))
        res = self._seg.segment_for_video(image, ts)
        m = res.confidence_masks[0].numpy_view()
        m = m.reshape(m.shape[0], m.shape[1]).astype(np.float32)
        # temporal smoothing kills edge flicker; a steep curve tightens the matte
        if self._prev is not None and self._prev.shape == m.shape:
            m = 0.55 * m + 0.45 * self._prev
        self._prev = m
        m = np.clip((m - 0.35) / 0.3, 0.0, 1.0)
        m = cv2.GaussianBlur(m, (0, 0), 1.5)
        return m

    def apply(self, rgb: np.ndarray) -> np.ndarray:
        m = self.mask(rgb)
        out = np.empty(rgb.shape[:2] + (4,), dtype=np.uint8)
        out[..., :3] = rgb
        out[..., 3] = (m * 255).astype(np.uint8)
        return out


# --------------------------------------------------------------------------- capture
class CameraSource:
    def __init__(self, cfg):
        self.cfg = cfg
        self.frame: np.ndarray | None = None  # latest RGBA uint8
        self.frame_id = 0
        self.error: str | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._seg: AISegmenter | None = None
        self._seg_failed = False
        self.pick_request = False  # set by the UI to sample the key colour

    def start(self):
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="camera", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None

    def latest(self):
        with self._lock:
            return self.frame, self.frame_id

    def _open(self):
        import cv2

        if sys.platform == "darwin":
            backend = cv2.CAP_AVFOUNDATION
        elif sys.platform.startswith("linux"):
            backend = cv2.CAP_V4L2
        else:
            backend = cv2.CAP_ANY
        cap = cv2.VideoCapture(int(self.cfg.camera_index), backend)
        if not cap.isOpened():
            cap = cv2.VideoCapture(int(self.cfg.camera_index))
        if cap.isOpened():
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.cfg.camera_width)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.cfg.camera_height)
        return cap

    def process(self, rgb: np.ndarray) -> np.ndarray:
        cfg = self.cfg
        if cfg.mirror:
            rgb = rgb[:, ::-1]
        z = float(cfg.camera_zoom or 1.0)
        if z > 1.01:
            h, w = rgb.shape[:2]
            ch, cw = int(h / z), int(w / z)
            y0, x0 = (h - ch) // 2, (w - cw) // 2
            rgb = rgb[y0:y0 + ch, x0:x0 + cw]
        # no point keying more pixels than end up on screen
        h, w = rgb.shape[:2]
        target_h = int(max(360, cfg.mascot_size * 1.5))
        if h > target_h * 1.1:
            import cv2

            rgb = cv2.resize(rgb, (int(w * target_h / h), target_h), interpolation=cv2.INTER_AREA)
        rgb = np.ascontiguousarray(rgb)
        if cfg.bg_removal == "chroma":
            return chroma_key(rgb, cfg.chroma_color, cfg.chroma_tolerance, cfg.chroma_softness, cfg.spill)
        if cfg.bg_removal == "ai":
            if self._seg is None and not self._seg_failed:
                try:
                    self._seg = AISegmenter()
                except Exception as e:
                    self._seg_failed = True
                    self.error = (f"AI background removal unavailable ({e}). "
                                  "Install it with: pip install mediapipe")
                    print(f"[onscreen] {self.error}", file=sys.stderr)
            if self._seg is not None:
                return self._seg.apply(rgb)
        out = np.empty(rgb.shape[:2] + (4,), dtype=np.uint8)
        out[..., :3] = rgb
        out[..., 3] = 255
        return out

    def _run(self):
        import cv2

        cap = self._open()
        if not cap.isOpened():
            self.error = (f"Cannot open camera {self.cfg.camera_index}. "
                          "On macOS, allow camera access for your terminal in System Settings → Privacy.")
            print(f"[onscreen] {self.error}", file=sys.stderr)
            return
        try:
            while not self._stop.is_set():
                ok, bgr = cap.read()
                if not ok:
                    time.sleep(0.02)
                    continue
                rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
                if self.pick_request:
                    # sample the key colour from the top-left corner of the frame
                    self.pick_request = False
                    patch = rgb[: rgb.shape[0] // 8, : rgb.shape[1] // 8].reshape(-1, 3)
                    self.cfg.chroma_color = [int(v) for v in np.median(patch, axis=0)]
                rgba = self.process(rgb)
                with self._lock:
                    self.frame = rgba
                    self.frame_id += 1
        finally:
            cap.release()
