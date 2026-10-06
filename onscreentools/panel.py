"""The control panel: every setting, applied live."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QCheckBox, QColorDialog, QComboBox, QDoubleSpinBox, QFileDialog,
                               QFormLayout, QGroupBox, QHBoxLayout, QLabel, QProgressBar,
                               QPushButton, QScrollArea, QSlider, QSpinBox, QVBoxLayout, QWidget)

from .puppet import PRESETS

WHISPER_MODELS = ["tiny", "base", "small", "medium", "large-v3", "large-v3-turbo",
                  "distil-large-v3", "tiny.en", "base.en", "small.en", "medium.en"]
LANGUAGES = [("Italiano", "it"), ("English", "en"), ("Español", "es"),
             ("Français", "fr"), ("Deutsch", "de"), ("Português", "pt"), ("Nederlands", "nl"),
             ("Polski", "pl"), ("Русский", "ru"), ("日本語", "ja"), ("中文", "zh"), ("한국어", "ko"),
             ("Auto-detect (unreliable)", None)]
BACKGROUNDS = [("Transparent (overlay)", "transparent"), ("Green #00b140 (for keying)", "#00b140"),
               ("Magenta #ff00ff (for keying)", "#ff00ff"), ("Black", "#000000"), ("White", "#ffffff")]


def _combo(items, current, on_change):
    cb = QComboBox()
    for label, value in items:
        cb.addItem(label, value)
    idx = next((i for i, (_, v) in enumerate(items) if v == current), -1)
    if idx < 0 and current is not None:
        cb.addItem(str(current), current)
        idx = cb.count() - 1
    cb.setCurrentIndex(max(idx, 0))
    cb.currentIndexChanged.connect(lambda i: on_change(cb.itemData(i)))
    return cb


def _slider(lo, hi, value, on_change):
    s = QSlider(Qt.Horizontal)
    s.setRange(lo, hi)
    s.setValue(int(value))
    s.valueChanged.connect(on_change)
    return s


class ControlPanel(QWidget):
    def __init__(self, ctl):
        super().__init__(None)
        self.ctl = ctl
        cfg = ctl.cfg
        set_ = ctl.set
        self.setWindowTitle("onscreen — controls")
        self.resize(430, 760)

        root = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        col = QVBoxLayout(inner)
        scroll.setWidget(inner)
        root.addWidget(scroll)

        # --- mascot -------------------------------------------------------
        g = QGroupBox("Mascot")
        f = QFormLayout(g)
        f.addRow("Show", _combo([("Puppet", "puppet"), ("Me (camera)", "camera")], cfg.mascot,
                                lambda v: set_("mascot", v)))
        self.size_slider = _slider(120, 1200, cfg.mascot_size, lambda v: set_("mascot_size", v))
        f.addRow("Size", self.size_slider)
        col.addWidget(g)

        # --- puppet -------------------------------------------------------
        g = QGroupBox("Puppet")
        f = QFormLayout(g)
        from .cutout import user_puppets

        items = [(name.capitalize(), name) for name in PRESETS]
        items += [(f"{d.name} (mine)", str(d)) for d in user_puppets()]
        self.puppet_combo = _combo(items, cfg.puppet, lambda v: set_("puppet", v))
        f.addRow("Character", self.puppet_combo)
        row = QHBoxLayout()
        b = QPushButton("New from PNG…")
        b.setToolTip("Cut a photo at the mouth and make the top of the head flap, South Park Canadian style")
        b.clicked.connect(self._new_cutout)
        row.addWidget(b)
        b = QPushButton("Load folder…")
        b.clicked.connect(self._load_puppet)
        row.addWidget(b)
        f.addRow("", row)
        cb = QCheckBox("Eyes follow the mouse")
        cb.setChecked(cfg.follow_mouse)
        cb.toggled.connect(lambda v: set_("follow_mouse", v))
        f.addRow("", cb)
        cb = QCheckBox("Point at where I click")
        cb.setToolTip("Needs Input Monitoring permission on macOS; works on X11/XWayland on Linux")
        cb.setChecked(cfg.point_on_click)
        cb.toggled.connect(lambda v: set_("point_on_click", v))
        f.addRow("", cb)
        col.addWidget(g)

        # --- camera -------------------------------------------------------
        g = QGroupBox("Camera")
        f = QFormLayout(g)
        sp = QSpinBox()
        sp.setRange(0, 9)
        sp.setValue(cfg.camera_index)
        sp.valueChanged.connect(lambda v: set_("camera_index", v))
        f.addRow("Device #", sp)
        f.addRow("Background", _combo([("Keep it", "none"), ("Green screen (chroma key)", "chroma"),
                                       ("AI removal (no green screen)", "ai")], cfg.bg_removal,
                                      lambda v: set_("bg_removal", v)))
        row = QHBoxLayout()
        self.key_btn = QPushButton()
        self._paint_key_btn()
        self.key_btn.clicked.connect(self._choose_key)
        row.addWidget(self.key_btn, 1)
        b = QPushButton("Sample corner")
        b.setToolTip("Take the key colour from the top corner of the camera image")
        b.clicked.connect(ctl.sample_key_color)
        row.addWidget(b)
        f.addRow("Key colour", row)
        f.addRow("Tolerance", _slider(0, 150, cfg.chroma_tolerance, lambda v: set_("chroma_tolerance", v)))
        f.addRow("Softness", _slider(1, 100, cfg.chroma_softness, lambda v: set_("chroma_softness", v)))
        f.addRow("Spill fix", _slider(0, 100, cfg.spill * 100, lambda v: set_("spill", v / 100)))
        f.addRow("Shape", _combo([("Full frame", "full"), ("Circle", "circle"), ("Rounded square", "rounded")],
                                 cfg.camera_shape, lambda v: set_("camera_shape", v)))
        z = QDoubleSpinBox()
        z.setRange(1.0, 4.0)
        z.setSingleStep(0.1)
        z.setValue(cfg.camera_zoom)
        z.valueChanged.connect(lambda v: set_("camera_zoom", v))
        f.addRow("Zoom", z)
        cb = QCheckBox("Mirror")
        cb.setChecked(cfg.mirror)
        cb.toggled.connect(lambda v: set_("mirror", v))
        f.addRow("", cb)
        col.addWidget(g)

        # --- audio --------------------------------------------------------
        g = QGroupBox("Microphone && lip-sync")
        f = QFormLayout(g)
        devices = [("System default", None)]
        try:
            from .audio import list_input_devices
            devices += [(name, name) for _, name in list_input_devices()]
        except Exception as e:
            devices += [(f"(audio unavailable: {e})", None)]
        f.addRow("Input", _combo(devices, cfg.audio_device, lambda v: set_("audio_device", v)))
        self.meter = QProgressBar()
        self.meter.setRange(0, 100)
        self.meter.setTextVisible(False)
        self.meter.setMaximumHeight(10)
        f.addRow("Level", self.meter)
        f.addRow("Noise gate", _slider(-80, -20, cfg.noise_gate_db, lambda v: set_("noise_gate_db", float(v))))
        f.addRow("Sensitivity", _slider(8, 50, 58 - cfg.mouth_range_db,
                                        lambda v: set_("mouth_range_db", float(58 - v))))
        col.addWidget(g)

        # --- subtitles ----------------------------------------------------
        g = QGroupBox("Live subtitles (Whisper)")
        f = QFormLayout(g)
        f.addRow("Mode", _combo([("Off", "off"), ("Comic bubble", "bubble"), ("Bottom of screen", "bottom")],
                                cfg.subtitles, lambda v: set_("subtitles", v)))
        f.addRow("Bubble side", _combo([("Right of mascot", "right"), ("Left of mascot", "left")],
                                       cfg.bubble_side, lambda v: set_("bubble_side", v)))
        f.addRow("Model", _combo([(m, m) for m in WHISPER_MODELS], cfg.whisper_model,
                                 lambda v: set_("whisper_model", v)))
        self.lang_combo = _combo(LANGUAGES, cfg.language, lambda v: set_("language", v))
        self.lang_combo.setToolTip("Set the language you speak. Auto-detect guesses per sentence "
                                   "and can jump to random languages.")
        f.addRow("Spoken language", self.lang_combo)
        cb = QCheckBox("Translate to English")
        cb.setChecked(cfg.translate)
        cb.toggled.connect(lambda v: set_("translate", v))
        f.addRow("", cb)
        fs = QSpinBox()
        fs.setRange(10, 96)
        fs.setValue(cfg.font_size)
        fs.valueChanged.connect(lambda v: set_("font_size", v))
        f.addRow("Font size", fs)
        cb = QCheckBox("UPPERCASE (comic lettering)")
        cb.setChecked(cfg.uppercase)
        cb.toggled.connect(lambda v: set_("uppercase", v))
        f.addRow("", cb)
        self.whisper_status = QLabel("")
        self.whisper_status.setWordWrap(True)
        f.addRow("Status", self.whisper_status)
        b = QPushButton("Clear subtitles")
        b.clicked.connect(ctl.clear_subtitles)
        f.addRow("", b)
        col.addWidget(g)

        # --- window -------------------------------------------------------
        g = QGroupBox("Window")
        f = QFormLayout(g)
        f.addRow("Background", _combo(BACKGROUNDS, cfg.window_bg, lambda v: set_("window_bg", v)))
        cb = QCheckBox("Click-through (lock in place)")
        cb.setChecked(cfg.click_through)
        cb.toggled.connect(lambda v: set_("click_through", v))
        f.addRow("", cb)
        cb = QCheckBox("Publish as virtual camera")
        cb.setChecked(cfg.vcam)
        cb.toggled.connect(lambda v: set_("vcam", v))
        f.addRow("", cb)
        col.addWidget(g)

        tips = QLabel("Drag the mascot to move it · scroll on it to resize · right-click for a menu · "
                      "double-click to reopen this panel.")
        tips.setWordWrap(True)
        tips.setStyleSheet("color: gray;")
        col.addWidget(tips)
        col.addStretch(1)

        self.error_label = QLabel("")
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet("color: #d9480f;")
        root.addWidget(self.error_label)

    # -- callbacks --------------------------------------------------------
    def _new_cutout(self):
        from .cutout import open_editor

        folder = open_editor(self, self.ctl)
        if folder:
            self._select_puppet(str(folder))

    def _load_puppet(self):
        folder = QFileDialog.getExistingDirectory(self, "Choose a puppet folder (with puppet.json)")
        self._select_puppet(folder)

    def _select_puppet(self, folder):
        if folder:
            if self.puppet_combo.findData(folder) < 0:
                self.puppet_combo.addItem(folder.rstrip("/").split("/")[-1] + " (folder)", folder)
            self.puppet_combo.blockSignals(True)
            self.puppet_combo.setCurrentIndex(self.puppet_combo.findData(folder))
            self.puppet_combo.blockSignals(False)
            self.ctl.set("puppet", folder, force=True)  # reloads even when re-saving the same folder
            self.ctl.set("mascot", "puppet")

    def _paint_key_btn(self):
        r, g, b = self.ctl.cfg.chroma_color
        fg = "black" if (r * 299 + g * 587 + b * 114) / 1000 > 128 else "white"
        self.key_btn.setText(f"#{r:02x}{g:02x}{b:02x}")
        self.key_btn.setStyleSheet(f"background: rgb({r},{g},{b}); color: {fg};")

    def _choose_key(self):
        c = QColorDialog.getColor(QColor(*self.ctl.cfg.chroma_color), self, "Green-screen colour")
        if c.isValid():
            self.ctl.set("chroma_color", [c.red(), c.green(), c.blue()])
            self._paint_key_btn()

    def refresh(self, level01: float, status: str, error: str):
        self.meter.setValue(int(level01 * 100))
        self.whisper_status.setText(status)
        self.error_label.setText(error)
        i = self.lang_combo.findData(self.ctl.cfg.language)
        if i >= 0 and i != self.lang_combo.currentIndex():
            self.lang_combo.blockSignals(True)
            self.lang_combo.setCurrentIndex(i)
            self.lang_combo.blockSignals(False)
        if self.key_btn.text() != "#%02x%02x%02x" % tuple(self.ctl.cfg.chroma_color):
            self._paint_key_btn()
        if self.size_slider.value() != self.ctl.cfg.mascot_size:
            self.size_slider.blockSignals(True)
            self.size_slider.setValue(self.ctl.cfg.mascot_size)
            self.size_slider.blockSignals(False)
