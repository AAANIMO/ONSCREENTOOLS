"""Publish the mascot as a virtual webcam (OBS Virtual Camera on macOS, v4l2loopback on Linux).

Use it in Zoom/Meet/OBS as a regular camera. The background is a flat key
colour so you can chroma-key it wherever it ends up.
"""
from __future__ import annotations

import numpy as np
from PySide6.QtCore import QRectF
from PySide6.QtGui import QColor, QImage, QPainter

from .overlay import paint_scene, scene_layout

WIDTH, HEIGHT, FPS = 1280, 720, 30


class VirtualCam:
    def __init__(self, ctl, width: int = WIDTH, height: int = HEIGHT):
        import pyvirtualcam

        self.ctl = ctl
        self.cam = pyvirtualcam.Camera(width=width, height=height, fps=FPS, fmt=pyvirtualcam.PixelFormat.RGB)
        self.w, self.h = width, height
        self.img = QImage(width, height, QImage.Format_RGB888)
        self._skip = 0

    def send(self):
        # the UI ticks at 60 fps, the camera wants 30
        self._skip ^= 1
        if self._skip:
            return
        cfg = self.ctl.cfg
        bg = cfg.window_bg if cfg.window_bg not in ("", "transparent", None) else "#00b140"
        self.img.fill(QColor(bg))
        p = QPainter(self.img)
        p.setRenderHint(QPainter.Antialiasing, True)
        # mascot fills ~60 % of the frame height, in the bottom corner
        size, mascot, bubble = scene_layout(cfg, self.ctl.mascot_aspect(), cfg.subtitles == "bubble")
        scale = (self.h * 0.6) / max(1, mascot.height())
        p.save()
        ox = 24 if cfg.bubble_side == "right" else self.w - size.width() * scale - 24
        p.translate(ox, self.h - size.height() * scale - 12)
        p.scale(scale, scale)
        paint_scene(p, self.ctl, self.ctl.frame, mascot, bubble)
        p.restore()
        paint_scene_bottom = cfg.subtitles == "bottom"
        if paint_scene_bottom:
            from .render import draw_bottom_subtitles

            f = self.ctl.frame
            draw_bottom_subtitles(p, QRectF(0, 0, self.w, self.h - 20), f.text, self.ctl.bottom_font,
                                  f.text_opacity)
        p.end()
        ptr = self.img.constBits()
        arr = np.frombuffer(ptr, dtype=np.uint8, count=self.img.sizeInBytes())
        arr = arr.reshape(self.h, self.img.bytesPerLine())[:, : self.w * 3].reshape(self.h, self.w, 3)
        self.cam.send(np.ascontiguousarray(arr))

    def close(self):
        try:
            self.cam.close()
        except Exception:
            pass
