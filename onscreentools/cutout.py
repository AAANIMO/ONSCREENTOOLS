"""Photo cutout puppets, Canadian style.

Take a PNG of a person and draw a line across the mouth. While you talk,
everything above the line flaps open like Terrance & Phillip: it pivots
on one end of the line or the other, picked at random on every syllable,
and hops around jittery. Nothing is painted into the gap by default, so
whatever is behind the mascot shows through.

puppet.json:

    {"type": "canadian",
     "image": "image.png",
     "a": [0.30, 0.55],        # the two ends of the mouth line, fractions of the image
     "b": [0.75, 0.53],
     "pivot": "random",        # "random" (per syllable), "left" or "right"
     "max_angle": 28,          # degrees the head opens at full volume
     "chaos": 1.0,             # 0 = clean flap, 2 = maniac hopping
     "mouth_fill": false,      # true = paint the gap with mouth_color
     "mouth_color": "#1a0505",
     "eyes": {"left": [0.4, 0.3], "right": [0.6, 0.3], "size": 0.06},     # optional
     "hands": {"left": [0.2, 0.7], "right": [0.8, 0.7], "length": 0.16,   # optional
               "sleeve": "#3355aa", "skin": "#e8b48f"}}

Cartoon eyes are drawn over the photo: they ride along with the flapping
head, blink, and follow the mouse. Hands hang from the shoulders and
point at your clicks, like the built-in puppets.

Older files with "front"/"hinge" are read as the two ends of the line.
"""
from __future__ import annotations

import json
import math
import random
import re
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath, QPen, QPolygonF, QTransform
from PySide6.QtWidgets import (QCheckBox, QColorDialog, QComboBox, QDialog, QDialogButtonBox,
                               QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
                               QMessageBox, QPushButton, QSlider, QVBoxLayout, QWidget)

from .config import config_dir
from .puppet import Puppet, fit_rect

PIVOTS = ("random", "left", "right")


