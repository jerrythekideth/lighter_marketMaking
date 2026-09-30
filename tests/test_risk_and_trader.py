import datetime as dt
import time
from types import SimpleNamespace

from src.risk import DailyLoss, MarketGuard, book_problem, recent_trips
from src.trader import MarketSpec, Trader, TxBudget


def test_daily_loss_persists_and_latches(tmp_path):
    path = str(tmp_path / "state.json")
    d1 = dt.datetime(2026, 9, 30, 1, 0, tzinfo=dt.timezone.utc)
    dl = DailyLoss(10, path=path)
    dl.load()
    assert dl.update(100.0, d1) == 0
    assert dl.update(95.0, d1) == -5 and not dl.halted
    # restart same day: start equity survives
    dl2 = DailyLoss(10, path=path)
    dl2.load()
    assert dl2.update(95.0, d1) == -5
    assert dl2.update(89.9, d1) < -10 + 0.2 and dl2.halted
    # recovery same day doesn't un-halt
    dl2.update(100.0, d1)
    assert dl2.halted
    dl3 = DailyLoss(10, path=path)
    dl3.load()
    assert dl3.halted
    # next UTC day resets
    dl3.update(90.0, d1 + dt.timedelta(days=1))
    assert not dl3.halted and dl3.start_equity == 90.0


def test_book_problem():
    assert book_problem(None, 1.0, 1.0, 150, 100) == "one-sided/empty book"
    assert book_problem(1.0, 1.0, 1.0, 150, 100) == "crossed book"
    assert "spread" in book_problem(100, 103, 101.5, 150, 100)
    assert book_problem(100, 100.1, None, 150, 100) == "no mark price yet"
    assert "from mark" in book_problem(100, 100.1, 102, 150, 100)
    assert book_problem(100, 100.1, 100.05, 150, 100) is None


def test_guard_and_systemic_count():
    g = {1: MarketGuard(), 2: MarketGuard(), 3: MarketGuard()}
    assert not g[1].paused
    g[1].pause(60, "x")
    g[2].pause(60, "y")
    assert g[1].paused and recent_trips(g) == 2


def test_tx_budget_rate_and_reserve():
    b = TxBudget(reserve=100, max_per_min=3)
    assert b.can_spend()
    for _ in range(3):
        b.record(SimpleNamespace(volume_quota_remaining=500))
    assert not b.can_spend()           # per-minute cap
    b = TxBudget(reserve=100, max_per_min=50)
    b.record(SimpleNamespace(volume_quota_remaining=90))
    assert not b.can_spend()           # at/below reserve: only the 15s free slot
    b._sent = [time.monotonic() - 16]
    assert b.can_spend()
    b.record(SimpleNamespace(volume_quota_remaining=None))  # free tx doesn't report quota
    assert b.quota_remaining == 90


def _trader_with(spec):
    cfg = SimpleNamespace(account_index="1", api_key_index="4", quota_reserve=100, max_paid_tx_per_min=10,
                          dry_run=True, api_key_private_key="")
    t = Trader(cfg)
    t.specs = {spec.market_id: spec}
    return t


def test_sizing_minimums_and_bump():
    xau = MarketSpec(92, "XAU", 4, 2, 0.0015, 10.0, "active", False, 0.0666)
    t = _trader_with(xau)
    assert t.base_int_for(92, 20, 4185.0) == 47          # 0.0047 XAU
    assert t.base_int_for(92, 8, 4185.0) == 24           # bumped to $10 min (0.0024), within 1.5x of $8
    assert t.base_int_for(92, 8, 4185.0, allow_bump=False) is None
    assert t.base_int_for(92, 5, 4185.0) is None         # $10 > 1.5 * $5


def test_price_rounding_away_from_mid():
    xau = MarketSpec(92, "XAU", 4, 2, 0.0015, 10.0, "active", False, 0.0666)
    t = _trader_with(xau)
    assert t.to_price_int(92, 4185.123, is_ask=False) == 418512
    assert t.to_price_int(92, 4185.123, is_ask=True) == 418513
    assert t.to_price_int(92, 4185.12, is_ask=True) == 418512   # exact: no spurious bump


def test_spec_tradable():
    assert MarketSpec(1, "A", 1, 1, 1, 1, "active", False, 0.1).tradable
    assert not MarketSpec(1, "A", 1, 1, 1, 1, "inactive", False, 0.1).tradable
    assert not MarketSpec(1, "A", 1, 1, 1, 1, "active", True, 0.1).tradable
