"""AIX cash flows and YTM, on the real schedules of bonds the old formula got wrong."""

from datetime import datetime

from kzbonds.aix_docs import coupon_note, parse_aix_terms
from kzbonds.sources.aix import _calculate_ytm, _cash_flows, _has_deferred_coupon

SETTLEMENT = datetime(2026, 11, 13)


def _period(start, end, rate):
    return {"coupon_date": f"{start}T00:00:00Z", "payment_commencement_date": f"{end}T00:00:00Z", "coupon_rate": rate}


def _flows(rate, freq, maturity, details, capitalization_freq=None, basis=360):
    return _cash_flows(100.0, rate, freq, SETTLEMENT, maturity, basis, details, None, capitalization_freq)


def test_half_year_bond_pays_half_the_annual_coupon():
    # NESL.1126: 8% a year, issued for six months. The old formula paid a full 8 and showed 939%.
    details = [_period("2026-05-18", "2026-11-18", 8)]
    assert _flows(8.0, 1, datetime(2026, 11, 18), details) == [(datetime(2026, 11, 18), 104.0)]
    ytm = _calculate_ytm(100.0, 103.8667, 8.0, 5 / 360, 1, SETTLEMENT, datetime(2026, 11, 18), 360, details)
    assert 5 < ytm < 12


def test_coupon_deferred_to_maturity_pays_every_year_of_the_term():
    # KAZP1.0629: 10.5%, "Deferred until maturity", one period of three years.
    details = [_period("2026-06-24", "2029-06-24", 10.5)]
    assert _flows(10.5, 1, datetime(2029, 6, 24), details)[0][1] == 131.5


def test_capitalised_coupon_compounds_as_in_the_offer_terms():
    # BIPC33.1226: annual capitalisation, 100 -> 108 -> 116.64.
    annual = _flows(8.0, 1, datetime(2026, 12, 30), [_period("2024-12-30", "2026-12-30", 8)], capitalization_freq=1)
    assert round(annual[0][1], 4) == 116.64
    # KT-Telecom: semi-annual, the offer terms list 1000 -> 1022.50 -> 1045.51 -> 1069.03 -> 1093.08.
    semi = _flows(4.5, 1, datetime(2028, 10, 28), [_period("2026-10-28", "2028-10-28", 4.5)], capitalization_freq=2)
    assert round(semi[0][1], 2) == 109.31


def test_schedule_that_stops_before_maturity_gets_its_last_period():
    # QAZP.1228: AIX lists coupons up to 13.09.2028, the bond matures 15.12.2028.
    details = [_period("2027-09-13", "2028-03-13", 4.2), _period("2028-03-13", "2028-09-13", 4.2)]
    flows = _flows(4.2, 2, datetime(2028, 12, 15), details)
    assert [d for d, _ in flows] == [datetime(2028, 3, 13), datetime(2028, 9, 13), datetime(2028, 12, 15)]
    assert flows[1][1] == 2.1 and flows[2][1] > 100


def test_regular_bond_is_unchanged():
    details = [_period("2026-06-21", "2026-12-21", 6)]
    assert _flows(6.0, 2, datetime(2026, 12, 21), details) == [(datetime(2026, 12, 21), 103.0)]


def test_deferred_coupon_detected_from_schedule():
    assert _has_deferred_coupon({"couponFreq": "1"}, [_period("2026-06-24", "2029-06-24", 10.5)])
    assert _has_deferred_coupon({"couponType": "Capitalized", "couponFreq": "1"}, None)
    assert not _has_deferred_coupon({"couponFreq": "2"}, [_period("2026-06-21", "2026-12-21", 6)])


def test_capitalisation_read_from_offer_terms():
    annual = parse_aix_terms("Coupon period Full period with annual capitalization, payable at maturity.")
    semi = parse_aix_terms("Full period with semi-annual capitalization, payable at maturity.")
    first_year = parse_aix_terms(
        "The Coupon during the first year (which is 9% p.a.) will be accrued but not paid "
        "(hereinafter Capitalized Coupon), increasing the Nominal Value of the Bonds for the second year"
    )
    assert (annual["capitalization_freq"], semi["capitalization_freq"], first_year["capitalization_freq"]) == (1, 2, 1)
    assert coupon_note(semi) == "Купон капитализируется раз в полгода и выплачивается при погашении"
    assert "capitalization_freq" not in parse_aix_terms("Frequency of coupon payments Deferred until maturity")


def test_accrued_interest_follows_the_same_schedule_as_the_cash_flows():
    # RWGN1.1126: AIX lists coupons up to 16.10.2026, the bond matures 16.11.2026.
    # Accrued interest must run from 16.10, not from a date on another grid -
    # otherwise the price paid exceeds everything the bond still pays.
    from kzbonds.sources.aix import _calculate_accrued_interest_pct

    details = [_period("2026-04-16", "2026-07-16", 10), _period("2026-07-16", "2026-10-16", 10)]
    maturity = datetime(2026, 11, 16)
    accrued = _calculate_accrued_interest_pct(10.0, 4, SETTLEMENT, maturity, 360, None, details)
    assert round(accrued, 4) == 0.75
    ytm = _calculate_ytm(100.0, 100 + accrued, 10.0, 3 / 360, 4, SETTLEMENT, maturity, 360, details)
    assert 8 < ytm < 12


def test_last_trade_is_the_latest_day_with_turnover():
    from kzbonds.sources.aix import _last_trade_in

    history = [
        {"date": "2026-11-10T00:00:00Z", "close": 0, "volume": None, "value": None},
        {"date": "2026-04-27T00:00:00Z", "close": 96.47, "volume": 12, "value": 11576.4},
        {"date": "2026-03-02T00:00:00Z", "close": 95.1, "volume": 5, "value": 4755.0},
    ]
    assert _last_trade_in(history) == "2026-04-27"
    assert _last_trade_in([{"date": "2026-11-10T00:00:00Z", "close": 0, "volume": None}]) is None
    assert _last_trade_in(None) is None


def test_price_is_todays_last_deal_when_the_bond_traded_today():
    from kzbonds.sources.aix import _current_price

    # SOLV1.0427 on 11.11.2026: reference price still the previous close.
    assert _current_price(95.28, 99.99, 2) == 99.99
    assert _current_price(95.28, 99.99, 0) == 95.28  # yesterday's deal: the close already is the reference price
    assert _current_price(100, None, 0) == 100.0
    assert _current_price("100%", None, None) == 100.0