def _sample_color(img: QImage, x: float, y: float, fallback: str, r: int = 3) -> QColor:
    """Average opaque colour around (x, y) in image pixels."""
    tot, n = [0, 0, 0], 0
    for dy in range(-r, r + 1):
        for dx in range(-r, r + 1):
            px, py = int(x) + dx, int(y) + dy
            if 0 <= px < img.width() and 0 <= py < img.height():
                c = img.pixelColor(px, py)
                if c.alpha() > 200:
                    tot[0] += c.red()
                    tot[1] += c.green()
                    tot[2] += c.blue()
                    n += 1
    return QColor(*(v // n for v in tot)) if n else QColor(fallback)


def _angle_lerp(a: float, b: float, k: float) -> float:
    d = (b - a + math.pi) % (2 * math.pi) - math.pi
    return a + d * k


def _noise(t: float, k: float) -> float:
    """Cheap smooth noise in -1..1 from incommensurate sines."""
    return (0.5 * math.sin(t * 11.3 + k * 1.7) + 0.3 * math.sin(t * 17.9 + k * 4.1)
            + 0.2 * math.sin(t * 5.3 + k * 2.9))


class CanadianPuppet(Puppet):
    def __init__(self, image: QImage, a, b, max_angle: float = 28.0, chaos: float = 1.0,
                 mouth_color: str = "#1a0505", pivot: str = "random", mouth_fill: bool = False,
                 eyes: dict | None = None, hands: dict | None = None):
        if image.isNull():
            raise ValueError("empty image")
        self.image = image.convertToFormat(QImage.Format_ARGB32_Premultiplied)
        self.w, self.h = self.image.width(), self.image.height()
        pa = QPointF(a[0] * self.w, a[1] * self.h)
        pb = QPointF(b[0] * self.w, b[1] * self.h)
        self.left, self.right = (pa, pb) if pa.x() <= pb.x() else (pb, pa)
        self.max_angle = float(max_angle)
        self.chaos = float(chaos)
        self.mouth_color = QColor(mouth_color)
        self.pivot_mode = pivot if pivot in PIVOTS else "random"
        self.mouth_fill = bool(mouth_fill)
        self.aspect = self.w / max(1, self.h)
        # per-syllable state for the random pivot + hop
        self._side = "left" if self.pivot_mode == "random" else self.pivot_mode
        self._was_open = False
        self._next_flip = 0.0
        self._hop = 0.6
        # optional cartoon eyes (image px)
        self.eyes = None
        if eyes:
            px = lambda f: QPointF(f[0] * self.w, f[1] * self.h)  # noqa: E731
            el, er = px(eyes["left"]), px(eyes["right"])
            if el.x() > er.x():
                el, er = er, el
            rx = float(eyes.get("size", 0.06)) * self.w
            lid = (QColor(eyes["lid"]) if eyes.get("lid") else
                   _sample_color(self.image, (el.x() + er.x()) / 2, max(el.y(), er.y()) + rx * 1.9, "#f1c27d"))
            self.eyes = {"left": el, "right": er, "rx": rx, "lid": lid}
        # optional hands hanging from the shoulders (image px)
        self.hands = None
        if hands:
            hl = QPointF(hands["left"][0] * self.w, hands["left"][1] * self.h)
            hr = QPointF(hands["right"][0] * self.w, hands["right"][1] * self.h)
            if hl.x() > hr.x():
                hl, hr = hr, hl
            self.hands = {"left": hl, "right": hr,
                          "length": float(hands.get("length", 0.16)) * self.h,
                          "sleeve": QColor(hands.get("sleeve", "#3355aa")),
                          "skin": QColor(hands.get("skin", "#e8b48f"))}

        # side margin so a pointing arm stays inside the window
        self.pad = self.hands["length"] * 1.6 if self.hands else 0.0
        self.aspect = (self.w + 2 * self.pad) / max(1, self.h)
        self.face_aspect = self.w / max(1, self.h)  # for the speech-bubble tail

    def _frame(self, rect: QRectF):
        """(origin x, origin y, scale) mapping image px into `rect`."""
        r = fit_rect(rect, self.aspect)
        s = r.height() / self.h
        return r.left() + self.pad * s, r.top(), s

    @property
    def can_pose(self) -> bool:
        return bool(self.eyes or self.hands)

    @classmethod
    def from_folder(cls, folder: Path, meta: dict) -> "CanadianPuppet":
        img = QImage(str(folder / meta.get("image", "image.png")))
        if img.isNull():
            raise ValueError(f"cannot load {folder / meta.get('image', 'image.png')}")
        a = meta.get("a", meta.get("front", [0.3, 0.55]))
        b = meta.get("b", meta.get("hinge", [0.7, 0.55]))
        return cls(img, a, b, meta.get("max_angle", 28), meta.get("chaos", 1.0),
                   meta.get("mouth_color", "#1a0505"), meta.get("pivot", "random"),
                   meta.get("mouth_fill", False), meta.get("eyes"), meta.get("hands"))

    # -- geometry (image pixel coordinates) -------------------------------
    def _line_y(self, x: float) -> float:
        a, b = self.left, self.right
        if abs(b.x() - a.x()) < 1e-3:
            return (a.y() + b.y()) / 2
        return a.y() + (b.y() - a.y()) * (x - a.x()) / (b.x() - a.x())

    def regions(self) -> tuple[QPainterPath, QPainterPath]:
        """(top, bottom): the image split along the extended mouth line."""
        big = max(self.w, self.h) * 2
        y0, y1 = self._line_y(-big), self._line_y(self.w + big)
        top = QPainterPath()
        top.addPolygon(QPolygonF([QPointF(-big, y0), QPointF(self.w + big, y1),
                                  QPointF(self.w + big, -big * 2), QPointF(-big, -big * 2)]))
        top.closeSubpath()
        bottom = QPainterPath()
        bottom.addPolygon(QPolygonF([QPointF(-big, y0), QPointF(self.w + big, y1),
                                     QPointF(self.w + big, self.h + big * 2), QPointF(-big, self.h + big * 2)]))
        bottom.closeSubpath()
        return top, bottom

    def _update_side(self, o: float, t: float) -> None:
        """Random pivot: re-roll on every new syllable, and every ~0.15-0.3 s of
        continuous talking, so the head jerks from one side to the other."""
        is_open = o > 0.08
        if self.pivot_mode != "random":
            self._side = self.pivot_mode
        elif is_open and (not self._was_open or t >= self._next_flip):
            if random.random() < 0.7:  # mostly switch, sometimes repeat: less predictable
                self._side = "right" if self._side == "left" else "left"
            self._next_flip = t + random.uniform(0.15, 0.3)
        if is_open and not self._was_open:
            self._hop = random.uniform(0.3, 1.0)
        self._was_open = is_open

    def head_transform(self, openness: float, t: float, side: str | None = None) -> QTransform:
        """Lift the top of the head around one end of the mouth line (the other end goes up)."""
        o = max(0.0, min(1.0, openness))
        side = side or self._side
        pivot, free = (self.left, self.right) if side == "left" else (self.right, self.left)
        # y is down: rotating counter-clockwise (negative) lifts a point to the right of the pivot
        sign = -1.0 if free.x() > pivot.x() else 1.0
        jitter = 1.0 + self.chaos * 0.35 * _noise(t, 0.0)
        angle = sign * self.max_angle * o * jitter
        angle += self.chaos * o * 4.0 * _noise(t, 3.0)  # extra wobble
        tx = self.chaos * o * 0.012 * self.w * _noise(t, 7.0)
        # the hop: the whole top jumps up a bit, more on some syllables than others
        ty = -self.chaos * o * self.h * (0.012 + 0.03 * self._hop * abs(_noise(t, 11.0)))
        px, py = pivot.x(), pivot.y()
        return QTransform().translate(px + tx, py + ty).rotate(angle).translate(-px, -py)

    def _to_rect(self, rect: QRectF, pt: QPointF) -> QPointF:
        ox, oy, s = self._frame(rect)
        return QPointF(ox + pt.x() * s, oy + pt.y() * s)

    def mouth_anchor(self, rect: QRectF) -> QPointF:
        return self._to_rect(rect, (self.left + self.right) / 2)

    def eye_anchor(self, rect: QRectF) -> QPointF:
        if self.eyes:
            return self._to_rect(rect, (self.eyes["left"] + self.eyes["right"]) / 2)
        return self.mouth_anchor(rect)

    def shoulder_anchor(self, rect: QRectF, side: str) -> QPointF:
        if self.hands:
            return self._to_rect(rect, self.hands[side])
        return self.mouth_anchor(rect)

    def _above_cut(self, pt: QPointF) -> bool:
        return pt.y() < self._line_y(pt.x())

    # -- eyes & hands ------------------------------------------------------
    def _draw_eyes(self, p: QPainter, T: QTransform | None, blink: float, pose) -> None:
        e = self.eyes
        rx = e["rx"]
        ry = rx * 1.15
        look_x = pose.look_x if pose else 0.0
        look_y = pose.look_y if pose else 0.0
        mid_x = (e["left"].x() + e["right"].x()) / 2
        for c in (e["left"], e["right"]):
            p.save()
            if T is not None and self._above_cut(c):
                p.setTransform(T, True)
            eye = QRectF(c.x() - rx, c.y() - ry, 2 * rx, 2 * ry)
            outline = QPen(QColor("#1b1b1b"), max(1.0, rx * 0.09))
            p.setPen(outline)
            p.setBrush(QColor("white"))
            p.drawEllipse(eye)
            # resting pupils lean toward the nose (that cutout cross-eyed look)
            rest_x = c.x() + (0.35 * rx if c.x() < mid_x else -0.35 * rx)
            edge = c.x() + math.copysign(0.68 * rx, look_x)
            px = rest_x + (edge - rest_x) * min(1.0, abs(look_x))
            py = c.y() + 0.1 * ry + look_y * 0.6 * ry
            p.setPen(Qt.NoPen)
            p.setBrush(QColor("#111"))
            p.drawEllipse(QPointF(px, py), rx * 0.24, rx * 0.24)
            if blink > 0.01:
                clip = QPainterPath()
                clip.addEllipse(eye)
                p.setClipPath(clip, Qt.IntersectClip)
                lid_h = eye.height() * blink
                p.setBrush(e["lid"])
                p.drawRect(QRectF(eye.left() - 2, eye.top() - 2, eye.width() + 4, lid_h + 2))
                p.setPen(outline)
                p.drawLine(QPointF(eye.left(), eye.top() + lid_h), QPointF(eye.right(), eye.top() + lid_h))
            p.restore()

    def _draw_hands(self, p: QPainter, pose) -> None:
        hd = self.hands
        L = hd["length"]
        thick = L * 0.42
        pen = QPen(QColor("#1b1b1b"), max(1.0, thick * 0.08))
        pen.setJoinStyle(Qt.RoundJoin)
        for side in ("left", "right"):
            s = hd[side]
            rest = math.radians(102 if side == "left" else 78)  # hanging, slightly outward
            k = 0.0
            if pose is not None and pose.point_side == side and pose.point_amount > 0.001:
                k = pose.point_amount
            ang = _angle_lerp(rest, pose.point_angle, min(k, 1.15)) if k else rest
            length = L * (1.0 + 0.3 * max(0.0, min(k, 1.0)))
            p.save()
            p.translate(s)
            p.rotate(math.degrees(ang))
            p.setPen(pen)
            p.setBrush(hd["sleeve"])
            p.drawRoundedRect(QRectF(-thick * 0.4, -thick / 2, length + thick * 0.4, thick),
                              thick / 2, thick / 2)
            p.setBrush(hd["skin"])
            hand_r = thick * 0.72
            p.drawEllipse(QPointF(length + hand_r * 0.3, 0), hand_r, hand_r * 0.92)
            if k > 0.2:
                reach = thick * 1.1 * min(1.0, (k - 0.2) / 0.5)
                fw = thick * 0.3
                p.drawRoundedRect(QRectF(length + hand_r * 0.6, -fw / 2, reach + hand_r * 0.4, fw), fw / 2, fw / 2)
                p.setPen(Qt.NoPen)
                p.drawEllipse(QPointF(length + hand_r * 0.3, 0), hand_r * 0.8, hand_r * 0.74)
            p.restore()

    def draw(self, p: QPainter, rect: QRectF, openness: float, t: float, blink: float,
             pose=None) -> None:
        ox, oy, s = self._frame(rect)
        o = max(0.0, min(1.0, openness))
        self._update_side(o, t)
        p.save()
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        p.translate(ox, oy + (math.sin(t * 2.2) * 0.003 - o * 0.01) * self.h * s)
        p.scale(s, s)

        if o <= 0.01:
            p.drawImage(0, 0, self.image)  # mouth shut: one piece, no seam
            self._draw_extras(p, None, blink, pose)
            p.restore()
            return
        top, bottom = self.regions()
        # 1. chin + body stay put
        p.save()
        p.setClipPath(bottom)
        p.drawImage(0, 0, self.image)
        p.restore()
        T = self.head_transform(o, t)
        if self.mouth_fill:
            # 2. optional: paint the gap between the cut and the lifted head
            gap = QPolygonF([self.left, self.right, T.map(self.right), T.map(self.left)])
            p.setPen(Qt.NoPen)
            p.setBrush(self.mouth_color)
            p.drawPolygon(gap)
        # 3. the top of the head, flapping
        p.save()
        p.setTransform(T, True)
        p.setClipPath(top)
        p.drawImage(0, 0, self.image)
        p.restore()
        self._draw_extras(p, T, blink, pose)
        p.restore()

    def _draw_extras(self, p: QPainter, T: QTransform | None, blink: float, pose) -> None:
        if self.eyes:
            self._draw_eyes(p, T, blink, pose)
        if self.hands:
            self._draw_hands(p, pose)


def save_cutout(image: QImage, name: str, a, b, max_angle: float, chaos: float,
                mouth_color: str, root: Path | None = None, pivot: str = "random",
                mouth_fill: bool = False, eyes: dict | None = None, hands: dict | None = None) -> Path:
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", name.strip()).strip("-") or "cutout"
    folder = (root or config_dir() / "puppets") / slug
    folder.mkdir(parents=True, exist_ok=True)
    if not image.save(str(folder / "image.png")):
        raise OSError(f"cannot write {folder / 'image.png'}")
    (folder / "puppet.json").write_text(json.dumps({
        "type": "canadian", "image": "image.png",
        "a": [round(a[0], 4), round(a[1], 4)],
        "b": [round(b[0], 4), round(b[1], 4)],
        "pivot": pivot, "max_angle": max_angle, "chaos": chaos,
        "mouth_fill": mouth_fill, "mouth_color": mouth_color,
        "eyes": eyes or None, "hands": hands or None,
    }, indent=2))
    return folder


def guess_cut(image: QImage):
    """Rough first guess for the handles from the opaque silhouette.

    In a head-and-shoulders cutout the mouth sits about 40% of the way
    down; the user drags the handles to the exact spot anyway.
    """
    import numpy as np

    img = image.convertToFormat(QImage.Format_RGBA8888)
    w, h = img.width(), img.height()
    arr = np.frombuffer(img.constBits(), np.uint8, img.sizeInBytes()).reshape(h, img.bytesPerLine())
    alpha = arr[:, : w * 4].reshape(h, w, 4)[..., 3]
    ys, xs = np.nonzero(alpha > 128)
    if len(xs) == 0 or alpha.min() > 128:  # no transparency: assume a centred portrait
        top, bot, left, right = 0.1 * h, h, 0.2 * w, 0.8 * w
    else:
        top, bot, left, right = ys.min(), ys.max(), xs.min(), xs.max()
    y = top + (bot - top) * 0.4
    # head width ≈ silhouette width near the mouth line
    row = np.nonzero(alpha[int(min(h - 1, y))] > 128)[0] if alpha.min() <= 128 else []
    if len(row):
        left, right = row.min(), row.max()
    cx, hw = (left + right) / 2, (right - left) / 2
    return [(cx - 0.8 * hw) / w, y / h], [(cx + 0.8 * hw) / w, y / h]


def remove_background(image: QImage) -> QImage:
    """Cut the person out of a photo with the AI segmenter."""
    import numpy as np

    from .camera import AISegmenter

    rgb = image.convertToFormat(QImage.Format_RGB888)
    w, h = rgb.width(), rgb.height()
    arr = np.frombuffer(rgb.constBits(), np.uint8, rgb.sizeInBytes()).reshape(h, rgb.bytesPerLine())
    arr = np.ascontiguousarray(arr[:, : w * 3].reshape(h, w, 3))
    rgba = AISegmenter().apply(arr)
    out = QImage(rgba.data, w, h, w * 4, QImage.Format_RGBA8888)
    return out.copy()


# ------------------------------------------------------------------ editor UI
HANDLE_STYLE = {  # key: (colour, label)
    "a": ("#e03131", "mouth"), "b": ("#e03131", "mouth"),
    "eye_l": ("#1c7ed6", "eye"), "eye_r": ("#1c7ed6", "eye"),
    "hand_l": ("#2b8a3e", "shoulder"), "hand_r": ("#2b8a3e", "shoulder"),
}


class _CutCanvas(QWidget):
    """The photo with draggable handles: mouth line, eyes, shoulders."""

    HANDLE = 9

    def __init__(self, editor):
        super().__init__()
        self.ed = editor
        self.setMinimumSize(360, 420)
        self._drag = None
        self.setMouseTracking(True)

    def _img_rect(self) -> QRectF:
        img = self.ed.image
        return fit_rect(QRectF(self.rect()).adjusted(8, 8, -8, -8), img.width() / max(1, img.height()))

    def _to_screen(self, frac) -> QPointF:
        r = self._img_rect()
        return QPointF(r.left() + frac[0] * r.width(), r.top() + frac[1] * r.height())

    def _to_frac(self, pt: QPointF):
        r = self._img_rect()
        return [min(1.0, max(0.0, (pt.x() - r.left()) / r.width())),
                min(1.0, max(0.0, (pt.y() - r.top()) / r.height()))]

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        # checkerboard so transparency is visible
        tile = 12
        for y in range(0, self.height(), tile):
            for x in range(0, self.width(), tile):
                p.fillRect(x, y, tile, tile, QColor("#ddd") if (x // tile + y // tile) % 2 else QColor("#f6f6f6"))
        r = self._img_rect()
        p.drawImage(r, self.ed.image)
        f, h = self._to_screen(self.ed.a), self._to_screen(self.ed.b)
        # the extended cut (everything above it flaps)
        d = h - f
        n = math.hypot(d.x(), d.y()) or 1.0
        far = QPointF(d.x() / n, d.y() / n) * 4000
        p.setPen(QPen(QColor(255, 255, 255, 160), 1, Qt.DashLine))
        p.drawLine(f - far, h + far)
        p.setPen(QPen(QColor("#e03131"), 3))
        p.drawLine(f, h)
        handles = self.ed.handles()
        if "eye_l" in handles:
            # eye size preview
            rx = self.ed.eye_size * r.width()
            p.setPen(QPen(QColor("#1c7ed6"), 2, Qt.DashLine))
            p.setBrush(Qt.NoBrush)
            for k in ("eye_l", "eye_r"):
                p.drawEllipse(self._to_screen(handles[k]), rx, rx * 1.15)
        if "hand_l" in handles:
            # arm length preview, hanging down
            L = self.ed.arm_length * r.height()
            p.setPen(QPen(QColor("#2b8a3e"), 4, Qt.DashLine))
            for k, deg in (("hand_l", 102), ("hand_r", 78)):
                s0 = self._to_screen(handles[k])
                p.drawLine(s0, s0 + QPointF(math.cos(math.radians(deg)), math.sin(math.radians(deg))) * L)
        for key, frac in handles.items():
            col, label = HANDLE_STYLE[key]
            pt = self._to_screen(frac)
            p.setPen(QPen(QColor("white"), 2))
            p.setBrush(QColor(col))
            p.drawEllipse(pt, self.HANDLE, self.HANDLE)
            p.setPen(QColor("black"))
            p.drawText(pt + QPointF(12, -10), label)
        p.end()

    def mousePressEvent(self, e):
        pos = e.position()
        handles = self.ed.handles()
        dist = {k: (self._to_screen(f) - pos).manhattanLength() for k, f in handles.items()}
        key = min(dist, key=dist.get)
        if dist[key] < self.HANDLE * 2.5:
            self._drag = key
            return
        # click elsewhere: move the nearest mouth handle there
        self._drag = "a" if dist["a"] <= dist["b"] else "b"
        self.mouseMoveEvent(e)

    def mouseMoveEvent(self, e):
        if self._drag and e.buttons() & Qt.LeftButton:
            self.ed.set_handle(self._drag, self._to_frac(e.position()))

    def mouseReleaseEvent(self, e):
        self._drag = None


class _Preview(QWidget):
    """Live preview. Move the mouse over it to test the eyes; click to test pointing."""

    def __init__(self, editor):
        super().__init__()
        self.ed = editor
        self.setMinimumSize(260, 320)
        self.setMouseTracking(True)
        self.setToolTip("Move the mouse here: the eyes follow. Click: it points.")

    def puppet_rect(self) -> QRectF:
        return QRectF(self.rect()).adjusted(10, 10, -10, -10)

    def mousePressEvent(self, e):
        pup = self.ed.puppet
        if pup is None or not pup.can_pose:
            return
        rect = self.puppet_rect()
        g = lambda pt: (self.mapToGlobal(pt.toPoint()).x(), self.mapToGlobal(pt.toPoint()).y())  # noqa: E731
        target = g(e.position())
        shoulders = {sd: g(pup.shoulder_anchor(rect, sd)) for sd in ("left", "right")}
        self.ed.tracker.click(self.ed.t, target, shoulders, g(pup.eye_anchor(rect)))

    def paintEvent(self, _):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#9fd3ff"))
        if self.ed.puppet is not None:
            self.ed.puppet.draw(p, self.puppet_rect(), self.ed.openness, self.ed.t,
                                self.ed.blink, self.ed.pose)
        p.end()


def _color_btn(text: str, color: str, on_pick, parent) -> QPushButton:
    btn = QPushButton(text)

    def paint(c):
        q = QColor(c)
        fg = "black" if q.lightness() > 128 else "white"
        btn.setStyleSheet(f"background: {q.name()}; color: {fg};")

    def pick():
        c = QColorDialog.getColor(QColor(btn.property("color")), parent, text)
        if c.isValid():
            btn.setProperty("color", c.name())
            paint(c.name())
            on_pick(c.name())

    def set_color(c):
        btn.setProperty("color", c)
        paint(c)

    btn.set_color = set_color
    set_color(color)
    btn.clicked.connect(pick)
    return btn


class CutoutEditor(QDialog):
    """Pick a PNG, drag the cut onto the mouth, add eyes and hands, preview, save."""

    def __init__(self, image: QImage, name: str = "me", ctl=None, parent=None):
        from .pointer import PointerTracker
        from .puppet import Blinker

        super().__init__(parent)
        self.setWindowTitle("New cutout puppet — South Park Canadian style")
        self.ctl = ctl
        self.image = image.convertToFormat(QImage.Format_ARGB32_Premultiplied)
        self.a, self.b = guess_cut(self.image)
        self.max_angle, self.chaos, self.mouth_color = 28.0, 1.0, "#1a0505"
        self.pivot, self.mouth_fill = "random", False
        # eyes & hands (off until ticked; positions guessed from the mouth line)
        self.eyes_on, self.hands_on = False, False
        self._touched: set[str] = set()   # handles the user has dragged
        self._guess_extras()
        self.openness, self.t, self.blink, self.pose = 0.0, 0.0, 0.0, None
        self.tracker, self.blinker = PointerTracker(reach=120), Blinker()
        self.puppet = None
        self.saved_path: Path | None = None

        root = QVBoxLayout(self)
        hint = QLabel("Drag the two <b style='color:#e03131'>red</b> handles onto the mouth line, one on "
                      "each side of the face. Everything above the line flaps open, pivoting on one end "
                      "or the other at random on every syllable. Tick <b>Eyes</b> / <b>Hands</b> to add "
                      "cartoon eyes (<b style='color:#1c7ed6'>blue</b> handles) and arms hanging from the "
                      "shoulders (<b style='color:#2b8a3e'>green</b> handles).")
        hint.setWordWrap(True)
        root.addWidget(hint)
        row = QHBoxLayout()
        self.canvas = _CutCanvas(self)
        self.preview = _Preview(self)
        row.addWidget(self.canvas, 3)
        row.addWidget(self.preview, 2)
        root.addLayout(row, 1)

        form = QFormLayout()
        self.name_edit = QLineEdit(name)
        form.addRow("Name", self.name_edit)
        ang = QSlider(Qt.Horizontal)
        ang.setRange(5, 60)
        ang.setValue(int(self.max_angle))
        ang.valueChanged.connect(lambda v: self._set("max_angle", float(v)))
        form.addRow("Opening angle", ang)
        ch = QSlider(Qt.Horizontal)
        ch.setRange(0, 200)
        ch.setValue(int(self.chaos * 100))
        ch.valueChanged.connect(lambda v: self._set("chaos", v / 100))
        form.addRow("Chaos / hop", ch)
        pv = QComboBox()
        for label, v in (("Random side, every syllable", "random"), ("Always left end", "left"),
                         ("Always right end", "right")):
            pv.addItem(label, v)
        pv.currentIndexChanged.connect(lambda i: self._set("pivot", pv.itemData(i)))
        form.addRow("Pivot", pv)

        # eyes
        eyes_row = QHBoxLayout()
        cb = QCheckBox("Eyes")
        cb.setToolTip("Cartoon eyes over the photo: they blink and follow the mouse")
        cb.toggled.connect(lambda v: self._set("eyes_on", v))
        eyes_row.addWidget(cb)
        eyes_row.addWidget(QLabel("size"))
        es = QSlider(Qt.Horizontal)
        es.setRange(10, 150)
        es.setValue(int(self.eye_size * 1000))
        es.valueChanged.connect(lambda v: self._set("eye_size", v / 1000))
        eyes_row.addWidget(es, 1)
        self.lid_btn = _color_btn("Eyelid colour", self.lid_color, lambda c: self._set("lid_color", c), self)
        eyes_row.addWidget(self.lid_btn)
        form.addRow("", eyes_row)

        # hands
        hands_row = QHBoxLayout()
        cb = QCheckBox("Hands")
        cb.setToolTip("Arms hanging from the shoulders: they point at your clicks")
        cb.toggled.connect(lambda v: self._set("hands_on", v))
        hands_row.addWidget(cb)
        hands_row.addWidget(QLabel("arm length"))
        al = QSlider(Qt.Horizontal)
        al.setRange(40, 400)
        al.setValue(int(self.arm_length * 1000))
        al.valueChanged.connect(lambda v: self._set("arm_length", v / 1000))
        hands_row.addWidget(al, 1)
        self.sleeve_btn = _color_btn("Sleeve", self.sleeve_color, lambda c: self._set("sleeve_color", c), self)
        self.skin_btn = _color_btn("Hand", self.skin_color, lambda c: self._set("skin_color", c), self)
        hands_row.addWidget(self.sleeve_btn)
        hands_row.addWidget(self.skin_btn)
        form.addRow("", hands_row)

        btns = QHBoxLayout()
        fill = QCheckBox("Dark mouth")
        fill.setToolTip("Paint the gap under the lifted head. Off: you see through it.")
        fill.toggled.connect(lambda v: self._set("mouth_fill", v))
        btns.addWidget(fill)
        self.color_btn = QPushButton("Mouth colour")
        self.color_btn.clicked.connect(self._pick_color)
        btns.addWidget(self.color_btn)
        bg = QPushButton("Remove background (AI)")
        bg.setToolTip("For photos without transparency")
        bg.clicked.connect(self._remove_bg)
        btns.addWidget(bg)
        self.simulate = QCheckBox("Simulate talking")
        self.simulate.setChecked(True)
        btns.addWidget(self.simulate)
        form.addRow("", btns)
        root.addLayout(form)

        box = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        box.accepted.connect(self._save)
        box.rejected.connect(self.reject)
        root.addWidget(box)
        self.resize(880, 760)

        self.changed()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(16)

    # -- model ------------------------------------------------------------
    def _guess_extras(self):
        """Place eyes above the mouth and shoulders below/outside it, sized to the face."""
        ax, ay = self.a[0] * self.image.width(), self.a[1] * self.image.height()
        bx, by = self.b[0] * self.image.width(), self.b[1] * self.image.height()
        w, h = self.image.width(), self.image.height()
        mx, my = (ax + bx) / 2, (ay + by) / 2
        half = max(10.0, abs(bx - ax) / 2)
        clamp = lambda v: min(0.97, max(0.03, v))  # noqa: E731
        self.eye_l = [clamp((mx - 0.6 * half) / w), clamp((my - 1.6 * half) / h)]
        self.eye_r = [clamp((mx + 0.6 * half) / w), clamp((my - 1.6 * half) / h)]
        self.eye_size = min(0.15, max(0.01, 0.38 * half / w))
        self.hand_l = [clamp((mx - 1.5 * half) / w), clamp((my + 1.4 * half) / h)]
        self.hand_r = [clamp((mx + 1.5 * half) / w), clamp((my + 1.4 * half) / h)]
        self.arm_length = min(0.4, max(0.04, 1.1 * half / h))
        self.lid_color = _sample_color(self.image, mx, my - 0.5 * half, "#f1c27d").name()
        self.skin_color = self.lid_color
        self.sleeve_color = _sample_color(self.image, mx, min(h - 3, my + 1.8 * half), "#3355aa").name()

    def handles(self) -> dict:
        hs = {"a": self.a, "b": self.b}
        if self.eyes_on:
            hs.update(eye_l=self.eye_l, eye_r=self.eye_r)
        if self.hands_on:
            hs.update(hand_l=self.hand_l, hand_r=self.hand_r)
        return hs

    def set_handle(self, key, frac):
        setattr(self, key, frac)
        self._touched.add(key)
        self.changed()

    def eyes_spec(self):
        if not self.eyes_on:
            return None
        return {"left": [round(v, 4) for v in self.eye_l], "right": [round(v, 4) for v in self.eye_r],
                "size": round(self.eye_size, 4), "lid": self.lid_color}

    def hands_spec(self):
        if not self.hands_on:
            return None
        return {"left": [round(v, 4) for v in self.hand_l], "right": [round(v, 4) for v in self.hand_r],
                "length": round(self.arm_length, 4), "sleeve": self.sleeve_color, "skin": self.skin_color}

    def _set(self, key, value):
        if value and key in ("eyes_on", "hands_on"):
            # first time on: fit them to the mouth line as it is now
            keys = ("eye_l", "eye_r") if key == "eyes_on" else ("hand_l", "hand_r")
            if not self._touched.intersection(keys):
                saved = {k: getattr(self, k) for k in ("eye_l", "eye_r", "eye_size", "hand_l", "hand_r",
                                                       "arm_length", "lid_color", "skin_color", "sleeve_color")}
                self._guess_extras()
                keep = ("hand_l", "hand_r", "arm_length", "sleeve_color") if key == "eyes_on" else \
                       ("eye_l", "eye_r", "eye_size", "lid_color")
                for k in keep:
                    setattr(self, k, saved[k])
                self.lid_btn.set_color(self.lid_color)
                self.skin_btn.set_color(self.skin_color)
                self.sleeve_btn.set_color(self.sleeve_color)
        setattr(self, key, value)
        self.changed()

    def changed(self):
        self.puppet = CanadianPuppet(self.image, self.a, self.b, self.max_angle, self.chaos,
                                     self.mouth_color, self.pivot, self.mouth_fill,
                                     self.eyes_spec(), self.hands_spec())
        self.canvas.update()

    def _tick(self):
        from PySide6.QtGui import QCursor

        dt = 0.016
        self.t += dt
        if self.simulate.isChecked() or self.ctl is None:
            # fake syllables: bursts of flapping with short pauses
            burst = max(0.0, math.sin(self.t * 1.3))
            self.openness = burst * max(0.0, math.sin(self.t * 13.0)) ** 0.6
        else:
            self.openness = self.ctl.frame.openness
        self.blink = self.blinker.update(self.t)
        if self.puppet is not None and self.puppet.can_pose:
            rect = self.preview.puppet_rect()
            eyes = self.preview.mapToGlobal(self.puppet.eye_anchor(rect).toPoint())
            c = QCursor.pos()
            self.pose = self.tracker.update(self.t, dt, (c.x(), c.y()), (eyes.x(), eyes.y()))
        else:
            self.pose = None
        self.preview.update()

    def _pick_color(self):
        c = QColorDialog.getColor(QColor(self.mouth_color), self, "Mouth colour")
        if c.isValid():
            self._set("mouth_color", c.name())

    def _remove_bg(self):
        try:
            self.image = remove_background(self.image).convertToFormat(QImage.Format_ARGB32_Premultiplied)
        except Exception as e:
            QMessageBox.warning(self, "Background removal", f"Couldn't remove the background:\n{e}\n\n"
                                "Install it with: pip install mediapipe")
            return
        self.a, self.b = guess_cut(self.image)
        self._guess_extras()
        self.changed()

    def _save(self):
        try:
            self.saved_path = save_cutout(self.image, self.name_edit.text(), self.a, self.b,
                                          self.max_angle, self.chaos, self.mouth_color,
                                          pivot=self.pivot, mouth_fill=self.mouth_fill,
                                          eyes=self.eyes_spec(), hands=self.hands_spec())
        except OSError as e:
            QMessageBox.warning(self, "Save", str(e))
            return
        self.accept()


def open_editor(parent=None, ctl=None, path: str | None = None) -> Path | None:
    """File dialog → editor. Returns the saved puppet folder, or None."""
    if not path:
        path, _ = QFileDialog.getOpenFileName(parent, "Choose a PNG cutout (or a photo)", "",
                                              "Images (*.png *.webp *.jpg *.jpeg)")
        if not path:
            return None
    img = QImage(path)
    if img.isNull():
        QMessageBox.warning(parent, "Cutout", f"Can't open {path}")
        return None
    ed = CutoutEditor(img, Path(path).stem, ctl, parent)
    ed.exec()
    return ed.saved_path


def user_puppets() -> list[Path]:
    """Puppet folders saved under the config dir."""
    root = config_dir() / "puppets"
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir() if (p / "puppet.json").is_file())
