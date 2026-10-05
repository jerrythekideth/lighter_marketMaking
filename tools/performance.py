#!/usr/bin/env python3
"""Full performance report: every bot run, every fill, rewards, cost per LIT. Read-only.

    .venv/bin/python tools/performance.py [--deposit 100]

Sources: data/mm_live.log (run boundaries, markets, equity at each start/heartbeat),
Lighter's trades endpoint (every fill, authenticated with the bot's API key — no tx sent),
1-minute candles (markout: where the price went after each fill), and the Liquidity Partner
spreadsheet (LIT actually paid to this account).
"""
import argparse
import asyncio
import bisect
import collections
import dataclasses
import datetime as dt
import os
import re
import statistics as st
import sys
import time

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from src.config import CONFIG  # noqa: E402
from src.trader import Trader  # noqa: E402

BASE = "https://mainnet.zklighter.elliot.ai"
LOG = os.path.join(ROOT, "data", "mm_live.log")
TS = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)")


def parse_runs() -> list[dict]:
    runs, cur = [], None
    with open(LOG, errors="replace") as f:
        for line in f:
            m = TS.match(line)
            if not m:
                continue
            t = dt.datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
            if "base_url=" in line and "markets=" in line:
                mk = re.search(r"markets=\(([^)]*)\)", line)
                markets = [s.strip(" '") for s in mk.group(1).split(",") if s.strip(" '")] if mk else []
                if "dry_run=True" in line:
                    cur = None
                    continue
                cur = {"start": t, "end": t, "markets": markets, "eq_start": None, "eq_last": None}
                runs.append(cur)
                continue
            if cur is None:
                continue
            cur["end"] = t
            # v2 logs "equity $X" / "equity=$X"; v1 logged "starting account value $X"
            e = re.search(r"(?:^|\s)(?:equity[ =]|account value )\$([\d.]+)", line[20:])
            if e:
                v = float(e.group(1))
                if cur["eq_start"] is None:
                    cur["eq_start"] = v
                cur["eq_last"] = v
    return [r for r in runs if r["eq_start"] is not None]


async def fetch_fills() -> tuple[list[dict], dict[int, str]]:
    t = Trader(dataclasses.replace(CONFIG, dry_run=False))
    await t.connect()
    await t.load_specs()
    out, cursor = [], None
    for _ in range(200):
        p = {"account_index": str(t.account_index), "sort_by": "timestamp", "sort_dir": "desc", "limit": "100"}
        if cursor:
            p["cursor"] = cursor
        d = await t._get("/api/v1/trades", p)
        batch = d.get("trades") or []
        out += batch
        cursor = d.get("next_cursor")
        if not batch or not cursor:
            break
    syms = {m: s.symbol for m, s in t.specs.items()}
    await t.close()
    return sorted(out, key=lambda x: x["timestamp"]), syms


_candles: dict[int, tuple[list, list]] = {}


def mid_at(market_id: int, t_ms: int, start_ms: int) -> float | None:
    if market_id not in _candles:
        rows, end = [], int(time.time() * 1000)
        while end > start_ms:
            c = requests.get(f"{BASE}/api/v1/candles", params={
                "market_id": market_id, "resolution": "1m", "start_timestamp": start_ms,
                "end_timestamp": end, "count_back": 500}, timeout=20).json().get("c") or []
            if not c:
                break
            rows = c + rows
            end = c[0]["t"] - 1
            time.sleep(0.15)
        _candles[market_id] = ([r["t"] for r in rows], [r["c"] for r in rows])
    ts, cl = _candles[market_id]
    i = bisect.bisect_right(ts, t_ms) - 1
    return cl[i] if i >= 0 else None


