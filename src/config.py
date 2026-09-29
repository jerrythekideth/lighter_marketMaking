"""Single place all tunables are loaded from. Everything else imports from here."""
import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


def _bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def _float(name: str, default: float) -> float:
    val = os.getenv(name)
    return float(val) if val else default


def _int(name: str, default: int) -> int:
    val = os.getenv(name)
    return int(val) if val else default


@dataclass(frozen=True)
class Config:
    base_url: str = field(default_factory=lambda: os.getenv(
        "LIGHTER_BASE_URL", "https://testnet.zklighter.elliot.ai"))

    # Market-data monitoring (Phase 0) is read-only and zero-risk, so it defaults to
    # mainnet regardless of LIGHTER_BASE_URL — that's where the real competition/edge is,
    # and testnet only lists a handful of markets (BTC/ETH/SOL). Trading (Phase 1+) stays
    # on LIGHTER_BASE_URL (testnet by default) until you deliberately move to Phase 2.
    market_data_base_url: str = field(default_factory=lambda: os.getenv(
        "MARKET_DATA_BASE_URL", "https://mainnet.zklighter.elliot.ai"))

    account_index: str = field(default_factory=lambda: os.getenv("LIGHTER_ACCOUNT_INDEX", ""))
    api_key_index: str = field(default_factory=lambda: os.getenv("LIGHTER_API_KEY_INDEX", ""))
    api_key_private_key: str = field(
        default_factory=lambda: os.getenv("LIGHTER_API_KEY_PRIVATE_KEY", ""))

    dry_run: bool = field(default_factory=lambda: _bool("DRY_RUN", True))

    markets: tuple = field(default_factory=lambda: tuple(
        s.strip().upper() for s in os.getenv("MARKETS", "XAU,EURUSD").split(",") if s.strip()))

    poll_interval_seconds: int = field(default_factory=lambda: _int("POLL_INTERVAL_SECONDS", 15))

    max_position_usd: float = field(default_factory=lambda: _float("MAX_POSITION_USD", 200))
    max_order_usd: float = field(default_factory=lambda: _float("MAX_ORDER_USD", 50))
    daily_loss_limit_usd: float = field(
        default_factory=lambda: _float("DAILY_LOSS_LIMIT_USD", 25))

    min_spread_bps: float = field(default_factory=lambda: _float("MIN_SPREAD_BPS", 5))
    max_spread_bps: float = field(default_factory=lambda: _float("MAX_SPREAD_BPS", 15))
    order_levels: int = field(default_factory=lambda: _int("ORDER_LEVELS", 2))
    # Leave resting orders alone unless target price drifts more than this from them.
    requote_bps: float = field(default_factory=lambda: _float("REQUOTE_BPS", 2))
    # How far (bps) to shift the quote center against inventory when at MAX_POSITION_USD.
    skew_bps_at_cap: float = field(default_factory=lambda: _float("SKEW_BPS_AT_CAP", 4))

    def is_mainnet(self) -> bool:
        return "mainnet" in self.base_url

    def require_trading_credentials(self) -> None:
        missing = [n for n, v in (
            ("LIGHTER_ACCOUNT_INDEX", self.account_index),
            ("LIGHTER_API_KEY_INDEX", self.api_key_index),
            ("LIGHTER_API_KEY_PRIVATE_KEY", self.api_key_private_key),
        ) if not v]
        if missing:
            raise RuntimeError(
                f"Missing required .env values for trading: {', '.join(missing)}. "
                "Market-data monitoring (run_monitor.py) doesn't need these."
            )


CONFIG = Config()
