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
     "mouth_color": "#1a0505"}

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


def _noise(t: float, k: float) -> float:
    """Cheap smooth noise in -1..1 from incommensurate sines."""
    return (0.5 * math.sin(t * 11.3 + k * 1.7) + 0.3 * math.sin(t * 17.9 + k * 4.1)
            + 0.2 * math.sin(t * 5.3 + k * 2.9))


class CanadianPuppet(Puppet):
    def __init__(self, image: QImage, a, b, max_angle: float = 28.0, chaos: float = 1.0,
                 mouth_color: str = "#1a0505", pivot: str = "random", mouth_fill: bool = False):
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

    @classmethod
    def from_folder(cls, folder: Path, meta: dict) -> "CanadianPuppet":
        img = QImage(str(folder / meta.get("image", "image.png")))
        if img.isNull():
            raise ValueError(f"cannot load {folder / meta.get('image', 'image.png')}")
        a = meta.get("a", meta.get("front", [0.3, 0.55]))
        b = meta.get("b", meta.get("hinge", [0.7, 0.55]))
        return cls(img, a, b, meta.get("max_angle", 28), meta.get("chaos", 1.0),
                   meta.get("mouth_color", "#1a0505"), meta.get("pivot", "random"),
                   meta.get("mouth_fill", False))

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

    def mouth_anchor(self, rect: QRectF) -> QPointF:
        r = fit_rect(rect, self.aspect)
        s = r.width() / self.w
        m = (self.left + self.right) / 2
        return QPointF(r.left() + m.x() * s, r.top() + m.y() * s)

    def draw(self, p: QPainter, rect: QRectF, openness: float, t: float, blink: float,
             pose=None) -> None:
        r = fit_rect(rect, self.aspect)
        s = r.width() / self.w
        o = max(0.0, min(1.0, openness))
        self._update_side(o, t)
        p.save()
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        p.translate(r.left(), r.top() + (math.sin(t * 2.2) * 0.003 - o * 0.01) * r.height())
        p.scale(s, s)

        if o <= 0.01:
            p.drawImage(0, 0, self.image)  # mouth shut: one piece, no seam
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
        p.restore()


def save_cutout(image: QImage, name: str, a, b, max_angle: float, chaos: float,
                mouth_color: str, root: Path | None = None, pivot: str = "random",
                mouth_fill: bool = False) -> Path:
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
class _CutCanvas(QWidget):
    """The photo with two draggable handles: the two ends of the mouth line."""

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
        p.drawImage(self._img_rect(), self.ed.image)
        f, h = self._to_screen(self.ed.a), self._to_screen(self.ed.b)
        # the extended cut (everything above it flaps)
        d = h - f
        n = math.hypot(d.x(), d.y()) or 1.0
        far = QPointF(d.x() / n, d.y() / n) * 4000
        p.setPen(QPen(QColor(255, 255, 255, 160), 1, Qt.DashLine))
        p.drawLine(f - far, h + far)
        p.setPen(QPen(QColor("#e03131"), 3))
        p.drawLine(f, h)
        for pt, col, label in ((f, "#e03131", "A"), (h, "#e03131", "B")):
            p.setPen(QPen(QColor("white"), 2))
            p.setBrush(QColor(col))
            p.drawEllipse(pt, self.HANDLE, self.HANDLE)
            p.setPen(QColor("black"))
            p.drawText(pt + QPointF(12, -10), label)
        p.end()

    def mousePressEvent(self, e):
        pos = e.position()
        for key in ("a", "b"):
            if (self._to_screen(getattr(self.ed, key)) - pos).manhattanLength() < self.HANDLE * 2.5:
                self._drag = key
                return
        # click elsewhere: move the nearest handle there
        da = (self._to_screen(self.ed.a) - pos).manhattanLength()
        db = (self._to_screen(self.ed.b) - pos).manhattanLength()
        self._drag = "a" if da <= db else "b"
        self.mouseMoveEvent(e)

    def mouseMoveEvent(self, e):
        if self._drag and e.buttons() & Qt.LeftButton:
            setattr(self.ed, self._drag, self._to_frac(e.position()))
            self.ed.changed()

    def mouseReleaseEvent(self, e):
        self._drag = None


class _Preview(QWidget):
    def __init__(self, editor):
        super().__init__()
        self.ed = editor
        self.setMinimumSize(260, 320)

    def paintEvent(self, _):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#9fd3ff"))
        if self.ed.puppet is not None:
            self.ed.puppet.draw(p, QRectF(self.rect()).adjusted(10, 10, -10, -10),
                                self.ed.openness, self.ed.t, 0.0)
        p.end()


class CutoutEditor(QDialog):
    """Pick a PNG, drag the cut onto the mouth, preview, save as a puppet."""

    def __init__(self, image: QImage, name: str = "me", ctl=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("New cutout puppet — South Park Canadian style")
        self.ctl = ctl
        self.image = image.convertToFormat(QImage.Format_ARGB32_Premultiplied)
        self.a, self.b = guess_cut(self.image)
        self.max_angle, self.chaos, self.mouth_color = 28.0, 1.0, "#1a0505"
        self.pivot, self.mouth_fill = "random", False
        self.openness, self.t = 0.0, 0.0
        self.puppet = None
        self.saved_path: Path | None = None

        root = QVBoxLayout(self)
        hint = QLabel("Drag the two <b style='color:#e03131'>red</b> handles onto the mouth line, one on "
                      "each side of the face. Everything above the line flaps open, pivoting on one end "
                      "or the other at random on every syllable.")
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
        self.resize(820, 680)

        self.changed()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(16)

    def _set(self, key, value):
        setattr(self, key, value)
        self.changed()

    def changed(self):
        self.puppet = CanadianPuppet(self.image, self.a, self.b, self.max_angle, self.chaos,
                                     self.mouth_color, self.pivot, self.mouth_fill)
        self.canvas.update()

    def _tick(self):
        self.t += 0.016
        if self.simulate.isChecked() or self.ctl is None:
            # fake syllables: bursts of flapping with short pauses
            burst = max(0.0, math.sin(self.t * 1.3))
            self.openness = burst * max(0.0, math.sin(self.t * 13.0)) ** 0.6
        else:
            self.openness = self.ctl.frame.openness
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
        self.changed()

    def _save(self):
        try:
            self.saved_path = save_cutout(self.image, self.name_edit.text(), self.a, self.b,
                                          self.max_angle, self.chaos, self.mouth_color,
                                          pivot=self.pivot, mouth_fill=self.mouth_fill)
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
