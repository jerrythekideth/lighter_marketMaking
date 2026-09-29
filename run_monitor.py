#!/usr/bin/env python3
"""Phase 0. Safe to run right now — no API keys needed, no orders placed.

Polls the configured MARKETS' public order books, computes spread + depth at the
$10k/$30k/$100k/$300k bands, prints a line per market per cycle, and appends every
snapshot to data/market_snapshots.csv for later analysis.
"""
import logging
import time

from src.config import CONFIG
from src.market_data import build_symbol_map, snapshot_market
from src.points_tracker import log_market_snapshot

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("monitor")


def main() -> None:
    base_url = CONFIG.market_data_base_url
    log.info("base_url=%s markets=%s", base_url, CONFIG.markets)
    symbol_map = build_symbol_map(base_url)

    missing = [s for s in CONFIG.markets if s not in symbol_map]
    if missing:
        log.warning("symbols not found on this book, skipping: %s", missing)

    active = [(s, symbol_map[s]) for s in CONFIG.markets if s in symbol_map]
    if not active:
        log.error("no valid markets to monitor, check MARKETS in .env against live symbols")
        return

    while True:
        for symbol, market_id in active:
            try:
                snap = snapshot_market(base_url, symbol, market_id)
            except Exception:
                log.exception("failed to snapshot %s", symbol)
                continue
            log.info(
                "%-8s mid=%.6f spread=%.2fbps depth100k(bid/ask)=%.0f/%.0f top_acct(bid/ask)=%s/%s",
                symbol,
                snap["mid"] or 0.0,
                snap["spread_bps"] or 0.0,
                snap["depth"].get("bid_100000", 0.0),
                snap["depth"].get("ask_100000", 0.0),
                snap.get("top_bid_account"),
                snap.get("top_ask_account"),
            )
            log_market_snapshot(snap)
        time.sleep(CONFIG.poll_interval_seconds)


if __name__ == "__main__":
    main()
