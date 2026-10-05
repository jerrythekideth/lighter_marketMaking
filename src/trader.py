"""Wraps the Lighter SDK's SignerClient (lighter-sdk 1.1.5, verified byte-identical to
github.com/elliottech/lighter-python). Only the API key is used — never the L1 key.

The bot only ever touches its OWN orders: every order it places gets a client_order_index
>= BOT_COI_MIN (2^47), and cancels/lookups filter on that. Orders you place manually in the
Lighter UI (small client_order_index) are left alone.

All orders are POST_ONLY, so the bot can never cross the spread and pay taker fees.

Reads (account, active orders) go over authenticated REST and are parsed from raw JSON with
.get(), not the SDK's strict pydantic models, so an added/removed API field can't crash the
bot. Authenticated reads count against the L1 address's Premium budget (24k weight/min)
instead of the per-IP limit (docs: Rate Limits).

Volume quota (docs: Volume Quota): every create/modify consumes 1 quota unless it lands in
the free slot (one free tx per 15s). Cancels are always free. The bot tracks the remaining
quota from sendTx responses and slows down when it runs low (see TxBudget).
"""
from __future__ import annotations

import asyncio
import logging
import math
import statistics
import time
from dataclasses import dataclass

import aiohttp
import lighter

from src.config import Config

log = logging.getLogger("trader")

BOT_COI_MIN = 2 ** 47  # Lighter caps client_order_index at 2^48 - 1
AUTH_TOKEN_TTL_S = 8 * 60  # tokens are signed for 10 min; refresh well before
# Every SDK transaction call gets a hard timeout: the SDK's HTTP client has none, and after
# a laptop sleep a half-dead connection hung a cancel for 15 minutes (2026-10-01).
TX_TIMEOUT_S = 20


@dataclass(frozen=True)
class MarketSpec:
    market_id: int
    symbol: str
    size_decimals: int
    price_decimals: int
    min_base: float
    min_quote: float
    status: str
    force_reduce_only: bool
    initial_margin_fraction: float  # e.g. 0.2 = 5x

    @property
    def tick(self) -> float:
        return 10 ** -self.price_decimals

    @property
    def tradable(self) -> bool:
        return self.status == "active" and not self.force_reduce_only


@dataclass
class BotOrder:
    order_index: int
    client_order_index: int
    market_id: int
    is_ask: bool
    price: float
    remaining_base: float


@dataclass
class Position:
    size: float       # signed base amount
    value_usd: float  # signed
    unrealized_pnl: float


@dataclass
class AccountSnapshot:
    total_asset_value: float
    available_balance: float
    positions: dict[int, Position]
    fetched_at: float  # monotonic

    def position(self, market_id: int) -> Position:
        return self.positions.get(market_id, Position(0.0, 0.0, 0.0))

    @property
    def gross_exposure_usd(self) -> float:
        return sum(abs(p.value_usd) for p in self.positions.values())


class TxBudget:
    """Decides whether a quota-consuming tx may be sent now.

    Volume quota (docs: Volume Quota) is consumed by CreateOrder, ModifyOrder and
    CancelAllOrders — the last one includes the dead man's switch refresh. One sendTx every
    15s is free, but the free slot goes to whichever tx comes first, *including cancels*
    (which never cost quota themselves). Measured 2026-10-01/02: quota drained 300 -> 27 in
    ~20h because cancels and DMS refreshes were eating the free slots. So every tx is noted
    here, and "free" means nothing at all was sent in the last FREE_SLOT_S seconds.

    Above `reserve` quota: up to `max_per_min` paid txs per minute. At or below it: free
    slot only, so quota never runs out and requoting just gets slower.
    """

    FREE_SLOT_S = 15.5
    # Lighter's per-account limit for most tx types is 40/min (docs: Rate Limits, "Default").
    # Stay under it counting EVERY tx; cancels are never blocked (they're the safety path).
    MAX_ANY_TX_PER_MIN = 35

    def __init__(self, reserve: int, max_per_min: int):
        self.reserve = reserve
        self.max_per_min = max_per_min
        self.quota_remaining: int | None = None  # unknown until a paid tx reports it
        self._paid: list[float] = []      # creates/modifies/cancel-alls sent
        self._any: list[float] = []       # every tx, cancels included
        self._last_any = 0.0              # last tx of ANY kind, cancels included

    def _prune(self, now: float) -> None:
        self._paid = [t for t in self._paid if t > now - 60]
        self._any = [t for t in self._any if t > now - 60]

    def free_slot_ready(self) -> bool:
        return time.monotonic() - self._last_any >= self.FREE_SLOT_S

    def can_spend(self) -> bool:
        now = time.monotonic()
        self._prune(now)
        if len(self._paid) >= self.max_per_min or len(self._any) >= self.MAX_ANY_TX_PER_MIN:
            return False
        if self.quota_remaining is not None and self.quota_remaining <= self.reserve:
            return self.free_slot_ready()
        return True

    def note_tx(self) -> None:
        """Any sendTx (incl. cancels): it may take the free slot."""
        self._last_any = time.monotonic()
        self._any.append(self._last_any)

    def record(self, resp) -> None:
        """A quota-type tx (create/modify/cancel-all) was sent."""
        now = time.monotonic()
        self._paid.append(now)
        self._any.append(now)
        self._last_any = now
        q = getattr(resp, "volume_quota_remaining", None)
        if isinstance(q, int):
            self.quota_remaining = q


