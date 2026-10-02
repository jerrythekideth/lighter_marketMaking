"""Drives run_mm.Bot.step() against a fake exchange + fake feed (no network), to check the
failure paths: feed down, STOP file, blackout, breaker, loss limit, tx errors, caps, orphans."""
import asyncio
import dataclasses
import datetime as dt
import time

import pytest

import run_mm
from src.config import CONFIG
from src.feed import MarketState
from src.risk import DailyLoss
from src.sessions import BLACKOUT, OPEN, REDUCE_ONLY
from src.trader import AccountSnapshot, BotOrder, MarketSpec, Position, Trader

SOL = MarketSpec(2, "SOL", 3, 3, 0.05, 10.0, "active", False, 0.0666)
ETH = MarketSpec(0, "ETH", 4, 2, 0.005, 10.0, "active", False, 0.05)


class FakeTrader(Trader):
    def __init__(self, cfg):
        super().__init__(cfg)
        self.specs = {SOL.market_id: SOL, ETH.market_id: ETH}
        self.acct = AccountSnapshot(100.0, 90.0, {}, time.monotonic())

    async def account_snapshot(self):
        self.acct.fetched_at = time.monotonic()
        return self.acct

    async def load_specs(self):
        pass

    async def vol_1h_bps(self, market_id, hours=168):
        return 20.0

    async def arm_dead_mans_switch(self, seconds):
        return True


class FakeFeed:
    def __init__(self, ids):
        self.markets = {m: MarketState() for m in ids}
        self.ok = True
        self.reconnects = 0

    def healthy(self, max_silence_s):
        return self.ok

    def set_book(self, m, bid, ask, mark=None):
        st = self.markets[m]
        st.bid, st.ask = bid, ask
        st.mark = mark if mark is not None else (bid + ask) / 2
        st.mids.append((time.monotonic(), (bid + ask) / 2))


@pytest.fixture
def bot(tmp_path, monkeypatch):
    cfg = dataclasses.replace(CONFIG, dry_run=True, markets=("SOL",), max_order_usd=20, max_position_usd=40,
                              max_gross_usd=150, daily_loss_limit_usd=10, min_edge_bps=1.0, edge_vol_mult=0.1,
                              requote_bps=1.0, max_paid_tx_per_min=100, account_refresh_s=0, api_key_private_key="")
    monkeypatch.setattr(run_mm, "STOP_FILE", str(tmp_path / "STOP"))
    monkeypatch.setattr(run_mm, "session_state", lambda sym, now, lead: (OPEN, ""))
    t = FakeTrader(cfg)
    b = run_mm.Bot(t, [("SOL", SOL.market_id)])
    b.feed = FakeFeed([SOL.market_id])
    b.daily = DailyLoss(10, path=str(tmp_path / "state.json"))
    b.vols = {SOL.market_id: 20.0}
    b.feed.set_book(SOL.market_id, 120.000, 120.010)
    return b


def run(coro):
    return asyncio.run(coro)


def bot_orders(b):
    return run(b.t.bot_orders()).get(SOL.market_id, [])


def test_normal_quotes_both_sides(bot):
    assert run(bot.step())
    orders = bot_orders(bot)
    assert sorted(o.is_ask for o in orders) == [False, True]
    bid = next(o for o in orders if not o.is_ask)
    ask = next(o for o in orders if o.is_ask)
    # edge = max(1, 0.1*20) = 2bps from mid 120.005
    assert bid.price <= 120.005 * (1 - 2e-4) + 1e-9 and ask.price >= 120.005 * (1 + 2e-4) - 1e-9
    assert bot.status[SOL.market_id] == "QUOTING"


def test_no_double_place_between_refreshes(bot):
    bot.cfg = dataclasses.replace(bot.cfg, account_refresh_s=999)
    bot._timers["account"] = time.monotonic()
    bot.last_account_ok = time.monotonic()
    bot.acct = bot.t.acct
    for _ in range(5):
        assert run(bot.step())
    assert len(bot_orders(bot)) == 2


def test_feed_down_pulls_everything(bot):
    run(bot.step())
    assert len(bot_orders(bot)) == 2
    bot.feed.ok = False
    assert run(bot.step())
    assert bot_orders(bot) == []
    run(bot.step())  # stays pulled, doesn't re-place
    assert bot_orders(bot) == []


def test_stop_file_stops(bot, tmp_path):
    (tmp_path / "STOP").write_text("")
    assert run(bot.step()) is False


