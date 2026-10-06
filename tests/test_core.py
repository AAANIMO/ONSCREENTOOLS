import os

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from onscreentools.audio import WHISPER_RATE, _resample
from onscreentools.camera import chroma_key
from onscreentools.config import Config
from onscreentools.lipsync import MouthDriver, quantize
from onscreentools.transcriber import SubtitleState, Transcriber, _clean


# ---------------------------------------------------------------- lipsync
def test_mouth_opens_on_loud_and_closes_on_silence():
    m = MouthDriver(gate_db=-50, range_db=30)
    for _ in range(30):
        m.update(0.2, 1 / 60)
    assert m.value > 0.5 and m.talking
    for _ in range(60):
        m.update(0.0, 1 / 60)
    assert m.value == 0.0 and not m.talking


def test_noise_below_gate_keeps_mouth_shut():
    m = MouthDriver(gate_db=-50, range_db=30)
    for _ in range(60):
        m.update(10 ** (-60 / 20), 1 / 60)
    assert m.value == 0.0


def test_quantize():
    assert quantize(0.0, 4) == 0
    assert quantize(0.2, 4) == 1
    assert quantize(1.0, 4) == 3
    assert quantize(0.9, 1) == 0


# ---------------------------------------------------------------- audio
def test_resample_length():
    x = np.random.rand(48000).astype(np.float32)
    assert len(_resample(x, 48000, 16000)) == 16000


# ---------------------------------------------------------------- keying
def test_chroma_key_removes_green_keeps_skin():
    frame = np.zeros((10, 20, 3), np.uint8)
    frame[:, :10] = (0, 177, 64)        # green screen
    frame[:, 10:] = (224, 172, 140)     # skin
    out = chroma_key(frame, (0, 177, 64), tolerance=40, softness=25, spill=0.8)
    assert out.shape == (10, 20, 4)
    assert out[:, :10, 3].max() == 0
    assert out[:, 10:, 3].min() == 255
    # skin isn't touched by spill suppression (green isn't its dominant channel)
    assert tuple(out[0, 15, :3]) == (224, 172, 140)


def test_chroma_key_handles_shadowed_screen():
    shadow = np.full((4, 4, 3), (0, 110, 40), np.uint8)  # darker green
    out = chroma_key(shadow, (0, 177, 64), tolerance=40, softness=25)
    assert out[..., 3].max() < 128


# ---------------------------------------------------------------- whisper streaming
class FakeBackend:
    def __init__(self):
        self.calls = []

    def __call__(self, audio, prompt, final):
        self.calls.append((len(audio) / WHISPER_RATE, final))
        return "hello world" if final else "hello"


def _tone(seconds, amp=0.2):
    t = np.arange(int(seconds * WHISPER_RATE)) / WHISPER_RATE
    return (amp * np.sin(2 * np.pi * 220 * t)).astype(np.float32)


def _run(tr, audio, chunk=320):
    for i in range(0, len(audio), chunk):
        tr.feed(audio[i:i + chunk])
        tr.tick()


def test_transcriber_partials_then_final():
    tr = Transcriber(None, None, gate_db=-50)
    tr._reset()
    tr.backend = FakeBackend()
    audio = np.concatenate([np.zeros(8000, np.float32), _tone(2.0), np.zeros(16000, np.float32)])
    _run(tr, audio)
    events = []
    while not tr.out.empty():
        events.append(tr.out.get())
    assert ("partial", "hello") in events
    assert events[-1] == ("final", "hello world")
    assert sum(1 for _, f in tr.backend.calls if f) == 1
    # utterance includes some pre-roll but not all the leading silence
    final_len = [d for d, f in tr.backend.calls if f][0]
    assert 2.0 <= final_len <= 3.0


def test_transcriber_ignores_silence():
    tr = Transcriber(None, None, gate_db=-50)
    tr._reset()
    tr.backend = FakeBackend()
    _run(tr, np.zeros(WHISPER_RATE * 3, np.float32))
    assert tr.backend.calls == [] and tr.out.empty()


def test_transcriber_splits_long_utterances():
    tr = Transcriber(None, None, gate_db=-50, max_utterance=3.0)
    tr._reset()
    tr.backend = FakeBackend()
    _run(tr, _tone(7.0))
    assert sum(1 for _, f in tr.backend.calls if f) == 2


def test_hallucination_filter():
    assert _clean(" Thanks for watching! ") == ""
    assert _clean("  so   this is   real ") == "so this is real"


