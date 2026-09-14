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
