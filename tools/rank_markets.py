#!/usr/bin/env python3
"""Rank this week's Liquidity Partner Program markets for a small account. Read-only.

Run every Monday after 17:00 UTC (when Lighter publishes the new weights) and pick MARKETS
from the top of the list:

    .venv/bin/python tools/rank_markets.py

Source: the public spreadsheet linked from docs.lighter.xyz/liquidity-partner-program
(newest weekly tab first). For each rewarded market it prints the weekly LIT pool, current
book spread, $ competing within 2bps of mid (rewards decay exponentially away from the top
of book, so this is roughly who we split the pool with), 1h volatility (our inventory risk),
max leverage, and whether src/sessions.py knows the market's trading hours.

`LIT/$comp` = weekly LIT per $1k of competing near-top liquidity: higher = more reward per
dollar quoted. It's a crude proxy — Lighter doesn't publish per-asset caps or the decay rate.
"""
import json
import math
import os
import statistics
import sys
import time

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.rewards import current_week  # noqa: E402
from src.sessions import ASSET_CLASS  # noqa: E402

BASE = "https://mainnet.zklighter.elliot.ai"


def vol_1h_bps(market_id: int) -> float:
    now = int(time.time() * 1000)
    c = requests.get(f"{BASE}/api/v1/candles", params={
        "market_id": market_id, "resolution": "1h", "start_timestamp": now - 168 * 3600_000,
        "end_timestamp": now, "count_back": 168}, timeout=15).json().get("c") or []
    closes = [float(x["c"]) for x in c if x.get("c")]
    rets = [math.log(b / a) * 1e4 for a, b in zip(closes, closes[1:]) if a > 0 and b > 0]
    return statistics.pstdev(rets) if len(rets) > 24 else float("nan")


def main() -> None:
    week, rewards = current_week()
    details = {m["symbol"]: m for m in requests.get(f"{BASE}/api/v1/orderBookDetails", timeout=20)
               .json()["order_book_details"]}
    lit_px = None
    try:
        lit = details["LIT"]
        lit_px = float(lit.get("last_trade_price") or 0) or None
    except KeyError:
        pass
    print(f"week: {week}   total pool: {sum(rewards.values()):,.0f} LIT"
          + (f" (~${sum(rewards.values()) * lit_px:,.0f} at ${lit_px:.2f})" if lit_px else ""))
    rows = []
    for sym, lit_wk in rewards.items():
        m = details.get(sym)
        if m is None:
            rows.append((sym, lit_wk, None))
            continue
        ob = requests.get(f"{BASE}/api/v1/orderBookOrders", params={"market_id": m["market_id"], "limit": 100},
                          timeout=15).json()
        bids, asks = ob.get("bids") or [], ob.get("asks") or []
        info = {"cls": ASSET_CLASS.get(sym, "?"), "status": m["status"],
                "fro": (m.get("market_config") or {}).get("force_reduce_only"),
                "lev": 10000 / m["default_initial_margin_fraction"]}
        if bids and asks:
            bb, ba = float(bids[0]["price"]), float(asks[0]["price"])
            mid = (bb + ba) / 2
            near = sum(float(l["price"]) * float(l["remaining_base_amount"]) for l in bids + asks
                       if abs(float(l["price"]) - mid) / mid * 1e4 <= 2)
            info.update(spread=(ba - bb) / mid * 1e4, near=near, vol=vol_1h_bps(m["market_id"]))
            info["score"] = lit_wk / max(near / 1000, 1)
        rows.append((sym, lit_wk, info))
        time.sleep(0.25)

    print(f"\n{'symbol':12}{'LIT/wk':>8}{'spread':>8}{'$<=2bp':>11}{'vol1h':>7}{'lev':>5}  "
          f"{'LIT/$comp':>9}  class / notes")
    for sym, lit_wk, i in sorted(rows, key=lambda r: -(r[2] or {}).get("score", -1)):
        if i is None:
            print(f"{sym:12}{lit_wk:8.0f}   (no such market on Lighter)")
            continue
        if "spread" not in i:
            print(f"{sym:12}{lit_wk:8.0f}   (empty book) {i['cls']}")
            continue
        notes = [i["cls"]]
        if i["cls"] in ("?", "unsupported"):
            notes.append("NOT QUOTABLE (no session model)")
        if i["status"] != "active" or i["fro"]:
            notes.append("NOT TRADABLE")
        if i["vol"] > 100:
            notes.append("very volatile")
        print(f"{sym:12}{lit_wk:8.0f}{i['spread']:8.2f}{i['near']:11,.0f}{i['vol']:7.1f}{i['lev']:5.0f}  "
              f"{i['score']:9.1f}  {' / '.join(notes)}")


if __name__ == "__main__":
    main()
