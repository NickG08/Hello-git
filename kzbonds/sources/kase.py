"""
KASE (Kazakhstan Stock Exchange) foreign-currency bonds source.

Adapted from the standalone KASE_bonds_v4.py scraper: same data acquisition
and YTM/duration math, repackaged as an importable async fetcher that
returns a DataFrame instead of writing an Excel file. Calculation logic is
kept as close to the original as possible - it was already validated
against KASE's own published numbers.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import date, datetime

import aiohttp
import pandas as pd
import requests
from dateutil.relativedelta import relativedelta
from scipy.optimize import brentq

from kzbonds import config

SECURITIES_URL = "https://kase.kz/api/instruments/securities/"
BOND_CHARACTERISTICS_URL = (
    "https://kase.kz/api/instruments/bonds/{ticker}/characteristics/?language=ru"
)
GSEC_CHARACTERISTICS_URL = (
    "https://kase.kz/api/instruments/gsecs/{ticker}/characteristics/?language=ru"
)

CORP_BONDS_PARAMS = {
    "ticker_category": "alternative_commercial_bonds,alternative_bonds,main_bonds,main_commercial_bonds,private_bonds",
    "category": "bond",
    "is_excluded": "false",
    "ordering": "currency_type__nulls_last",
}
GSEC_PARAMS = {"category": "gsec", "is_excluded": "false"}


# === Cache (characteristics only - static per-issue data) ===


def _load_cache() -> dict:
    if not config.KASE_CACHE_FILE.exists():
        return {}
    try:
        with open(config.KASE_CACHE_FILE, encoding="utf-8") as f:
            cache = json.load(f)
        cache_time = datetime.fromisoformat(cache.get("timestamp", "2000-01-01"))
        age_hours = (datetime.now() - cache_time).total_seconds() / 3600
        if age_hours > config.KASE_CACHE_HOURS:
            return {}
        return cache
    except Exception:
        return {}


def _save_cache(cache: dict) -> None:
    try:
        cache["timestamp"] = datetime.now().isoformat()
        with open(config.KASE_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


async def _fetch_characteristics_one(session, ticker, bond_type, semaphore):
    async with semaphore:
        try:
            url = (
                GSEC_CHARACTERISTICS_URL
                if bond_type == "gsec"
                else BOND_CHARACTERISTICS_URL
            ).format(ticker=ticker)
            async with session.get(
                url, timeout=aiohttp.ClientTimeout(total=config.REQUEST_TIMEOUT)
            ) as resp:
                if resp.status == 200:
                    return ticker, await resp.json()
                return ticker, {}
        except Exception:
            return ticker, {}


async def _fetch_all_characteristics(bonds: list[dict]) -> dict:
    cache = _load_cache()
    cached_chars = cache.get("characteristics", {})
    to_fetch = [
        (b.get("code"), b.get("_bond_type", "corp"))
        for b in bonds
        if b.get("code") not in cached_chars
    ]

    if to_fetch:
        semaphore = asyncio.Semaphore(config.MAX_CONCURRENT_REQUESTS)
        async with aiohttp.ClientSession(trust_env=True) as session:
            tasks = [
                _fetch_characteristics_one(session, t, bt, semaphore)
                for t, bt in to_fetch
            ]
            results = await asyncio.gather(*tasks)
        cache.setdefault("characteristics", {})
        for ticker, data in results:
            if data:
                cache["characteristics"][ticker] = data
        _save_cache(cache)

    return cache.get("characteristics", cached_chars)


async def _fetch_boards_one(session, ticker, bond_type, semaphore):
    async with semaphore:
        ep = "gsecs" if bond_type == "gsec" else "bonds"
        url = f"https://kase.kz/api/instruments/{ep}/{ticker}/?language=ru"
        try:
            async with session.get(
                url, timeout=aiohttp.ClientTimeout(total=config.REQUEST_TIMEOUT)
            ) as resp:
                if resp.status == 200:
                    d = await resp.json()
                    return ticker, d.get("boards_en", {})
        except Exception:
            pass
        return ticker, None


async def _fetch_all_boards(bonds: list[dict]) -> dict:
    """
    boards_en reflects CURRENT trading state, not static issue data, so
    (unlike characteristics) it is never cached to disk.
    """
    semaphore = asyncio.Semaphore(config.MAX_CONCURRENT_REQUESTS)
    async with aiohttp.ClientSession(trust_env=True) as session:
        tasks = [
            _fetch_boards_one(
                session, b.get("code"), b.get("_bond_type", "corp"), semaphore
            )
            for b in bonds
        ]
        results = await asyncio.gather(*tasks)
    return {t: v for t, v in results if v is not None}


def _format_trading_regime(boards_en) -> str:
    if not boards_en:
        return "—"
    has_tplus = any("T+" in v for v in boards_en.values())
    has_t0 = any("T0" in v for v in boards_en.values())
    if has_tplus and has_t0:
        return "T0 + T+"
    if has_tplus:
        return "T+"
    if has_t0:
        return "T0"
    return "нестанд. лоты"


def _get_all_bonds() -> list[dict]:
    # requests.Response.json() re-parses the body into fresh objects on every
    # call - each category's response must be parsed exactly once into a
    # variable before tagging, or the "_bond_type" mutation lands on
    # throwaway dicts and every bond silently ends up untagged.
    all_bonds = []
    try:
        r = requests.get(SECURITIES_URL, params=CORP_BONDS_PARAMS, timeout=30)
        r.raise_for_status()
        corp_bonds = r.json()
        for b in corp_bonds:
            b["_bond_type"] = "corp"
        all_bonds.extend(corp_bonds)
    except Exception:
        pass
    try:
        r = requests.get(SECURITIES_URL, params=GSEC_PARAMS, timeout=30)
        r.raise_for_status()
        gsec_bonds = r.json()
        for b in gsec_bonds:
            b["_bond_type"] = "gsec"
        all_bonds.extend(gsec_bonds)
    except Exception:
        pass
    return all_bonds


def _filter_fx_bonds(bonds: list[dict]) -> list[dict]:
    return [
        b for b in bonds if b.get("currency_type") and b.get("currency_type") != "KZT"
    ]


def _get_bond_main_api(ticker, bond_type="corp") -> dict:
    try:
        ep = "gsecs" if bond_type == "gsec" else "bonds"
        r = requests.get(
            f"https://kase.kz/api/instruments/{ep}/{ticker}/?language=ru", timeout=30
        )
        r.raise_for_status()
        return r.json()
    except Exception:
        return {}


def _parse_bond_html_fallback(ticker: str) -> dict:
    """Last-resort HTML scrape when the API is missing coupon/maturity."""
    try:
        r = requests.get(f"https://kase.kz/ru/bonds/show/{ticker}/", timeout=20)
        if r.status_code != 200:
            return {}
        text = r.text
        result = {}

        finish_match = re.search(
            rf'"code":"{ticker}"[^}}]*?"finish_date":"(\d{{4}}-\d{{2}}-\d{{2}})"', text
        )
        if finish_match:
            result["finish_date"] = finish_match.group(1)

        cupon_match = re.search(rf'"code":"{ticker}"[^}}]*?"cupon":(\d+\.?\d*)', text)
        if cupon_match:
            result["cupon"] = float(cupon_match.group(1))

        if "cupon" not in result:
            for pattern in (
                rf"{ticker}[;\s][^%]*?(\d+[,\.]\d+)\s*%\s*годовых",
                rf"{ticker}[^%]*?купон[,\s]+(\d+[,\.]\d+)\s*%",
            ):
                m = re.search(pattern, text, re.IGNORECASE)
                if m:
                    result["cupon"] = float(m.group(1).replace(",", "."))
                    break

        ticker_context = re.search(rf"{ticker}[^{{}}]{{0,500}}", text, re.IGNORECASE)
        if ticker_context:
            ctx = ticker_context.group().lower()
            if "ежемесячный" in ctx or "месячный купон" in ctx:
                result["frequency"] = 12
            elif "квартальный" in ctx:
                result["frequency"] = 4
            elif "полугодовой" in ctx:
                result["frequency"] = 2
            elif "годовой" in ctx or "ежегодный" in ctx:
                result["frequency"] = 1

        if "finish_date" not in result:
            period_match = re.search(
                rf"{ticker}[^;]*?(\d{{2}}\.\d{{2}}\.\d{{2}})\s*[–-]\s*(\d{{2}}\.\d{{2}}\.\d{{2}})",
                text,
            )
            if period_match:
                try:
                    dt = datetime.strptime(period_match.group(2), "%d.%m.%y")
                    result["finish_date"] = dt.strftime("%Y-%m-%d")
                except Exception:
                    result["finish_date"] = period_match.group(2)

        return result
    except Exception:
        return {}


# === Date / day-count helpers ===


def _parse_date(date_str) -> date | None:
    if not date_str:
        return None
    date_str = str(date_str).replace("&ndash;", "-").replace("&mdash;", "-")
    if " - " in date_str or " – " in date_str:
        date_str = re.split(r"\s*[-–]\s*", date_str)[0].strip()
    if "T" in date_str:
        date_str = date_str.split("T")[0]
    for fmt in ("%Y-%m-%d", "%d.%m.%y", "%d.%m.%Y"):
        try:
            return datetime.strptime(date_str, fmt).date()
        except Exception:
            continue
    return None


def _days_30_360(d1, d2):
    if not d1 or not d2:
        return None
    y1, m1, day1 = d1.year, d1.month, min(d1.day, 30)
    y2, m2, day2 = d2.year, d2.month, min(d2.day, 30)
    return (y2 - y1) * 360 + (m2 - m1) * 30 + (day2 - day1)


def _calculate_coupon_frequency(prev_coupon_date, next_coupon_date) -> int:
    if not prev_coupon_date or not next_coupon_date:
        return 2
    prev_date, next_date = _parse_date(prev_coupon_date), _parse_date(next_coupon_date)
    if not prev_date or not next_date:
        return 2
    period_days = (next_date - prev_date).days
    if period_days <= 0:
        return 2
    if 355 <= period_days <= 375:
        return 1
    if 175 <= period_days <= 190:
        return 2
    if 85 <= period_days <= 95:
        return 4
    if 28 <= period_days <= 32:
        return 12
    freq = round(365 / period_days)
    return freq if freq in (1, 2, 4, 12) else 2


def _calculate_accrued_interest(
    coupon_rate, last_coupon_date, settlement_date, basis, frequency
):
    if not coupon_rate or not last_coupon_date:
        return None
    try:
        if isinstance(last_coupon_date, str):
            last_coupon_date = _parse_date(last_coupon_date)
        if isinstance(settlement_date, str):
            settlement_date = _parse_date(settlement_date)
        if not last_coupon_date or not settlement_date:
            return None
        if basis in (360, "360"):
            days_accrued = _days_30_360(last_coupon_date, settlement_date)
            days_in_period = 360 / frequency
        else:
            days_accrued = (settlement_date - last_coupon_date).days
            days_in_period = 365 / frequency
        if days_accrued is None or days_accrued < 0:
            return None
        accrued = (coupon_rate / frequency) * (days_accrued / days_in_period)
        return round(accrued, 4)
    except Exception:
        return None


def _calculate_ytm_irr(
    clean_price,
    coupon_rate,
    settlement_date,
    maturity_date,
    frequency,
    basis,
    face_value=100,
):
    if not clean_price or not maturity_date:
        return None
    try:
        if isinstance(maturity_date, str):
            maturity_date = _parse_date(maturity_date)
        if isinstance(settlement_date, str):
            settlement_date = _parse_date(settlement_date)
        if not maturity_date or not settlement_date or settlement_date >= maturity_date:
            return None

        price = clean_price * face_value / 100

        if basis in (360, "360"):
            days_to_maturity, year_days = (
                _days_30_360(settlement_date, maturity_date),
                360,
            )
        else:
            days_to_maturity, year_days = (maturity_date - settlement_date).days, 365

        if not days_to_maturity or days_to_maturity <= 0:
            return None

        years_to_maturity = days_to_maturity / year_days

        if not coupon_rate:
            # Discount bond: no coupons, single redemption of face_value at
            # maturity - closed-form bond-equivalent yield (same convention
            # AIX's scraper already uses for its zero-coupon case).
            if price <= 0:
                return None
            return round(
                (2 * ((face_value / price) ** (1.0 / (2 * years_to_maturity)) - 1))
                * 100,
                4,
            )

        coupon_payment = (coupon_rate / frequency) * face_value / 100
        n_periods = int(years_to_maturity * frequency) + 1
        if n_periods <= 0:
            return None

        def npv(ytm):
            if ytm <= -1:
                return float("inf")
            pv = 0
            for i in range(1, n_periods + 1):
                cf = coupon_payment + (face_value if i == n_periods else 0)
                pv += cf / ((1 + ytm / frequency) ** i)
            return pv - price

        try:
            return round(brentq(npv, -0.99, 2.0, maxiter=1000) * 100, 4)
        except Exception:
            return None
    except Exception:
        return None


def _calculate_macaulay_duration(
    clean_price,
    coupon_rate,
    settlement_date,
    maturity_date,
    ytm,
    frequency,
    basis,
    face_value=100,
):
    if not clean_price or not maturity_date or not ytm:
        return None
    try:
        if isinstance(maturity_date, str):
            maturity_date = _parse_date(maturity_date)
        if isinstance(settlement_date, str):
            settlement_date = _parse_date(settlement_date)
        if not maturity_date or not settlement_date or settlement_date >= maturity_date:
            return None

        ytm_decimal = ytm / 100

        if basis in (360, "360"):
            days_to_maturity, year_days = (
                _days_30_360(settlement_date, maturity_date),
                360,
            )
        else:
            days_to_maturity, year_days = (maturity_date - settlement_date).days, 365

        if not days_to_maturity or days_to_maturity <= 0:
            return None

        years_to_maturity = days_to_maturity / year_days

        if not coupon_rate:
            # Discount bond: the whole cash flow is the redemption at
            # maturity, so Macaulay duration is just time to maturity.
            return round(years_to_maturity, 4)

        coupon_payment = (coupon_rate / frequency) * face_value / 100
        n_periods = int(years_to_maturity * frequency) + 1
        if n_periods <= 0:
            return None

        weighted_sum, total_pv = 0, 0
        for i in range(1, n_periods + 1):
            t = i / frequency
            cf = coupon_payment + (face_value if i == n_periods else 0)
            pv = cf / ((1 + ytm_decimal / frequency) ** i)
            weighted_sum += t * pv
            total_pv += pv

        return round(weighted_sum / total_pv, 4) if total_pv else None
    except Exception:
        return None


def _calculate_modified_duration(macaulay_duration, ytm, frequency):
    if not macaulay_duration or not ytm:
        return None
    try:
        return round(macaulay_duration / (1 + (ytm / 100) / frequency), 4)
    except Exception:
        return None


def _extract_characteristic_value(characteristics, key):
    if not characteristics:
        return None
    char = characteristics.get(key, {})
    return char.get("value") if isinstance(char, dict) else char


def _extract_coupon_from_description(characteristics):
    if not characteristics:
        return None
    desc = characteristics.get("coupon_description_ru", "")
    if not desc:
        return None
    match = re.search(r"ставка[^:]*:\s*([\d,\.]+)", desc, re.IGNORECASE)
    if match:
        try:
            return float(match.group(1).replace(",", "."))
        except Exception:
            return None
    return None


def _extract_characteristic_by_label(characteristics, label_search):
    if not characteristics:
        return None
    for val in characteristics.values():
        if (
            isinstance(val, dict)
            and "label" in val
            and label_search.lower() in val.get("label", "").lower()
        ):
            return val.get("value")
    return None


def _process_bond(
    bond: dict, settlement_date: date, all_characteristics: dict, all_boards: dict
) -> dict:
    ticker = bond.get("code")
    bond_type_flag = bond.get("_bond_type", "corp")

    currency = bond.get("currency_type")
    clean_price = bond.get("price") or bond.get("close_price")

    # GSEC bonds are quoted as an absolute fraction of par (0.xx) - normalize to %.
    if clean_price and clean_price < 2 and bond_type_flag == "gsec":
        clean_price = clean_price * 100

    ytm_kase_dohod = bond.get(
        "dohod"
    )  # actual KASE YTM (NOT the 'ytm' field, which is DTM/365)
    dohod_total = bond.get("dohod_total")
    dtm = bond.get("dtm")
    date0 = bond.get("date0")
    volume_release = bond.get("volume_release")
    emitter = bond.get("org_short_name_ru")

    ticker_data = bond.get("ticker", {})
    coupon_rate = ticker_data.get("cupon")
    maturity_date = ticker_data.get("finish_date")

    basis_raw = ticker_data.get("basis", "360")
    if basis_raw == "360":
        basis = 360
    elif basis_raw == "365":
        basis = 365
    elif basis_raw in ("actual", "Actual"):
        basis = 365
    else:
        basis = 360

    bond_type = ticker_data.get("typesec_ru")
    category = ticker_data.get("ticker_category", "")
    nbrk_view = ticker_data.get("nbrk_view_ru")
    # Genuinely zero-coupon bonds (e.g. discount T-bills) report cupon=null
    # by design - don't treat that as "coupon missing, go find it".
    is_discount = ticker_data.get("typesec_en") == "discount"

    characteristics = all_characteristics.get(ticker, {})
    trading_regime = _format_trading_regime((all_boards or {}).get(ticker, {}))
    isin = _extract_characteristic_by_label(characteristics, "ISIN")

    # KASE's characteristics API exposes a nominal-value label on at least
    # some issues; label text unconfirmed against a live response, so this
    # is a best-effort lookup with the same 100 fallback the YTM/duration
    # math below already assumes (corp bonds are quoted per 100 of par).
    face_value_raw = _extract_characteristic_by_label(characteristics, "Номинал")
    try:
        face_value = float(str(face_value_raw).replace(",", ".").replace(" ", ""))
        if face_value <= 0:
            face_value = 100.0
    except (TypeError, ValueError):
        face_value = 100.0

    last_coupon_date_str = _extract_characteristic_value(
        characteristics, "coupon_prev_date"
    )
    next_coupon_date_str = _extract_characteristic_value(
        characteristics, "coupon_near_pay_date"
    )
    last_coupon_date = _parse_date(last_coupon_date_str)
    next_coupon_date = _parse_date(next_coupon_date_str)
    frequency = _calculate_coupon_frequency(last_coupon_date_str, next_coupon_date_str)

    quality_flags: list[str] = []

    if not last_coupon_date and next_coupon_date and frequency:
        calc_prev = next_coupon_date - relativedelta(months=12 // frequency)
        if calc_prev < settlement_date:
            last_coupon_date = calc_prev
            last_coupon_date_str = calc_prev.isoformat()

    if not coupon_rate and not is_discount:
        main_api_characteristics = _get_bond_main_api(ticker, bond_type_flag).get(
            "characteristics", {}
        )
        coupon_from_desc = _extract_coupon_from_description(main_api_characteristics)
        if coupon_from_desc:
            coupon_rate = coupon_from_desc
            quality_flags.append("coupon_from_description")

    if (not coupon_rate and not is_discount) or not maturity_date:
        html_data = _parse_bond_html_fallback(ticker)
        if html_data:
            if not coupon_rate and not is_discount and "cupon" in html_data:
                coupon_rate = html_data["cupon"]
                quality_flags.append("html_fallback")
            if not maturity_date and "finish_date" in html_data:
                maturity_date = html_data["finish_date"]
                quality_flags.append("html_fallback")
            if frequency == 2 and "frequency" in html_data:
                frequency = html_data["frequency"]

    nkd_percent = _calculate_accrued_interest(
        coupon_rate, last_coupon_date, settlement_date, basis, frequency
    )
    # Discount bonds accrue nothing (nkd_percent is None), so dirty == clean,
    # same convention the AIX source uses.
    dirty_price = (
        clean_price + nkd_percent if clean_price and nkd_percent else clean_price
    )

    ytm_calc_irr = _calculate_ytm_irr(
        clean_price, coupon_rate, settlement_date, maturity_date, frequency, basis
    )
    ytm_for_duration = ytm_calc_irr or ytm_kase_dohod
    macaulay_duration = _calculate_macaulay_duration(
        clean_price,
        coupon_rate,
        settlement_date,
        maturity_date,
        ytm_for_duration,
        frequency,
        basis,
    )
    modified_duration = _calculate_modified_duration(
        macaulay_duration, ytm_for_duration, frequency
    )

    if clean_price is None:
        quality_flags.append("no_price")
    elif trading_regime == "—":
        quality_flags.append("not_currently_traded")
    if ytm_calc_irr is None and clean_price is not None:
        quality_flags.append("ytm_calc_failed")
    if not coupon_rate and not is_discount:
        quality_flags.append("no_coupon_rate")

    return {
        "exchange": "KASE",
        "ticker": ticker,
        "isin": isin,
        "currency": currency,
        "issuer": emitter,
        "instrument_type": nbrk_view or bond_type,
        "market_segment": category or "unknown",
        "face_value": face_value,
        "face_currency": currency,
        "clean_price": round(clean_price, 4) if clean_price else None,
        "accrued_interest": nkd_percent,
        "dirty_price": round(dirty_price, 4) if dirty_price else None,
        "coupon_rate": coupon_rate,
        "coupon_freq": frequency,
        "day_count_basis": "ACT/ACT (365)" if basis_raw == "actual" else basis,
        "last_coupon_date": last_coupon_date_str,
        "maturity_date": maturity_date,
        "days_to_maturity": dtm,
        "trade_date": date0,
        "trading_regime": trading_regime,
        "ytm": ytm_calc_irr if ytm_calc_irr is not None else ytm_kase_dohod,
        "ytm_exchange_reported": ytm_kase_dohod,
        "ytm_total_return_reported": dohod_total,
        "macaulay_duration": macaulay_duration,
        "modified_duration": modified_duration,
        "issue_volume": volume_release,
        "_quality_flags": quality_flags,
    }


async def fetch_kase_bonds_async(settlement_date: date | None = None) -> pd.DataFrame:
    """Fetch, cache and normalize all KASE foreign-currency (non-KZT) bonds."""
    settlement_date = settlement_date or date.today()
    all_bonds = _get_all_bonds()
    fx_bonds = _filter_fx_bonds(all_bonds)
    if not fx_bonds:
        return pd.DataFrame()

    all_characteristics = await _fetch_all_characteristics(fx_bonds)
    all_boards = await _fetch_all_boards(fx_bonds)

    rows = []
    for bond in fx_bonds:
        try:
            rows.append(
                _process_bond(bond, settlement_date, all_characteristics, all_boards)
            )
        except Exception:
            continue
    return pd.DataFrame(rows)


def fetch_kase_bonds(settlement_date: date | None = None) -> pd.DataFrame:
    """Sync wrapper around fetch_kase_bonds_async for non-async callers."""
    return asyncio.run(fetch_kase_bonds_async(settlement_date))
