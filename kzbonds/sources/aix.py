"""
AIX (Astana International Exchange) foreign-currency bonds source.

Adapted from the standalone AIX API scraper (internal, undocumented
market-backend.aixkz.com JSON API discovered via network inspection - no
official SLA, endpoints may change without notice). Calculation logic
(day-count calibration, T+2 settlement over the real KZ trading calendar,
coupon-schedule handling) is kept as close to the original as possible.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta

import aiohttp
import pandas as pd
from dateutil import parser as date_parser
from dateutil.relativedelta import relativedelta
from scipy.optimize import newton

from kzbonds import config

try:
    import holidays as holidays_lib

    KZ_HOLIDAYS_AVAILABLE = True
except ImportError:
    holidays_lib = None
    KZ_HOLIDAYS_AVAILABLE = False

API_BASE = "https://market-backend.aixkz.com"
MAIN_RECORDS_URL = (
    f"{API_BASE}/api/table/mw-main-records"
    "?instrument=&search=&listing_before=&listing_after="
    "&listing_between_start=&listing_between_end=&is_etf_etn=true"
)
PROFILE_URL = f"{API_BASE}/api/profile/{{ticker}}"
COUPON_DETAILS_URL = f"{API_BASE}/api/coupon-details/{{ticker}}"
ACCRUED_INTEREST_URL = f"{API_BASE}/api/bonds"

SETTLEMENT_DAYS = (
    2  # T+2 trading days, matches AIX's own "Accrued Interest (T+2)" column
)

BOND_INSTRUMENT_KEYWORDS = ("Bonds", "Sukuk")
NON_BOND_INSTRUMENT_KEYWORDS = ("Structured Product", "ETNs", "ETFs")

DOTNET_NULL_DATE = "0001-01-01T00:00:00"

_kz_holidays_cache: dict = {}


def _parse_float_safe(value):
    if value is None or value == "—" or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        s = str(value).replace(",", "").replace(" ", "").replace("%", "").strip()
        if s.upper() == "NULL" or s == "":
            return None
        return float(s)
    except Exception:
        return None


def _parse_date_safe(date_str):
    if not date_str or date_str == "—":
        return None
    if str(date_str).startswith(DOTNET_NULL_DATE):
        return None
    try:
        dt = date_parser.parse(date_str)
        if dt.tzinfo is not None:
            dt = dt.replace(tzinfo=None)
        return dt
    except Exception:
        return None


def _is_trading_day(d) -> bool:
    if d.weekday() >= 5:
        return False
    if not KZ_HOLIDAYS_AVAILABLE:
        return True
    year = d.year
    if year not in _kz_holidays_cache:
        _kz_holidays_cache[year] = holidays_lib.Kazakhstan(years=year)
    return d.date() not in _kz_holidays_cache[year]


def get_settlement_date(from_date=None):
    """T+2 TRADING days (not calendar days), matching AIX's own convention."""
    cur = from_date or datetime.now()
    added = 0
    while added < SETTLEMENT_DAYS:
        cur += timedelta(days=1)
        if _is_trading_day(cur):
            added += 1
    return cur


def _calculate_days_30_360(start_date, end_date):
    d1, m1, y1 = start_date.day, start_date.month, start_date.year
    d2, m2, y2 = end_date.day, end_date.month, end_date.year
    if d1 == 31:
        d1 = 30
    if d2 == 31 and d1 >= 30:
        d2 = 30
    return 360 * (y2 - y1) + 30 * (m2 - m1) + (d2 - d1)


def _day_count(start_date, end_date, year_basis):
    if year_basis == 360:
        return _calculate_days_30_360(start_date, end_date)
    return (end_date - start_date).days


def _calculate_years_to_maturity(start_date, end_date, year_basis):
    if year_basis == 360:
        return _calculate_days_30_360(start_date, end_date) / 360.0
    return (end_date - start_date).days / float(year_basis)


