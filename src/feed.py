"""Live market data over Lighter's public WebSocket (no auth).

Channels (docs: WebSocket reference, verified live 2026-09-29):
  ticker/{id}        best bid/ask on every book change; empty side comes as price ""
  market_stats/{id}  mark/index price, ~1/s

The feed is the bot's eyes: if it goes quiet, the bot must stop quoting. `healthy()` is
connection-level (any message recently), because a quiet market's ticker can legitimately
not change for minutes while market_stats keeps ticking.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field

import websockets

log = logging.getLogger("feed")

MID_HISTORY_SECONDS = 300
# App-level ping (docs: {"type":"ping"} -> {"type":"pong"}). Quiet markets can go >10s
# without a ticker/stats message; the pong proves the connection is alive so the bot
# doesn't pull all quotes on a false "feed down" (seen 5x/day with 2-4 quiet markets).
APP_PING_SECONDS = 5


@dataclass
class MarketState:
    bid: float | None = None
    ask: float | None = None
    bid_size: float = 0.0
    ask_size: float = 0.0
    mark: float | None = None
    index: float | None = None
    ticker_at: float = 0.0   # monotonic time of last ticker update
    stats_at: float = 0.0
    mids: deque = field(default_factory=deque)  # (monotonic t, mid)

    @property
    def mid(self) -> float | None:
        if self.bid is None or self.ask is None or self.bid <= 0 or self.ask <= self.bid:
            return None
        return (self.bid + self.ask) / 2

    def max_move_bps(self, window_s: float) -> float:
        """Largest |mid - mid_then| over the last window_s seconds, in bps of current mid."""
        cur = self.mid
        if cur is None or not self.mids:
            return 0.0
        cutoff = time.monotonic() - window_s
        return max((abs(cur - m) / cur * 1e4 for t, m in self.mids if t >= cutoff), default=0.0)


def _px(level: dict) -> tuple[float | None, float]:
    p, s = level.get("price", ""), level.get("size", "")
    if p in ("", None):
        return None, 0.0
    return float(p), float(s or 0)


class MarketFeed:
    def __init__(self, ws_url: str, market_ids: list[int]):
        self.ws_url = ws_url
        self.market_ids = list(market_ids)
        self.markets: dict[int, MarketState] = {m: MarketState() for m in market_ids}
        self.connected = False
        self.last_msg_at = 0.0
        self.reconnects = 0
        self._ws = None

    def healthy(self, max_silence_s: float) -> bool:
        return self.connected and time.monotonic() - self.last_msg_at < max_silence_s

    def _clear(self) -> None:
        for st in self.markets.values():
            st.bid = st.ask = st.mark = st.index = None
            st.mids.clear()

    def reset(self) -> None:
        """Drop all state and force a reconnect (e.g. after the machine slept)."""
        self.connected = False
        self.last_msg_at = 0.0
        self._clear()
        if self._ws is not None:
            asyncio.ensure_future(self._ws.close())

    def _on_message(self, msg: dict) -> None:
        now = time.monotonic()
        self.last_msg_at = now
        mtype, channel = msg.get("type", ""), msg.get("channel", "")
        if channel.startswith("ticker:"):
            st = self.markets.get(int(channel.split(":")[1]))
            if st is None:
                return
            t = msg.get("ticker") or {}
            st.bid, st.bid_size = _px(t.get("b") or {})
            st.ask, st.ask_size = _px(t.get("a") or {})
            st.ticker_at = now
            mid = st.mid
            if mid is not None:
                st.mids.append((now, mid))
                while st.mids and st.mids[0][0] < now - MID_HISTORY_SECONDS:
                    st.mids.popleft()
        elif channel.startswith("market_stats:"):
            s = msg.get("market_stats") or {}
            st = self.markets.get(s.get("market_id"))
            if st is None:
                return
            st.mark = float(s["mark_price"]) if s.get("mark_price") else None
            st.index = float(s["index_price"]) if s.get("index_price") else None
            st.stats_at = now
        elif mtype == "error" or "error" in msg:
            log.error("ws error message: %s", msg)

    async def _ping(self, ws) -> None:
        while True:
            await asyncio.sleep(APP_PING_SECONDS)
            await ws.send(json.dumps({"type": "ping"}))

    async def run(self) -> None:
        backoff = 1.0
        while True:
            try:
                async with websockets.connect(
                    self.ws_url, max_size=2 ** 22, ping_interval=20, ping_timeout=20, open_timeout=15,
                ) as ws:
                    self._ws = ws
                    for mid in self.market_ids:
                        await ws.send(json.dumps({"type": "subscribe", "channel": f"ticker/{mid}"}))
                        await ws.send(json.dumps({"type": "subscribe", "channel": f"market_stats/{mid}"}))
                    self.connected = True
                    backoff = 1.0
                    log.info("feed connected, %d markets", len(self.market_ids))
                    pinger = asyncio.create_task(self._ping(ws))
                    try:
                        async for raw in ws:
                            self._on_message(json.loads(raw))
                    finally:
                        pinger.cancel()
            except asyncio.CancelledError:
                self.connected = False
                raise
            except Exception as e:
                log.warning("feed disconnected: %s", e)
            self.connected = False
            self._ws = None
            self.reconnects += 1
            # A fresh snapshot comes with every resubscribe; clear state so nothing stale is used.
            self._clear()
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)