def lit_paid(address: str) -> tuple[float, list[str], list[str]]:
    from src.rewards import SHEET, results_tabs
    import csv
    import io
    short = f"{address[:6]}...{address[-4:]}".lower()
    total, mine, tabs = 0.0, [], []
    for name, gid in results_tabs():
        tabs.append(name)
        text = requests.get(f"https://docs.google.com/spreadsheets/d/{SHEET}/export?format=csv&gid={gid}",
                            timeout=20).text
        for r in csv.reader(io.StringIO(text)):
            if r and r[0].strip().lower() == short:
                v = float(r[1].replace(",", "") or 0)
                total += v
                mine.append(f"{name}: {v} LIT")
    return total, mine, tabs


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--deposit", type=float, default=100.0, help="total USDC deposited")
    args = ap.parse_args()

    acct = requests.get(f"{BASE}/api/v1/account", params={"by": "index", "value": CONFIG.account_index},
                        timeout=15).json()["accounts"][0]
    equity_now = float(acct["total_asset_value"])
    runs = parse_runs()
    fills, syms = asyncio.run(fetch_fills())
    ai = int(CONFIG.account_index)
    first_ms = fills[0]["timestamp"] if fills else int(time.time() * 1000)
    lit_px = None
    for b in requests.get(f"{BASE}/api/v1/orderBookDetails", timeout=20).json()["order_book_details"]:
        if b["symbol"] == "LIT":
            lit_px = float(b["last_trade_price"])

    # ---- per fill ----
    by_mkt = collections.defaultdict(lambda: dict(n=0, vol=0.0, fee=0.0, taker=0, mk5=[], mk5usd=0.0))
    by_day = collections.defaultdict(lambda: dict(n=0, vol=0.0))
    per_run_fills = collections.defaultdict(lambda: dict(n=0, vol=0.0, fee=0.0, mk5usd=0.0))
    for x in fills:
        s = syms.get(x["market_id"], str(x["market_id"]))
        px, usd = float(x["price"]), float(x["usd_amount"])
        sell = x["ask_account_id"] == ai
        maker = (x["is_maker_ask"] and sell) or (not x["is_maker_ask"] and not sell)
        fee = usd * (0.4e-4 if maker else 2.8e-4)
        a = by_mkt[s]
        a["n"] += 1
        a["vol"] += usd
        a["fee"] += fee
        a["taker"] += 0 if maker else 1
        t = dt.datetime.fromtimestamp(x["timestamp"] / 1000)
        by_day[t.date()]["n"] += 1
        by_day[t.date()]["vol"] += usd
        mk = 0.0
        if maker:
            p5 = mid_at(x["market_id"], x["timestamp"] + 300_000, first_ms - 60_000)
            if p5:
                bps = (-1 if sell else 1) * (p5 - px) / px * 1e4
                a["mk5"].append(bps)
                mk = bps * usd / 1e4
                a["mk5usd"] += mk
        for i, r in enumerate(runs):
            nxt = runs[i + 1]["start"] if i + 1 < len(runs) else dt.datetime.max
            if r["start"] <= t < nxt:
                pr = per_run_fills[i]
                pr["n"] += 1
                pr["vol"] += usd
                pr["fee"] += fee
                pr["mk5usd"] += mk
                break

    total_vol = sum(a["vol"] for a in by_mkt.values())
    total_fee = sum(a["fee"] for a in by_mkt.values())
    pnl = equity_now - args.deposit
    span_days = (dt.datetime.now() - runs[0]["start"]).total_seconds() / 86400 if runs else 0
    run_hours = sum((r["end"] - r["start"]).total_seconds() for r in runs) / 3600

    print("=" * 78)
    print(f"LIGHTER MM PERFORMANCE  (as of {dt.datetime.now():%Y-%m-%d %H:%M})")
    print("=" * 78)
    print(f"Deposited ${args.deposit:.2f} -> equity now ${equity_now:.2f}   net PnL ${pnl:+.2f} "
          f"({pnl / args.deposit * 100:+.1f}%)")
    print(f"{len(runs)} live runs over {span_days:.1f} days, bot live {run_hours:.0f}h "
          f"({run_hours / max(span_days * 24, 1) * 100:.0f}% of the time)")
    print(f"{len(fills)} fills, volume ${total_vol:,.0f}, fees ${total_fee:.2f}  "
          f"(cost per $1k traded: ${-pnl / max(total_vol, 1) * 1000:.2f})")

    print("\nBY RUN (PnL = equity at next start or now, minus equity at start; includes closing costs)")
    print(f"{'start':16}{'hours':>6}  {'markets':34}{'fills':>6}{'vol $':>9}{'fees':>6}{'5m mkout':>9}{'PnL $':>8}{'$/h':>7}")
    for i, r in enumerate(runs):
        end_eq = runs[i + 1]["eq_start"] if i + 1 < len(runs) else equity_now
        h = (r["end"] - r["start"]).total_seconds() / 3600
        pr = per_run_fills[i]
        rp = end_eq - r["eq_start"]
        print(f"{r['start']:%m-%d %H:%M}  {h:6.1f}  {','.join(r['markets'])[:33]:34}{pr['n']:6}{pr['vol']:9,.0f}"
              f"{pr['fee']:6.2f}{pr['mk5usd']:9.2f}{rp:8.2f}{rp / max(h, 0.1):7.2f}")

    print("\nBY MARKET")
    print(f"{'market':11}{'fills':>6}{'vol $':>9}{'fees $':>8}{'taker':>6}{'avg 5m markout bps':>20}{'markout $':>10}")
    for s, a in sorted(by_mkt.items(), key=lambda kv: -kv[1]["vol"]):
        mk = st.mean(a["mk5"]) if a["mk5"] else float("nan")
        print(f"{s:11}{a['n']:6}{a['vol']:9,.0f}{a['fee']:8.2f}{a['taker']:6}{mk:20.1f}{a['mk5usd']:10.2f}")

    print("\nBY DAY (local)")
    for d in sorted(by_day):
        print(f"  {d}  fills {by_day[d]['n']:4}  volume ${by_day[d]['vol']:9,.0f}")

    total_lit, mine, tabs = lit_paid(acct["l1_address"])
    print("\nREWARDS (Liquidity Partner Program)")
    print(f"  published results: {', '.join(tabs[:3])}{' ...' if len(tabs) > 3 else ''}")
    print(f"  LIT paid to this account: {total_lit:.2f}" + (f"  -> {'; '.join(mine)}" if mine else "  (not listed in any week yet)"))
    if lit_px:
        print(f"  LIT price ${lit_px:.2f}: rewards worth ${total_lit * lit_px:.2f}")
        cost = -pnl
        if total_lit > 0:
            print(f"  cost per LIT ${cost / total_lit:.2f}  (net incl. rewards ${total_lit * lit_px - cost:+.2f})")
        else:
            print(f"  break-even needs {cost / lit_px:.1f} LIT for the ${cost:.2f} spent so far")
    print("=" * 78)


if __name__ == "__main__":
    main()