def _build_coupon_schedule_approx(
    settlement_date, maturity_date, coupon_freq, issue_date=None
):
    months_per_period = round(12 / coupon_freq)
    future_dates = [maturity_date]
    d = maturity_date
    for _ in range(2000):
        prev = d - relativedelta(months=months_per_period)
        if prev <= settlement_date:
            future_dates.reverse()
            if issue_date is not None and prev < issue_date:
                prev = issue_date
            return prev, future_dates
        d = prev
        future_dates.append(d)
    future_dates.reverse()
    if issue_date is not None and d < issue_date:
        d = issue_date
    return d, future_dates


def _get_coupon_schedule(
    settlement_date,
    maturity_date,
    coupon_freq,
    year_basis=365,
    coupon_details=None,
    issue_date=None,
):
    """Real coupon dates from /api/coupon-details when available, else an
    approximate regular grid counted back from maturity."""
    if coupon_details:
        periods = []
        for d in coupon_details:
            start = _parse_date_safe(d.get("coupon_date"))
            end = _parse_date_safe(d.get("payment_commencement_date"))
            if start and end and end > start:
                periods.append((start, end))
        periods.sort()
        future_dates = [end for start, end in periods if end > settlement_date]
        if future_dates:
            prev_coupon_date = periods[0][0]
            for start, end in periods:
                if start <= settlement_date:
                    prev_coupon_date = start
                else:
                    break
            return prev_coupon_date, future_dates
    return _build_coupon_schedule_approx(
        settlement_date, maturity_date, coupon_freq, issue_date
    )


def _calculate_ytm(
    face_value,
    current_price,
    coupon_rate,
    years_to_maturity,
    coupon_freq,
    settlement_date=None,
    maturity_date=None,
    year_basis=365,
    coupon_details=None,
    issue_date=None,
):
    try:
        price = face_value * current_price / 100.0
        if price <= 0 or face_value <= 0 or years_to_maturity <= 0 or coupon_freq <= 0:
            return None
        if not coupon_rate:
            return (
                2 * ((face_value / price) ** (1.0 / (2 * years_to_maturity)) - 1)
            ) * 100

        annual_coupon = face_value * coupon_rate / 100.0
        coupon_payment = annual_coupon / coupon_freq
        _, future_dates = _get_coupon_schedule(
            settlement_date,
            maturity_date,
            coupon_freq,
            year_basis,
            coupon_details,
            issue_date,
        )
        n = len(future_dates)
        nominal_period_days = year_basis / coupon_freq

        def bond_price(ytm_guess):
            ytm_period = ytm_guess / coupon_freq
            pv = 0.0
            for k, pay_date in enumerate(future_dates):
                t_periods = (
                    _day_count(settlement_date, pay_date, year_basis)
                    / nominal_period_days
                )
                cf = coupon_payment + (face_value if k == n - 1 else 0)
                pv += cf / ((1 + ytm_period) ** t_periods)
            return pv - price

        ytm = newton(bond_price, coupon_rate / 100.0, maxiter=100, tol=1e-6)
        return ytm * 100
    except Exception:
        return None


def _calculate_macaulay_duration(
    face_value,
    current_price,
    coupon_rate,
    years_to_maturity,
    coupon_freq,
    ytm,
    settlement_date=None,
    maturity_date=None,
    year_basis=365,
    coupon_details=None,
    issue_date=None,
):
    try:
        price = face_value * current_price / 100.0
        if price <= 0 or face_value <= 0 or years_to_maturity <= 0 or coupon_freq <= 0:
            return None
        if not coupon_rate:
            return years_to_maturity

        annual_coupon = face_value * coupon_rate / 100.0
        coupon_payment = annual_coupon / coupon_freq
        _, future_dates = _get_coupon_schedule(
            settlement_date,
            maturity_date,
            coupon_freq,
            year_basis,
            coupon_details,
            issue_date,
        )
        n = len(future_dates)
        nominal_period_days = year_basis / coupon_freq
        ytm_period = (ytm / 100.0) / coupon_freq

        weighted_cash_flows = 0
        for k, pay_date in enumerate(future_dates):
            days_to_pay = _day_count(settlement_date, pay_date, year_basis)
            t_periods = days_to_pay / nominal_period_days
            time_years = days_to_pay / year_basis
            cf = coupon_payment + (face_value if k == n - 1 else 0)
            pv = cf / ((1 + ytm_period) ** t_periods)
            weighted_cash_flows += time_years * pv

        return weighted_cash_flows / price
    except Exception:
        return None


