"""Entry point and controller that wires audio, camera, Whisper and the windows together."""
from __future__ import annotations

import argparse
import os
import queue
import sys
import time

from .config import Config

FPS = 60


class Controller:
    def __init__(self, cfg: Config, show_panel: bool = True):
        from PySide6.QtCore import Qt, QTimer
        from PySide6.QtWidgets import QApplication

        from .audio import AudioEngine
        from .lipsync import MouthDriver
        from .overlay import Frame, MascotWindow, SubtitleBar
        from .puppet import Blinker, BuiltinPuppet, load_puppet
        from .render import pick_font
        from .transcriber import SubtitleState

        self.app = QApplication.instance()
        self.cfg = cfg
        self.frame = Frame()
        self._pick_font = pick_font
        self._load_puppet = load_puppet
        self.errors: dict[str, str] = {}

        try:
            self.puppet = load_puppet(cfg.puppet)
        except Exception as e:
            self.errors["puppet"] = str(e)
            self.puppet = BuiltinPuppet("beanie")
        self.blinker = Blinker()
        self.mouth = MouthDriver(cfg.noise_gate_db, cfg.mouth_range_db)
        self.subs = SubtitleState(cfg.hide_after)
        self._update_fonts()

        self.audio = AudioEngine(cfg.audio_device)
        self.audio.start()
        self.camera = None
        self.transcriber = None
        self._whisper_q = None
        self._cam_frame_id = -1
        self._cam_aspect = 16 / 9
        self.vcam = None

        self.mascot_window = MascotWindow(self)
        self.subtitle_bar = SubtitleBar(self)
        self.panel = None

        self._save_timer = QTimer()
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(800)
        self._save_timer.timeout.connect(self.cfg.save)

        self._sync_camera()
        self._sync_transcriber()
        self.mascot_window.relayout()
        self.mascot_window.place_default()
        self.mascot_window.show()
        self._sync_subtitle_bar()
        self._sync_vcam()
        if show_panel:
            self.show_panel()
        self._setup_tray()

        self._t0 = time.monotonic()
        self._last = self._t0
        self.timer = QTimer()
        self.timer.setTimerType(Qt.PreciseTimer)
        self.timer.timeout.connect(self.tick)
        self.timer.start(int(1000 / FPS))
        self.app.aboutToQuit.connect(self.shutdown)

    # -- state ------------------------------------------------------------
    def mascot_aspect(self) -> float:
        if self.cfg.mascot == "camera":
            return 1.0 if self.cfg.camera_shape in ("circle", "rounded") else self._cam_aspect
        return self.puppet.aspect

    def _update_fonts(self):
        self.font = self._pick_font(self.cfg.font_family, self.cfg.font_size)
        self.bottom_font = self._pick_font(self.cfg.font_family, int(self.cfg.font_size * 1.5))

    def set(self, key: str, value, force: bool = False):
        if getattr(self.cfg, key) == value and not force:
            return
        setattr(self.cfg, key, value)
        cfg = self.cfg
        if key == "puppet":
            try:
                self.puppet = self._load_puppet(value)
                self.errors.pop("puppet", None)
            except Exception as e:
                self.errors["puppet"] = str(e)
            self.mascot_window.relayout()
        elif key in ("mascot", "camera_index", "camera_width", "camera_height"):
            self._sync_camera(restart=key != "mascot")
            self.mascot_window.relayout()
        elif key in ("camera_shape", "mascot_size", "bubble_side"):
            self.mascot_window.relayout()
        elif key in ("font_size", "font_family"):
            self._update_fonts()
            self.mascot_window.relayout()
            self._sync_subtitle_bar()
        elif key == "subtitles":
            self._sync_transcriber()
            self.mascot_window.relayout()
            self._sync_subtitle_bar()
        elif key in ("whisper_model", "whisper_backend", "language", "translate"):
            self._sync_transcriber(restart=True)
        elif key == "audio_device":
            self.audio.stop()
            self.audio.device = value
            self.audio.start()
        elif key in ("noise_gate_db", "mouth_range_db"):
            self.mouth.gate_db = cfg.noise_gate_db
            self.mouth.range_db = cfg.mouth_range_db
            if self.transcriber:
                self.transcriber.gate_db = cfg.noise_gate_db
        elif key == "click_through":
            self.mascot_window.apply_flags()
        elif key == "window_bg":
            self.mascot_window.update()
        elif key == "vcam":
            self._sync_vcam()
        elif key == "hide_after":
            self.subs.hide_after = value
        self.save_soon()

    def save_soon(self):
        self._save_timer.start()

    # -- engines ----------------------------------------------------------
    def _sync_camera(self, restart: bool = False):
        from .camera import CameraSource

        want = self.cfg.mascot == "camera"
        if self.camera and (restart or not want):
            self.camera.stop()
            self.camera = None
            self.frame.camera = None
        if want and self.camera is None:
            self.errors.pop("camera", None)
            self.camera = CameraSource(self.cfg)
            self.camera.start()
            self._cam_frame_id = -1

    def _sync_transcriber(self, restart: bool = False):
        from .transcriber import Transcriber, make_backend

        want = self.cfg.subtitles != "off"
        if self.transcriber and (restart or not want):
            self.transcriber.stop()
            self.transcriber = None
            self.audio.unsubscribe(self._whisper_q)
            self._whisper_q = None
        if want and self.transcriber is None:
            cfg = self.cfg
            self._whisper_q = self.audio.subscribe()
            self.transcriber = Transcriber(
                self._whisper_q,
                lambda: make_backend(cfg.whisper_backend, cfg.whisper_model, cfg.language, cfg.translate),
                gate_db=cfg.noise_gate_db,
            )
            self.transcriber.start()

    def _sync_subtitle_bar(self):
        if self.cfg.subtitles == "bottom":
            self.subtitle_bar.place()
            self.subtitle_bar.show()
        else:
            self.subtitle_bar.hide()

    def _sync_vcam(self):
        if self.vcam is not None:
            self.vcam.close()
            self.vcam = None
        if self.cfg.vcam:
            from .vcam import VirtualCam

            try:
                self.vcam = VirtualCam(self)
                self.errors.pop("vcam", None)
            except Exception as e:
                self.errors["vcam"] = f"Virtual camera: {e}"

    def sample_key_color(self):
        if self.camera:
            self.camera.pick_request = True

    def clear_subtitles(self):
        self.subs.clear()

    # -- main loop --------------------------------------------------------
    def tick(self):
        from .render import rgba_to_qimage

        now = time.monotonic()
        dt, self._last = now - self._last, now
        f = self.frame
        f.t = now - self._t0
        f.openness = self.mouth.update(self.audio.level, dt)
        f.blink = self.blinker.update(f.t)

        if self.camera is not None:
            rgba, fid = self.camera.latest()
            if rgba is not None and fid != self._cam_frame_id:
                self._cam_frame_id = fid
                f.camera = rgba_to_qimage(rgba)
                aspect = rgba.shape[1] / rgba.shape[0]
                if abs(aspect - self._cam_aspect) > 0.01:
                    self._cam_aspect = aspect
                    self.mascot_window.relayout()
            if self.camera.error:
                self.errors["camera"] = self.camera.error
            f.status = "" if f.camera is not None else (self.camera.error or "Starting camera…")

        if self.transcriber is not None:
            while True:
                try:
                    kind, text = self.transcriber.out.get_nowait()
                except queue.Empty:
                    break
                self.subs.push(kind, text, now)
        f.text = self.subs.text()
        f.text_opacity = self.subs.opacity(now, self.mouth.talking)

        if self.audio.error:
            self.errors["audio"] = self.audio.error
        else:
            self.errors.pop("audio", None)

        self.mascot_window.update()
        if self.subtitle_bar.isVisible():
            self.subtitle_bar.update()
        if self.vcam is not None:
            self.vcam.send()
        if self.panel is not None and self.panel.isVisible():
            self.panel.refresh(min(1.0, self.mouth.target(self.audio.level)), self._whisper_status(),
                               "\n".join(self.errors.values()))

    def _whisper_status(self) -> str:
        if self.transcriber is None:
            return "off"
        st = self.transcriber.status
        if st == "loading":
            return f"loading '{self.cfg.whisper_model}' (first run downloads it)…"
        if st == "ready":
            lang = getattr(self.transcriber.backend, "language", None) or "auto"
            return f"listening · {self.cfg.whisper_model} · {lang}"
        return st

    # -- UI glue ----------------------------------------------------------
    def show_panel(self):
        if self.panel is None:
            from .panel import ControlPanel

            self.panel = ControlPanel(self)
        self.panel.show()
        self.panel.raise_()
        self.panel.activateWindow()

    def build_menu(self, menu):
        cfg = self.cfg

        def add(label, fn, checked=None):
            a = menu.addAction(label)
            if checked is not None:
                a.setCheckable(True)
                a.setChecked(checked)
            a.triggered.connect(fn)

        add("Puppet", lambda: self.set("mascot", "puppet"), cfg.mascot == "puppet")
        add("Me (camera)", lambda: self.set("mascot", "camera"), cfg.mascot == "camera")
        menu.addSeparator()
        add("Subtitles: off", lambda: self.set("subtitles", "off"), cfg.subtitles == "off")
        add("Subtitles: comic bubble", lambda: self.set("subtitles", "bubble"), cfg.subtitles == "bubble")
        add("Subtitles: bottom", lambda: self.set("subtitles", "bottom"), cfg.subtitles == "bottom")
        from .panel import LANGUAGES

        lang_menu = menu.addMenu("Spoken language")
        for label, code in LANGUAGES:
            a = lang_menu.addAction(label)
            a.setCheckable(True)
            a.setChecked(cfg.language == code)
            a.triggered.connect(lambda _=False, c=code: self.set("language", c))
        menu.addSeparator()
        add("Click-through", lambda: self.set("click_through", not cfg.click_through), cfg.click_through)
        add("Controls…", self.show_panel)
        menu.addSeparator()
        add("Quit", self.app.quit)
        return menu

    def _setup_tray(self):
        from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
        from PySide6.QtWidgets import QMenu, QSystemTrayIcon

        self.tray = None
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        pm = QPixmap(64, 64)
        pm.fill(QColor(0, 0, 0, 0))
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        p.setBrush(QColor("#ffdcb4"))
        p.drawEllipse(6, 10, 52, 46)
        p.setBrush(QColor("#2f6db5"))
        p.drawChord(6, 6, 52, 40, 0, 180 * 16)
        p.setBrush(QColor("white"))
        p.drawEllipse(18, 26, 14, 16)
        p.drawEllipse(32, 26, 14, 16)
        p.end()
        self.tray = QSystemTrayIcon(QIcon(pm))
        self._tray_menu = QMenu()
        self._tray_menu.aboutToShow.connect(lambda: (self._tray_menu.clear(), self.build_menu(self._tray_menu)))
        self.build_menu(self._tray_menu)
        self.tray.setContextMenu(self._tray_menu)
        self.tray.setToolTip("onscreen mascot")
        self.tray.show()

    def handle_key(self, e):
        from PySide6.QtCore import Qt

        k, cfg = e.key(), self.cfg
        if k == Qt.Key_Q and e.modifiers() & (Qt.ControlModifier | Qt.MetaModifier):
            self.app.quit()
        elif k == Qt.Key_M:
            self.set("mascot", "camera" if cfg.mascot == "puppet" else "puppet")
        elif k == Qt.Key_S:
            order = ["off", "bubble", "bottom"]
            self.set("subtitles", order[(order.index(cfg.subtitles) + 1) % 3])
        elif k == Qt.Key_C:
            self.clear_subtitles()
        elif k == Qt.Key_P:
            self.show_panel()
        elif k in (Qt.Key_Plus, Qt.Key_Equal):
            self.set("mascot_size", min(1400, int(cfg.mascot_size * 1.1)))
        elif k == Qt.Key_Minus:
            self.set("mascot_size", max(120, int(cfg.mascot_size / 1.1)))

    def shutdown(self):
        self.timer.stop()
        self.cfg.save()
        if self.vcam is not None:
            self.vcam.close()
        if self.transcriber:
            self.transcriber.stop()
        if self.camera:
            self.camera.stop()
        self.audio.stop()


