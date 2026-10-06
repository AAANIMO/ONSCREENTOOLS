"""On-screen windows: the floating mascot and the bottom subtitle bar."""
from __future__ import annotations

import sys
from dataclasses import dataclass

from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QRegion
from PySide6.QtWidgets import QMenu, QWidget

from .render import draw_bottom_subtitles, draw_bubble, draw_camera


@dataclass
class Frame:
    """Everything needed to paint one frame. Filled in by the controller."""
    openness: float = 0.0
    t: float = 0.0
    blink: float = 0.0
    camera: QImage | None = None
    text: str = ""
    text_opacity: float = 0.0
    status: str = ""
    pose: object = None     # pointer.Pose: gaze + pointing arm


def scene_layout(cfg, mascot_aspect: float, bubble: bool):
    """Window size, mascot rect and bubble area (window coordinates)."""
    m = int(cfg.mascot_size)
    mw = int(m * mascot_aspect)
    if not bubble:
        return QSize(mw, m), QRectF(0, 0, mw, m), None
    bubble_w = int(max(240, m * 1.15, cfg.font_size * 14))
    bubble_h = int(max(m * 0.62, cfg.font_size * 7))
    overlap = int(mw * 0.35)
    w = mw + bubble_w - overlap
    h = m + int(bubble_h * 0.6)
    if cfg.bubble_side == "right":
        mascot = QRectF(0, h - m, mw, m)
        area = QRectF(mw - overlap, 4, bubble_w - 10, bubble_h)
    else:
        mascot = QRectF(w - mw, h - m, mw, m)
        area = QRectF(4, 4, bubble_w - 10, bubble_h)
    return QSize(w, h), mascot, area


def paint_scene(p: QPainter, ctl, frame: Frame, mascot: QRectF, bubble_area: QRectF | None,
                bottom_area: QRectF | None = None) -> QRectF | None:
    """Paint mascot + subtitles. Shared by the window and the virtual camera."""
    cfg = ctl.cfg
    tail_tip = None
    if cfg.mascot == "camera":
        target = draw_camera(p, mascot, frame.camera, cfg.camera_shape,
                             ring=QColor(20, 20, 20) if cfg.camera_shape != "full" else None)
        if frame.camera is None and frame.status:
            p.setPen(QColor(255, 255, 255))
            p.drawText(mascot, Qt.AlignCenter | Qt.TextWordWrap, frame.status)
        tail_tip = QPointF(target.center().x() + (target.width() * (0.2 if cfg.bubble_side == "right" else -0.2)),
                           target.top() + target.height() * 0.35)
    else:
        ctl.puppet.draw(p, mascot, frame.openness, frame.t, frame.blink, frame.pose)
        # stop the tail just outside the cheek, at mouth height
        mouth = ctl.puppet.mouth_anchor(mascot)
        face_w = min(mascot.width(), mascot.height() * (ctl.puppet.face_aspect or ctl.puppet.aspect))
        dx = face_w * (0.44 if cfg.bubble_side == "right" else -0.44)
        tail_tip = QPointF(mouth.x() + dx, mouth.y() - mascot.height() * 0.06)

    bubble_rect = None
    font = ctl.font
    text = frame.text.upper() if cfg.uppercase else frame.text
    if bubble_area is not None and cfg.subtitles == "bubble":
        bubble_rect = draw_bubble(p, text, font, bubble_area, tail_tip, cfg.bubble_side,
                                  frame.text_opacity, max_width=bubble_area.width() * 0.8)
    if bottom_area is not None and cfg.subtitles == "bottom":
        draw_bottom_subtitles(p, bottom_area, frame.text, ctl.bottom_font, frame.text_opacity)
    return bubble_rect


