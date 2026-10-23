"""Immutable calendar trial policy and fail-closed host-clock checks."""
from datetime import datetime, timedelta, timezone
from time import monotonic

from .const import BENCH_CUTOFF, TEST_RETENTION_SECONDS

CUTOFF = datetime.fromisoformat(BENCH_CUTOFF)
CLOCK_FLOOR = datetime(2026, 10, 20, tzinfo=timezone.utc)


class ClockError(RuntimeError):
    pass


class TrustedClock:
    """Use HA host UTC; reject implausible, rolled-back or jumping clocks.

    This does not invent an NTP observation. HA OS must maintain its host clock.
    A persisted high-water mark and monotonic comparison catch unsafe changes;
    an operator must repair host time before restarting a clock-blocked runtime.
    """
    def __init__(self, now=None, elapsed=None):
        self.source = now or (lambda: datetime.now(timezone.utc))
        self.elapsed = elapsed or monotonic
        self.high_water = None
        self.previous = None
        self.previous_elapsed = None
        self.error = None

    def read(self):
        if self.error:
            raise ClockError(self.error)
        now, tick = self.source(), self.elapsed()
        if not isinstance(now, datetime) or now.tzinfo is None or not CLOCK_FLOOR <= now <= datetime(2036, 1, 1, tzinfo=timezone.utc):
            self.error = "HA host UTC is unavailable or implausible"
        elif self.high_water and now < self.high_water - timedelta(seconds=5):
            self.error = "HA host UTC moved behind its persisted high-water mark"
        elif self.previous and abs((now - self.previous).total_seconds() - (tick - self.previous_elapsed)) > 5:
            self.error = "HA host UTC changed discontinuously; collection blocked"
        if self.error:
            raise ClockError(self.error)
        self.previous, self.previous_elapsed = now, tick
        self.high_water = max(now, self.high_water or now)
        return now


def expiration(info):
    """The original retention basis survives retries and never moves to now."""
    value = info.get("expires_at")
    if value:
        result = datetime.fromisoformat(value)
        if result.tzinfo is None:
            raise ValueError("expiry lacks UTC offset")
        return result
    value = info.get("captured_at") if info.get("capture_clock_synchronized") else info.get("received_at")
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("retention basis lacks UTC offset")
    return result + timedelta(seconds=TEST_RETENTION_SECONDS)
