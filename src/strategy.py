"""Pure quote-generation + risk-limit logic. No network calls, no SDK — easy to reason
about and unit-test on its own before it's wired to anything that can touch real orders.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Quote:
    side: str          # "bid" or "ask"
    price: float
    size_usd: float
    level: int          # 0 = innermost


@dataclass
class PositionState:
    net_position_usd: float = 0.0
    daily_pnl_usd: float = 0.0
    open_orders_usd: float = field(default=0.0)  # sum of |size_usd| currently resting


def generate_quotes(
    mid: float,
    min_spread_bps: float,
    max_spread_bps: float,
    order_levels: int,
    order_size_usd: float,
) -> list[Quote]:
    """Ladder of resting orders between min and max spread from mid, evenly spaced.
    We deliberately do NOT try to sit at min_spread on liquid majors/RWA pairs — top-of-book
    there is already sub-1-3bps and held by systematic quoters. This ladder is meant to sit
    in the depth bands (5-10bps+) that the points scoring rewards without fighting for the
    front of the queue.
    """
    if order_levels < 1:
        return []
    quotes: list[Quote] = []
    step = (max_spread_bps - min_spread_bps) / max(order_levels - 1, 1)
    for level in range(order_levels):
        spread_bps = min_spread_bps + step * level
        offset = mid * spread_bps / 1e4
        quotes.append(Quote("bid", round(mid - offset, 8), order_size_usd, level))
        quotes.append(Quote("ask", round(mid + offset, 8), order_size_usd, level))
    return quotes


def check_risk_limits(
    state: PositionState,
    max_position_usd: float,
    daily_loss_limit_usd: float,
) -> tuple[bool, str]:
    """Returns (ok_to_quote, reason). Call before every requoting cycle."""
    if abs(state.net_position_usd) >= max_position_usd:
        return False, f"net position {state.net_position_usd:.2f} >= max {max_position_usd}"
    if state.daily_pnl_usd <= -abs(daily_loss_limit_usd):
        return False, f"daily pnl {state.daily_pnl_usd:.2f} breached loss limit"
    return True, "ok"


def clamp_order_size(
    requested_usd: float,
    max_order_usd: float,
    state: PositionState,
    max_position_usd: float,
) -> float:
    """Never let a single order push net position past the cap, and never exceed the
    per-order cap either."""
    headroom = max_position_usd - abs(state.net_position_usd)
    return max(0.0, min(requested_usd, max_order_usd, headroom))


def skewed_mid(mid: float, net_position_usd: float, max_position_usd: float, skew_bps_at_cap: float) -> float:
    """Shift the quoting center against our inventory so fills tend to flatten us.
    Short -> center moves up (bid closer to market to buy back, ask further away to sell less);
    long -> center moves down. Linear in position, reaching skew_bps_at_cap at the position cap.
    """
    if max_position_usd <= 0:
        return mid
    ratio = max(-1.0, min(1.0, net_position_usd / max_position_usd))
    return mid * (1 - ratio * skew_bps_at_cap / 1e4)
