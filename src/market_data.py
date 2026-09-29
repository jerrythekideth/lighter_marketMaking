"""Public, no-auth Lighter market data. Safe to call with zero API keys.

Endpoints used (all public, verified live on 2026-09-29):
  GET {base_url}/api/v1/orderBooks
  GET {base_url}/api/v1/orderBookOrders?market_id=<id>&limit=<n>
"""
from __future__ import annotations

import requests

DEPTH_THRESHOLDS_USD = (10_000, 30_000, 100_000, 300_000)


def fetch_order_books(base_url: str) -> list[dict]:
    r = requests.get(f"{base_url}/api/v1/orderBooks", timeout=10)
    r.raise_for_status()
    return r.json()["order_books"]


def build_symbol_map(base_url: str) -> dict[str, int]:
    """symbol -> market_id, built fresh each call (ids are stable but don't hardcode them)."""
    return {b["symbol"]: b["market_id"] for b in fetch_order_books(base_url)}


def fetch_book(base_url: str, market_id: int, limit: int = 50) -> tuple[list[dict], list[dict]]:
    r = requests.get(
        f"{base_url}/api/v1/orderBookOrders",
        params={"market_id": market_id, "limit": limit},
        timeout=10,
    )
    r.raise_for_status()
    data = r.json()
    return data.get("bids", []), data.get("asks", [])


def _cum_depth_usd(levels: list[dict], threshold_usd: float) -> float:
    """USD notional resting at or better than the point where cumulative depth crosses threshold.
    Returns the actual cumulative USD reached within the levels we fetched (capped by `limit`)."""
    cum = 0.0
    for lvl in levels:
        price = float(lvl["price"])
        size = float(lvl["remaining_base_amount"])
        cum += price * size
        if cum >= threshold_usd:
            return cum
    return cum  # ran out of levels before hitting threshold


def analyze_book(bids: list[dict], asks: list[dict]) -> dict:
    if not bids or not asks:
        return {"mid": None, "spread_bps": None, "depth": {}}

    best_bid = float(bids[0]["price"])
    best_ask = float(asks[0]["price"])
    mid = (best_bid + best_ask) / 2
    spread_bps = (best_ask - best_bid) / mid * 1e4

    depth = {}
    for t in DEPTH_THRESHOLDS_USD:
        depth[f"bid_{t}"] = _cum_depth_usd(bids, t)
        depth[f"ask_{t}"] = _cum_depth_usd(asks, t)

    return {
        "best_bid": best_bid,
        "best_ask": best_ask,
        "mid": mid,
        "spread_bps": spread_bps,
        "depth": depth,
        "top_bid_account": bids[0].get("owner_account_index"),
        "top_ask_account": asks[0].get("owner_account_index"),
    }


def snapshot_market(base_url: str, symbol: str, market_id: int, limit: int = 50) -> dict:
    bids, asks = fetch_book(base_url, market_id, limit=limit)
    analysis = analyze_book(bids, asks)
    analysis["symbol"] = symbol
    analysis["market_id"] = market_id
    return analysis