def _calculate_modified_duration(macaulay_duration, ytm, coupon_freq):
    try:
        if macaulay_duration is None or ytm is None or not coupon_freq:
            return None
        return macaulay_duration / (1 + (ytm / 100.0) / coupon_freq)
    except Exception:
        return None


def _determine_bond_type_and_calculate_duration(bond_data: dict) -> dict:
    try:
        face_value = _parse_float_safe(bond_data.get("face_value"))
        current_price = _parse_float_safe(bond_data.get("reference_price"))
        coupon_rate = _parse_float_safe(bond_data.get("coupon_rate"))
        coupon_freq = _parse_float_safe(bond_data.get("coupon_freq"))
        year_basis_raw = bond_data.get("year_basis")
        maturity_date = bond_data.get("maturity_date")
        coupon_type = (bond_data.get("coupon_type") or "").strip()
        issue_date = bond_data.get("issue_date")
        coupon_details = bond_data.get("coupon_details")
        is_floating = coupon_type.lower() == "floating"

        empty = {
            "bond_type": None,
            "ytm": None,
            "macaulay_duration": None,
            "modified_duration": None,
        }

        if current_price is not None and current_price > 1000:
            return {**empty, "bond_type": "Аномальная цена"}

        year_basis = None
        if year_basis_raw is not None:
            try:
                parsed_basis = int(float(year_basis_raw))
                if 300 <= parsed_basis <= 370:
                    year_basis = parsed_basis
            except Exception:
                pass
        if year_basis is None:
            return {**empty, "bond_type": "Нет year_basis"}
        if face_value is None or current_price is None or face_value <= 0:
            return {**empty, "bond_type": "Недостаточно данных"}
        if maturity_date is None:
            return {**empty, "bond_type": "Нет даты погашения"}

        settlement_date = get_settlement_date()
        years_to_maturity = _calculate_years_to_maturity(
            settlement_date, maturity_date, year_basis
        )
        if years_to_maturity <= 0:
            return {**empty, "bond_type": "Погашена"}

        if is_floating and (coupon_rate is None or coupon_rate == 0):
            return {**empty, "bond_type": "Floating (ставка неизвестна)"}

        if coupon_rate is None or coupon_rate == 0:
            bond_type, coupon_freq_calc = "Дисконтная", 1
        else:
            bond_type = "Купонная"
            if not coupon_freq or coupon_freq <= 0:
                return {**empty, "bond_type": "Купонная (нет частоты купонов)"}
            coupon_freq_calc = int(coupon_freq)

        ytm = _calculate_ytm(
            face_value,
            current_price,
            coupon_rate or 0,
            years_to_maturity,
            coupon_freq_calc,
            settlement_date=settlement_date,
            maturity_date=maturity_date,
            year_basis=year_basis,
            coupon_details=coupon_details,
            issue_date=issue_date,
        )
        macaulay_duration = _calculate_macaulay_duration(
            face_value,
            current_price,
            coupon_rate or 0,
            years_to_maturity,
            coupon_freq_calc,
            ytm,
            settlement_date=settlement_date,
            maturity_date=maturity_date,
            year_basis=year_basis,
            coupon_details=coupon_details,
            issue_date=issue_date,
        )
        modified_duration = _calculate_modified_duration(
            macaulay_duration, ytm, coupon_freq_calc
        )

        if is_floating:
            bond_type = f"{bond_type} (Floating)"

        return {
            "bond_type": bond_type,
            "ytm": round(ytm, 4) if ytm is not None else None,
            "macaulay_duration": (
                round(macaulay_duration, 4) if macaulay_duration is not None else None
            ),
            "modified_duration": (
                round(modified_duration, 4) if modified_duration is not None else None
            ),
        }
    except Exception:
        return {
            "bond_type": "Ошибка расчета",
            "ytm": None,
            "macaulay_duration": None,
            "modified_duration": None,
        }