def _ok(resp, err) -> bool:
    return err is None and resp is not None and getattr(resp, "code", None) == 200


class Trader:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.account_index = int(cfg.account_index) if cfg.account_index else 0
        self.key_index = int(cfg.api_key_index) if cfg.api_key_index else 0
        self._client: lighter.SignerClient | None = None
        self._http: aiohttp.ClientSession | None = None
        self._token: str | None = None
        self._token_at = 0.0
        self._seq = 0
        self.specs: dict[int, MarketSpec] = {}
        self.budget = TxBudget(cfg.quota_reserve, cfg.max_paid_tx_per_min)
        self.consecutive_tx_errors = 0
        # DRY_RUN keeps simulated resting orders here so requote/quota logic runs realistically.
        self._paper: dict[int, BotOrder] = {}

    async def connect(self) -> None:
        """Must run inside the event loop (the SDK's aiohttp client needs a running loop)."""
        self._http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
        if self.cfg.dry_run and not self.cfg.api_key_private_key:
            return
        self.cfg.require_trading_credentials()
        self._client = lighter.SignerClient(
            url=self.cfg.base_url,
            account_index=self.account_index,
            api_private_keys={self.key_index: self.cfg.api_key_private_key},
        )
        err = self._client.check_client()
        if err:
            raise RuntimeError(f"API key does not match the one registered on Lighter: {err}")
        log.info("connected: account=%s api_key_index=%s", self.account_index, self.key_index)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
        if self._http is not None:
            await self._http.close()

    # --- auth / reads ---

    def _auth(self) -> dict:
        if self._client is None:
            return {}
        # Wall clock on purpose: macOS's monotonic clock stops during sleep, which made the bot
        # keep a token that had expired in real time (401s after waking, 2026-10-01).
        if self._token is None or abs(time.time() - self._token_at) > AUTH_TOKEN_TTL_S:
            token, err = self._client.create_auth_token_with_expiry(api_key_index=self.key_index)
            if err:
                raise RuntimeError(f"auth token: {err}")
            self._token, self._token_at = token, time.time()
        return {"Authorization": self._token}

    async def _get(self, path: str, params: dict, _retry: bool = True) -> dict:
        async with self._http.get(f"{self.cfg.base_url}{path}", params=params, headers=self._auth()) as r:
            if r.status == 401 and _retry and self._client is not None:
                self._token = None  # stale/expired token: mint a new one and try once more
                return await self._get(path, params, _retry=False)
            if r.status in (429, 405):
                raise RuntimeError(f"rate limited ({r.status}) on {path}")
            r.raise_for_status()
            data = await r.json(content_type=None)
        if data.get("code") not in (None, 200):
            raise RuntimeError(f"{path} returned code={data.get('code')} {data.get('message')}")
        return data

    async def load_specs(self) -> None:
        data = await self._get("/api/v1/orderBookDetails", {})
        specs = {}
        for b in data.get("order_book_details", []):
            specs[b["market_id"]] = MarketSpec(
                market_id=b["market_id"],
                symbol=b["symbol"],
                size_decimals=b["size_decimals"],
                price_decimals=b["price_decimals"],
                min_base=float(b["min_base_amount"]),
                min_quote=float(b["min_quote_amount"]),
                status=b.get("status", ""),
                force_reduce_only=bool((b.get("market_config") or {}).get("force_reduce_only")),
                initial_margin_fraction=b.get("default_initial_margin_fraction", 10000) / 10000,
            )
        self.specs = specs

    async def account_snapshot(self) -> AccountSnapshot:
        data = await self._get("/api/v1/account", {"by": "index", "value": str(self.account_index)})
        a = data["accounts"][0]
        positions = {}
        for p in a.get("positions", []):
            sign = 1 if p.get("sign", 1) >= 0 else -1
            positions[p["market_id"]] = Position(
                size=float(p.get("position", 0)) * sign,
                value_usd=float(p.get("position_value", 0)) * sign,
                unrealized_pnl=float(p.get("unrealized_pnl", 0)),
            )
        return AccountSnapshot(
            total_asset_value=float(a["total_asset_value"]),
            available_balance=float(a["available_balance"]),
            positions=positions,
            fetched_at=time.monotonic(),
        )

    async def vol_1h_bps(self, market_id: int, hours: int = 168) -> float | None:
        """Std-dev of hourly log returns over the last `hours`, in bps (None if too little data)."""
        now_ms = int(time.time() * 1000)
        data = await self._get("/api/v1/candles", {
            "market_id": str(market_id), "resolution": "1h", "start_timestamp": str(now_ms - hours * 3600_000),
            "end_timestamp": str(now_ms), "count_back": str(hours),
        })
        closes = [float(c["c"]) for c in data.get("c") or [] if c.get("c")]
        rets = [math.log(b / a) * 1e4 for a, b in zip(closes, closes[1:]) if a > 0 and b > 0]
        return statistics.pstdev(rets) if len(rets) >= 24 else None

    async def bot_orders(self) -> dict[int, list[BotOrder]]:
        """All of the bot's resting orders, grouped by market (one request for all markets)."""
        if self.cfg.dry_run:
            out: dict[int, list[BotOrder]] = {}
            for o in self._paper.values():
                out.setdefault(o.market_id, []).append(o)
            return out
        if self._client is None:
            return {}
        data = await self._get("/api/v1/accountActiveOrders", {"account_index": str(self.account_index)})
        out: dict[int, list[BotOrder]] = {}
        for o in data.get("orders") or []:
            coi = int(o.get("client_order_index", 0))
            if coi < BOT_COI_MIN:
                continue
            bo = BotOrder(
                order_index=int(o["order_index"]),
                client_order_index=coi,
                market_id=int(o["market_index"]),
                is_ask=bool(o["is_ask"]),
                price=float(o["price"]),
                remaining_base=float(o["remaining_base_amount"]),
            )
            out.setdefault(bo.market_id, []).append(bo)
        return out

    # --- price / size helpers ---

    def to_price_int(self, market_id: int, price: float, is_ask: bool) -> int:
        """Round away from mid, so a post-only order never lands on the wrong side of target."""
        scale = 10 ** self.specs[market_id].price_decimals
        x = price * scale
        return math.ceil(x - 1e-9) if is_ask else math.floor(x + 1e-9)

    def base_int_for(self, market_id: int, size_usd: float, price: float, allow_bump: bool = True) -> int | None:
        """Base amount (int) for ~size_usd at price, or None if below exchange minimums.
        With allow_bump, a size just under the minimum is raised to it (<= 1.5x requested);
        reduce-only sizing passes False so an order can never exceed the position it closes."""
        spec = self.specs[market_id]
        scale = 10 ** spec.size_decimals
        base_int = math.floor(size_usd / price * scale)
        base = base_int / scale
        if base < spec.min_base or base * price < spec.min_quote:
            # Bump to the minimum if that stays within 1.5x of the requested size.
            need = max(spec.min_base, spec.min_quote / price * 1.001)
            bumped = math.ceil(need * scale)
            if allow_bump and bumped / scale * price <= size_usd * 1.5:
                return bumped
            return None
        return base_int

    # --- orders ---

    def _next_coi(self) -> int:
        self._seq = (self._seq + 1) % 10
        return BOT_COI_MIN + int(time.time() * 1000) * 10 + self._seq

    async def _tx(self, coro):
        """Run an SDK tx call with a hard timeout; a timeout comes back as an error result
        (counted by the consecutive-error halt) instead of hanging the loop."""
        try:
            return await asyncio.wait_for(coro, TX_TIMEOUT_S)
        except (asyncio.TimeoutError, aiohttp.ClientError, OSError) as e:
            return None, None, f"{type(e).__name__}: {e}"

    def _too_big(self, what: str, notional: float) -> bool:
        ceiling = self.cfg.order_ceiling_usd()
        if notional > ceiling:
            self.consecutive_tx_errors += 1
            log.error("REFUSED %s: notional $%.2f > hard ceiling $%.2f (sizing bug?)", what, notional, ceiling)
            return True
        return False

    def _tx_result(self, what: str, resp, err) -> bool:
        if _ok(resp, err):
            self.consecutive_tx_errors = 0
            return True
        self.consecutive_tx_errors += 1
        log.error("%s failed: %s", what, err or resp)
        return False

    async def place(self, symbol: str, market_id: int, is_ask: bool, price_int: int, base_int: int) -> BotOrder | None:
        """Returns the order as we expect it to rest (order_index unknown yet: -1), or None."""
        spec = self.specs[market_id]
        px, base = price_int / 10 ** spec.price_decimals, base_int / 10 ** spec.size_decimals
        side = "ask" if is_ask else "bid"
        if self._too_big(f"place {symbol} {side}", base * px):
            return None
        coi = self._next_coi()
        expected = BotOrder(-1, coi, market_id, is_ask, px, base)
        if self.cfg.dry_run:
            self._paper[coi] = expected
            self.budget.record(None)
            log.info("[DRY_RUN] place %s %s %s @ %s ($%.2f)", symbol, side, base, px, base * px)
            return expected
        _, resp, err = await self._tx(self._client.create_order(
            market_index=market_id,
            client_order_index=coi,
            base_amount=base_int,
            price=price_int,
            is_ask=is_ask,
            order_type=lighter.SignerClient.ORDER_TYPE_LIMIT,
            time_in_force=lighter.SignerClient.ORDER_TIME_IN_FORCE_POST_ONLY,
            reduce_only=False,
        ))
        self.budget.record(resp)
        if self._tx_result(f"place {symbol} {side}", resp, err):
            log.info("placed %s %s %s @ %s ($%.2f)", symbol, side, base, px, base * px)
            return expected
        return None

    async def modify(self, symbol: str, order: BotOrder, price_int: int, base_int: int) -> bool:
        spec = self.specs[order.market_id]
        px = price_int / 10 ** spec.price_decimals
        side = "ask" if order.is_ask else "bid"
        if self._too_big(f"modify {symbol} {side}", base_int / 10 ** spec.size_decimals * px):
            return False
        if self.cfg.dry_run:
            self._paper[order.client_order_index] = BotOrder(
                order.order_index, order.client_order_index, order.market_id, order.is_ask, px,
                base_int / 10 ** spec.size_decimals)
            self.budget.record(None)
            log.info("[DRY_RUN] modify %s %s %s -> %s", symbol, side, order.price, px)
            return True
        _, resp, err = await self._tx(self._client.modify_order(
            market_index=order.market_id,
            order_index=order.client_order_index,
            base_amount=base_int,
            price=price_int,
        ))
        self.budget.record(resp)
        if self._tx_result(f"modify {symbol} {side}", resp, err):
            log.info("moved %s %s %s -> %s", symbol, side, order.price, px)
            return True
        return False

    async def cancel(self, symbol: str, order: BotOrder, why: str = "") -> bool:
        side = "ask" if order.is_ask else "bid"
        if self.cfg.dry_run:
            self._paper.pop(order.client_order_index, None)
            log.info("[DRY_RUN] cancel %s %s @ %s %s", symbol, side, order.price, why)
            return True
        _, resp, err = await self._tx(self._client.cancel_order(
            market_index=order.market_id, order_index=order.client_order_index,
        ))
        self.budget.note_tx()
        if self._tx_result(f"cancel {symbol} {side}", resp, err):
            log.info("cancelled %s %s @ %s %s", symbol, side, order.price, why)
            return True
        return False

    # --- dead man's switch ---
    # Lighter's scheduled cancel-all: if we stop refreshing it (process killed, laptop asleep),
    # every resting order on the account is cancelled at the scheduled time. Lighter rejects
    # schedules less than 5 minutes out (code 21714), so that's the floor on how fast it fires.
    # Note it is account-wide — it also cancels orders placed manually in the UI.

    async def arm_dead_mans_switch(self, seconds: float) -> bool:
        if self.cfg.dry_run:
            return True
        ts_ms = int((time.time() + seconds) * 1000)
        _, resp, err = await self._tx(self._client.cancel_all_orders(
            time_in_force=lighter.SignerClient.CANCEL_ALL_TIF_SCHEDULED, timestamp_ms=ts_ms,
        ))
        self.budget.record(resp)
        if not _ok(resp, err):
            log.error("arming dead man's switch failed: %s", err or resp)
            return False
        return True

    async def disarm_dead_mans_switch(self) -> None:
        if self.cfg.dry_run:
            return
        _, resp, err = await self._tx(self._client.cancel_all_orders(
            time_in_force=lighter.SignerClient.CANCEL_ALL_TIF_ABORT, timestamp_ms=0,
        ))
        self.budget.record(resp)
        if not _ok(resp, err):
            log.error("disarming dead man's switch failed: %s", err or resp)
        else:
            log.info("dead man's switch disarmed")

    async def flatten_all_positions(self, slippage_steps_bps: tuple[float, ...] = (100, 300)) -> bool:
        """Close every open perp position on the account (not just bot markets) with
        reduce-only market orders, so the account holds only USDC. Each attempt uses a worst
        acceptable price of best bid/ask -/+ the slippage step; anything left after all steps
        is reported and left alone rather than chased at any price. Returns True if flat."""
        if self.cfg.dry_run:
            acct = await self.account_snapshot()
            for m, p in acct.positions.items():
                if p.size:
                    log.info("[DRY_RUN] would close %s %+g ($%.2f)", self._sym(m), p.size, p.value_usd)
            return True
        for step in slippage_steps_bps:
            acct = await self.account_snapshot()
            open_pos = {m: p for m, p in acct.positions.items() if p.size}
            if not open_pos:
                return True
            for m, p in open_pos.items():
                await self._close_position(m, p, step)
            await asyncio.sleep(2)
        acct = await self.account_snapshot()
        left = {self._sym(m): p.size for m, p in acct.positions.items() if p.size}
        if left:
            log.error("could not fully flatten (slippage cap hit?) — still open: %s", left)
            return False
        return True

    def _sym(self, market_id: int) -> str:
        spec = self.specs.get(market_id)
        return spec.symbol if spec else str(market_id)

    async def _close_position(self, market_id: int, pos: "Position", slippage_bps: float) -> None:
        spec = self.specs.get(market_id)
        if spec is None:
            log.error("can't close market %s: no spec", market_id)
            return
        book = await self._get("/api/v1/orderBookOrders", {"market_id": str(market_id), "limit": "1"})
        is_ask = pos.size > 0  # long -> sell to close
        side = (book.get("bids") if is_ask else book.get("asks")) or []
        if not side:
            log.error("can't close %s: no %s in the book", spec.symbol, "bids" if is_ask else "asks")
            return
        best = float(side[0]["price"])
        worst = best * (1 - slippage_bps / 1e4) if is_ask else best * (1 + slippage_bps / 1e4)
        price_int = self.to_price_int(market_id, worst, is_ask=not is_ask)  # round toward the book
        base_int = round(abs(pos.size) * 10 ** spec.size_decimals)
        _, resp, err = await self._tx(self._client.create_market_order(
            market_index=market_id,
            client_order_index=self._next_coi(),
            base_amount=base_int,
            avg_execution_price=price_int,
            is_ask=is_ask,
            reduce_only=True,
        ))
        self.budget.record(resp)
        if self._tx_result(f"close {spec.symbol}", resp, err):
            log.info("closing %s %+g ($%.2f) at market, worst price %s (%.0fbps)",
                     spec.symbol, pos.size, pos.value_usd, price_int / 10 ** spec.price_decimals, slippage_bps)

    async def cancel_everything_now(self) -> bool:
        """Account-wide immediate cancel-all (also hits manual orders). Shutdown fallback for
        when listing our orders fails, since it needs no read first."""
        if self.cfg.dry_run:
            return True
        _, resp, err = await self._tx(self._client.cancel_all_orders(
            time_in_force=lighter.SignerClient.CANCEL_ALL_TIF_IMMEDIATE, timestamp_ms=0,
        ))
        self.budget.record(resp)
        if not _ok(resp, err):
            log.error("immediate cancel-all failed: %s", err or resp)
            return False
        log.info("sent account-wide cancel-all")
        return True

    async def cancel_all_bot_orders(self, why: str) -> bool:
        """Cancel every bot order in every market; True if confirmed none remain."""
        if self.cfg.dry_run:
            self._paper.clear()
            log.info("[DRY_RUN] cancel all bot orders (%s)", why)
            return True
        orders = await self.bot_orders()
        for market_orders in orders.values():
            for o in market_orders:
                spec = self.specs.get(o.market_id)
                await self.cancel(spec.symbol if spec else str(o.market_id), o, why)
        await asyncio.sleep(1.5)
        return not any(await self.bot_orders())
