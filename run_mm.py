#!/usr/bin/env python3
"""Market-making loop for Lighter's Liquidity Partner Program.

Every LOOP_SECONDS:
  1. Account-wide checks: STOP file, daily loss (persisted, per UTC day), consecutive tx
     errors, market-data feed health, gross exposure, free margin, systemic breaker.
  2. Per market, in order, any "no" pulls that market's quotes (cancels are free):
       market status (active, not force-reduce-only) -> session blackout (src/sessions.py)
       -> book sanity (spread, mid vs mark) -> volatility breaker -> pause cooldown.
  3. Target = join the best bid/ask (step inside if the spread is wide), no closer to mid
     than a volatility-scaled edge, skewed against inventory. Sides that would breach the
     position cap are dropped; in reduce-only (pre-blackout / exposure caps) only the side
     that shrinks the position is quoted, sized to the position.
  4. Reconcile with resting bot orders: modify when the target moved > REQUOTE_BPS, place
     when missing, cancel extras. Creates/modifies spend volume quota and go through
     TxBudget; when budget is short, orders that became dangerous are cancelled (free).

Safety nets: dead man's switch (Lighter scheduled cancel-all, 330s, refreshed every 60s);
Ctrl-C / kill / STOP file / halt all cancel bot orders on the way out. Only bot-tagged
orders are ever touched (see src/trader.py).

Start:  nohup .venv/bin/python -u run_mm.py >> data/mm_live.log 2>&1 &
Stop:   touch data/STOP   (or: pkill -f run_mm.py)
"""
import asyncio
import datetime as dt
import logging
import os
import signal
import time

from src.config import CONFIG
from src.feed import MarketFeed
from src.risk import DailyLoss, MarketGuard, book_problem, recent_trips
from src.sessions import ASSET_CLASS, BLACKOUT, REDUCE_ONLY, session_state
from src.strategy import compute_quote, half_edge_bps, is_dangerous, needs_requote, sides_allowed
from src.trader import BotOrder, Trader

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("mm")

ROOT = os.path.dirname(os.path.abspath(__file__))
STOP_FILE = os.path.join(ROOT, "data", "STOP")

DMS_SECONDS = 330          # Lighter's minimum is 300s out
DMS_REFRESH_SECONDS = 60
SPECS_REFRESH_SECONDS = 300
VOL_REFRESH_SECONDS = 3600
HEARTBEAT_SECONDS = 300
ACCOUNT_STALE_SECONDS = 30
# How long a just-sent place/cancel is trusted over a refresh that may predate it.
RECENT_TX_SECONDS = 5
# Wall-clock gap between loop steps that means the machine slept or the process stalled.
# (macOS's monotonic clock stops during sleep, so monotonic-based staleness can't see it.)
WAKE_GAP_SECONDS = 20


