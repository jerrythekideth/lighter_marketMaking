"""Wraps the Lighter SDK's signing client. Order-placement calls are intentionally left
unimplemented — see README "Order placement — needs verification before Phase 1".

Do not fill these in by guessing. Before Phase 1:
  1. pip install lighter-sdk  (or the git+https install from their README)
  2. Read examples/system_setup.py and the order-placement examples in that repo
  3. Confirm the exact SignerClient method names/params, then implement below.

Until then, every call here either no-ops (DRY_RUN) or raises NotImplementedError, so this
module can never accidentally send a malformed order.
"""
from __future__ import annotations

import logging

from src.config import Config

log = logging.getLogger("trader")


class Trader:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._client = None
        if not cfg.dry_run:
            cfg.require_trading_credentials()
            self._connect()

    def _connect(self) -> None:
        """Real connection goes here once verified against the SDK. Kept lazy/absent
        until then so Phase 0/1 never needs lighter-sdk installed correctly to run."""
        raise NotImplementedError(
            "Trader._connect: verify SignerClient(...) init against the SDK's own "
            "examples/system_setup.py before implementing. See README."
        )

    def place_order(self, symbol: str, market_id: int, side: str, price: float, size_usd: float) -> None:
        if self.cfg.dry_run:
            log.info(
                "[DRY_RUN] would place %s %s @ %.6f size_usd=%.2f (market_id=%s)",
                side, symbol, price, size_usd, market_id,
            )
            return
        raise NotImplementedError(
            "Trader.place_order: implement against verified SignerClient.create_order(...) "
            "signature. Do not guess params for a live order call. See README."
        )

    def cancel_all(self, symbol: str, market_id: int) -> None:
        if self.cfg.dry_run:
            log.info("[DRY_RUN] would cancel all orders for %s (market_id=%s)", symbol, market_id)
            return
        raise NotImplementedError(
            "Trader.cancel_all: implement against verified SignerClient cancel call. "
            "See README."
        )
