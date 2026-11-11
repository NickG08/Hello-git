"""
Pure-function tests for the KASE YTM/duration math. The network layer isn't
mocked (see README), but these calculations take plain values and are cheap
to verify directly - this is where the discount-bond gap (duration/YTM
silently None whenever coupon_rate was falsy) was caught, using the real
US186_2610 T-bill values pulled live from KASE.
"""

from datetime import date

from kzbonds.sources.kase import (
    _calculate_macaulay_duration,
    _calculate_ytm_irr,
    _days_30_360,
)


def test_zero_coupon_ytm_uses_bond_equivalent_yield_formula():
    # Real KASE gsec US186_2610: price 97.8958, 45 days to maturity (30/360
    # basis, per its own 'basis': '360' field), no coupon.
    settlement = date(2026, 9, 14)
    maturity = date(2026, 10, 29)
    ytm = _calculate_ytm_irr(
        clean_price=97.8958,
        coupon_rate=None,
        settlement_date=settlement,
        maturity_date=maturity,
        frequency=2,
        basis=360,
    )
    assert ytm is not None
    years = _days_30_360(settlement, maturity) / 360
    expected = (2 * ((100 / 97.8958) ** (1.0 / (2 * years)) - 1)) * 100
    assert ytm == round(expected, 4)


def test_zero_coupon_duration_equals_years_to_maturity():
    settlement = date(2026, 9, 14)
    maturity = date(2026, 10, 29)
    duration = _calculate_macaulay_duration(
        clean_price=97.8958,
        coupon_rate=None,
        settlement_date=settlement,
        maturity_date=maturity,
        ytm=18.0152,
        frequency=2,
        basis=360,
    )
    assert duration == round(_days_30_360(settlement, maturity) / 360, 4)


def test_coupon_bond_ytm_still_requires_a_coupon_free_price_move():
    # Sanity check the existing coupon-bearing path wasn't touched: a price
    # right at par with a coupon above zero still yields close to the coupon.
    settlement = date(2026, 1, 1)
    maturity = date(2029, 1, 1)
    ytm = _calculate_ytm_irr(
        clean_price=100.0,
        coupon_rate=6.0,
        settlement_date=settlement,
        maturity_date=maturity,
        frequency=2,
        basis=360,
    )
    assert ytm is not None
    assert abs(ytm - 6.0) < 0.5


def test_maturity_derived_from_trading_open_date_plus_term(monkeypatch):
    # Live MFSCb7: KASE publishes no maturity or circulation start, only the
    # trading open date (26.11.25) and an 18-month circulation term.
    from kzbonds.sources import kase

    monkeypatch.setattr(
        kase, "_get_bond_main_api", lambda t, b: {"characteristics": {"circulation_months": 18.0}}
    )
    chars = {"596": {"label": "Дата открытия торгов", "value": "26.11.25"}}
    assert kase._derive_maturity("MFSCb7", "corp", chars) == date(2027, 5, 26)


def test_maturity_derivation_prefers_circulation_start(monkeypatch):
    from kzbonds.sources import kase

    monkeypatch.setattr(
        kase, "_get_bond_main_api", lambda t, b: {"characteristics": {"circulation_months": 12}}
    )
    chars = {
        "554": {"label": "Дата начала обращения", "value": "01.09.25"},
        "596": {"label": "Дата открытия торгов", "value": "23.09.25"},
    }
    assert kase._derive_maturity("X", "corp", chars) == date(2026, 9, 1)


def test_coupon_ytm_matches_the_yield_kase_publishes():
    # Real deals of 10.11.2026; the expected values are KASE's own "dohod".
    # The old whole-period approximation gave 12.41 and 16.19 for these two.
    settlement = date(2026, 11, 10)
    mfmo = _calculate_ytm_irr(99.9, 12.0, settlement, date(2026, 11, 12), 4, 360)
    ncom = _calculate_ytm_irr(97.6, 11.0, settlement, date(2027, 2, 2), 2, 360)
    assert abs(mfmo - 30.24) < 0.01
    assert abs(ncom - 22.04) < 0.01


def test_coupon_duration_never_exceeds_time_to_maturity():
    settlement, maturity = date(2026, 11, 10), date(2027, 2, 2)
    duration = _calculate_macaulay_duration(97.6, 11.0, settlement, maturity, 22.04, 2, 360)
    assert 0 < duration <= round(_days_30_360(settlement, maturity) / 360, 4)


def test_nominal_is_read_from_values_that_carry_a_note():
    from kzbonds.sources.kase import _leading_number

    note = "(минимальный размер долга в номинальном выражении, доступный для торговли на KASE)"
    assert _leading_number(f"1,00 {note}") == 1.0  # used to become 100
    assert _leading_number(f"100 000,00 {note}") == 100000.0
    assert _leading_number("1 000,00") == 1000.0
    assert _leading_number("1 000,0") == 1000.0  # lot size
    assert _leading_number("100,00") == 100.0
    assert _leading_number(None) is None and _leading_number("—") is None
