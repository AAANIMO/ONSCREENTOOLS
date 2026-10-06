"""Cutout-style puppets.

* `BuiltinPuppet` – a construction-paper kid drawn with QPainter (no assets needed).
* `ImagePuppet`   – your own artwork, either as mouth frames or as a head + jaw cutout.

A puppet folder holds a `puppet.json`:

    {"type": "frames",
     "base": "base.png",                              # optional, drawn first
     "mouths": ["closed.png", "half.png", "open.png"], # same canvas size as base
     "blink": "blink.png",                             # optional overlay
     "mouth": [0.5, 0.62]}                             # mouth position (fraction), for the bubble tail

    {"type": "jaw",
     "back": "mouth_inside.png",   # optional, drawn behind the jaw
     "jaw": "jaw.png",             # moves down while talking
     "head": "head.png",           # drawn on top, with the mouth area cut out
     "max_drop": 0.08,             # jaw drop as a fraction of the image height
     "blink": "blink.png",
     "mouth": [0.5, 0.62]}

    {"type": "builtin", "base": "beanie", "hat": "#ff00aa", ...}   # recolour a preset

    {"type": "canadian", "image": "me.png", ...}   # photo cut at the mouth, see cutout.py

All images in a puppet share one canvas size so they line up.
"""
from __future__ import annotations

import json
import math
import random
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath, QPen

from .lipsync import quantize

PRESETS: dict[str, dict] = {
    "beanie": dict(hat_style="beanie", skin="#ffdcb4", hat="#2f6db5", rim="#e03131",
                   coat="#8b5a2b", pants="#2b4c9a", mitten="#e03131", outline="#2b1d14"),
    "pompom": dict(hat_style="beanie", skin="#ffdcb4", hat="#2e9e44", rim="#f2c200",
                   coat="#d94141", pants="#3a3a3a", mitten="#2e9e44", outline="#2b1d14"),
    "hood": dict(hat_style="hood", skin="#ffdcb4", hat="#f08c00", rim="#b85f00",
                 coat="#f08c00", pants="#f08c00", mitten="#7a4b1f", outline="#2b1d14"),
    "hair": dict(hat_style="hair", skin="#ffdcb4", hat="#3b2a1a", rim="#3b2a1a",
                 coat="#c92a2a", pants="#3d5a80", mitten="#ffdcb4", outline="#2b1d14"),
    "redhead": dict(hat_style="hair", skin="#ffe3c8", hat="#d9480f", rim="#d9480f",
                    coat="#5c940d", pants="#495057", mitten="#ffe3c8", outline="#2b1d14"),
}

# Builtin puppets are drawn in a 100 x 120 unit box.
_UW, _UH = 100.0, 120.0


class Blinker:
    """Random blinks: returns lid closure 0..1."""

    def __init__(self):
        self._next = random.uniform(1.5, 4.0)
        self._t0 = None

    def update(self, t: float) -> float:
        if self._t0 is None and t >= self._next:
            self._t0 = t
        if self._t0 is None:
            return 0.0
        k = (t - self._t0) / 0.14  # 140 ms blink
        if k >= 1.0:
            self._t0 = None
            # occasional double blink
            self._next = t + (0.18 if random.random() < 0.15 else random.uniform(2.0, 5.5))
            return 0.0
        return 1.0 - abs(2 * k - 1)


class Puppet:
    aspect = _UW / _UH  # width / height

    def draw(self, p: QPainter, rect: QRectF, openness: float, t: float, blink: float) -> None:
        raise NotImplementedError

    def mouth_anchor(self, rect: QRectF) -> QPointF:
        return QPointF(rect.center().x(), rect.top() + rect.height() * 0.5)


def fit_rect(outer: QRectF, aspect: float) -> QRectF:
    """Largest rect with `aspect` (w/h) inside `outer`, bottom-centred."""
    w, h = outer.width(), outer.height()
    if w / max(h, 1e-6) > aspect:
        w = h * aspect
    else:
        h = w / aspect
    return QRectF(outer.center().x() - w / 2, outer.bottom() - h, w, h)


