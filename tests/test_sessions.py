import datetime as dt

from src.sessions import BLACKOUT, ET, OPEN, REDUCE_ONLY, session_state


def at(y, mo, d, h, mi):
    """ET wall-clock -> UTC datetime (exercises DST handling)."""
    return dt.datetime(y, mo, d, h, mi, tzinfo=ET).astimezone(dt.timezone.utc)


def state(sym, *args, lead=10):
    return session_state(sym, at(*args), lead)[0]


def test_us_cash_open_blackout_and_lead():
    # Wed 2026-09-30
    assert state("NVDA", 2026, 9, 30, 9, 30) == BLACKOUT
    assert state("NVDA", 2026, 9, 30, 9, 20) == REDUCE_ONLY
    assert state("NVDA", 2026, 9, 30, 9, 50) == OPEN
    assert state("NVDA", 2026, 9, 30, 12, 0) == OPEN


def test_macro_window_applies_to_all_classes():
    for sym in ("NVDA", "XAU", "EURUSD", "SOL", "OPENAI"):
        assert state(sym, 2026, 9, 30, 8, 30) == BLACKOUT, sym
    # not on weekends
    assert state("SOL", 2026, 10, 3, 8, 30) == OPEN


def test_fomc_day_only():
    assert state("SOL", 2026, 10, 28, 14, 0) == BLACKOUT      # FOMC decision day
    assert state("SOL", 2026, 10, 28, 13, 50) == REDUCE_ONLY
    assert state("SOL", 2026, 10, 29, 14, 0) == OPEN          # day after


def test_crypto_quotes_through_weekend():
    assert state("SOL", 2026, 10, 3, 3, 0) == OPEN
    assert state("HYPE", 2026, 10, 4, 18, 0) == OPEN


def test_cme_daily_break_and_sunday_reopen():
    assert state("XAU", 2026, 9, 30, 17, 30) == BLACKOUT
    assert state("XAU", 2026, 9, 30, 16, 50) == REDUCE_ONLY
    assert state("WTI", 2026, 10, 4, 18, 0) == BLACKOUT       # Sunday reopen
    assert state("WTI", 2026, 10, 3, 18, 0) == OPEN           # Saturday: no window


def test_fx_and_brent():
    assert state("EURUSD", 2026, 10, 4, 17, 0) == BLACKOUT
    assert state("BRENTOIL", 2026, 9, 30, 19, 0) == BLACKOUT


def test_unknown_and_unsupported_symbols_never_open():
    assert state("NOTAREALMARKET", 2026, 9, 30, 12, 0) == BLACKOUT
    assert state("SKHYNIX", 2026, 9, 30, 12, 0) == BLACKOUT


def test_dst_transition_is_wall_clock():
    # US DST ends 2026-11-01. 09:30 ET is 13:30 UTC before and 14:30 UTC after.
    assert session_state("NVDA", dt.datetime(2026, 10, 30, 13, 30, tzinfo=dt.timezone.utc), 10)[0] == BLACKOUT
    assert session_state("NVDA", dt.datetime(2026, 11, 2, 14, 30, tzinfo=dt.timezone.utc), 10)[0] == BLACKOUT
    # 13:30 UTC on 11-02 is 08:30 EST -> the macro window, not the cash open.
    assert session_state("NVDA", dt.datetime(2026, 11, 2, 13, 30, tzinfo=dt.timezone.utc), 10)[1] == \
        "US data release 08:30 ET"
    assert session_state("NVDA", dt.datetime(2026, 11, 2, 13, 0, tzinfo=dt.timezone.utc), 10)[0] == OPEN


def test_reason_mentions_minutes_to_event():
    s, why = session_state("NVDA", at(2026, 9, 30, 9, 20), 10)
    assert s == REDUCE_ONLY and "in 5m" in why
