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
        s.strip().upper() for s in os.getenv("MARKETS", "").split(",") if s.strip()))

    poll_interval_seconds: int = field(default_factory=lambda: _int("POLL_INTERVAL_SECONDS", 15))

    # --- sizing / risk (per market unless noted) ---
    max_order_usd: float = field(default_factory=lambda: _float("MAX_ORDER_USD", 20))
    max_position_usd: float = field(default_factory=lambda: _float("MAX_POSITION_USD", 40))
    # Account-wide cap on sum(|position value|); above it every market goes reduce-only.
    max_gross_usd: float = field(default_factory=lambda: _float("MAX_GROSS_USD", 120))
    # Loss vs. equity at the start of the UTC day (persisted in data/state.json). Breach = halt.
    daily_loss_limit_usd: float = field(
        default_factory=lambda: _float("DAILY_LOSS_LIMIT_USD", 10))
    # Keep at least this fraction of equity as available balance; below it, reduce-only.
    min_free_margin_frac: float = field(default_factory=lambda: _float("MIN_FREE_MARGIN_FRAC", 0.3))

    # --- quoting ---
    # Half-spread floor from fair price: max(MIN_EDGE_BPS, EDGE_VOL_MULT * 1h volatility bps).
    # Within that floor the bot joins the best bid/ask (where liquidity rewards are highest).
    min_edge_bps: float = field(default_factory=lambda: _float("MIN_EDGE_BPS", 1.0))
    edge_vol_mult: float = field(default_factory=lambda: _float("EDGE_VOL_MULT", 0.1))
    # If the book spread is at least this wide, step one tick inside it instead of joining.
    improve_spread_bps: float = field(default_factory=lambda: _float("IMPROVE_SPREAD_BPS", 10))
    # Leave a resting order alone unless its target moved more than this (saves quota).
    requote_bps: float = field(default_factory=lambda: _float("REQUOTE_BPS", 2.0))
    # How far (bps) to shift the quote center against inventory when at MAX_POSITION_USD.
    skew_bps_at_cap: float = field(default_factory=lambda: _float("SKEW_BPS_AT_CAP", 4))

    # --- circuit breakers ---
    # Pull a market's quotes if mid moves more than max(BREAKER_BPS_MIN, BREAKER_VOL_MULT *
    # 1h vol) within BREAKER_WINDOW_S; stay out for BREAKER_PAUSE_S.
    breaker_window_s: float = field(default_factory=lambda: _float("BREAKER_WINDOW_S", 30))
    breaker_bps_min: float = field(default_factory=lambda: _float("BREAKER_BPS_MIN", 40))
    breaker_vol_mult: float = field(default_factory=lambda: _float("BREAKER_VOL_MULT", 2.0))
    breaker_pause_s: float = field(default_factory=lambda: _float("BREAKER_PAUSE_S", 300))
    # If this many markets trip within 2 minutes, treat it as systemic and pause everything.
    systemic_trip_count: int = field(default_factory=lambda: _int("SYSTEMIC_TRIP_COUNT", 3))
    # Don't quote books wider than this (too thin to trust the mid).
    max_book_spread_bps: float = field(default_factory=lambda: _float("MAX_BOOK_SPREAD_BPS", 150))
    # Don't quote if book mid is this far from Lighter's mark price (dislocated book).
    max_mark_divergence_bps: float = field(
        default_factory=lambda: _float("MAX_MARK_DIVERGENCE_BPS", 100))
    # Minutes before a session blackout (see src/sessions.py) to stop adding inventory.
    session_lead_minutes: int = field(default_factory=lambda: _int("SESSION_LEAD_MINUTES", 10))
    # Consecutive failed transactions before the bot halts (e.g. breaking API change).
    max_tx_errors: int = field(default_factory=lambda: _int("MAX_TX_ERRORS", 8))
    # Stop quoting if the market-data websocket has been silent this long.
    feed_max_silence_s: float = field(default_factory=lambda: _float("FEED_MAX_SILENCE_S", 10))

    # --- pacing / quota ---
    loop_seconds: float = field(default_factory=lambda: _float("LOOP_SECONDS", 1.0))
    account_refresh_s: float = field(default_factory=lambda: _float("ACCOUNT_REFRESH_S", 3.0))
    # Below this remaining volume quota, only use the free tx slot (1 per 15s).
    quota_reserve: int = field(default_factory=lambda: _int("QUOTA_RESERVE", 300))
    max_paid_tx_per_min: int = field(default_factory=lambda: _int("MAX_PAID_TX_PER_MIN", 8))

    @property
    def ws_url(self) -> str:
        return self.base_url.replace("https://", "wss://").rstrip("/") + "/stream"

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
