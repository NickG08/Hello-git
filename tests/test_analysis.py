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
        "last_trade_date": pd.Timestamp.now().strftime("%Y-%m-%d"),
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


def test_note_lists_top_yields_per_currency_and_nothing_else():
    rows = [
        _bond("KASE", "K_LOW", ytm=5.0, duration=2.0),
        _bond("AIX", "A_HIGH", ytm=9.0, duration=1.0),
        _bond("KASE", "K_MID", ytm=7.0, duration=3.0),
        _bond("AIX", "E_ONLY", ytm=4.0, duration=1.5, currency="EUR"),
    ]
    note = generate_note(combine(pd.DataFrame(rows), pd.DataFrame()))
    assert note.startswith("## Топ доходностей среди надёжных цен (по валютам)")
    usd = note[note.index("**USD:**") :]
    assert usd.index("A_HIGH") < usd.index("K_MID") < usd.index("K_LOW")
    assert "**EUR:**" in note and "E_ONLY" in note
    assert "AIX A_HIGH — YTM 9.00%, дюрация 1.00л, погашение 2029-01-01" in note
    for gone in ("Качество данных", "price_quality", "расхождение", "Сформировано автоматически"):
        assert gone not in note


def test_note_keeps_at_most_five_bonds_per_currency():
    rows = [_bond("KASE", f"K{i}", ytm=5.0 + i, duration=2.0) for i in range(8)]
    note = generate_note(combine(pd.DataFrame(rows), pd.DataFrame()))
    assert note.count("\n- ") == 5
    assert "K7" in note and "K2" not in note


def test_note_leaves_out_bonds_without_a_reliable_price():
    rows = [
        _bond("KASE", "K1", ytm=99.0, duration=2.0, quality_flags=["no_price"]),
        _bond("KASE", "K2", ytm=6.0, duration=2.0),
    ]
    note = generate_note(combine(pd.DataFrame(rows), pd.DataFrame()))
    assert "99.0" not in note and "K1" not in note
    assert "K2" in note


def test_note_says_so_when_no_bond_has_a_reliable_price():
    rows = [_bond("KASE", "K1", ytm=99.0, duration=2.0, quality_flags=["no_price"])]
    note = generate_note(combine(pd.DataFrame(rows), pd.DataFrame()))
    assert "Бумаг с надёжной ценой и YTM в этом снапшоте нет." in note