def _calculate_accrued_interest_pct(
    coupon_rate,
    coupon_freq,
    settlement_date,
    maturity_date,
    year_basis=365,
    issue_date=None,
    coupon_details=None,
    convention=None,
):
    """convention: '30/360' | 'ACT' | 'ACTACT' | None (defaults by year_basis).
    Not derivable from the API (verified: two bonds with an identical coupon
    period can use different denominators) - calibrated empirically instead,
    see calibrate_conventions()."""
    if not coupon_rate or not coupon_freq:
        return 0.0
    try:
        freq = int(coupon_freq)
        prev_coupon_date, future_dates = _get_coupon_schedule(
            settlement_date, maturity_date, freq, year_basis, coupon_details, issue_date
        )
        if convention is None:
            convention = "30/360" if year_basis == 360 else "ACT"

        if convention == "30/360":
            elapsed = _calculate_days_30_360(prev_coupon_date, settlement_date)
            denom = year_basis / freq
        elif convention == "ACTACT":
            elapsed = (settlement_date - prev_coupon_date).days
            denom = (
                (future_dates[0] - prev_coupon_date).days
                if future_dates
                else year_basis / freq
            )
        else:
            elapsed = (settlement_date - prev_coupon_date).days
            denom = year_basis / freq

        return (coupon_rate / freq) * (elapsed / denom) if denom else 0.0
    except Exception:
        return 0.0


def _calibrate_conventions(
    profiles, coupon_details_map, aix_accrued_map, settlement_date
) -> dict:
    result = {}
    for ticker, profile in profiles.items():
        cr = _parse_float_safe(profile.get("couponRate"))
        cf = _parse_float_safe(profile.get("couponFreq"))
        mat = _parse_date_safe(profile.get("maturityDate"))
        iss = _parse_date_safe(profile.get("issueDate"))
        yb = _parse_float_safe(profile.get("yearBasis"))
        aix = _parse_float_safe(aix_accrued_map.get(ticker))
        cd = coupon_details_map.get(ticker)
        if (
            not cr
            or not cf
            or not mat
            or not yb
            or aix is None
            or aix == 0
            or mat <= settlement_date
        ):
            continue
        candidates = {
            conv: _calculate_accrued_interest_pct(
                cr, cf, settlement_date, mat, yb, iss, cd, convention=conv
            )
            for conv in ("30/360", "ACT", "ACTACT")
        }
        matches = [c for c, v in candidates.items() if abs(v - aix) < 0.0005]
        distinct = len({round(v, 6) for v in candidates.values()})
        if len(matches) == 1 and distinct > 1:
            result[ticker] = matches[0]
    return result


# === Cache & network I/O ===


def _load_cache() -> dict:
    if not config.AIX_CACHE_FILE.exists():
        return {}
    try:
        with open(config.AIX_CACHE_FILE, encoding="utf-8") as f:
            cache = json.load(f)
        cache_time = datetime.fromisoformat(cache.get("timestamp", "2000-01-01"))
        if datetime.now() - cache_time > timedelta(days=config.AIX_CACHE_DAYS):
            return {}
        return cache
    except Exception:
        return {}


