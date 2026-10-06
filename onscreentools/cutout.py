"""Photo cutout puppets, Canadian style.

Take a PNG of a person, cut it along the mouth line, and the whole top of
the head flaps open around a hinge at the back of the skull while you
talk — bouncing around chaotically like Terrance & Phillip.

puppet.json:

    {"type": "canadian",
     "image": "image.png",
     "front": [0.42, 0.55],   # mouth corner (front end of the cut), fractions of the image
     "hinge": [0.70, 0.50],   # back of the head: the top pivots around this point
     "max_angle": 28,         # degrees the head opens at full volume
     "chaos": 1.0,            # 0 = clean flap, 2 = maniac
     "mouth_color": "#1a0505"}
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath, QPen, QPolygonF, QTransform
from PySide6.QtWidgets import (QCheckBox, QColorDialog, QDialog, QDialogButtonBox, QFileDialog,
                               QFormLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
                               QPushButton, QSlider, QVBoxLayout, QWidget)

from .config import config_dir
from .puppet import Puppet, fit_rect


def _noise(t: float, k: float) -> float:
    """Cheap smooth noise in -1..1 from incommensurate sines."""
    return (0.5 * math.sin(t * 11.3 + k * 1.7) + 0.3 * math.sin(t * 17.9 + k * 4.1)
            + 0.2 * math.sin(t * 5.3 + k * 2.9))


class CanadianPuppet(Puppet):
    def __init__(self, image: QImage, front, hinge, max_angle: float = 28.0, chaos: float = 1.0,
                 mouth_color: str = "#1a0505"):
        if image.isNull():
            raise ValueError("empty image")
        self.image = image.convertToFormat(QImage.Format_ARGB32_Premultiplied)
        self.w, self.h = self.image.width(), self.image.height()
        self.front = QPointF(front[0] * self.w, front[1] * self.h)
        self.hinge = QPointF(hinge[0] * self.w, hinge[1] * self.h)
        self.max_angle = float(max_angle)
        self.chaos = float(chaos)
        self.mouth_color = QColor(mouth_color)
        self.aspect = self.w / max(1, self.h)

    @classmethod
    def from_folder(cls, folder: Path, meta: dict) -> "CanadianPuppet":
        img = QImage(str(folder / meta.get("image", "image.png")))
        if img.isNull():
            raise ValueError(f"cannot load {folder / meta.get('image', 'image.png')}")
        return cls(img, meta.get("front", [0.4, 0.55]), meta.get("hinge", [0.7, 0.5]),
                   meta.get("max_angle", 28), meta.get("chaos", 1.0), meta.get("mouth_color", "#1a0505"))

    # -- geometry (image pixel coordinates) -------------------------------
    def _line_y(self, x: float) -> float:
        f, h = self.front, self.hinge
        if abs(h.x() - f.x()) < 1e-3:
            return (f.y() + h.y()) / 2
        return f.y() + (h.y() - f.y()) * (x - f.x()) / (h.x() - f.x())

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

    def head_transform(self, openness: float, t: float) -> QTransform:
        o = max(0.0, min(1.0, openness))
        # the front of the head must go *up*: in y-down coords that's
        # counter-clockwise when the face looks left, clockwise when it looks right
        sign = -1.0 if self.front.x() > self.hinge.x() else 1.0
        jitter = 1.0 + self.chaos * 0.35 * _noise(t, 0.0)
        angle = sign * self.max_angle * o * jitter
        angle += self.chaos * o * 4.0 * _noise(t, 3.0)  # extra wobble
        tx = self.chaos * o * 0.012 * self.w * _noise(t, 7.0)
        ty = -self.chaos * o * 0.02 * self.h * abs(_noise(t, 11.0))
        hx, hy = self.hinge.x(), self.hinge.y()
        return QTransform().translate(hx + tx, hy + ty).rotate(angle).translate(-hx, -hy)

    def mouth_anchor(self, rect: QRectF) -> QPointF:
        r = fit_rect(rect, self.aspect)
        s = r.width() / self.w
        return QPointF(r.left() + self.front.x() * s, r.top() + self.front.y() * s)

    def draw(self, p: QPainter, rect: QRectF, openness: float, t: float, blink: float) -> None:
        r = fit_rect(rect, self.aspect)
        s = r.width() / self.w
        o = max(0.0, min(1.0, openness))
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
        # 2. the dark gap between the cut and the lifted head
        T = self.head_transform(o, t)
        gap = QPolygonF([self.front, self.hinge, T.map(self.hinge), T.map(self.front)])
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


def save_cutout(image: QImage, name: str, front, hinge, max_angle: float, chaos: float,
                mouth_color: str, root: Path | None = None) -> Path:
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", name.strip()).strip("-") or "cutout"
    folder = (root or config_dir() / "puppets") / slug
    folder.mkdir(parents=True, exist_ok=True)
    if not image.save(str(folder / "image.png")):
        raise OSError(f"cannot write {folder / 'image.png'}")
    (folder / "puppet.json").write_text(json.dumps({
        "type": "canadian", "image": "image.png",
        "front": [round(front[0], 4), round(front[1], 4)],
        "hinge": [round(hinge[0], 4), round(hinge[1], 4)],
        "max_angle": max_angle, "chaos": chaos, "mouth_color": mouth_color,
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
    return [(cx - 0.35 * hw) / w, y / h], [(cx + 0.85 * hw) / w, (y - 0.03 * (bot - top)) / h]


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
    """The photo with two draggable handles: mouth corner and head hinge."""

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
        f, h = self._to_screen(self.ed.front), self._to_screen(self.ed.hinge)
        # the extended cut (everything above it flaps)
        d = h - f
        n = math.hypot(d.x(), d.y()) or 1.0
        far = QPointF(d.x() / n, d.y() / n) * 4000
        p.setPen(QPen(QColor(255, 255, 255, 160), 1, Qt.DashLine))
        p.drawLine(f - far, h + far)
        p.setPen(QPen(QColor("#e03131"), 3))
        p.drawLine(f, h)
        for pt, col, label in ((f, "#e03131", "mouth"), (h, "#1c7ed6", "hinge")):
            p.setPen(QPen(QColor("white"), 2))
            p.setBrush(QColor(col))
            p.drawEllipse(pt, self.HANDLE, self.HANDLE)
            p.setPen(QColor("black"))
            p.drawText(pt + QPointF(12, -10), label)
        p.end()

    def mousePressEvent(self, e):
        pos = e.position()
        for key in ("front", "hinge"):
            if (self._to_screen(getattr(self.ed, key)) - pos).manhattanLength() < self.HANDLE * 2.5:
                self._drag = key
                return
        # click elsewhere: move the nearest handle there
        df = (self._to_screen(self.ed.front) - pos).manhattanLength()
        dh = (self._to_screen(self.ed.hinge) - pos).manhattanLength()
        self._drag = "front" if df <= dh else "hinge"
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
        self.front, self.hinge = guess_cut(self.image)
        self.max_angle, self.chaos, self.mouth_color = 28.0, 1.0, "#1a0505"
        self.openness, self.t = 0.0, 0.0
        self.puppet = None
        self.saved_path: Path | None = None

        root = QVBoxLayout(self)
        hint = QLabel("Drag the <b style='color:#e03131'>red</b> handle onto the corner of the mouth "
                      "and the <b style='color:#1c7ed6'>blue</b> one to the back of the head, at the "
                      "same height (around the ear). Everything above the line flaps open.")
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
        form.addRow("Chaos", ch)
        btns = QHBoxLayout()
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
        self.puppet = CanadianPuppet(self.image, self.front, self.hinge, self.max_angle,
                                     self.chaos, self.mouth_color)
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
        self.front, self.hinge = guess_cut(self.image)
        self.changed()

    def _save(self):
        try:
            self.saved_path = save_cutout(self.image, self.name_edit.text(), self.front, self.hinge,
                                          self.max_angle, self.chaos, self.mouth_color)
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