class MascotWindow(QWidget):
    def __init__(self, ctl):
        super().__init__(None)
        self.ctl = ctl
        self.setWindowTitle("onscreen mascot")
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.apply_flags()
        self._mascot = QRectF()
        self._bubble_area = None
        self._mask_key = None
        self._drag_offset: QPoint | None = None

    # -- setup ------------------------------------------------------------
    def apply_flags(self):
        flags = Qt.Window | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
        if sys.platform.startswith("linux"):
            flags |= Qt.Tool  # keep it out of the taskbar (on macOS Tool windows hide when unfocused)
        if self.ctl.cfg.click_through:
            flags |= Qt.WindowTransparentForInput | Qt.WindowDoesNotAcceptFocus
        visible = self.isVisible()
        self.setWindowFlags(flags)
        if visible:
            self.show()

    def relayout(self):
        cfg = self.ctl.cfg
        size, self._mascot, self._bubble_area = scene_layout(
            cfg, self.ctl.mascot_aspect(), cfg.subtitles == "bubble")
        if size == self.size():
            return
        # keep the mascot's feet where they are on screen
        if self.isVisible() and not self._mascot.isNull():
            old_anchor = self.mapToGlobal(QPoint(0, self.height()))
            if cfg.bubble_side == "left":
                old_anchor = self.mapToGlobal(QPoint(self.width(), self.height()))
                self.setGeometry(QRect(old_anchor.x() - size.width(), old_anchor.y() - size.height(),
                                       size.width(), size.height()))
            else:
                self.setGeometry(QRect(old_anchor.x(), old_anchor.y() - size.height(),
                                       size.width(), size.height()))
        else:
            self.resize(size)
        self._mask_key = None

    def place_default(self):
        screen = self.screen().availableGeometry() if self.screen() else QRect(0, 0, 1920, 1080)
        pos = self.ctl.cfg.window_pos
        if pos and screen.adjusted(-200, -200, 200, 200).contains(QPoint(*pos)):
            self.move(*pos)
        else:
            self.move(screen.right() - self.width() - 40, screen.bottom() - self.height() - 40)

    # -- painting ---------------------------------------------------------
    def paintEvent(self, _):
        cfg = self.ctl.cfg
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        if cfg.window_bg and cfg.window_bg != "transparent":
            p.fillRect(self.rect(), QColor(cfg.window_bg))
        else:
            p.setCompositionMode(QPainter.CompositionMode_Source)
            p.fillRect(self.rect(), Qt.transparent)
            p.setCompositionMode(QPainter.CompositionMode_SourceOver)
        bubble = paint_scene(p, self.ctl, self.ctl.frame, self._mascot, self._bubble_area)
        p.end()
        self._update_input_mask(bubble)

    def _update_input_mask(self, bubble: QRectF | None):
        """On X11 transparent pixels still eat clicks, so restrict input to what's drawn."""
        if sys.platform == "darwin":
            return
        if self.ctl.cfg.window_bg not in ("", "transparent", None):
            if self._mask_key is not None:
                self.clearMask()
                self._mask_key = None
            return
        rects = [self._mascot.toAlignedRect()]
        if bubble is not None:
            rects.append(bubble.adjusted(-4, -4, 8, 8).toAlignedRect())
        key = tuple((r.x(), r.y(), r.width(), r.height()) for r in rects)
        if key == self._mask_key:
            return
        region = QRegion()
        for r in rects:
            region = region.united(QRegion(r))
        self.setMask(region)
        self._mask_key = key

    # -- interaction ------------------------------------------------------
    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            handle = self.windowHandle()
            if handle is not None and handle.startSystemMove():
                return  # the window manager drives the move (works on Wayland too)
            self._drag_offset = e.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, e):
        if self._drag_offset is not None and e.buttons() & Qt.LeftButton:
            self.move(e.globalPosition().toPoint() - self._drag_offset)

    def mouseReleaseEvent(self, e):
        self._drag_offset = None
        self.ctl.cfg.window_pos = [self.x(), self.y()]
        self.ctl.save_soon()

    def moveEvent(self, e):
        self.ctl.cfg.window_pos = [self.x(), self.y()]
        self.ctl.save_soon()

    def wheelEvent(self, e):
        step = 1 if e.angleDelta().y() > 0 else -1
        self.ctl.set("mascot_size", max(120, min(1400, int(self.ctl.cfg.mascot_size * (1 + 0.08 * step)))))

    def mouseDoubleClickEvent(self, e):
        self.ctl.show_panel()

    def keyPressEvent(self, e):
        self.ctl.handle_key(e)

    def contextMenuEvent(self, e):
        self.ctl.build_menu(QMenu(self)).exec(e.globalPos())


class SubtitleBar(QWidget):
    """Click-through strip along the bottom of the screen."""

    def __init__(self, ctl):
        super().__init__(None)
        self.ctl = ctl
        self.setWindowTitle("onscreen subtitles")
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        flags = (Qt.Window | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
                 | Qt.WindowTransparentForInput | Qt.WindowDoesNotAcceptFocus)
        if sys.platform.startswith("linux"):
            flags |= Qt.Tool
        self.setWindowFlags(flags)

    def place(self):
        screen = self.ctl.mascot_window.screen() or self.screen()
        g = screen.availableGeometry() if screen else QRect(0, 0, 1920, 1080)
        h = int(self.ctl.bottom_font.pixelSize() * 3.6)
        w = int(g.width() * 0.9)
        self.setGeometry(g.x() + (g.width() - w) // 2, g.bottom() - h - int(g.height() * 0.04), w, h)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setCompositionMode(QPainter.CompositionMode_Source)
        p.fillRect(self.rect(), Qt.transparent)
        p.setCompositionMode(QPainter.CompositionMode_SourceOver)
        f = self.ctl.frame
        draw_bottom_subtitles(p, QRectF(self.rect()), f.text, self.ctl.bottom_font, f.text_opacity)
        p.end()