class BuiltinPuppet(Puppet):
    def __init__(self, style: dict | str = "beanie"):
        if isinstance(style, str):
            style = PRESETS.get(style, PRESETS["beanie"])
        self.s = {k: v for k, v in style.items()}
        self.c = {k: QColor(v) for k, v in style.items() if isinstance(v, str) and v.startswith("#")}

    # -- helpers ---------------------------------------------------------
    def _pen(self, width=1.4):
        pen = QPen(self.c["outline"], width)
        pen.setJoinStyle(Qt.RoundJoin)
        pen.setCapStyle(Qt.RoundCap)
        return pen

    @staticmethod
    def _head_path(cx, cy, rx, ry_top, ry_bot) -> QPainterPath:
        """Ellipse whose lower half can be stretched (the jaw drops)."""
        path = QPainterPath()
        path.moveTo(cx + rx, cy)
        path.arcTo(QRectF(cx - rx, cy - ry_top, 2 * rx, 2 * ry_top), 0, 180)
        path.arcTo(QRectF(cx - rx, cy - ry_bot, 2 * rx, 2 * ry_bot), 180, 180)
        path.closeSubpath()
        return path

    def mouth_anchor(self, rect: QRectF) -> QPointF:
        r = fit_rect(rect, self.aspect)
        return QPointF(r.left() + r.width() * 0.5, r.top() + r.height() * (60 / _UH))

    # -- drawing ---------------------------------------------------------
    def draw(self, p: QPainter, rect: QRectF, openness: float, t: float, blink: float) -> None:
        r = fit_rect(rect, self.aspect)
        o = max(0.0, min(1.0, openness))
        p.save()
        p.setRenderHint(QPainter.Antialiasing, True)
        p.translate(r.left(), r.top())
        p.scale(r.width() / _UW, r.height() / _UH)

        # idle breathing + a little hop while talking
        bob = math.sin(t * 2.2) * 0.6 - o * 2.0
        self._draw_body(p, bob * 0.4)
        p.translate(0, bob)

        # head wobbles around the neck while talking
        tilt = math.sin(t * 7.3) * o * 3.5 + math.sin(t * 0.9) * 1.2
        p.translate(50, 74)
        p.rotate(tilt)
        p.translate(-50, -74)
        self._draw_head(p, o, blink)
        p.restore()

    def _draw_body(self, p: QPainter, dy: float):
        c = self.c
        p.save()
        p.translate(0, dy)
        pen = self._pen()
        # feet
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#1a1a1a"))
        p.drawEllipse(QRectF(22, 111, 27, 9))
        p.drawEllipse(QRectF(51, 111, 27, 9))
        # pants
        p.setBrush(c["pants"])
        p.setPen(pen)
        p.drawRoundedRect(QRectF(30, 100, 40, 14), 3, 3)
        p.drawLine(QPointF(50, 104), QPointF(50, 114))
        # coat
        coat = QPainterPath()
        coat.moveTo(27, 70)
        coat.lineTo(73, 70)
        coat.quadTo(78, 88, 76, 104)
        coat.quadTo(50, 108, 24, 104)
        coat.quadTo(22, 88, 27, 70)
        p.setBrush(c["coat"])
        p.drawPath(coat)
        p.drawLine(QPointF(50, 78), QPointF(50, 105))
        # buttons / zipper detail
        p.setBrush(c["outline"])
        for y in (86, 94):
            p.drawEllipse(QPointF(46, y), 0.9, 0.9)
        # arms + mittens
        p.setBrush(c["coat"])
        p.drawEllipse(QRectF(17, 78, 12, 16))
        p.drawEllipse(QRectF(71, 78, 12, 16))
        p.setBrush(c["mitten"])
        p.drawEllipse(QPointF(22, 96), 6.2, 5.6)
        p.drawEllipse(QPointF(78, 96), 6.2, 5.6)
        p.restore()

    def _draw_head(self, p: QPainter, o: float, blink: float):
        c, style = self.c, self.s.get("hat_style", "beanie")
        pen = self._pen()
        drop = o * 9.0
        cx, cy, rx = 50.0, 44.0, 40.0
        head = self._head_path(cx, cy, rx, 36.0, 30.0 + drop)

        if style == "hood":
            hood = self._head_path(cx, cy + 1, rx + 7, 43.0, 35.0 + drop)
            p.setPen(pen)
            p.setBrush(c["hat"])
            p.drawPath(hood)

        p.setPen(pen)
        p.setBrush(c["skin"])
        p.drawPath(head)

        if style == "beanie":
            cap = QPainterPath()
            cap.addEllipse(QRectF(cx - rx - 1.5, cy - 38, 2 * rx + 3, 76))
            clip = QPainterPath()
            clip.addRect(QRectF(0, -10, _UW, 36))
            p.setBrush(c["hat"])
            p.drawPath(cap.intersected(clip))
            p.setBrush(c["rim"])
            p.drawRoundedRect(QRectF(cx - rx - 2, 20.5, 2 * rx + 4, 8.5), 4, 4)
            p.drawEllipse(QPointF(cx, 5.5), 6.5, 6.0)
        elif style == "hair":
            hair = QPainterPath()
            hair.addEllipse(QRectF(cx - rx - 1.5, cy - 38, 2 * rx + 3, 76))
            fringe = QPainterPath()
            fringe.moveTo(0, -10)
            fringe.lineTo(_UW, -10)
            fringe.lineTo(_UW, 30)
            x, up = _UW - 6, True
            while x > 6:
                fringe.lineTo(x, 22 if up else 31)
                x -= 7
                up = not up
            fringe.lineTo(0, 30)
            fringe.closeSubpath()
            p.setBrush(c["hat"])
            p.drawPath(hair.intersected(fringe))
            # a cowlick on top
            tuft = QPainterPath()
            tuft.moveTo(46, 9)
            tuft.quadTo(50, -1, 57, 2)
            tuft.quadTo(53, 5, 54, 9)
            p.drawPath(tuft)

        self._draw_eyes(p, blink)
        self._draw_mouth(p, o, drop)

        if style == "hood":
            # the face opening of the parka, drawn over the head
            ring = self._head_path(cx, cy + 1, rx + 7, 43.0, 35.0 + drop)
            face = QPainterPath()
            face.addEllipse(QRectF(cx - 31, 19, 62, 50 + drop * 0.9))
            p.setBrush(c["hat"])
            p.setPen(pen)
            p.drawPath(ring.subtracted(face))
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(c["rim"], 3.0))
            p.drawEllipse(QRectF(cx - 31, 19, 62, 50 + drop * 0.9))
            p.setPen(pen)
            p.setBrush(c["rim"])
            p.drawEllipse(QPointF(44, 72 + drop), 1.6, 1.6)
            p.drawEllipse(QPointF(56, 72 + drop), 1.6, 1.6)

    def _draw_eyes(self, p: QPainter, blink: float):
        c = self.c
        pen = self._pen(1.1)
        for ex, px in ((40.5, 46.0), (59.5, 54.0)):
            eye = QRectF(ex - 10, 28.5, 20, 23)
            p.setPen(pen)
            p.setBrush(QColor("white"))
            p.drawEllipse(eye)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor("#111"))
            p.drawEllipse(QPointF(px, 41.0), 2.2, 2.2)
            if blink > 0.01:
                p.save()
                clip = QPainterPath()
                clip.addEllipse(eye)
                p.setClipPath(clip)
                p.setBrush(c["skin"])
                lid_h = eye.height() * blink
                p.drawRect(QRectF(eye.left() - 1, eye.top() - 1, eye.width() + 2, lid_h + 1))
                p.setPen(pen)
                p.drawLine(QPointF(eye.left(), eye.top() + lid_h), QPointF(eye.right(), eye.top() + lid_h))
                p.restore()
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)

    def _draw_mouth(self, p: QPainter, o: float, drop: float):
        pen = self._pen(1.3)
        top, half = 58.0, 8.0 + 2.5 * o
        if o < 0.06:
            smile = QPainterPath()
            smile.moveTo(50 - half, top)
            smile.quadTo(50, top + 3.0, 50 + half, top - 0.5)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            p.drawPath(smile)
            return
        depth = 3.0 + o * 13.0 + drop * 0.3
        mouth = QPainterPath()
        mouth.moveTo(50 - half, top)
        mouth.quadTo(50, top + 1.5, 50 + half, top - 0.5)
        mouth.cubicTo(50 + half, top + depth * 1.1, 50 - half, top + depth * 1.1, 50 - half, top)
        mouth.closeSubpath()
        p.setPen(pen)
        p.setBrush(QColor("#3d0b0b"))
        p.drawPath(mouth)
        p.save()
        p.setClipPath(mouth)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#e8596b"))
        p.drawEllipse(QPointF(50, top + depth * 0.95), half * 0.7, depth * 0.38)
        if o > 0.3:
            p.setBrush(QColor("white"))
            p.drawRect(QRectF(50 - half, top - 2, half * 2, 2.8 + o * 1.2))
        p.restore()