def test_subtitle_state_accumulates_and_fades():
    s = SubtitleState(hide_after=2.0, max_chars=40)
    s.push("partial", "hello", now=0.0)
    assert s.text() == "hello" and s.opacity(now=0.1) == 1.0
    s.push("final", "hello there.", now=0.5)
    s.push("final", "how are you?", now=1.0)
    assert s.text() == "hello there. how are you?"
    assert s.opacity(now=2.5) == 1.0
    assert s.opacity(now=10.0) == 0.0
    # after a long pause a new caption starts fresh
    s.push("partial", "next", now=10.0)
    assert s.text() == "next"


def test_subtitle_state_truncates_on_word_boundary():
    s = SubtitleState(max_chars=20)
    s.push("final", "one two three four five six seven eight", now=0)
    t = s.text()
    assert t.startswith("…") and len(t) <= 21 and "eight" in t


# ---------------------------------------------------------------- config
def test_config_roundtrip(tmp_path):
    c = Config(mascot="camera", language="it", chroma_color=[1, 2, 3])
    c.save(tmp_path / "c.json")
    d = Config.load(tmp_path / "c.json")
    assert d.mascot == "camera" and d.language == "it" and d.chroma_color == [1, 2, 3]


def test_config_ignores_unknown_and_bad_files(tmp_path):
    (tmp_path / "c.json").write_text('{"nope": 1, "mascot_size": 500}')
    assert Config.load(tmp_path / "c.json").mascot_size == 500
    (tmp_path / "bad.json").write_text("{not json")
    assert Config.load(tmp_path / "bad.json").mascot == "puppet"


# ---------------------------------------------------------------- rendering
@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _render(puppet, openness, w=200, h=240):
    from PySide6.QtCore import QRectF
    from PySide6.QtGui import QImage, QPainter

    img = QImage(w, h, QImage.Format_ARGB32)
    img.fill(0)
    p = QPainter(img)
    puppet.draw(p, QRectF(0, 0, w, h), openness, 0.0, 0.0)
    p.end()
    return img


def test_builtin_puppets_draw_and_mouth_moves(qapp):
    from onscreentools.puppet import PRESETS, BuiltinPuppet

    for name in PRESETS:
        pup = BuiltinPuppet(name)
        closed, opened = _render(pup, 0.0), _render(pup, 1.0)
        assert not closed.isNull()
        assert closed != opened, name


def test_export_and_load_image_puppet(qapp, tmp_path):
    from onscreentools.puppet import ImagePuppet, export_template, load_puppet

    folder = export_template("beanie", tmp_path / "pup", height=240)
    pup = load_puppet(str(folder))
    assert isinstance(pup, ImagePuppet)
    assert len(pup.mouths) == 4 and pup.blink_img is not None
    assert _render(pup, 0.0) != _render(pup, 1.0)


def test_recoloured_builtin_puppet(qapp, tmp_path):
    from onscreentools.puppet import BuiltinPuppet, load_puppet

    (tmp_path / "puppet.json").write_text('{"type": "builtin", "base": "hood", "hat": "#ff00aa"}')
    pup = load_puppet(str(tmp_path))
    assert isinstance(pup, BuiltinPuppet) and pup.s["hat"] == "#ff00aa"


def test_unknown_puppet_raises():
    from onscreentools.puppet import load_puppet

    with pytest.raises(ValueError):
        load_puppet("/definitely/not/here")


def test_bubble_and_bottom_subtitles_paint(qapp):
    from PySide6.QtCore import QPointF, QRectF
    from PySide6.QtGui import QImage, QPainter

    from onscreentools.render import draw_bottom_subtitles, draw_bubble, pick_font

    img = QImage(600, 400, QImage.Format_ARGB32)
    img.fill(0)
    p = QPainter(img)
    font = pick_font("", 22)
    rect = draw_bubble(p, "a fairly long sentence that needs to wrap " * 3, font,
                       QRectF(0, 0, 400, 250), QPointF(450, 330))
    draw_bottom_subtitles(p, QRectF(0, 0, 600, 400), "bottom text", font)
    p.end()
    assert rect is not None and rect.width() <= 400 and rect.top() >= 0
    assert img.pixelColor(int(rect.center().x()), int(rect.center().y())).alpha() == 255


