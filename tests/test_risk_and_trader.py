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


def test_flatten_closes_every_position_reduce_only_with_slippage_cap():
    import asyncio
    from src.trader import AccountSnapshot, Position

    xau = MarketSpec(92, "XAU", 4, 2, 0.0015, 10.0, "active", False, 0.0666)
    eur = MarketSpec(96, "EURUSD", 1, 5, 5.0, 10.0, "active", False, 0.05)
    cfg = SimpleNamespace(account_index="1", api_key_index="4", quota_reserve=100, max_paid_tx_per_min=10,
                          dry_run=False, api_key_private_key="k")
    t = Trader(cfg)
    t.specs = {92: xau, 96: eur}
    positions = {92: Position(0.0018, 7.5, 0), 96: Position(-17.6, -20.0, 0)}
    sent = []

    class Client:
        async def create_market_order(self, **kw):
            sent.append(kw)
            positions.pop(kw["market_index"], None)   # exchange fills it
            return None, SimpleNamespace(code=200, volume_quota_remaining=500), None

    async def snap():
        return AccountSnapshot(100, 90, dict(positions), 0)

    async def get(path, params):
        return {"bids": [{"price": "4000.00"}], "asks": [{"price": "4000.10"}]} if params["market_id"] == "92" \
            else {"bids": [{"price": "1.13400"}], "asks": [{"price": "1.13410"}]}

    t._client, t.account_snapshot, t._get = Client(), snap, get
    assert asyncio.run(t.flatten_all_positions((100,))) is True
    by_m = {k["market_index"]: k for k in sent}
    assert by_m[92]["is_ask"] is True and by_m[92]["reduce_only"] is True and by_m[92]["base_amount"] == 18
    assert by_m[92]["avg_execution_price"] == 396000          # 4000.00 * 0.99, sell
    assert by_m[96]["is_ask"] is False and by_m[96]["base_amount"] == 176
    assert by_m[96]["avg_execution_price"] == 114545          # ceil(1.13410 * 1.01 * 1e5), buy


def test_flatten_reports_leftovers():
    import asyncio
    from src.trader import AccountSnapshot, Position

    xau = MarketSpec(92, "XAU", 4, 2, 0.0015, 10.0, "active", False, 0.0666)
    cfg = SimpleNamespace(account_index="1", api_key_index="4", quota_reserve=100, max_paid_tx_per_min=10,
                          dry_run=False, api_key_private_key="k")
    t = Trader(cfg)
    t.specs = {92: xau}

    class Client:
        async def create_market_order(self, **kw):   # never fills (e.g. book too thin within cap)
            return None, SimpleNamespace(code=200, volume_quota_remaining=500), None

    async def snap():
        return AccountSnapshot(100, 90, {92: Position(0.0018, 7.5, 0)}, 0)

    async def get(path, params):
        return {"bids": [{"price": "4000.00"}], "asks": [{"price": "4000.10"}]}

    async def no_sleep(_):
        return None
    t._client, t.account_snapshot, t._get = Client(), snap, get
    orig = asyncio.sleep
    asyncio.sleep = no_sleep
    try:
        assert asyncio.run(t.flatten_all_positions((100, 300))) is False
    finally:
        asyncio.sleep = orig
