import pandas as pd

from kzbonds.normalize import combine, normalize

# Rows are "traded today" unless a test says otherwise, so the freshness rule leaves them alone.
AS_OF = pd.Timestamp.now().strftime("%Y-%m-%d")


def _row(**overrides):
    row = {
        "exchange": "KASE",
        "ticker": "TEST1",
        "isin": "KZ000",
        "currency": "USD",
        "issuer": "Test Issuer",
        "instrument_type": "Купонная",
        "market_segment": "main_bonds",
        "face_value": 100,
        "face_currency": "USD",
        "clean_price": 99.5,
        "last_trade_date": AS_OF,
        "accrued_interest": 0.5,
        "dirty_price": 100.0,
        "coupon_rate": 5.0,
        "coupon_freq": 2,
        "day_count_basis": 360,
        "last_coupon_date": "2026-01-01",
        "maturity_date": "2030-01-01",
        "days_to_maturity": 1500,
        "ytm": 6.2,
        "ytm_exchange_reported": 6.1,
        "ytm_total_return_reported": None,
        "macaulay_duration": 3.2,
        "modified_duration": 3.1,
        "issue_volume": 1000,
        "_quality_flags": [],
    }
    row.update(overrides)
    return row


def test_ok_bond_has_ok_quality():
    df = normalize(pd.DataFrame([_row()]))
    assert df.iloc[0]["price_quality"] == "OK"
    assert df.iloc[0]["price_quality_detail"] == ""


def test_no_price_wins_over_other_flags():
    df = normalize(pd.DataFrame([_row(_quality_flags=["no_price", "html_fallback"])]))
    assert df.iloc[0]["price_quality"] == "NO_PRICE"


def test_capitalizing_beats_stale_but_not_no_price():
    df = normalize(
        pd.DataFrame([_row(_quality_flags=["capitalizing_coupon", "html_fallback"])])
    )
    assert df.iloc[0]["price_quality"] == "CAPITALIZING"

    df2 = normalize(
        pd.DataFrame([_row(_quality_flags=["capitalizing_coupon", "no_price"])])
    )
    assert df2.iloc[0]["price_quality"] == "NO_PRICE"


def test_unrecognized_flag_falls_back_to_ok():
    df = normalize(
        pd.DataFrame([_row(_quality_flags=["some_future_flag_not_mapped_yet"])])
    )
    assert df.iloc[0]["price_quality"] == "OK"


def test_combine_stacks_and_normalizes_both_sources():
    kase_df = pd.DataFrame([_row(exchange="KASE", ticker="K1")])
    aix_df = pd.DataFrame([_row(exchange="AIX", ticker="A1", isin=None)])
    combined = combine(kase_df, aix_df)
    assert set(combined["exchange"]) == {"KASE", "AIX"}
    assert len(combined) == 2


def test_combine_with_empty_sources():
    assert combine(pd.DataFrame(), pd.DataFrame()).empty
    only_kase = combine(pd.DataFrame([_row()]), pd.DataFrame())
    assert len(only_kase) == 1


def test_face_value_is_numeric_and_distinct_from_issue_volume():
    df = normalize(pd.DataFrame([_row(face_value=1000, issue_volume=40_000_000)]))
    assert df.iloc[0]["face_value"] == 1000
    assert df.iloc[0]["issue_volume"] == 40_000_000


def test_missing_face_value_defaults_to_null_not_issue_volume():
    row = _row()
    del row["face_value"]
    df = normalize(pd.DataFrame([row]))
    assert pd.isna(df.iloc[0]["face_value"])


def _fresh(**overrides):
    return normalize(pd.DataFrame([_row(**overrides)]), as_of="2026-11-10").iloc[0]


def test_ytm_shown_for_a_recent_deal():
    row = _fresh(last_trade_date="2026-10-11")  # exactly 30 days
    assert (row["ytm"], row["price_age_days"], row["price_quality"]) == (6.2, 30, "OK")


def test_ytm_hidden_when_the_price_is_older_than_the_threshold():
    # US105_2612: price from a deal on 22.10.2024, the yield "as of today" came out at 36%.
    row = _fresh(last_trade_date="2024-10-22T17:00:49")
    assert pd.isna(row["ytm"]) and pd.isna(row["modified_duration"]) and pd.isna(row["macaulay_duration"])
    assert row["ytm_calc"] == 6.2
    assert (row["last_trade_date"], row["price_age_days"], row["price_quality"]) == ("2024-10-22", 749, "STALE_PRICE")
    assert "больше 30 дней" in row["price_quality_detail"]


def test_ytm_hidden_for_a_bond_that_never_traded():
    # 131 of 191 AIX bonds: the only price is the exchange's reference price.
    row = _fresh(last_trade_date=None)
    assert pd.isna(row["ytm"]) and row["ytm_calc"] == 6.2
    assert row["price_quality"] == "STALE_PRICE" and "ни одной сделки" in row["price_quality_detail"]


def test_bond_without_a_price_stays_no_price():
    row = _fresh(clean_price=None, ytm=None, last_trade_date=None, _quality_flags=["no_price"])
    assert row["price_quality"] == "NO_PRICE"
