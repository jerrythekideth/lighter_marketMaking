"""Pure quote math. No network, no SDK — unit-tested in tests/test_strategy.py.

Why this shape (docs: Liquidity Partner Program, fetched 2026-09-29): Lighter pays weekly
LIT rewards for resting liquidity, sampled ~every minute, with a multiplier that decays
exponentially with distance from the best bid/offer, half to bids and half to asks. So the
bot wants to sit AT the top of book on both sides — but never closer to fair value than a
volatility-scaled edge, and leaning against its inventory so fills tend to flatten it.
"""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class Quote:
    bid: int | None  # price in integer ticks, None = don't quote this side
    ask: int | None


def skewed_mid(mid: float, net_position_usd: float, max_position_usd: float, skew_bps_at_cap: float) -> float:
    """Shift the quoting center against our inventory so fills tend to flatten us.
    Short -> center moves up (bid closer to market to buy back, ask further away to sell less);
    long -> center moves down. Linear in position, reaching skew_bps_at_cap at the position cap.
    """
    if max_position_usd <= 0:
        return mid
    ratio = max(-1.0, min(1.0, net_position_usd / max_position_usd))
    return mid * (1 - ratio * skew_bps_at_cap / 1e4)


def half_edge_bps(vol_1h_bps: float | None, min_edge_bps: float, edge_vol_mult: float) -> float:
    if vol_1h_bps is None or not math.isfinite(vol_1h_bps):
        return min_edge_bps
    return max(min_edge_bps, edge_vol_mult * vol_1h_bps)


def compute_quote(
    best_bid: float,
    best_ask: float,
    fair: float,
    price_decimals: int,
    edge_bps: float,
    net_position_usd: float,
    max_position_usd: float,
    skew_bps_at_cap: float,
    improve_spread_bps: float,
) -> Quote:
    """Target bid/ask in integer ticks.

    Join the best bid/ask (or step one tick inside when the spread is wide), but never
    closer to the skewed fair price than edge_bps, and never crossing the book (post-only).
    """
    scale = 10 ** price_decimals
    bb, ba = round(best_bid * scale), round(best_ask * scale)
    if bb <= 0 or ba <= bb:
        return Quote(None, None)
    mid = (best_bid + best_ask) / 2
    spread_bps = (best_ask - best_bid) / mid * 1e4

    center = skewed_mid(fair, net_position_usd, max_position_usd, skew_bps_at_cap)
    bid_cap = math.floor(center * (1 - edge_bps / 1e4) * scale + 1e-9)
    ask_floor = math.ceil(center * (1 + edge_bps / 1e4) * scale - 1e-9)

    improve = spread_bps >= improve_spread_bps and ba - bb >= 3
    join_bid = bb + 1 if improve else bb
    join_ask = ba - 1 if improve else ba

    bid = min(join_bid, bid_cap, ba - 1)
    ask = max(join_ask, ask_floor, bb + 1)
    if bid >= ask or bid <= 0:
        return Quote(None, None)
    return Quote(bid, ask)


def sides_allowed(net_position_usd: float, order_usd: float, max_position_usd: float,
                  reduce_only: bool) -> tuple[bool, bool]:
    """(bid_ok, ask_ok). A side is allowed if a full fill keeps |position| within the cap;
    in reduce-only mode only the side that shrinks an existing position is allowed."""
    if reduce_only:
        return net_position_usd < -1e-9, net_position_usd > 1e-9
    return (net_position_usd + order_usd <= max_position_usd + 1e-9,
            net_position_usd - order_usd >= -max_position_usd - 1e-9)


def needs_requote(current_price: float, target_price: float, requote_bps: float) -> bool:
    return abs(current_price - target_price) / target_price * 1e4 > requote_bps


def is_dangerous(is_ask: bool, current_price: float, target_price: float, requote_bps: float) -> bool:
    """Current order is MORE aggressive than target by more than tolerance (market moved
    against it). These get cancelled even when quota is short — cancels are free."""
    diff_bps = (current_price - target_price) / target_price * 1e4
    return (-diff_bps if is_ask else diff_bps) > requote_bps
