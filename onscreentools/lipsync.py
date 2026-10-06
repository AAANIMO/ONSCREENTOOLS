"""Turns microphone level into a cartoon mouth opening."""
from __future__ import annotations

import math
import random


def rms_to_db(rms: float) -> float:
    return 20.0 * math.log10(max(rms, 1e-9))


class MouthDriver:
    """Envelope follower with a noise gate.

    Fast attack / slower release gives the snappy "flap" of cutout animation.
    A little flutter while the mouth is open keeps long vowels alive.
    """

    def __init__(self, gate_db: float = -50.0, range_db: float = 28.0,
                 attack: float = 35.0, release: float = 14.0, flutter: float = 0.12):
        self.gate_db = gate_db
        self.range_db = range_db
        self.attack = attack
        self.release = release
        self.flutter = flutter
        self.value = 0.0
        self._phase = random.random() * 10

    def target(self, rms: float) -> float:
        norm = (rms_to_db(rms) - self.gate_db) / max(self.range_db, 1e-3)
        return min(1.0, max(0.0, norm))

    def update(self, rms: float, dt: float) -> float:
        t = self.target(rms)
        if t > 0.05:
            self._phase += dt * 17.0
            t *= 1.0 - self.flutter * (0.5 + 0.5 * math.sin(self._phase))
        rate = self.attack if t > self.value else self.release
        self.value += (t - self.value) * min(1.0, dt * rate)
        if self.value < 0.02:
            self.value = 0.0
        return self.value

    @property
    def talking(self) -> bool:
        return self.value > 0.08


def quantize(openness: float, levels: int) -> int:
    """Pick a mouth frame index 0..levels-1 (0 = closed)."""
    if levels <= 1:
        return 0
    if openness < 0.08:
        return 0
    return min(levels - 1, 1 + int(openness * (levels - 1)))