class Bot:
    def __init__(self, trader: Trader, active: list[tuple[str, int]]):
        self.t = trader
        self.cfg = trader.cfg
        self.active = active
        self.active_ids = {m for _, m in active}
        self.feed = MarketFeed(self.cfg.ws_url, [m for _, m in active])
        self.daily = DailyLoss(self.cfg.daily_loss_limit_usd)
        self.guards = {m: MarketGuard() for _, m in active}
        self.vols: dict[int, float | None] = {}
        self.status: dict[int, str] = {}
        self.acct = None
        self.orders: dict[int, list[BotOrder]] = {}
        self.last_account_ok = 0.0
        self.pulled_all = False
        self._timers: dict[str, float] = {}
        # client_order_index -> (monotonic time, order) for orders we just placed, and
        # client_order_index -> monotonic time for orders we just cancelled. A refresh can race
        # a tx by a moment; these keep us from double-placing or re-touching a dead order.
        self._recent_placed: dict[int, tuple[float, BotOrder]] = {}
        self._recent_cancelled: dict[int, float] = {}
        self._last_wall = time.time()

    # --- small helpers ---

    def _due(self, name: str, every: float) -> bool:
        now = time.monotonic()
        if now - self._timers.get(name, 0.0) >= every:
            self._timers[name] = now
            return True
        return False

    def _set_status(self, symbol: str, market_id: int, status: str) -> None:
        if self.status.get(market_id) != status:
            self.status[market_id] = status
            log.info("%s -> %s", symbol, status)

    async def _pull_market(self, symbol: str, market_id: int, why: str) -> None:
        for o in list(self.orders.get(market_id, [])):
            await self._cancel_local(symbol, market_id, o, why)

    async def _pull_all(self, why: str) -> None:
        # Retries every loop (cancels are free and only hit orders we still see); logs once.
        if not self.pulled_all:
            log.warning("pulling all quotes: %s", why)
            self.pulled_all = True
        for symbol, m in self.active:
            await self._pull_market(symbol, m, why)

    # --- periodic refreshes ---

    async def _refresh_vols(self) -> None:
        for symbol, m in self.active:
            try:
                self.vols[m] = await self.t.vol_1h_bps(m)
            except Exception as e:
                log.warning("vol refresh %s failed: %s", symbol, e)
        log.info("1h vol (bps): %s", {s: round(self.vols.get(m) or 0, 1) for s, m in self.active})

    async def _refresh_account(self) -> None:
        try:
            acct = await self.t.account_snapshot()
            orders = await self.t.bot_orders()
        except Exception as e:
            log.warning("account refresh failed: %s", e)
            return
        now = time.monotonic()
        self._recent_placed = {k: v for k, v in self._recent_placed.items() if now - v[0] < RECENT_TX_SECONDS}
        self._recent_cancelled = {k: t for k, t in self._recent_cancelled.items() if now - t < RECENT_TX_SECONDS}
        seen = set()
        for m in list(orders):
            orders[m] = [o for o in orders[m] if o.client_order_index not in self._recent_cancelled]
            seen.update(o.client_order_index for o in orders[m])
        for coi, (_, o) in self._recent_placed.items():
            if coi not in seen and coi not in self._recent_cancelled:
                orders.setdefault(o.market_id, []).append(o)
        self.acct, self.orders, self.last_account_ok = acct, orders, now

    def _heartbeat(self, pnl: float) -> None:
        pos = {s: round(self.acct.position(m).value_usd, 2) for s, m in self.active
               if abs(self.acct.position(m).value_usd) > 0.01}
        log.info(
            "HEARTBEAT equity=$%.2f day_pnl=$%.2f free=$%.2f gross=$%.2f quota=%s pos=%s states=%s feed_reconnects=%d",
            self.acct.total_asset_value, pnl, self.acct.available_balance, self.acct.gross_exposure_usd,
            self.t.budget.quota_remaining, pos,
            {s: self.status.get(m, "?") for s, m in self.active}, self.feed.reconnects,
        )

    # --- per-market reconcile ---

    async def _cancel_local(self, symbol: str, m: int, order: BotOrder, why: str) -> None:
        await self.t.cancel(symbol, order, why)
        self._recent_cancelled[order.client_order_index] = time.monotonic()
        self._recent_placed.pop(order.client_order_index, None)
        self.orders[m] = [o for o in self.orders.get(m, []) if o.client_order_index != order.client_order_index]

    async def _reconcile_side(self, symbol: str, m: int, is_ask: bool, target: int | None,
                              size_usd: float, reduce_only: bool) -> None:
        # self.orders is refreshed from the exchange every ACCOUNT_REFRESH_S and kept in sync
        # locally in between (place/modify/cancel below), so a fast loop can't double-place.
        side_orders = [o for o in self.orders.get(m, []) if o.is_ask == is_ask]
        for extra in side_orders[1:]:
            await self._cancel_local(symbol, m, extra, "(duplicate)")
        cur = side_orders[0] if side_orders else None
        spec = self.t.specs[m]

        base_int = None
        if target is not None:
            base_int = self.t.base_int_for(m, size_usd, target / 10 ** spec.price_decimals,
                                           allow_bump=not reduce_only)
        if target is None or base_int is None:
            if cur:
                await self._cancel_local(symbol, m, cur, "(side off)")
            return

        tgt_px = target / 10 ** spec.price_decimals
        want_base = base_int / 10 ** spec.size_decimals
        if cur is None:
            if self.t.budget.can_spend():
                placed = await self.t.place(symbol, m, is_ask, target, base_int)
                if placed:
                    self.orders.setdefault(m, []).append(placed)
                    self._recent_placed[placed.client_order_index] = (time.monotonic(), placed)
            return

        stale_price = needs_requote(cur.price, tgt_px, self.cfg.requote_bps)
        stale_size = cur.remaining_base < 0.5 * want_base or cur.remaining_base > 1.5 * want_base
        if not (stale_price or stale_size):
            return
        if self.t.budget.can_spend():
            if await self.t.modify(symbol, cur, target, base_int):
                cur.price, cur.remaining_base = tgt_px, want_base
        elif is_dangerous(is_ask, cur.price, tgt_px, self.cfg.requote_bps):
            await self._cancel_local(symbol, m, cur, "(moved against us, no quota to modify)")

    async def _market(self, symbol: str, m: int, now_utc: dt.datetime, global_reduce: str | None,
                      systemic: bool) -> None:
        spec = self.t.specs.get(m)
        st = self.feed.markets[m]
        guard = self.guards[m]

        if spec is None or not spec.tradable:
            self._set_status(symbol, m, f"OFF (market {spec.status if spec else 'missing'}"
                             f"{', force-reduce-only' if spec and spec.force_reduce_only else ''})")
            return await self._pull_market(symbol, m, "(market not tradable)")
        sess, why = session_state(symbol, now_utc, self.cfg.session_lead_minutes)
        if sess == BLACKOUT:
            self._set_status(symbol, m, f"BLACKOUT ({why})")
            return await self._pull_market(symbol, m, "(session blackout)")
        problem = book_problem(st.bid, st.ask, st.mark, self.cfg.max_book_spread_bps,
                               self.cfg.max_mark_divergence_bps)
        if problem:
            self._set_status(symbol, m, f"WAIT ({problem})")
            return await self._pull_market(symbol, m, f"({problem})")

        vol = self.vols.get(m)
        if vol is None:
            self._set_status(symbol, m, "WAIT (no volatility estimate yet)")
            return await self._pull_market(symbol, m, "(no vol estimate)")
        trip_bps = max(self.cfg.breaker_bps_min, self.cfg.breaker_vol_mult * vol)
        move = st.max_move_bps(self.cfg.breaker_window_s)
        if move > trip_bps and not guard.paused:
            guard.pause(self.cfg.breaker_pause_s, f"moved {move:.0f}bps in {self.cfg.breaker_window_s:.0f}s")
            log.warning("%s BREAKER: %s (limit %.0f) — out for %.0fs", symbol, guard.reason, trip_bps,
                        self.cfg.breaker_pause_s)
        if systemic and not guard.paused:
            guard.pause(self.cfg.breaker_pause_s * 2, "systemic breaker")
        if guard.paused:
            self._set_status(symbol, m, f"PAUSED ({guard.reason})")
            return await self._pull_market(symbol, m, "(breaker)")

        pos = self.acct.position(m).value_usd
        reduce_reason = why if sess == REDUCE_ONLY else global_reduce
        edge = half_edge_bps(vol, self.cfg.min_edge_bps, self.cfg.edge_vol_mult)
        q = compute_quote(st.bid, st.ask, st.mid, spec.price_decimals, edge, pos,
                          self.cfg.max_position_usd, self.cfg.skew_bps_at_cap, self.cfg.improve_spread_bps)
        bid_ok, ask_ok = sides_allowed(pos, self.cfg.max_order_usd, self.cfg.max_position_usd,
                                       reduce_reason is not None)
        size = min(self.cfg.max_order_usd, abs(pos)) if reduce_reason else self.cfg.max_order_usd
        self._set_status(symbol, m, f"REDUCE-ONLY ({reduce_reason})" if reduce_reason else "QUOTING")

        reduce = reduce_reason is not None
        await self._reconcile_side(symbol, m, False, q.bid if bid_ok else None, size, reduce)
        await self._reconcile_side(symbol, m, True, q.ask if ask_ok else None, size, reduce)

    # --- main loop ---

    async def run(self) -> None:
        self.daily.load()
        await self._refresh_account()
        if self.acct is None:
            log.error("could not read account — not starting")
            return
        pnl = self.daily.update(self.acct.total_asset_value)
        if self.daily.halted:
            log.error("daily loss limit already hit today (pnl $%.2f) — not starting until next UTC day", pnl)
            return
        log.info("equity $%.2f, day start $%.2f, day pnl $%.2f, loss limit $%.2f",
                 self.acct.total_asset_value, self.daily.start_equity, pnl, self.cfg.daily_loss_limit_usd)

        # Clear out any bot orders from a previous run before arming anything.
        if not await self.t.cancel_all_bot_orders("(startup cleanup)"):
            log.error("could not confirm startup cleanup — not starting")
            return
        self.orders = {}
        if not await self.t.arm_dead_mans_switch(DMS_SECONDS):
            log.error("could not arm dead man's switch — refusing to quote without it")
            return
        self._timers["dms"] = time.monotonic()

        feed_task = asyncio.create_task(self.feed.run())
        try:
            await self._refresh_vols()
            self._timers["vols"] = self._timers["specs"] = self._timers["heartbeat"] = time.monotonic()
            # Give the feed a moment to deliver snapshots before the first decisions.
            for _ in range(20):
                if self.feed.healthy(self.cfg.feed_max_silence_s):
                    break
                await asyncio.sleep(0.5)
            self._last_wall = time.time()  # startup can be slow; don't mistake it for a sleep
            await self._loop()
        finally:
            feed_task.cancel()

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self.cfg.loop_seconds)
            if not await self.step():
                return

    async def step(self) -> bool:
        """One loop iteration. Returns False when the bot should shut down."""
        wall = time.time()
        gap, self._last_wall = wall - self._last_wall, wall
        if gap > WAKE_GAP_SECONDS:
            log.warning("loop stalled %.0fs (machine slept?) — pulling quotes, resyncing everything", gap)
            await self._pull_all("woke from sleep/stall")
            self.feed.reset()
            self.last_account_ok = 0.0          # force a fresh account read before quoting
            self._timers["dms"] = 0.0           # re-arm the dead man's switch now
            self._timers["account"] = 0.0
            self._recent_placed.clear()
            return True
        if os.path.exists(STOP_FILE):
            log.warning("STOP file found — shutting down")
            return False
        if self._due("dms", DMS_REFRESH_SECONDS):
            if not await self.t.arm_dead_mans_switch(DMS_SECONDS):
                self._timers["dms"] = 0.0  # retry next loop
        if self._due("specs", SPECS_REFRESH_SECONDS):
            try:
                await self.t.load_specs()
            except Exception as e:
                log.warning("specs refresh failed: %s", e)
        if self._due("vols", VOL_REFRESH_SECONDS):
            await self._refresh_vols()
        if self._due("account", self.cfg.account_refresh_s):
            await self._refresh_account()

        if time.monotonic() - self.last_account_ok > ACCOUNT_STALE_SECONDS:
            await self._pull_all("account data stale")
            return True
        pnl = self.daily.update(self.acct.total_asset_value)
        if self.daily.halted:
            log.error("DAILY LOSS LIMIT HIT: pnl $%.2f <= -$%.2f — halting until next UTC day",
                      pnl, self.cfg.daily_loss_limit_usd)
            return False
        if self.t.consecutive_tx_errors >= self.cfg.max_tx_errors:
            log.error("%d consecutive transaction errors — halting (API change? check the log)",
                      self.t.consecutive_tx_errors)
            return False
        if not self.feed.healthy(self.cfg.feed_max_silence_s):
            await self._pull_all("market data feed down")
            return True
        self.pulled_all = False

        # Orphans: bot-tagged orders in markets we're not running (e.g. config changed).
        for m, orders in list(self.orders.items()):
            if m not in self.active_ids:
                spec = self.t.specs.get(m)
                for o in orders:
                    await self.t.cancel(spec.symbol if spec else str(m), o, "(not in MARKETS)")
                self.orders[m] = []

        global_reduce = None
        if self.acct.gross_exposure_usd > self.cfg.max_gross_usd:
            global_reduce = f"gross ${self.acct.gross_exposure_usd:.0f} > ${self.cfg.max_gross_usd:.0f}"
        elif self.acct.available_balance < self.cfg.min_free_margin_frac * self.acct.total_asset_value:
            global_reduce = f"free margin ${self.acct.available_balance:.0f} low"
        systemic = recent_trips(self.guards) >= self.cfg.systemic_trip_count

        now_utc = dt.datetime.now(dt.timezone.utc)
        # Rotate the starting market each loop so the tx budget is shared fairly.
        self._rr = (getattr(self, "_rr", -1) + 1) % len(self.active)
        for symbol, m in self.active[self._rr:] + self.active[:self._rr]:
            try:
                await self._market(symbol, m, now_utc, global_reduce, systemic)
            except Exception:
                log.exception("market %s cycle failed — pulling its quotes", symbol)
                try:
                    await self._pull_market(symbol, m, "(error)")
                except Exception:
                    log.exception("pull %s failed", symbol)

        if self._due("heartbeat", HEARTBEAT_SECONDS):
            self._heartbeat(pnl)
        return True


