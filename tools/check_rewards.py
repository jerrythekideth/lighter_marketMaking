#!/usr/bin/env python3
"""Did we get paid? Looks for this account's address in the Liquidity Partner Program
results. Read-only, no API key needed (only LIGHTER_ACCOUNT_INDEX from .env).

    .venv/bin/python tools/check_rewards.py

Lighter publishes one "Rewards <week>" tab per week in the public spreadsheet linked from
docs.lighter.xyz/liquidity-partner-program, a day or two after the week ends (weeks run
Monday 17:00 UTC to Monday 17:00 UTC). Addresses are shown shortened: 0xAbCd...1234.
"""
import csv
import io
import os
import re
import sys

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.config import CONFIG  # noqa: E402

SHEET = "1_Jr4C94Q-F2DsVar_Tmc15SBGLc-lSVCjwgkQ6f_3po"
BASE = "https://mainnet.zklighter.elliot.ai"


def num(s: str) -> float:
    try:
        return float(s.replace(",", "") or 0)
    except ValueError:
        return 0.0


def main() -> None:
    acct = requests.get(f"{BASE}/api/v1/account", params={"by": "index", "value": CONFIG.account_index},
                        timeout=15).json()["accounts"][0]
    addr = acct["l1_address"]
    short = f"{addr[:6]}...{addr[-4:]}".lower()
    print(f"account {CONFIG.account_index}, address {addr} (shown in the sheet as {addr[:6]}...{addr[-4:]})\n")

    html = requests.get(f"https://docs.google.com/spreadsheets/d/{SHEET}/htmlview", timeout=20).text
    tabs = [(n, g) for n, g in re.findall(r'\{name: "([^"]+)", pageUrl: "[^"]*", gid: "(\d+)"', html)
            if n.startswith("Rewards")]
    if not tabs:
        raise SystemExit("no Rewards tabs found — has the spreadsheet layout changed?")
    for name, gid in tabs[:4]:
        text = requests.get(f"https://docs.google.com/spreadsheets/d/{SHEET}/export?format=csv&gid={gid}",
                            timeout=20).text
        rows = list(csv.reader(io.StringIO(text)))
        header, body = rows[0], [r for r in rows[1:] if r and r[0].startswith("0x")]
        totals = sorted(num(r[1]) for r in body)
        mine = next((r for r in body if r[0].strip().lower() == short), None)
        line = f"{name}: {len(body)} addresses paid, smallest payout {totals[0]:.2f} LIT"
        if mine:
            per_market = {h: num(v) for h, v in zip(header[2:], mine[2:]) if num(v) > 0}
            print(f"{line}\n   YOU: {mine[1]} LIT  {per_market}")
        else:
            print(f"{line}\n   you: not listed")
        print(f"   https://docs.google.com/spreadsheets/d/{SHEET}/edit#gid={gid}")


if __name__ == "__main__":
    main()
