"""Mouse awareness: puppets follow the cursor with their eyes and point at clicks.

* Gaze: the cursor position is polled every frame (works everywhere Qt
  runs). While the mouse moves, the eyes turn toward it; once it rests
  they drift back to looking straight ahead. A critically damped spring
  makes the eyes glide instead of snapping.
* Pointing: clicks anywhere on screen need a global hook (pynput). On
  macOS that asks for Input Monitoring permission; on Linux it works on
  X11/XWayland. Without it the puppet still points at clicks on itself.
"""
from __future__ import annotations

import math
import queue
import sys
from dataclasses import dataclass


@dataclass
class Pose:
    look_x: float = 0.0          # -1 (left) .. 1 (right)
    look_y: float = 0.0          # -1 (up) .. 1 (down)
    point_amount: float = 0.0    # 0 = arm at rest, 1 = fully raised
    point_angle: float = 0.0     # radians, screen space (0 = right, +pi/2 = down)
    point_side: str = "right"    # which arm


class Spring2D:
    """Critically damped spring: smooth, no overshoot, ~`settle` seconds to arrive."""

    def __init__(self, settle: float = 0.35):
        self.k = (4.0 / settle) ** 2  # stiffness for a critically damped settle time
        self.x = self.y = self.vx = self.vy = 0.0

    def update(self, tx: float, ty: float, dt: float):
        dt = min(dt, 0.05)
        c = 2.0 * math.sqrt(self.k)
        self.vx += (self.k * (tx - self.x) - c * self.vx) * dt
        self.vy += (self.k * (ty - self.y) - c * self.vy) * dt
        self.x += self.vx * dt
        self.y += self.vy * dt
        return self.x, self.y


def _ease_out_back(x: float) -> float:
    """Overshoots a touch then settles — a snappy cartoon 'pop'."""
    c1, c3 = 1.4, 2.4
    return 1 + c3 * (x - 1) ** 3 + c1 * (x - 1) ** 2


class PointAnim:
    """Raise → hold → lower, timed to be snappy but clearly visible."""

    RAISE, HOLD, LOWER = 0.18, 1.3, 0.35

    def __init__(self):
        self.t0: float | None = None
        self.angle = 0.0
        self.side = "right"

    def trigger(self, now: float, angle: float, side: str):
        if self.t0 is not None and side == self.side and now - self.t0 < self.RAISE + self.HOLD:
            # already pointing with this arm: swing to the new target, restart the hold
            self.t0 = now - self.RAISE
        else:
            self.t0 = now
        self.angle, self.side = angle, side

    def amount(self, now: float) -> float:
        if self.t0 is None:
            return 0.0
        k = now - self.t0
        if k < self.RAISE:
            return max(0.0, _ease_out_back(k / self.RAISE))
        k -= self.RAISE
        if k < self.HOLD:
            return 1.0
        k -= self.HOLD
        if k < self.LOWER:
            x = k / self.LOWER
            return 1.0 - x * x
        self.t0 = None
        return 0.0

    @property
    def active(self) -> bool:
        return self.t0 is not None


class ClickListener:
    """Global left-clicks via pynput, delivered as (x, y) in physical pixels."""

    def __init__(self):
        self.clicks: queue.Queue = queue.Queue()
        self.error: str | None = None
        self._listener = None

    def start(self):
        try:
            from pynput import mouse
        except Exception as e:  # not installed, or no X server
            self.error = f"Pointing at clicks needs pynput on X11/macOS ({e.__class__.__name__}: {e})"
            return

        def on_click(x, y, button, pressed):
            if pressed and button == mouse.Button.left:
                self.clicks.put((x, y))

        try:
            self._listener = mouse.Listener(on_click=on_click)
            self._listener.daemon = True
            self._listener.start()
        except Exception as e:
            self.error = f"Can't listen to mouse clicks: {e}"
            return
        if sys.platform == "darwin" and not getattr(self._listener, "IS_TRUSTED", True):
            self.error = ("To point at your clicks, allow your terminal in System Settings → "
                          "Privacy & Security → Input Monitoring (and Accessibility), then restart.")

    def stop(self):
        if self._listener is not None:
            try:
                self._listener.stop()
            except Exception:
                pass
            self._listener = None


class PointerTracker:
    """Turns cursor motion and clicks into a Pose for the puppet."""

    def __init__(self, idle_after: float = 0.9, reach: float = 260.0):
        self.idle_after = idle_after    # seconds of stillness before looking straight again
        self.reach = reach              # px: cursor farther than this = eyes fully turned
        self.gaze = Spring2D(0.35)
        self.point = PointAnim()
        self._last_cursor = None
        self._last_move = -1e9
        self._look_target = None        # global point to look at while pointing

    def click(self, now: float, target, shoulders: dict, eyes):
        """target/shoulders/eyes are global (x, y) tuples."""
        side = "left" if target[0] < eyes[0] else "right"
        sx, sy = shoulders[side]
        angle = math.atan2(target[1] - sy, target[0] - sx)
        self.point.trigger(now, angle, side)
        self._look_target = target

    def update(self, now: float, dt: float, cursor, eyes) -> Pose:
        if self._last_cursor is not None and cursor != self._last_cursor:
            dx, dy = cursor[0] - self._last_cursor[0], cursor[1] - self._last_cursor[1]
            if abs(dx) + abs(dy) > 1:
                self._last_move = now
        self._last_cursor = cursor

        amount = self.point.amount(now)
        if self.point.active and self._look_target is not None:
            target = self._look_target
        elif now - self._last_move < self.idle_after:
            target = cursor
        else:
            target = None

        tx = ty = 0.0
        if target is not None:
            vx, vy = target[0] - eyes[0], target[1] - eyes[1]
            dist = math.hypot(vx, vy)
            if dist > 1:
                strength = min(1.0, dist / self.reach)
                tx, ty = vx / dist * strength, vy / dist * strength
        lx, ly = self.gaze.update(tx, ty, dt)
        return Pose(lx, ly, amount, self.point.angle, self.point.side)
