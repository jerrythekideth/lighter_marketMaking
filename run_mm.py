#!/usr/bin/env python3
"""Phase 1/2. The actual market-making loop.

With DRY_RUN=true (the .env.example default) this runs end-to-end against LIVE public
market data, computes the exact quotes it would place and checks risk limits against a
purely local, in-memory position (starts flat, no real account state), and logs every
"would place / would cancel" via Trader's dry-run path. Nothing touches your funds.

Flipping DRY_RUN=false requires LIGHTER_ACCOUNT_INDEX / API_KEY_INDEX / API_KEY_PRIVATE_KEY
in .env AND requires src/trader.py's place_order/cancel_all to be implemented first — see
README "Order placement — needs verification before Phase 1". Until that's done, DRY_RUN=false
will raise NotImplementedError on the first quoting cycle rather than guess at an order call.
"""
import logging
import time

from src.config import CONFIG
from src.market_data import build_symbol_map, snapshot_market
from src.strategy import PositionState, check_risk_limits, clamp_order_size, generate_quotes
from src.trader import Trader

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("mm")


def main() -> None:
    log.info(
        "base_url=%s markets=%s dry_run=%s mainnet=%s",
        CONFIG.base_url, CONFIG.markets, CONFIG.dry_run, CONFIG.is_mainnet(),
    )
    if CONFIG.is_mainnet() and not CONFIG.dry_run:
        log.warning(
            "MAINNET + LIVE ORDERS. Confirm this is intentional (Phase 2) — "
            "Phase 0/1 should stay on testnet and/or DRY_RUN=true."
        )

    trader = Trader(CONFIG)
    symbol_map = build_symbol_map(CONFIG.base_url)
    active = [(s, symbol_map[s]) for s in CONFIG.markets if s in symbol_map]
    if not active:
        log.error("no valid markets, check MARKETS in .env against live symbols")
        return

    # Local-only position state. Real position sync from the account requires the
    # authed account endpoints — add once Trader is fully implemented.
    states = {symbol: PositionState() for symbol, _ in active}

    while True:
        for symbol, market_id in active:
            try:
                snap = snapshot_market(CONFIG.base_url, symbol, market_id)
            except Exception:
                log.exception("failed to snapshot %s", symbol)
                continue
            if snap["mid"] is None:
                continue

            state = states[symbol]
            ok, reason = check_risk_limits(state, CONFIG.max_position_usd, CONFIG.daily_loss_limit_usd)
            if not ok:
                log.warning("%s risk limit breached (%s), cancelling and skipping", symbol, reason)
                trader.cancel_all(symbol, market_id)
                continue

            size = clamp_order_size(CONFIG.max_order_usd, CONFIG.max_order_usd, state, CONFIG.max_position_usd)
            if size <= 0:
                log.info("%s no headroom to quote (position at cap)", symbol)
                continue

            quotes = generate_quotes(
                snap["mid"], CONFIG.min_spread_bps, CONFIG.max_spread_bps, CONFIG.order_levels, size
            )
            trader.cancel_all(symbol, market_id)
            for q in quotes:
                trader.place_order(symbol, market_id, q.side, q.price, q.size_usd)

        time.sleep(CONFIG.poll_interval_seconds)


if __name__ == "__main__":
    main()
