#!/usr/bin/env python3
"""The market-making loop.

Each cycle, per market:
  1. Sync real account state (position, total asset value) from Lighter.
  2. Daily loss check vs. the value at startup — breach cancels bot orders and exits.
  3. Build the quote ladder around mid, skewed against inventory (short -> quotes shift up so
     the bid buys back sooner); drop the bid if long at the cap, the ask if short.
  4. If the bot's resting orders already match (same sides, within REQUOTE_BPS of target),
     leave them — otherwise cancel the bot's orders and re-place.

Only bot-tagged orders are ever cancelled (see src/trader.py). Ctrl-C cancels them on exit.
A dead man's switch (Lighter scheduled cancel-all, refreshed every DMS_REFRESH_SECONDS) pulls
all resting orders ~5-6 min after the process dies or the machine sleeps.
With DRY_RUN=true nothing is sent; it just logs what it would do.
"""
import asyncio
import logging
import signal
import time

from src.config import CONFIG
from src.market_data import build_symbol_map, snapshot_market
from src.strategy import generate_quotes, skewed_mid
from src.trader import BotOrder, Trader

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("mm")

DMS_SECONDS = 330          # Lighter's minimum is 300s out
DMS_REFRESH_SECONDS = 60


def _orders_match(current: list[BotOrder], wanted: list, requote_bps: float) -> bool:
    if len(current) != len(wanted):
        return False
    cur = sorted(current, key=lambda o: (o.is_ask, o.price))
    want = sorted(wanted, key=lambda q: (q.side == "ask", q.price))
    for o, q in zip(cur, want):
        if o.is_ask != (q.side == "ask"):
            return False
        if abs(o.price - q.price) / q.price * 1e4 > requote_bps:
            return False
    return True


async def run(trader: Trader, active: list[tuple[str, int]]) -> None:
    start_value = trader.account_snapshot().total_asset_value
    log.info("starting account value $%.2f, daily loss limit $%.2f", start_value, CONFIG.daily_loss_limit_usd)

    if not await trader.arm_dead_mans_switch(DMS_SECONDS):
        log.error("could not arm dead man's switch — refusing to quote without it")
        return
    last_armed = time.monotonic()

    while True:
        if time.monotonic() - last_armed >= DMS_REFRESH_SECONDS:
            if await trader.arm_dead_mans_switch(DMS_SECONDS):
                last_armed = time.monotonic()

        acct = trader.account_snapshot()
        pnl = acct.total_asset_value - start_value
        if pnl <= -abs(CONFIG.daily_loss_limit_usd):
            log.error("LOSS LIMIT HIT: pnl $%.2f — cancelling bot orders and stopping", pnl)
            return

        for symbol, market_id in active:
            try:
                snap = snapshot_market(CONFIG.base_url, symbol, market_id)
                if snap["mid"] is None:
                    continue

                _, net_usd = acct.positions.get(market_id, (0.0, 0.0))
                size = CONFIG.max_order_usd
                center = skewed_mid(snap["mid"], net_usd, CONFIG.max_position_usd, CONFIG.skew_bps_at_cap)
                quotes = generate_quotes(
                    center, CONFIG.min_spread_bps, CONFIG.max_spread_bps, CONFIG.order_levels, size
                )
                # Per-side position cap: stop adding to a position at the cap, keep quoting the side that reduces it.
                quotes = [
                    q for q in quotes
                    if (q.side == "bid" and net_usd + size <= CONFIG.max_position_usd)
                    or (q.side == "ask" and net_usd - size >= -CONFIG.max_position_usd)
                ]

                current = await trader.bot_orders(market_id)
                if current and _orders_match(current, quotes, CONFIG.requote_bps):
                    continue

                log.info(
                    "%s mid=%.5f book_spread=%.1fbps pos=$%.2f skew=%+.1fbps pnl=$%.2f -> requote %d orders",
                    symbol, snap["mid"], snap["spread_bps"], net_usd,
                    (center / snap["mid"] - 1) * 1e4, pnl, len(quotes),
                )
                await trader.cancel_bot_orders(symbol, market_id, current)
                for q in quotes:
                    await trader.place_order(symbol, market_id, q.side, q.price, q.size_usd)
            except Exception:
                log.exception("cycle failed for %s", symbol)

        await asyncio.sleep(CONFIG.poll_interval_seconds)


async def main() -> None:
    log.info(
        "base_url=%s markets=%s dry_run=%s mainnet=%s",
        CONFIG.base_url, CONFIG.markets, CONFIG.dry_run, CONFIG.is_mainnet(),
    )
    if CONFIG.is_mainnet() and not CONFIG.dry_run:
        log.warning("MAINNET + LIVE ORDERS")

    trader = Trader(CONFIG)
    trader.load_specs()
    symbol_map = build_symbol_map(CONFIG.base_url)
    active = [(s, symbol_map[s]) for s in CONFIG.markets if s in symbol_map]
    if not active:
        log.error("no valid markets, check MARKETS in .env against live symbols")
        return

    # Ctrl-C / kill both cancel the run task so the finally block below always pulls our orders.
    task = asyncio.current_task()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, task.cancel)

    try:
        await run(trader, active)
    except asyncio.CancelledError:
        pass
    finally:
        log.info("shutting down — cancelling bot orders")
        all_clear = True
        for symbol, market_id in active:
            try:
                await trader.cancel_bot_orders(symbol, market_id)
                await asyncio.sleep(1)
                if await trader.bot_orders(market_id):
                    all_clear = False
            except Exception:
                all_clear = False
                log.exception("cancel on shutdown failed for %s — check the Lighter UI", symbol)
        # Only drop the safety net once we've confirmed our orders are gone.
        if all_clear:
            await trader.disarm_dead_mans_switch()
        else:
            log.warning("leaving dead man's switch armed — it will cancel all orders within ~5 min")
        await trader.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
