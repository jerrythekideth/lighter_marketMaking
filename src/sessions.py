"""When each market is safe to quote.

Lighter lists RWAs (stocks, FX, commodities) 24/7, but their oracles follow the real
underlying market. While the underlying is closed Lighter prices off its own book (EMA,
docs: "RWA Pricing Mechanism"), and when the underlying reopens the price snaps to the
oracle. Resting quotes / open inventory across that snap is how a maker gets run over.
Lighter's orderBookDetails exposes `trading_hours`, but it is empty for every market
(checked 2026-09-29), so session boundaries are encoded here.

Evidence behind the windows (3 weeks of Lighter 1h candles, 2026-09):
  * US equities/indices: biggest hourly moves at 09:00-10:00 ET (cash open) and Mon pre-market.
  * Everything RWA + crypto: Wed 14:00 ET FOMC (2026-09-16: XAU 229bps, XAG 278bps in 1h),
    Thu/Fri 08:00-08:30 ET data releases.
  * Weekends: RWA books barely move (internal pricing), crypto keeps moving.

Two levels per market:
  BLACKOUT     -> pull all quotes.
  REDUCE_ONLY  -> the LEAD minutes before a blackout: only quote the side that shrinks
                  inventory, so we don't carry fresh risk into the event.
All times are America/New_York, so DST is handled by zoneinfo.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

# federalreserve.gov/monetarypolicy/fomccalendars.htm (fetched 2026-09-29). Decision day = day 2.
FOMC_DECISION_DATES = {
    dt.date(2026, 1, 28), dt.date(2026, 3, 18), dt.date(2026, 4, 29), dt.date(2026, 6, 17),
    dt.date(2026, 7, 29), dt.date(2026, 9, 16), dt.date(2026, 10, 28), dt.date(2026, 12, 9),
    dt.date(2027, 1, 27), dt.date(2027, 3, 17), dt.date(2027, 4, 28), dt.date(2027, 6, 9),
    dt.date(2027, 7, 28), dt.date(2027, 9, 15), dt.date(2027, 10, 27), dt.date(2027, 12, 8),
}

# Asset class per symbol. Anything not listed is NOT quoted (unknown session = unknown risk).
# RWA list from docs.lighter.xyz "Market Specifications"; crypto = 24/7 tokens.
ASSET_CLASS: dict[str, str] = {
    **{s: "crypto" for s in (
        "BTC", "ETH", "SOL", "HYPE", "ZEC", "PONS", "CASHCAT", "AI", "LIT", "DOGE", "XRP", "BNB",
        "AVAX", "SUI", "LINK", "AAVE", "ENA", "PUMP", "WLFI", "TRX", "ADA", "LTC",
    )},
    **{s: "us_equity" for s in (
        "NVDA", "TSLA", "AAPL", "AMZN", "GOOGL", "META", "MSFT", "HOOD", "AMD", "COIN", "INTC",
        "MU", "SNDK", "CRCL", "SPCX", "STABLECOINX", "MSTR", "ORCL", "MRVL", "ASML", "IBM", "DELL",
        "SOXL", "DRAM", "SPY", "QQQ", "IWM", "US100", "US500",
    )},
    **{s: "preipo" for s in ("OPENAI", "ANTHROPIC", "OURA")},
    **{s: "cme_commodity" for s in ("XAU", "XAG", "XPT", "XPD", "XCU", "WTI", "NATGAS")},
    "BRENTOIL": "ice_brent",
    **{s: "fx" for s in ("EURUSD", "GBPUSD", "USDJPY", "USDCHF", "NZDUSD", "AUDUSD")},
    # KRX names: books are empty outside Korean hours (seen 2026-09-29) — not supported.
    **{s: "unsupported" for s in ("SKHYNIX", "SAMSUNG", "HYUNDAIUSD", "USDKRW")},
}


@dataclass(frozen=True)
class Window:
    """[start, end) in ET minutes-of-day, on the given weekdays (Mon=0)."""
    days: tuple[int, ...]
    start: tuple[int, int]
    end: tuple[int, int]
    reason: str

    def contains(self, t: dt.datetime) -> bool:
        if t.weekday() not in self.days:
            return False
        m = t.hour * 60 + t.minute
        return self.start[0] * 60 + self.start[1] <= m < self.end[0] * 60 + self.end[1]


WEEKDAYS = (0, 1, 2, 3, 4)
SUN = (6,)

# Applies to every class: US macro releases (CPI/NFP/claims etc. are 08:30 ET).
MACRO = (Window(WEEKDAYS, (8, 28), (8, 40), "US data release 08:30 ET"),)

CLASS_WINDOWS: dict[str, tuple[Window, ...]] = {
    "crypto": (),
    "preipo": (),  # internally priced 24/7, no external session to snap to
    "us_equity": (
        Window(WEEKDAYS, (3, 55), (4, 10), "US pre-market open"),
        Window(WEEKDAYS, (9, 25), (9, 45), "US cash open"),
        Window(WEEKDAYS, (15, 58), (16, 5), "US cash close"),
        Window(WEEKDAYS[:4] + SUN, (19, 55), (20, 10), "US overnight session open"),
    ),
    "cme_commodity": (
        Window(WEEKDAYS, (16, 58), (18, 5), "CME daily break / reopen"),
        Window(SUN, (17, 55), (18, 10), "CME weekly reopen"),
    ),
    "ice_brent": (
        Window(WEEKDAYS, (17, 58), (20, 5), "ICE Brent daily break / reopen"),
        Window(SUN, (19, 55), (20, 10), "ICE Brent weekly reopen"),
    ),
    "fx": (
        Window(WEEKDAYS, (16, 58), (17, 5), "FX daily rollover"),
        Window(SUN, (16, 55), (17, 10), "FX weekly reopen"),
    ),
}

BLACKOUT = "blackout"
REDUCE_ONLY = "reduce_only"
OPEN = "open"


def _fomc_window(t: dt.datetime) -> str | None:
    if t.date() in FOMC_DECISION_DATES:
        m = t.hour * 60 + t.minute
        if 13 * 60 + 58 <= m < 14 * 60 + 30:
            return "FOMC decision 14:00 ET"
    return None


def _blackout_reason(symbol: str, t: dt.datetime) -> str | None:
    cls = ASSET_CLASS.get(symbol)
    if cls is None or cls == "unsupported":
        return f"no session model for {symbol}"
    for w in MACRO + CLASS_WINDOWS[cls]:
        if w.contains(t):
            return w.reason
    return _fomc_window(t)


def session_state(symbol: str, now_utc: dt.datetime, lead_minutes: int) -> tuple[str, str]:
    """-> (OPEN | REDUCE_ONLY | BLACKOUT, reason)."""
    t = now_utc.astimezone(ET)
    reason = _blackout_reason(symbol, t)
    if reason:
        return BLACKOUT, reason
    # Look ahead minute by minute (cheap: <= lead_minutes checks).
    for m in range(1, lead_minutes + 1):
        reason = _blackout_reason(symbol, t + dt.timedelta(minutes=m))
        if reason:
            return REDUCE_ONLY, f"{reason} in {m}m"
    return OPEN, ""