def select_markets(trader: Trader) -> list[tuple[str, int]]:
    by_symbol = {s.symbol: s for s in trader.specs.values()}
    active = []
    for sym in CONFIG.markets:
        spec = by_symbol.get(sym)
        cls = ASSET_CLASS.get(sym)
        if spec is None:
            log.error("%s: no such market — skipping", sym)
        elif cls is None or cls == "unsupported":
            log.error("%s: no session model in src/sessions.py — skipping (add it there first)", sym)
        else:
            active.append((sym, spec.market_id))
    return active


async def main() -> None:
    log.info("base_url=%s markets=%s dry_run=%s", CONFIG.base_url, CONFIG.markets, CONFIG.dry_run)
    if os.path.exists(STOP_FILE):
        log.error("data/STOP exists — delete it to start")
        return
    if CONFIG.is_mainnet() and not CONFIG.dry_run:
        log.warning("MAINNET + LIVE ORDERS")

    trader = Trader(CONFIG)
    await trader.connect()
    try:
        await trader.load_specs()
        active = select_markets(trader)
        if not active:
            log.error("no valid markets, check MARKETS in .env")
            return
        bot = Bot(trader, active)

        task = asyncio.current_task()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, task.cancel)

        try:
            await bot.run()
        except asyncio.CancelledError:
            pass
        finally:
            log.info("shutting down — cancelling bot orders")
            try:
                clear = await trader.cancel_all_bot_orders("(shutdown)")
            except Exception:
                clear = False
                log.exception("cancel on shutdown failed")
            # Only drop the safety net once we've confirmed our orders are gone.
            if clear:
                await trader.disarm_dead_mans_switch()
            else:
                log.warning("leaving dead man's switch armed — it will cancel all orders within ~5 min")
    finally:
        await trader.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
