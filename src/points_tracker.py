"""CSV logging so we can compute real $/point after each Friday distribution.

Two logs:
  data/market_snapshots.csv  — written by run_monitor.py, no keys needed
  data/points_log.csv        — hand-updated weekly from the Lighter points dashboard
                                 (or wired up later if/when an authed points endpoint exists)
"""
from __future__ import annotations

import csv
import os
from datetime import datetime, timezone

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")

MARKET_SNAPSHOT_FIELDS = [
    "timestamp", "symbol", "market_id", "best_bid", "best_ask", "mid", "spread_bps",
    "bid_10000", "ask_10000", "bid_30000", "ask_30000",
    "bid_100000", "ask_100000", "bid_300000", "ask_300000",
    "top_bid_account", "top_ask_account",
]

POINTS_LOG_FIELDS = [
    "week_ending", "maker_volume_usd", "fees_paid_usd", "mm_points", "retail_points",
    "usd_per_point",
]


def _ensure_file(path: str, fields: list[str]) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    if not os.path.exists(path):
        with open(path, "w", newline="") as f:
            csv.DictWriter(f, fieldnames=fields).writeheader()


def log_market_snapshot(snapshot: dict) -> None:
    path = os.path.join(DATA_DIR, "market_snapshots.csv")
    _ensure_file(path, MARKET_SNAPSHOT_FIELDS)
    depth = snapshot.get("depth", {})
    row = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "symbol": snapshot.get("symbol"),
        "market_id": snapshot.get("market_id"),
        "best_bid": snapshot.get("best_bid"),
        "best_ask": snapshot.get("best_ask"),
        "mid": snapshot.get("mid"),
        "spread_bps": snapshot.get("spread_bps"),
        "top_bid_account": snapshot.get("top_bid_account"),
        "top_ask_account": snapshot.get("top_ask_account"),
    }
    for k, v in depth.items():
        row[k] = v
    with open(path, "a", newline="") as f:
        csv.DictWriter(f, fieldnames=MARKET_SNAPSHOT_FIELDS).writerow(row)


def log_points_week(
    week_ending: str,
    maker_volume_usd: float,
    fees_paid_usd: float,
    mm_points: float,
    retail_points: float,
) -> None:
    """Call this by hand once a week after checking the Lighter points dashboard."""
    path = os.path.join(DATA_DIR, "points_log.csv")
    _ensure_file(path, POINTS_LOG_FIELDS)
    total_points = mm_points + retail_points
    usd_per_point = (fees_paid_usd / total_points) if total_points else None
    row = {
        "week_ending": week_ending,
        "maker_volume_usd": maker_volume_usd,
        "fees_paid_usd": fees_paid_usd,
        "mm_points": mm_points,
        "retail_points": retail_points,
        "usd_per_point": usd_per_point,
    }
    with open(path, "a", newline="") as f:
        csv.DictWriter(f, fieldnames=POINTS_LOG_FIELDS).writerow(row)
    return row
