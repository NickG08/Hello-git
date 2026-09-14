import pandas as pd

from kzbonds.analysis import generate_note
from kzbonds.normalize import combine


def _bond(exchange, ticker, ytm, duration, currency="USD", quality_flags=None):
    return {
        "exchange": exchange,
        "ticker": ticker,
        "isin": None,
        "currency": currency,
        "issuer": "Issuer",
        "instrument_type": "Купонная",
        "market_segment": "main",
        "clean_price": 98.0,
        "accrued_interest": 0.3,
        "dirty_price": 98.3,
        "coupon_rate": 5.0,
        "coupon_freq": 2,
        "day_count_basis": 360,
        "last_coupon_date": None,
        "maturity_date": "2029-01-01",
        "days_to_maturity": 1000,
        "ytm": ytm,
        "ytm_exchange_reported": ytm,
        "ytm_total_return_reported": None,
        "macaulay_duration": duration,
        "modified_duration": duration,
        "issue_volume": 1000,
        "_quality_flags": quality_flags or [],
    }


def test_empty_dataframe_produces_placeholder_note():
    note = generate_note(pd.DataFrame())
    assert "Данных нет" in note


def test_note_reports_yield_spread_between_exchanges():
    rows = [_bond("KASE", f"K{i}", ytm=7.0, duration=2.0) for i in range(3)] + [
        _bond("AIX", f"A{i}", ytm=5.0, duration=2.0) for i in range(3)
    ]
    df = combine(pd.DataFrame(rows), pd.DataFrame())
    note = generate_note(df)
    assert "Самое большое расхождение" in note
    assert "USD" in note


def test_note_excludes_non_ok_quality_from_spread_calc():
    rows = [_bond("KASE", "K1", ytm=99.0, duration=2.0, quality_flags=["no_price"])]
    df = combine(pd.DataFrame(rows), pd.DataFrame())
    note = generate_note(df)
    assert "99.0" not in note