def test_blackout_pulls_market(bot, monkeypatch):
    run(bot.step())
    monkeypatch.setattr(run_mm, "session_state", lambda s, n, l: (BLACKOUT, "US cash open"))
    run(bot.step())
    assert bot_orders(bot) == []
    assert bot.status[SOL.market_id].startswith("BLACKOUT")


def test_reduce_only_quotes_one_side_sized_to_position(bot, monkeypatch):
    bot.t.acct.positions = {SOL.market_id: Position(size=-0.1, value_usd=-12.0, unrealized_pnl=0)}
    monkeypatch.setattr(run_mm, "session_state", lambda s, n, l: (REDUCE_ONLY, "US cash open in 5m"))
    run(bot.step())
    orders = bot_orders(bot)
    assert [o.is_ask for o in orders] == [False]            # short -> only bid
    assert orders[0].remaining_base * orders[0].price <= 12.0 + 1e-6


def test_reduce_only_below_minimum_places_nothing(bot, monkeypatch):
    bot.t.acct.positions = {SOL.market_id: Position(size=-0.05, value_usd=-6.0, unrealized_pnl=0)}
    monkeypatch.setattr(run_mm, "session_state", lambda s, n, l: (REDUCE_ONLY, "x"))
    run(bot.step())
    assert bot_orders(bot) == []   # $6 < $10 min, and bumping would flip the position


def test_position_cap_drops_adding_side(bot):
    bot.t.acct.positions = {SOL.market_id: Position(size=0.25, value_usd=30.0, unrealized_pnl=0)}
    run(bot.step())
    assert [o.is_ask for o in bot_orders(bot)] == [True]   # long $30: a $20 bid would breach $40


def test_breaker_pauses_and_cancels(bot):
    run(bot.step())
    assert len(bot_orders(bot)) == 2
    # 60bps jump inside the window (limit = max(40, 2*20) = 40bps)
    bot.feed.set_book(SOL.market_id, 120.72, 120.73)
    run(bot.step())
    assert bot_orders(bot) == []
    assert bot.status[SOL.market_id].startswith("PAUSED")
    assert bot.guards[SOL.market_id].paused
    run(bot.step())  # still paused, nothing placed
    assert bot_orders(bot) == []


def test_bad_book_waits(bot):
    bot.feed.set_book(SOL.market_id, 120.0, 123.0)  # 250bps spread
    run(bot.step())
    assert bot_orders(bot) == [] and bot.status[SOL.market_id].startswith("WAIT")


def test_daily_loss_halts(bot):
    assert run(bot.step())
    bot.t.acct.total_asset_value = 89.0
    assert run(bot.step()) is False


def test_tx_errors_halt(bot):
    bot.t.consecutive_tx_errors = bot.cfg.max_tx_errors
    assert run(bot.step()) is False


def test_gross_exposure_forces_reduce_only(bot):
    bot.t.acct.positions = {
        SOL.market_id: Position(size=0.1, value_usd=12.0, unrealized_pnl=0),
        ETH.market_id: Position(size=0.05, value_usd=160.0, unrealized_pnl=0),  # not a bot market
    }
    run(bot.step())
    assert [o.is_ask for o in bot_orders(bot)] == [True]
    assert "gross" in bot.status[SOL.market_id]


def test_orphan_orders_cancelled(bot):
    bot.t._paper[123] = BotOrder(-1, 123, ETH.market_id, False, 3000.0, 0.01)
    run(bot.step())
    assert ETH.market_id not in run(bot.t.bot_orders())


def test_account_stale_pulls_all(bot):
    run(bot.step())
    assert len(bot_orders(bot)) == 2

    async def boom():
        raise RuntimeError("network")
    bot.t.account_snapshot = boom
    bot.last_account_ok = time.monotonic() - 60
    assert run(bot.step())
    assert bot_orders(bot) == []


def test_requote_modifies_instead_of_new_order(bot):
    run(bot.step())
    before = {o.client_order_index for o in bot_orders(bot)}
    bot.feed.markets[SOL.market_id].mids.clear()      # not a breaker-sized move
    bot.feed.set_book(SOL.market_id, 120.100, 120.110)  # +8bps
    run(bot.step())
    after = bot_orders(bot)
    assert {o.client_order_index for o in after} == before  # same orders, moved
    assert min(o.price for o in after) > 120.0


