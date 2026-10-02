"""Liquidity Partner Program reward list, read from Lighter's public spreadsheet (linked from
docs.lighter.xyz/liquidity-partner-program). Newest weekly tab first; a new week (and new
weights) starts every Monday 17:00 UTC. Shared by the bot and tools/*.py."""
from __future__ import annotations

import csv
import io
import re

import requests

SHEET = "1_Jr4C94Q-F2DsVar_Tmc15SBGLc-lSVCjwgkQ6f_3po"
ALIASES = {"BRENT": "BRENTOIL"}  # sheet name -> Lighter symbol


def _tabs() -> list[tuple[str, str]]:
    html = requests.get(f"https://docs.google.com/spreadsheets/d/{SHEET}/htmlview", timeout=20).text
    tabs = re.findall(r'\{name: "([^"]+)", pageUrl: "[^"]*", gid: "(\d+)"', html)
    if not tabs:
        raise RuntimeError("no tabs found in the rewards spreadsheet — layout changed?")
    return tabs


def _csv(gid: str) -> list[dict]:
    text = requests.get(f"https://docs.google.com/spreadsheets/d/{SHEET}/export?format=csv&gid={gid}",
                        timeout=20).text
    return list(csv.DictReader(io.StringIO(text)))


def current_week() -> tuple[str, dict[str, float]]:
    """(week name, {symbol: LIT per week}) for the newest weights tab."""
    name, gid = next((n, g) for n, g in _tabs() if not n.startswith("Rewards"))
    out = {}
    for row in _csv(gid):
        sym = (row.get("Symbol") or "").strip()
        if sym:
            out[ALIASES.get(sym, sym)] = float(row["LIT Reward"].replace(",", ""))
    if not out:
        raise RuntimeError(f"rewards tab {name!r} has no symbols")
    return name, out


def results_tabs() -> list[tuple[str, str]]:
    """[(tab name, gid)] of published weekly results, newest first."""
    return [(n, g) for n, g in _tabs() if n.startswith("Rewards")]
