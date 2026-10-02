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
    b._paid = [time.monotonic() - 16]
    b._last_any = time.monotonic() - 16
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


def _live_trader():
    cfg = SimpleNamespace(account_index="1", api_key_index="4", quota_reserve=100, max_paid_tx_per_min=10,
                          dry_run=False, api_key_private_key="k", base_url="https://x")
    return Trader(cfg)


def test_auth_token_age_uses_wall_clock():
    # 2026-10-01: monotonic clock stopped during laptop sleep -> expired token reused -> 401s.
    t = _live_trader()
    minted = []

    class Client:
        def create_auth_token_with_expiry(self, api_key_index):
            minted.append(1)
            return f"tok{len(minted)}", None
    t._client = Client()
    assert t._auth() == {"Authorization": "tok1"}
    assert t._auth() == {"Authorization": "tok1"}          # cached
    t._token_at -= 20 * 60                                  # 20 real minutes passed (e.g. asleep)
    assert t._auth() == {"Authorization": "tok2"}


def test_get_retries_once_on_401_with_fresh_token():
    import asyncio
    t = _live_trader()
    seen = []

    class Client:
        def create_auth_token_with_expiry(self, api_key_index):
            return f"tok{len(seen)}", None

    class Resp:
        def __init__(self, status): self.status = status
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        def raise_for_status(self):
            if self.status >= 400: raise RuntimeError(self.status)
        async def json(self, content_type=None): return {"code": 200, "ok": True}

    class Http:
        def get(self, url, params, headers):
            seen.append(headers["Authorization"])
            return Resp(401 if len(seen) == 1 else 200)
    t._client, t._http = Client(), Http()
    assert asyncio.run(t._get("/x", {}))["ok"]
    assert len(seen) == 2 and seen[0] != seen[1]


def test_tx_timeout_returns_error_instead_of_hanging(monkeypatch):
    import asyncio
    import src.trader as tr
    monkeypatch.setattr(tr, "TX_TIMEOUT_S", 0.05)
    t = _live_trader()

    async def hang():
        await asyncio.sleep(10)
    _, resp, err = asyncio.run(t._tx(hang()))
    assert resp is None and "Timeout" in err


def test_cancel_takes_the_free_slot():
    # Below reserve, a paid tx is only "free" if NOTHING (not even a cancel) went out in 15s.
    b = TxBudget(reserve=100, max_per_min=50)
    b.record(SimpleNamespace(volume_quota_remaining=50))
    b._paid, b._last_any = [], time.monotonic() - 20
    assert b.can_spend() and b.free_slot_ready()
    b.note_tx()                       # a cancel just went out
    assert not b.free_slot_ready() and not b.can_spend()


def test_cancel_all_counts_as_quota_tx():
    import asyncio
    t = _live_trader()

    class Client:
        async def cancel_all_orders(self, **kw):
            return None, SimpleNamespace(code=200, volume_quota_remaining=77), None
    t._client = Client()
    assert asyncio.run(t.arm_dead_mans_switch(330))
    assert t.budget.quota_remaining == 77 and len(t.budget._paid) == 1


def test_market_overrides_and_validation():
    import dataclasses
    import pytest
    from src.config import CONFIG
    c = dataclasses.replace(CONFIG, markets=("ANTHROPIC", "AMD"), max_order_usd=20, max_position_usd=40,
                            market_params={"ANTHROPIC": {"max_order_usd": 50, "max_position_usd": 100,
                                                         "min_edge_bps": 0.5}}, hard_max_order_usd=0)
    assert c.for_market("ANTHROPIC", "max_order_usd") == 50 and c.for_market("AMD", "max_order_usd") == 20
    assert c.for_market("ANTHROPIC", "min_edge_bps") == 0.5
    assert c.order_ceiling_usd() == 100
    c.validate()
    with pytest.raises(ValueError):
        dataclasses.replace(c, market_params={"X": {"max_order_usd": 50, "max_position_usd": 10}}).validate()
    with pytest.raises(ValueError):
        dataclasses.replace(c, market_params={"X": {"oops": 1}}).validate()


def test_hard_ceiling_refuses_oversized_orders():
    import asyncio
    t = _live_trader()
    t.cfg.order_ceiling_usd = lambda: 50.0
    t.specs = {92: MarketSpec(92, "XAU", 4, 2, 0.0015, 10.0, "active", False, 0.0666)}

    class Client:
        async def create_order(self, **kw):
            raise AssertionError("must not be sent")
    t._client = Client()
    assert asyncio.run(t.place("XAU", 92, False, 418500, 500)) is None   # 0.05 XAU = $209
    assert t.consecutive_tx_errors == 1


def test_pong_keeps_feed_healthy():
    from src.feed import MarketFeed
    f = MarketFeed("wss://x", [1])
    f.connected, f.last_msg_at = True, time.monotonic() - 30
    assert not f.healthy(10)
    f._on_message({"type": "pong"})
    assert f.healthy(10)