def test_fill_then_requote_uses_exchange_truth(bot):
    run(bot.step())
    # Simulate the ask getting filled: exchange no longer has it, position is now short.
    ask = next(o for o in bot_orders(bot) if o.is_ask)
    bot.t._paper.pop(ask.client_order_index)
    bot.t.acct.positions = {SOL.market_id: Position(size=-0.166, value_usd=-20.0, unrealized_pnl=0)}
    run(bot.step())
    orders = bot_orders(bot)
    assert sorted(o.is_ask for o in orders) == [False, True]   # ask re-placed, bid kept


def test_refresh_race_does_not_double_place(bot):
    # Exchange read lags our place: the refresh doesn't show the just-placed orders yet.
    run(bot.step())
    placed = {o.client_order_index for o in bot_orders(bot)}
    real = bot.t.bot_orders

    async def lagging():
        return {}
    bot.t.bot_orders = lagging
    run(bot.step())
    bot.t.bot_orders = real
    assert {o.client_order_index for o in bot_orders(bot)} == placed   # nothing new placed


def test_refresh_race_ignores_just_cancelled(bot, monkeypatch):
    run(bot.step())
    snapshot = {k: list(v) for k, v in run(bot.t.bot_orders()).items()}
    monkeypatch.setattr(run_mm, "session_state", lambda s, n, l: (BLACKOUT, "x"))
    run(bot.step())                      # cancels both

    async def stale():
        return snapshot                  # exchange read still shows the cancelled orders
    bot.t.bot_orders = stale
    run(bot.step())
    assert bot.orders.get(SOL.market_id, []) == []


def test_no_vol_estimate_waits(bot):
    async def no_vol(market_id, hours=168):
        return None                      # e.g. a market listed < 1 day ago
    bot.t.vol_1h_bps = no_vol
    run(bot.step())
    assert bot_orders(bot) == [] and "volatility" in bot.status[SOL.market_id]


def test_wake_from_sleep_pulls_and_resyncs(bot):
    bot.feed.reset = lambda: setattr(bot.feed, "was_reset", True)
    run(bot.step())
    assert len(bot_orders(bot)) == 2
    bot._last_wall -= 600                # 10 minutes vanished between steps
    assert run(bot.step())
    assert bot_orders(bot) == []
    assert getattr(bot.feed, "was_reset", False)
    assert bot.last_account_ok == 0.0 and bot._timers["dms"] == 0.0


def test_status_logs_on_kind_change_only(bot, caplog):
    import logging
    caplog.set_level(logging.INFO, logger="mm")
    bot._set_status("SOL", SOL.market_id, "WAIT (mid 306bps from mark)")
    bot._set_status("SOL", SOL.market_id, "WAIT (mid 302bps from mark)")
    bot._set_status("SOL", SOL.market_id, "QUOTING")
    msgs = [r.getMessage() for r in caplog.records]
    assert msgs == ["SOL -> WAIT (mid 306bps from mark)", "SOL -> QUOTING"]
    assert bot.status[SOL.market_id] == "QUOTING"


def test_loop_survives_step_exception_then_halts_after_sustained_failure(bot, monkeypatch):
    calls = {"n": 0}

    async def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("Operation timed out")        # transient: must not kill the bot
        return False                                      # then a normal stop
    bot.step = flaky
    monkeypatch.setattr(run_mm.asyncio, "sleep", _no_sleep)
    run(bot._loop())
    assert calls["n"] == 2

    calls["n"] = 0

    async def always_fail():
        calls["n"] += 1
        raise OSError("down")
    bot.step = always_fail
    t0 = [1000.0]
    monkeypatch.setattr(run_mm.time, "time", lambda: t0.__setitem__(0, t0[0] + 100) or t0[0])
    run(bot._loop())                                      # returns (halts) instead of looping forever
    assert 3 <= calls["n"] <= 5


async def _no_sleep(_):
    return None


def test_dms_refresh_waits_for_free_slot_but_forced_when_old(bot):
    armed = []

    async def arm(seconds):
        armed.append(seconds)
        return True
    bot.t.arm_dead_mans_switch = arm
    now = time.monotonic()
    bot._timers["dms"] = now - 100           # due, but a tx just went out -> wait for free slot
    bot.t.budget._last_any = now
    bot.feed.ok = False                       # keep the step from placing orders
    run(bot.step())
    assert armed == []
    bot._timers["dms"] = now - 220           # past DMS_FORCE_SECONDS -> arm even if paid
    run(bot.step())
    assert armed == [run_mm.DMS_SECONDS]