def _save_cache(cache: dict) -> None:
    try:
        cache["timestamp"] = datetime.now().isoformat()
        with open(config.AIX_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


async def _get_bonds_from_market(session) -> list[dict]:
    bonds_list = []
    try:
        async with session.get(
            MAIN_RECORDS_URL,
            timeout=aiohttp.ClientTimeout(total=config.REQUEST_TIMEOUT),
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
        for item in data:
            ticker = (item.get("secCode") or "").strip()
            currency = (item.get("currency") or "").replace("*", "").strip()
            state = (item.get("state") or "").strip()
            instrument = item.get("instrument") or ""
            if not ticker or currency == "KZT" or state != "Active":
                continue
            if not any(kw in instrument for kw in BOND_INSTRUMENT_KEYWORDS) or any(
                kw in instrument for kw in NON_BOND_INSTRUMENT_KEYWORDS
            ):
                continue
            bonds_list.append(
                {
                    "ticker": ticker,
                    "currency": currency,
                    "instrument_category": instrument,
                    "number_of_trades_today": item.get("numberOfTrades"),
                    "market_reference_price": item.get("referencePrice"),
                }
            )
    except Exception:
        pass
    return bonds_list


async def _fetch_profile(session, ticker, semaphore, cache):
    if ticker in cache.get("profiles", {}):
        return ticker, cache["profiles"][ticker]
    async with semaphore:
        try:
            async with session.get(
                PROFILE_URL.format(ticker=ticker),
                timeout=aiohttp.ClientTimeout(total=config.REQUEST_TIMEOUT),
            ) as resp:
                resp.raise_for_status()
                profile = await resp.json()
            cache.setdefault("profiles", {})[ticker] = profile
            return ticker, profile
        except Exception:
            return ticker, None


async def _fetch_coupon_details(session, ticker, semaphore, cache):
    if ticker in cache.get("coupon_details", {}):
        return ticker, cache["coupon_details"][ticker]
    async with semaphore:
        try:
            async with session.get(
                COUPON_DETAILS_URL.format(ticker=ticker),
                timeout=aiohttp.ClientTimeout(total=config.REQUEST_TIMEOUT),
            ) as resp:
                resp.raise_for_status()
                details = await resp.json()
            details = details or None
            cache.setdefault("coupon_details", {})[ticker] = details
            return ticker, details
        except Exception:
            return ticker, None


async def _get_accrued_interest(session) -> dict:
    accrued = {}
    try:
        async with session.get(
            ACCRUED_INTEREST_URL,
            timeout=aiohttp.ClientTimeout(total=config.REQUEST_TIMEOUT),
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
        for item in data:
            ticker = item.get("trading_ticker")
            if ticker:
                accrued[ticker] = item.get("accrued_interest")
    except Exception:
        pass
    return accrued


async def fetch_aix_bonds_async() -> pd.DataFrame:
    """Fetch, cache and normalize all AIX foreign-currency (non-KZT) bonds."""
    cache = _load_cache()

    async with aiohttp.ClientSession(headers={"User-Agent": "Mozilla/5.0"}) as session:
        bonds_list = await _get_bonds_from_market(session)
        if not bonds_list:
            return pd.DataFrame()

        accrued_interest_map = await _get_accrued_interest(session)

        semaphore = asyncio.Semaphore(config.MAX_CONCURRENT_REQUESTS)
        profile_results = dict(
            await asyncio.gather(
                *[
                    _fetch_profile(session, b["ticker"], semaphore, cache)
                    for b in bonds_list
                ]
            )
        )
        coupon_details_results = dict(
            await asyncio.gather(
                *[
                    _fetch_coupon_details(session, b["ticker"], semaphore, cache)
                    for b in bonds_list
                ]
            )
        )

    settlement_date_calib = get_settlement_date()
    known = cache.setdefault("conventions", {})
    uncalibrated_profiles = {
        b["ticker"]: profile_results[b["ticker"]]
        for b in bonds_list
        if b["ticker"] in profile_results
        and profile_results[b["ticker"]]
        and b["ticker"] not in known
    }
    if uncalibrated_profiles:
        known.update(
            _calibrate_conventions(
                uncalibrated_profiles,
                coupon_details_results,
                accrued_interest_map,
                settlement_date_calib,
            )
        )

    _save_cache(cache)

    rows = []
    for b in bonds_list:
        profile = profile_results.get(b["ticker"])
        if profile is None:
            continue

        market_price = _parse_float_safe(b["market_reference_price"])
        profile_price = _parse_float_safe(profile.get("referencePrice"))
        face_value_check = _parse_float_safe(profile.get("faceValue"))

        current_price = market_price
        price_source = "market"
        if current_price is not None and (
            current_price > 1000
            or (
                face_value_check
                and face_value_check > 1000
                and abs(current_price - face_value_check) < 0.01
            )
        ):
            current_price = profile_price
            price_source = "profile_fallback_anomalous"
        elif current_price is None:
            current_price = profile_price
            price_source = "profile_fallback_no_market_price"

        clean_price = current_price

        coupon_rate_val = _parse_float_safe(profile.get("couponRate"))
        coupon_freq_val = _parse_float_safe(profile.get("couponFreq"))
        maturity_date_val = _parse_date_safe(profile.get("maturityDate"))
        issue_date_val = _parse_date_safe(profile.get("issueDate"))
        year_basis_val = _parse_float_safe(profile.get("yearBasis")) or 365
        settlement_date = get_settlement_date()
        coupon_details_val = coupon_details_results.get(b["ticker"])
        bond_convention = known.get(b["ticker"])

        accrued = 0.0
        if (
            coupon_rate_val
            and coupon_freq_val
            and maturity_date_val
            and maturity_date_val > settlement_date
        ):
            accrued = _calculate_accrued_interest_pct(
                coupon_rate_val,
                coupon_freq_val,
                settlement_date,
                maturity_date_val,
                year_basis_val,
                issue_date_val,
                coupon_details_val,
                convention=bond_convention,
            )

        accrued_aix = _parse_float_safe(accrued_interest_map.get(b["ticker"]))
        likely_capitalizing = False
        if coupon_rate_val and coupon_freq_val and accrued_aix is not None:
            if accrued_aix > (coupon_rate_val / coupon_freq_val) * 1.1:
                likely_capitalizing = True
                accrued = accrued_aix

        dirty_price = (
            current_price + accrued
            if current_price is not None and accrued
            else current_price
        )

        merged = {
            "face_value": profile.get("faceValue"),
            "reference_price": dirty_price,
            "coupon_rate": profile.get("couponRate"),
            "coupon_freq": profile.get("couponFreq"),
            "year_basis": profile.get("yearBasis"),
            "maturity_date": maturity_date_val,
            "issuer": profile.get("issuer"),
            "coupon_type": profile.get("couponType"),
            "issue_date": issue_date_val,
            "coupon_details": coupon_details_val,
        }
        duration_result = _determine_bond_type_and_calculate_duration(merged)

        quality_flags = []
        if price_source != "market":
            quality_flags.append(price_source)
        if likely_capitalizing:
            quality_flags.append("capitalizing_coupon")
        if duration_result["bond_type"] in ("Floating (ставка неизвестна)",):
            quality_flags.append("floating_unknown_rate")
        if duration_result["bond_type"] in (
            "Аномальная цена",
            "Недостаточно данных",
            "Нет year_basis",
            "Нет даты погашения",
            "Погашена",
            "Ошибка расчета",
        ):
            quality_flags.append("calc_unavailable")
        if not (b.get("number_of_trades_today") or 0):
            quality_flags.append("no_trades_today")

        rows.append(
            {
                "exchange": "AIX",
                "ticker": b["ticker"],
                "isin": None,
                "currency": b["currency"],
                "issuer": merged["issuer"] or None,
                "instrument_type": duration_result["bond_type"],
                "market_segment": b["instrument_category"],
                "clean_price": clean_price,
                "accrued_interest": round(accrued, 4) if accrued else 0,
                "dirty_price": dirty_price,
                "coupon_rate": coupon_rate_val,
                "coupon_freq": coupon_freq_val,
                "day_count_basis": year_basis_val,
                "last_coupon_date": None,
                "maturity_date": (
                    maturity_date_val.strftime("%Y-%m-%d")
                    if maturity_date_val
                    else None
                ),
                "days_to_maturity": (
                    (maturity_date_val - settlement_date).days
                    if maturity_date_val
                    else None
                ),
                "trade_date": None,
                "trading_regime": None,
                "ytm": duration_result["ytm"],
                "ytm_exchange_reported": None,
                "ytm_total_return_reported": None,
                "macaulay_duration": duration_result["macaulay_duration"],
                "modified_duration": duration_result["modified_duration"],
                "issue_volume": _parse_float_safe(merged["face_value"]),
                "_quality_flags": quality_flags,
            }
        )

    return pd.DataFrame(rows)


def fetch_aix_bonds() -> pd.DataFrame:
    """Sync wrapper around fetch_aix_bonds_async for non-async callers."""
    return asyncio.run(fetch_aix_bonds_async())
