"""Guards that decide, per market and account-wide, whether the bot may quote.

Everything here fails closed: when in doubt, the answer is "don't quote" (cancels are free
and always allowed; missing a snapshot costs a little reward, carrying a bad fill costs money).
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import time
from dataclasses import dataclass, field

log = logging.getLogger("risk")

STATE_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "state.json")


@dataclass
class DailyLoss:
    """Loss limit measured from equity at the start of the current UTC day, persisted to
    disk so a restart doesn't reset it. A breach latches until the next UTC day."""
    limit_usd: float
    path: str = STATE_PATH
    day: str = ""
    start_equity: float = 0.0
    halted: bool = False

    def load(self) -> None:
        try:
            with open(self.path) as f:
                s = json.load(f)
            self.day, self.start_equity, self.halted = s["day"], float(s["start_equity"]), bool(s["halted"])
        except (FileNotFoundError, KeyError, ValueError, json.JSONDecodeError):
            pass

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"day": self.day, "start_equity": self.start_equity, "halted": self.halted}, f)
        os.replace(tmp, self.path)

    def update(self, equity: float, now_utc: dt.datetime | None = None) -> float:
        """Roll the day if needed, latch a breach, return today's pnl."""
        today = (now_utc or dt.datetime.now(dt.timezone.utc)).strftime("%Y-%m-%d")
        if today != self.day:
            self.day, self.start_equity, self.halted = today, equity, False
            self._save()
            log.info("new UTC day %s, start equity $%.2f", today, equity)
        pnl = equity - self.start_equity
        if not self.halted and pnl <= -abs(self.limit_usd):
            self.halted = True
            self._save()
        return pnl


@dataclass
class MarketGuard:
    """Per-market circuit breaker state."""
    paused_until: float = 0.0
    reason: str = ""
    trips: list = field(default_factory=list)  # monotonic times of breaker trips

    def pause(self, seconds: float, reason: str) -> None:
        now = time.monotonic()
        self.paused_until = max(self.paused_until, now + seconds)
        self.reason = reason
        self.trips.append(now)

    @property
    def paused(self) -> bool:
        return time.monotonic() < self.paused_until


def recent_trips(guards: dict[int, MarketGuard], window_s: float = 120) -> int:
    cutoff = time.monotonic() - window_s
    return sum(1 for g in guards.values() if any(t >= cutoff for t in g.trips))


def book_problem(bid: float | None, ask: float | None, mark: float | None,
                 max_spread_bps: float, max_mark_div_bps: float) -> str | None:
    """Reason not to trust this book right now, or None."""
    if bid is None or ask is None:
        return "one-sided/empty book"
    if ask <= bid:
        return "crossed book"
    mid = (bid + ask) / 2
    spread = (ask - bid) / mid * 1e4
    if spread > max_spread_bps:
        return f"book spread {spread:.0f}bps > {max_spread_bps:.0f}"
    if mark is None:
        return "no mark price yet"
    div = abs(mid - mark) / mark * 1e4
    if div > max_mark_div_bps:
        return f"mid {div:.0f}bps from mark"
    return None
