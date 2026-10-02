from src.strategy import (
    compute_quote, half_edge_bps, is_dangerous, needs_requote, sides_allowed, skewed_mid,
)


def q(bid, ask, fair=None, dec=2, edge=1.0, pos=0.0, cap=40.0, skew=4.0, improve=10.0):
    fair = (bid + ask) / 2 if fair is None else fair
    return compute_quote(bid, ask, fair, dec, edge, pos, cap, skew, improve)


def test_tight_book_backs_off_to_edge():
    # 1 tick spread on a $100 market (1bp), edge 5bps -> quotes sit 5bps from mid, not at touch.
    r = q(100.00, 100.01, edge=5.0)
    mid = 100.005
    assert r.bid == 9995   # floor(100.005 * 0.9995 * 100) = 9995.49 -> 9995
    assert r.ask == 10006  # ceil(100.005 * 1.0005 * 100) = 10005.5 -> 10006
    assert r.bid / 100 < mid < r.ask / 100


def test_wide_book_joins_or_improves():
    # 50bps spread, edge 1bp: step one tick inside both sides.
    r = q(100.00, 100.50, edge=1.0, improve=10.0)
    assert (r.bid, r.ask) == (10001, 10049)
    # Same book but improvement disabled -> join exactly.
    r = q(100.00, 100.50, edge=1.0, improve=1e9)
    assert (r.bid, r.ask) == (10000, 10050)


def test_never_crosses_book():
    for bid, ask in [(100.00, 100.01), (100.00, 100.02), (99.99, 100.03)]:
        r = q(bid, ask, edge=0.0, improve=0.0)
        assert r.bid is not None and r.ask is not None
        assert r.bid < round(ask * 100) and r.ask > round(bid * 100)
        assert r.bid < r.ask


def test_skew_moves_center_against_inventory():
    # Tight book so the edge (not the touch) binds and skew is visible on both sides.
    flat = q(1000.00, 1000.01, edge=5.0, pos=0.0)
    short = q(1000.00, 1000.01, edge=5.0, pos=-40.0, skew=4.0)
    long = q(1000.00, 1000.01, edge=5.0, pos=40.0, skew=4.0)
    assert short.bid > flat.bid and short.ask > flat.ask    # short: lean up, buy back sooner
    assert long.bid < flat.bid and long.ask < flat.ask      # long: lean down, sell sooner


def test_skew_never_pushes_past_touch():
    # Wide book: bid is capped at best+1 tick even when short (never chase through the top).
    short = q(100.00, 100.50, edge=20.0, pos=-40.0, skew=10.0)
    assert short.bid == 10001


def test_bad_books_return_none():
    assert q(0.0, 100.0).bid is None
    assert q(100.0, 100.0).bid is None
    assert q(100.0, 99.0).bid is None


def test_skewed_mid_is_capped():
    assert skewed_mid(100, 1000, 40, 4) == skewed_mid(100, 40, 40, 4)
    assert skewed_mid(100, 0, 40, 4) == 100


def test_half_edge():
    assert half_edge_bps(None, 1.0, 0.1) == 1.0
    assert half_edge_bps(float("nan"), 1.0, 0.1) == 1.0
    assert half_edge_bps(70.0, 1.0, 0.1) == 7.0
    assert half_edge_bps(5.0, 1.0, 0.1) == 1.0


def test_sides_allowed_caps_and_reduce_only():
    assert sides_allowed(0, 20, 40, False) == (True, True)
    assert sides_allowed(25, 20, 40, False) == (False, True)   # a bid fill would reach $45
    assert sides_allowed(-25, 20, 40, False) == (True, False)
    assert sides_allowed(20, 20, 40, False) == (True, True)    # exactly at cap is fine
    assert sides_allowed(10, 20, 40, True) == (False, True)    # long -> only sell
    assert sides_allowed(-10, 20, 40, True) == (True, False)   # short -> only buy
    assert sides_allowed(0, 20, 40, True) == (False, False)    # flat -> nothing


def test_requote_and_danger():
    assert not needs_requote(100.00, 100.005, 1.0)   # 0.5bp
    assert needs_requote(100.00, 100.02, 1.0)        # 2bp
    # bid above target (market fell) is dangerous; bid below target is merely stale.
    assert is_dangerous(False, 100.05, 100.00, 1.0)
    assert not is_dangerous(False, 99.95, 100.00, 1.0)
    # ask below target (market rose) is dangerous.
    assert is_dangerous(True, 99.95, 100.00, 1.0)
    assert not is_dangerous(True, 100.05, 100.00, 1.0)


def test_unwind_price_never_worse_than_mark():
    from src.strategy import unwind_price
    # OPENAI 2026-09-30: book ~1715, mark 1665, we're long -> sell at the (better) book ask.
    assert unwind_price(20, 1715.10, 1715.56, 1665.10, 2) == 171556
    # Long but book BELOW mark -> wait at mark, don't dump into the book.
    assert unwind_price(20, 1600.00, 1600.50, 1665.10, 2) == 166510
    # Short, book above mark -> buy back at mark, not at the inflated book.
    assert unwind_price(-20, 1715.10, 1715.56, 1665.10, 2) == 166510
    # Short, book below mark -> join the (better) best bid.
    assert unwind_price(-20, 1600.00, 1600.50, 1665.10, 2) == 160000
    # Post-only: never cross. Long, mark below the bid -> at least bid + 1 tick.
    assert unwind_price(20, 1700.00, None, 1690.00, 2) == 170001
    # One-sided / no mark / flat.
    assert unwind_price(20, None, None, 1665.10, 2) == 166510
    assert unwind_price(20, 1700.0, 1701.0, None, 2) is None
    assert unwind_price(0, 1700.0, 1701.0, 1700.5, 2) is None
