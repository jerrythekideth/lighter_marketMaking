"""Wraps the Lighter SDK's SignerClient (lighter-sdk 1.1.5, verified byte-identical to
github.com/elliottech/lighter-python). Only the API key is used — never the L1 key.

The bot only ever touches its OWN orders: every order it places gets a client_order_index
>= BOT_COI_MIN (2^47), and cancels/lookups filter on that. Orders you place manually in the
Lighter UI (small client_order_index) are left alone.

All orders are POST_ONLY, so the bot can never cross the spread and pay taker fees.
"""
from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass

import lighter
import requests

from src.config import Config
from src.market_data import fetch_order_books

log = logging.getLogger("trader")

BOT_COI_MIN = 2 ** 47  # Lighter caps client_order_index at 2^48 - 1


@dataclass(frozen=True)
class MarketSpec:
    market_id: int
    size_decimals: int
    price_decimals: int
    min_base: float
    min_quote: float


@dataclass
class BotOrder:
    order_index: int
    client_order_index: int
    is_ask: bool
    price: float
    remaining_base: float


@dataclass
class AccountSnapshot:
    total_asset_value: float
    available_balance: float
    positions: dict[int, tuple[float, float]]  # market_id -> (signed base size, position_value usd)


class Trader:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._client: lighter.SignerClient | None = None
        self._order_api: lighter.OrderApi | None = None
        self._specs: dict[int, MarketSpec] = {}
        self._seq = 0
        if not cfg.dry_run:
            cfg.require_trading_credentials()
            self._connect()

    def _connect(self) -> None:
        key_index = int(self.cfg.api_key_index)
        self._client = lighter.SignerClient(
            url=self.cfg.base_url,
            account_index=int(self.cfg.account_index),
            api_private_keys={key_index: self.cfg.api_key_private_key},
        )
        err = self._client.check_client()
        if err:
            raise RuntimeError(f"API key does not match the one registered on Lighter: {err}")
        self._order_api = lighter.OrderApi(self._client.api_client)
        log.info("connected: account=%s api_key_index=%s", self.cfg.account_index, key_index)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()

    # --- market metadata / account state (public endpoints) ---

    def load_specs(self) -> None:
        for b in fetch_order_books(self.cfg.base_url):
            self._specs[b["market_id"]] = MarketSpec(
                market_id=b["market_id"],
                size_decimals=b["supported_size_decimals"],
                price_decimals=b["supported_price_decimals"],
                min_base=float(b["min_base_amount"]),
                min_quote=float(b["min_quote_amount"]),
            )

    def account_snapshot(self) -> AccountSnapshot:
        r = requests.get(
            f"{self.cfg.base_url}/api/v1/account",
            params={"by": "index", "value": self.cfg.account_index},
            timeout=10,
        )
        r.raise_for_status()
        a = r.json()["accounts"][0]
        positions = {}
        for p in a.get("positions", []):
            size = float(p["position"]) * (1 if p["sign"] >= 0 else -1)
            value = float(p["position_value"]) * (1 if p["sign"] >= 0 else -1)
            positions[p["market_id"]] = (size, value)
        return AccountSnapshot(
            total_asset_value=float(a["total_asset_value"]),
            available_balance=float(a["available_balance"]),
            positions=positions,
        )

    # --- orders ---

    async def bot_orders(self, market_id: int) -> list[BotOrder]:
        if self.cfg.dry_run:
            return []
        token, err = self._client.create_auth_token_with_expiry(api_key_index=int(self.cfg.api_key_index))
        if err:
            raise RuntimeError(f"auth token: {err}")
        resp = await self._order_api.account_active_orders(
            authorization=token, account_index=int(self.cfg.account_index), market_id=market_id,
        )
        return [
            BotOrder(o.order_index, o.client_order_index, o.is_ask, float(o.price), float(o.remaining_base_amount))
            for o in (resp.orders or [])
            if o.client_order_index >= BOT_COI_MIN
        ]

    def _next_coi(self) -> int:
        self._seq = (self._seq + 1) % 10
        return BOT_COI_MIN + int(time.time() * 1000) * 10 + self._seq

    async def place_order(self, symbol: str, market_id: int, side: str, price: float, size_usd: float) -> bool:
        spec = self._specs[market_id]
        is_ask = side == "ask"
        # Round away from mid so a post-only order never lands on the wrong side of our target.
        price_scale = 10 ** spec.price_decimals
        price_int = math.ceil(price * price_scale) if is_ask else math.floor(price * price_scale)
        size_scale = 10 ** spec.size_decimals
        base_int = math.floor(size_usd / price * size_scale)
        base = base_int / size_scale
        px = price_int / price_scale

        if base < spec.min_base or base * px < spec.min_quote:
            log.warning(
                "%s %s skipped: size %.6f ($%.2f) below exchange minimum (%.6f / $%.2f)",
                symbol, side, base, base * px, spec.min_base, spec.min_quote,
            )
            return False

        if self.cfg.dry_run:
            log.info("[DRY_RUN] would place %s %s %.6f @ %s ($%.2f)", side, symbol, base, px, base * px)
            return True

        _, resp, err = await self._client.create_order(
            market_index=market_id,
            client_order_index=self._next_coi(),
            base_amount=base_int,
            price=price_int,
            is_ask=is_ask,
            order_type=lighter.SignerClient.ORDER_TYPE_LIMIT,
            time_in_force=lighter.SignerClient.ORDER_TIME_IN_FORCE_POST_ONLY,
            reduce_only=False,
        )
        if err or (resp is not None and resp.code != 200):
            log.error("place %s %s failed: %s", side, symbol, err or resp)
            return False
        log.info("placed %s %s %.6f @ %s ($%.2f)", side, symbol, base, px, base * px)
        return True

    # --- dead man's switch ---
    # Lighter's scheduled cancel-all: if we stop refreshing it (process killed, laptop asleep),
    # every resting order on the account is cancelled at the scheduled time. Lighter rejects
    # schedules less than 5 minutes out (code 21714), so that's the floor on how fast it fires.
    # Note it is account-wide — it also cancels orders placed manually in the UI.

    async def arm_dead_mans_switch(self, seconds: float) -> bool:
        if self.cfg.dry_run:
            return True
        ts_ms = int((time.time() + seconds) * 1000)
        _, resp, err = await self._client.cancel_all_orders(
            time_in_force=lighter.SignerClient.CANCEL_ALL_TIF_SCHEDULED, timestamp_ms=ts_ms,
        )
        if err or (resp is not None and resp.code != 200):
            log.error("arming dead man's switch failed: %s", err or resp)
            return False
        return True

    async def disarm_dead_mans_switch(self) -> None:
        if self.cfg.dry_run:
            return
        _, resp, err = await self._client.cancel_all_orders(
            time_in_force=lighter.SignerClient.CANCEL_ALL_TIF_ABORT, timestamp_ms=0,
        )
        if err or (resp is not None and resp.code != 200):
            log.error("disarming dead man's switch failed: %s", err or resp)
        else:
            log.info("dead man's switch disarmed")

    async def cancel_bot_orders(self, symbol: str, market_id: int, orders: list[BotOrder] | None = None) -> None:
        if self.cfg.dry_run:
            log.info("[DRY_RUN] would cancel bot orders for %s", symbol)
            return
        if orders is None:
            orders = await self.bot_orders(market_id)
        for o in orders:
            _, resp, err = await self._client.cancel_order(market_index=market_id, order_index=o.order_index)
            if err or (resp is not None and resp.code != 200):
                log.error("cancel %s order %s failed: %s", symbol, o.order_index, err or resp)
            else:
                log.info("cancelled %s %s @ %s", symbol, "ask" if o.is_ask else "bid", o.price)
