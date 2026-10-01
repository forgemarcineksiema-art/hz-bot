from __future__ import annotations

import threading
import time


class RealClock:
    def now(self) -> float:
        return time.time()

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds)


class VirtualClock:
    """Clock for simulation and tests: sleeping only advances the counter."""

    def __init__(self, start: float = 1_790_000_000.0):
        self.t = start

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            self.t += seconds


class StopRequested(Exception):
    """Raised from ``sleep`` when the controlling thread asked the bot to stop."""


class ControlledClock:
    """Real-time clock (optionally sped up) whose sleep can be interrupted."""

    def __init__(self, stop: threading.Event, speed: float = 1.0):
        self.stop = stop
        self.speed = speed
        self._real0 = time.time()

    def now(self) -> float:
        return self._real0 + (time.time() - self._real0) * self.speed

    def sleep(self, seconds: float) -> None:
        if self.stop.wait(max(0.0, seconds) / self.speed):
            raise StopRequested