def _prefer_xwayland():
    """Wayland doesn't let apps keep a window on top or place it; XWayland does."""
    if (sys.platform.startswith("linux") and os.environ.get("WAYLAND_DISPLAY")
            and os.environ.get("DISPLAY") and "QT_QPA_PLATFORM" not in os.environ):
        os.environ["QT_QPA_PLATFORM"] = "xcb"


def parse_args(argv=None):
    ap = argparse.ArgumentParser(
        prog="onscreen",
        description="A talking on-screen mascot for tutorials: a lip-synced cutout puppet or you "
                    "from the webcam, with live Whisper subtitles.")
    ap.add_argument("--puppet", help="preset name (beanie, pompom, hood, hair, redhead) or puppet folder")
    ap.add_argument("--cutout", metavar="PNG",
                    help="make a South Park Canadian-style puppet from a photo/PNG cutout (opens the editor)")
    ap.add_argument("--camera", nargs="?", const=-1, type=int, metavar="INDEX",
                    help="show yourself from the webcam instead of the puppet")
    ap.add_argument("--bg", choices=["none", "chroma", "ai"], help="camera background removal")
    ap.add_argument("--subs", choices=["off", "bubble", "bottom"], help="subtitle style")
    ap.add_argument("--model", help="Whisper model (tiny, base, small, medium, large-v3, large-v3-turbo…)")
    ap.add_argument("--lang", help="spoken language code (it, en, …) or 'auto'; default it")
    ap.add_argument("--size", type=int, help="mascot height in pixels")
    ap.add_argument("--window-bg", help="'transparent' or a colour like '#00b140' for keying in OBS")
    ap.add_argument("--vcam", action="store_true", help="also publish as a virtual camera (pyvirtualcam)")
    ap.add_argument("--no-panel", action="store_true", help="start without the control panel")
    ap.add_argument("--list-devices", action="store_true", help="list microphones and exit")
    ap.add_argument("--export-puppet", nargs=2, metavar=("PRESET", "DIR"),
                    help="render a builtin puppet as PNG frames you can repaint, then exit")
    ap.add_argument("--reset", action="store_true", help="ignore saved settings")
    return ap.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)

    if args.list_devices:
        from .audio import list_input_devices

        for i, name in list_input_devices():
            print(f"{i:3d}  {name}")
        return 0

    _prefer_xwayland()
    from PySide6.QtWidgets import QApplication

    if args.export_puppet:
        app = QApplication(sys.argv[:1])
        from .puppet import export_template

        out = export_template(*args.export_puppet)
        print(f"puppet template written to {out} — repaint the PNGs, then run: onscreen --puppet {out}")
        return 0

    cfg = Config() if args.reset else Config.load()
    if args.puppet:
        cfg.puppet, cfg.mascot = args.puppet, "puppet"
    if args.camera is not None:
        cfg.mascot = "camera"
        if args.camera >= 0:
            cfg.camera_index = args.camera
    if args.bg:
        cfg.bg_removal = args.bg
    if args.subs:
        cfg.subtitles = args.subs
    if args.model:
        cfg.whisper_model = args.model
    if args.lang:
        cfg.language = None if args.lang == "auto" else args.lang
    if args.size:
        cfg.mascot_size = args.size
    if args.window_bg:
        cfg.window_bg = args.window_bg
    if args.vcam:
        cfg.vcam = True

    app = QApplication(sys.argv[:1])
    app.setApplicationName("onscreen")
    if args.cutout:
        from .cutout import open_editor

        folder = open_editor(path=args.cutout)
        if folder is None:
            return 0
        cfg.puppet, cfg.mascot = str(folder), "puppet"
    app.setQuitOnLastWindowClosed(False)
    app._controller = Controller(cfg, show_panel=not args.no_panel)  # keep a reference
    import signal

    signal.signal(signal.SIGINT, lambda *_: app.quit())
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