class ImagePuppet(Puppet):
    def __init__(self, folder: str | Path):
        self.folder = Path(folder).expanduser()
        meta = json.loads((self.folder / "puppet.json").read_text())
        self.kind = meta.get("type", "frames")
        self.mouth_pos = meta.get("mouth", [0.5, 0.6])
        self.max_drop = float(meta.get("max_drop", 0.08))
        load = self._load
        self.base = load(meta.get("base"))
        self.blink_img = load(meta.get("blink"))
        self.mouths = [load(m) for m in meta.get("mouths", [])]
        self.mouths = [m for m in self.mouths if m is not None]
        self.back = load(meta.get("back"))
        self.jaw = load(meta.get("jaw"))
        self.head = load(meta.get("head"))
        ref = next((i for i in (self.base, self.head, *(self.mouths or [None])) if i is not None), None)
        if ref is None:
            raise ValueError(f"{self.folder}: puppet.json references no images")
        self.size = ref.size()
        self.aspect = self.size.width() / max(1, self.size.height())

    def _load(self, name):
        if not name:
            return None
        img = QImage(str(self.folder / name))
        if img.isNull():
            raise ValueError(f"cannot load image {self.folder / name}")
        return img

    def mouth_anchor(self, rect: QRectF) -> QPointF:
        r = fit_rect(rect, self.aspect)
        return QPointF(r.left() + r.width() * self.mouth_pos[0], r.top() + r.height() * self.mouth_pos[1])

    def draw(self, p: QPainter, rect: QRectF, openness: float, t: float, blink: float) -> None:
        r = fit_rect(rect, self.aspect)
        p.save()
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        bob = (math.sin(t * 2.2) * 0.004 - openness * 0.012) * r.height()
        r = r.translated(0, bob)
        if self.kind == "jaw":
            if self.back is not None:
                p.drawImage(r, self.back)
            if self.jaw is not None:
                p.drawImage(r.translated(0, openness * self.max_drop * r.height()), self.jaw)
            if self.head is not None:
                p.drawImage(r, self.head)
        else:
            if self.base is not None:
                p.drawImage(r, self.base)
            if self.mouths:
                p.drawImage(r, self.mouths[quantize(openness, len(self.mouths))])
        if self.blink_img is not None and blink > 0.5:
            p.drawImage(r, self.blink_img)
        p.restore()


