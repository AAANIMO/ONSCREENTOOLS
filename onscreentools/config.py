"""Settings, persisted as JSON in the platform's config dir."""
from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path


def config_dir() -> Path:
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "onscreentools"


def cache_dir() -> Path:
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Caches"
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return base / "onscreentools"


@dataclass
class Config:
    # what's on screen
    mascot: str = "puppet"                # "puppet" | "camera"
    puppet: str = "beanie"                # builtin preset name, or a path to a puppet folder
    mascot_size: int = 320                # mascot height in px

    # camera
    camera_index: int = 0
    camera_width: int = 1280
    camera_height: int = 720
    mirror: bool = True
    bg_removal: str = "ai"                # "none" | "chroma" | "ai"
    chroma_color: list = field(default_factory=lambda: [0, 177, 64])
    chroma_tolerance: int = 40            # 0..150, distance in CbCr space
    chroma_softness: int = 25             # 0..100
    spill: float = 0.8                    # 0..1 green/blue spill suppression
    camera_shape: str = "full"            # "full" | "circle" | "rounded"
    camera_zoom: float = 1.0              # crop in on the camera frame (1.0 = whole frame)

    # audio / lipsync
    audio_device: str | None = None       # sounddevice name or index; None = system default
    follow_mouse: bool = True             # puppet's eyes follow the cursor while it moves
    point_on_click: bool = True           # puppet points at where you click (global hook)
    noise_gate_db: float = -50.0          # below this the mouth stays shut
    mouth_range_db: float = 28.0          # dB above the gate for a fully open mouth

    # subtitles
    subtitles: str = "bubble"             # "off" | "bottom" | "bubble"
    subtitle_style: str = "bubble"        # style used when subtitles are toggled back on
    whisper_backend: str = "auto"         # "auto" | "faster-whisper" | "mlx"
    whisper_model: str = "small"
    language: str | None = "it"           # spoken language ("it", "en", ...); None = auto-detect (unreliable)
    translate: bool = False               # translate to English
    font_family: str = ""                 # "" = pick a comic-ish font automatically
    font_size: int = 22
    uppercase: bool = True                # comic lettering
    hide_after: float = 2.5               # seconds after you stop talking
    bubble_side: str = "right"            # side of the mascot the bubble sits on

    # window
    window_bg: str = "transparent"        # "transparent" | "#00b140" | any colour
    click_through: bool = False
    window_pos: list | None = None
    vcam: bool = False                    # also publish as virtual camera (pyvirtualcam)

    config_version: int = 2

    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        path = path or config_dir() / "config.json"
        cfg = cls()
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            return cfg
        known = {f.name for f in fields(cls)}
        for k, v in data.items():
            if k in known:
                setattr(cfg, k, v)
        if cfg.subtitles in ("bubble", "bottom"):
            cfg.subtitle_style = cfg.subtitles
        if data.get("config_version", 1) < 2:
            # v1 defaulted to auto-detect, which hops between languages mid-sentence
            if data.get("language") is None:
                cfg.language = "it"
            cfg.config_version = 2
        return cfg

    def save(self, path: Path | None = None) -> None:
        path = path or config_dir() / "config.json"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(asdict(self), indent=2))
        except OSError as e:
            print(f"[onscreen] could not save config: {e}", file=sys.stderr)
