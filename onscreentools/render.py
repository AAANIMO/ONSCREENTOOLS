"""Painting: subtitles (bottom bar or comic bubble) and the whole mascot scene."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (QColor, QFont, QFontDatabase, QFontMetricsF, QImage, QPainter,
                           QPainterPath, QPen)

COMIC_FONTS = ["Comic Neue", "Bangers", "Chalkboard SE", "Comic Sans MS", "Comic Relief",
               "Marker Felt", "Patrick Hand", "Noto Sans", "DejaVu Sans"]


_FONTS_LOADED = False


def _load_bundled_fonts():
    global _FONTS_LOADED
    if _FONTS_LOADED:
        return
    _FONTS_LOADED = True
    for ttf in sorted((Path(__file__).parent / "fonts").glob("*.ttf")):
        QFontDatabase.addApplicationFont(str(ttf))


def pick_font(family: str, size: int, bold: bool = True) -> QFont:
    _load_bundled_fonts()
    fam = family
    if not fam:
        available = set(QFontDatabase.families())
        fam = next((f for f in COMIC_FONTS if f in available), "")
    font = QFont(fam) if fam else QFont()
    font.setPixelSize(max(6, int(size)))
    font.setBold(bold)
    return font


def wrap(text: str, fm: QFontMetricsF, max_width: float) -> list[str]:
    lines, cur = [], ""
    for word in text.split():
        trial = f"{cur} {word}".strip()
        if fm.horizontalAdvance(trial) <= max_width or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines


# --------------------------------------------------------------------------- bottom bar
def draw_bottom_subtitles(p: QPainter, area: QRectF, text: str, font: QFont,
                          opacity: float = 1.0, max_lines: int = 2) -> None:
    if not text or opacity <= 0:
        return
    fm = QFontMetricsF(font)
    lines = wrap(text, fm, area.width() * 0.92)[-max_lines:]
    lh = fm.height() * 1.15
    pad = fm.height() * 0.35
    total_h = lh * len(lines)
    p.save()
    p.setOpacity(opacity)
    p.setRenderHint(QPainter.Antialiasing, True)
    y = area.bottom() - total_h - pad
    for line in lines:
        w = fm.horizontalAdvance(line)
        x = area.center().x() - w / 2
        box = QRectF(x - pad, y, w + 2 * pad, lh)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 170))
        p.drawRoundedRect(box, 6, 6)
        path = QPainterPath()
        path.addText(QPointF(x, y + (lh - fm.height()) / 2 + fm.ascent()), font, line)
        p.setPen(QPen(QColor(0, 0, 0), max(2.0, font.pixelSize() / 8), Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.setBrush(Qt.NoBrush)
        p.drawPath(path)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(255, 255, 255))
        p.drawPath(path)
        y += lh
    p.restore()


# --------------------------------------------------------------------------- comic bubble
def bubble_layout(text: str, font: QFont, max_width: float, max_lines: int = 5):
    fm = QFontMetricsF(font)
    lines = wrap(text, fm, max_width)
    if len(lines) > max_lines:
        lines = ["…" + lines[-max_lines].lstrip()] + lines[-max_lines + 1:]
    w = max((fm.horizontalAdvance(l) for l in lines), default=0)
    h = fm.height() * 1.1 * len(lines)
    return lines, w, h


def draw_bubble(p: QPainter, text: str, font: QFont, anchor: QRectF, tail_tip: QPointF,
                side: str = "right", opacity: float = 1.0, max_width: float = 320) -> QRectF | None:
    """Draw a speech balloon whose bottom corner sits near `anchor`'s top on `side`.

    `anchor` is the area the bubble may occupy (it's placed at that area's
    bottom-left/right and grows upward). Returns the bubble rect.
    """
    if not text or opacity <= 0:
        return None
    fm = QFontMetricsF(font)
    lines, tw, th = bubble_layout(text, font, max_width)
    # an ellipse needs ~sqrt(2) the room of the text box; a bit less looks tighter
    bw = min(tw * 1.32 + fm.height() * 1.6, anchor.width())
    bh = th * 1.38 + fm.height() * 1.1
    x = anchor.left() if side == "right" else anchor.right() - bw
    y = max(anchor.top(), anchor.bottom() - bh)
    rect = QRectF(x, y, bw, bh)

    body = QPainterPath()
    body.addEllipse(rect)
    # tail: a curved wedge from inside the balloon toward the speaker's mouth
    c = rect.center()
    base = QPointF(c.x() + (-0.22 if side == "right" else 0.22) * bw, c.y() + 0.3 * bh)
    half = min(bw * 0.07, fm.height() * 0.7)
    mid = QPointF((base.x() + tail_tip.x()) / 2, (base.y() + tail_tip.y()) / 2)
    bend = QPointF((6 if side == "right" else -6), -6)
    tail = QPainterPath()
    tail.moveTo(base.x() - half, base.y())
    tail.quadTo(mid + bend, tail_tip)
    tail.quadTo(mid + bend + QPointF(half * 0.8, 0), QPointF(base.x() + half, base.y()))
    tail.closeSubpath()
    shape = body.united(tail)
    pady = (bh - th) / 2

    p.save()
    p.setOpacity(opacity)
    p.setRenderHint(QPainter.Antialiasing, True)
    # drop shadow, comic style
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(0, 0, 0, 60))
    p.drawPath(shape.translated(4, 5))
    p.setBrush(QColor(255, 255, 255))
    p.setPen(QPen(QColor(20, 20, 20), max(2.5, font.pixelSize() / 7), Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    p.drawPath(shape)
    p.setPen(QColor(20, 20, 20))
    p.setFont(font)
    lh = fm.height() * 1.1
    ty = rect.top() + pady
    for line in lines:
        lw = fm.horizontalAdvance(line)
        p.drawText(QPointF(rect.center().x() - lw / 2, ty + (lh - fm.height()) / 2 + fm.ascent()), line)
        ty += lh
    p.restore()
    return rect


# --------------------------------------------------------------------------- camera
def rgba_to_qimage(rgba) -> QImage:
    h, w = rgba.shape[:2]
    img = QImage(rgba.data, w, h, w * 4, QImage.Format_RGBA8888)
    return img.copy()  # detach from the numpy buffer


def draw_camera(p: QPainter, rect: QRectF, img: QImage, shape: str = "full",
                ring: QColor | None = None) -> QRectF:
    """Draw the camera frame fitted (bottom-centred) into rect, optionally clipped."""
    if img is None or img.isNull():
        return rect
    aspect = img.width() / max(1, img.height())
    if shape in ("circle", "rounded"):
        aspect = 1.0
    w, h = rect.width(), rect.height()
    if w / h > aspect:
        w = h * aspect
    else:
        h = w / aspect
    target = QRectF(rect.center().x() - w / 2, rect.bottom() - h, w, h)
    p.save()
    p.setRenderHint(QPainter.Antialiasing, True)
    p.setRenderHint(QPainter.SmoothPixmapTransform, True)
    src = QRectF(0, 0, img.width(), img.height())
    if shape in ("circle", "rounded"):
        s = min(img.width(), img.height())
        src = QRectF((img.width() - s) / 2, (img.height() - s) / 2, s, s)
        clip = QPainterPath()
        if shape == "circle":
            clip.addEllipse(target)
        else:
            clip.addRoundedRect(target, w * 0.12, w * 0.12)
        p.setClipPath(clip)
        p.drawImage(target, img, src)
        p.setClipping(False)
        if ring is not None:
            p.setPen(QPen(ring, max(3.0, w / 60)))
            p.setBrush(Qt.NoBrush)
            p.drawPath(clip)
    else:
        p.drawImage(target, img, src)
    p.restore()
    return target