# ---------------------------------------------------------------- canadian cutout
def _person(qapp):
    from PySide6.QtCore import QRectF, Qt
    from PySide6.QtGui import QColor, QImage, QPainter

    img = QImage(200, 250, QImage.Format_ARGB32_Premultiplied)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setBrush(QColor("#e8b48f"))
    p.drawEllipse(QRectF(50, 20, 100, 130))
    p.setBrush(QColor("#3355aa"))
    p.drawRect(QRectF(30, 160, 140, 90))
    p.end()
    return img


@pytest.mark.parametrize("front_x,hinge_x", [(0.35, 0.7), (0.7, 0.35)])
def test_canadian_head_front_goes_up(qapp, front_x, hinge_x):
    from onscreentools.cutout import CanadianPuppet

    pup = CanadianPuppet(_person(qapp), [front_x, 0.45], [hinge_x, 0.43], chaos=0.0)
    T = pup.head_transform(1.0, 0.0)
    moved = T.map(pup.front)
    assert moved.y() < pup.front.y() - 20      # the face side lifts
    assert T.map(pup.hinge) == pup.hinge        # the hinge stays put


def test_canadian_closed_is_the_plain_image_and_open_shows_mouth(qapp):
    from PySide6.QtCore import QRectF
    from PySide6.QtGui import QColor, QImage, QPainter

    from onscreentools.cutout import CanadianPuppet

    src = _person(qapp)
    pup = CanadianPuppet(src, [0.35, 0.45], [0.7, 0.43], mouth_color="#ff0000")

    def render(o):
        img = QImage(200, 250, QImage.Format_ARGB32_Premultiplied)
        img.fill(0)
        p = QPainter(img)
        pup.draw(p, QRectF(0, 0, 200, 250), o, 0.0, 0.0)
        p.end()
        return img

    closed, opened = render(0.0), render(1.0)
    assert closed != opened
    reds = sum(1 for y in range(0, 250, 2) for x in range(0, 200, 2)
               if opened.pixelColor(x, y) == QColor("#ff0000"))
    assert reds > 20


def test_canadian_save_and_load(qapp, tmp_path):
    from onscreentools.cutout import CanadianPuppet, guess_cut, save_cutout
    from onscreentools.puppet import load_puppet

    img = _person(qapp)
    front, hinge = guess_cut(img)
    assert 0 < front[0] < hinge[0] < 1
    folder = save_cutout(img, "Mr Test!", front, hinge, 30, 1.5, "#110000", root=tmp_path)
    assert folder.name == "Mr-Test"
    pup = load_puppet(str(folder))
    assert isinstance(pup, CanadianPuppet) and pup.max_angle == 30 and pup.chaos == 1.5


# ---------------------------------------------------------------- language
def test_default_language_is_italian():
    assert Config().language == "it"


def test_old_config_with_auto_detect_migrates_to_italian(tmp_path):
    (tmp_path / "c.json").write_text('{"language": null, "whisper_model": "small"}')
    c = Config.load(tmp_path / "c.json")
    assert c.language == "it" and c.config_version == 2


def test_explicit_choices_survive(tmp_path):
    (tmp_path / "a.json").write_text('{"language": "en"}')
    assert Config.load(tmp_path / "a.json").language == "en"
    # a v2 config where the user deliberately picked auto-detect keeps it
    (tmp_path / "b.json").write_text('{"language": null, "config_version": 2}')
    assert Config.load(tmp_path / "b.json").language is None


def test_italian_subtitle_credit_hallucinations_are_dropped():
    assert _clean("Sottotitoli e revisione a cura di QTSS") == ""
    assert _clean("Sottotitoli creati dalla comunità Amara.org") == ""
    assert _clean("Oggi vediamo come si centra un div.") == "Oggi vediamo come si centra un div."


def test_backend_uses_greedy_partials(monkeypatch):
    from onscreentools import transcriber as T

    calls = []

    class Model:
        def transcribe(self, audio, **kw):
            calls.append(kw)
            return iter(()), type("I", (), {"language": "it", "language_probability": 1.0})()

    b = T.FasterWhisperBackend.__new__(T.FasterWhisperBackend)
    b.model, b.language, b.task = Model(), "it", "transcribe"
    b(np.zeros(16000, np.float32), "", final=False)
    b(np.zeros(16000, np.float32), "", final=True)
    assert calls[0]["language"] == "it" and calls[0]["temperature"] == (0.0,)
    assert max(calls[1]["temperature"]) <= 0.4