def load_puppet(spec: str) -> Puppet:
    """A preset name, or a folder containing puppet.json."""
    if spec in PRESETS:
        return BuiltinPuppet(spec)
    folder = Path(spec).expanduser()
    meta_path = folder / "puppet.json"
    if meta_path.is_file():
        meta = json.loads(meta_path.read_text())
        if meta.get("type") == "builtin":
            style = dict(PRESETS.get(meta.get("base", "beanie"), PRESETS["beanie"]))
            style.update({k: v for k, v in meta.items() if k not in ("type", "base")})
            return BuiltinPuppet(style)
        if meta.get("type") == "canadian":
            from .cutout import CanadianPuppet

            return CanadianPuppet.from_folder(folder, meta)
        return ImagePuppet(folder)
    raise ValueError(f"unknown puppet '{spec}': use one of {', '.join(PRESETS)} or a folder with puppet.json")


def export_template(preset: str, folder: str | Path, height: int = 1200) -> Path:
    """Render a builtin puppet into a `frames` puppet folder you can repaint."""
    folder = Path(folder).expanduser()
    folder.mkdir(parents=True, exist_ok=True)
    pup = BuiltinPuppet(preset)
    w = int(height * pup.aspect)

    def render(name, openness, blink, clip=None):
        img = QImage(w, height, QImage.Format_ARGB32_Premultiplied)
        img.fill(Qt.transparent)
        p = QPainter(img)
        if clip is not None:
            p.setClipRect(clip)
        # t chosen so the idle sway is zero-ish and frames line up
        pup.draw(p, QRectF(0, 0, w, height), openness, 0.0, blink)
        p.end()
        img.save(str(folder / name))

    names = ["mouth_0.png", "mouth_1.png", "mouth_2.png", "mouth_3.png"]
    for i, n in enumerate(names):
        render(n, i / (len(names) - 1), 0.0)
    # blink overlay = just the eye region with closed lids
    render("blink.png", 0.0, 1.0, QRectF(w * 0.28, height * 27 / _UH, w * 0.44, height * 26 / _UH))
    anchor = pup.mouth_anchor(QRectF(0, 0, w, height))
    (folder / "puppet.json").write_text(json.dumps({
        "type": "frames",
        "mouths": names,
        "blink": "blink.png",
        "mouth": [round(anchor.x() / w, 3), round(anchor.y() / height, 3)],
    }, indent=2))
    return folder