# ---------------------------------------------------------------- mouse: gaze + pointing
def test_gaze_glides_instead_of_snapping():
    from onscreentools.pointer import PointerTracker

    tr = PointerTracker(reach=100)
    eyes = (500, 500)
    tr.update(0.0, 1 / 60, (500, 500), eyes)
    xs = []
    for i in range(1, 60):
        cursor = (500 + 10 * i, 500)  # mouse moving right
        xs.append(tr.update(i / 60, 1 / 60, cursor, eyes).look_x)
    assert xs[0] < 0.1                        # first frame: barely moved
    assert all(b >= a - 1e-9 for a, b in zip(xs, xs[1:]))  # smooth, monotonic
    assert xs[-1] > 0.9                       # ...but gets there within a second
    assert max(xs) <= 1.0 + 1e-6              # no overshoot


def test_gaze_returns_straight_when_mouse_rests():
    from onscreentools.pointer import PointerTracker

    tr = PointerTracker(idle_after=0.5)
    t = 0.0
    for i in range(30):
        t = i / 60
        pose = tr.update(t, 1 / 60, (900 + i * 5, 100), (500, 500))
    assert pose.look_x > 0.3 and pose.look_y < -0.1
    for i in range(1, 120):
        pose = tr.update(t + i / 60, 1 / 60, (1045, 100), (500, 500))
    assert abs(pose.look_x) < 0.05 and abs(pose.look_y) < 0.05


def test_point_animation_is_visible_but_snappy():
    from onscreentools.pointer import PointAnim

    a = PointAnim()
    a.trigger(10.0, 0.0, "right")
    assert 0.0 < a.amount(10.05) < 1.0        # raising, not instant
    assert a.amount(10.0 + a.RAISE) >= 0.99   # up within ~0.2 s
    assert a.amount(11.2) == 1.0              # held for over a second
    assert a.amount(10.0 + a.RAISE + a.HOLD + a.LOWER + 0.01) == 0.0
    assert not a.active


def test_click_picks_arm_and_angle_and_eyes_follow():
    import math

    from onscreentools.pointer import PointerTracker

    tr = PointerTracker()
    shoulders = {"left": (480, 560), "right": (520, 560)}
    tr.click(0.0, (100, 160), shoulders, (500, 500))          # up-left
    pose = None
    for i in range(1, 30):
        pose = tr.update(i / 60, 1 / 60, (500, 500), (500, 500))
    assert pose.point_side == "left" and pose.point_amount == 1.0
    assert math.isclose(pose.point_angle, math.atan2(160 - 560, 100 - 480))
    assert pose.look_x < -0.3 and pose.look_y < -0.2         # looks where it points
    tr.click(1.0, (1500, 900), shoulders, (500, 500))
    assert tr.point.side == "right"


def test_puppet_pose_changes_drawing(qapp):
    import math

    from PySide6.QtCore import QRectF
    from PySide6.QtGui import QImage, QPainter

    from onscreentools.pointer import Pose
    from onscreentools.puppet import BuiltinPuppet

    pup = BuiltinPuppet("beanie")

    def render(pose):
        img = QImage(200, 240, QImage.Format_ARGB32)
        img.fill(0)
        p = QPainter(img)
        pup.draw(p, QRectF(0, 0, 200, 240), 0.0, 0.0, 0.0, pose)
        p.end()
        return img

    base = render(None)
    assert render(Pose()) == base
    assert render(Pose(look_x=1.0)) != base
    assert render(Pose(point_amount=1.0, point_angle=-math.pi / 4, point_side="right")) != base


# ---------------------------------------------------------------- subtitle toggle
def test_subtitle_style_remembered_from_old_config(tmp_path):
    (tmp_path / "c.json").write_text('{"subtitles": "bottom"}')
    c = Config.load(tmp_path / "c.json")
    assert c.subtitle_style == "bottom"


def test_paused_transcriber_skips_whisper():
    import queue as _q

    import time as _t

    q = _q.Queue()
    backend = FakeBackend()
    tr = Transcriber(q, lambda: backend, gate_db=-50)
    tr.paused = True
    tr.start()
    audio = np.concatenate([_tone(1.5), np.zeros(16000, np.float32)])
    for i in range(0, len(audio), 320):
        q.put(audio[i:i + 320])
    deadline = _t.time() + 2
    while not q.empty() and _t.time() < deadline:
        _t.sleep(0.01)
    _t.sleep(0.2)
    tr.stop()
    assert backend.calls == [] and tr.out.empty()
