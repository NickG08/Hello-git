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
import logging
import re
import time
from datetime import date, datetime

import aiohttp
import pandas as pd
import requests
from dateutil.relativedelta import relativedelta
from scipy.optimize import brentq

from kzbonds import config, prospectus

logger = logging.getLogger(__name__)

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


async def _get_json_with_retry(session, url, attempts=3, base_delay=0.6):
    """A single transient failure (proxy hiccup) must not silently cost a
    bond its ISIN/coupon data or wrongly flag a liquid bond as untraded -
    retry a few times with backoff before giving up."""
    for attempt in range(attempts):
        try:
            async with session.get(
                url, timeout=aiohttp.ClientTimeout(total=config.REQUEST_TIMEOUT)
            ) as resp:
                if resp.status == 200:
                    return await resp.json()
                return None
        except Exception:
            if attempt < attempts - 1:
                await asyncio.sleep(base_delay * (attempt + 1))
    return None


async def _fetch_characteristics_one(session, ticker, bond_type, semaphore):
    async with semaphore:
        url = (
            GSEC_CHARACTERISTICS_URL
            if bond_type == "gsec"
            else BOND_CHARACTERISTICS_URL
        ).format(ticker=ticker)
        data = await _get_json_with_retry(session, url)
        return ticker, (data or {})


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
        failed = []
        for ticker, data in results:
            if data:
                cache["characteristics"][ticker] = data
            else:
                failed.append(ticker)
        _save_cache(cache)
        if failed:
            logger.warning(
                "KASE: %d/%d bonds missing characteristics after retries (ISIN/coupon may be incomplete): %s",
                len(failed),
                len(to_fetch),
                ", ".join(failed[:20]) + (" ..." if len(failed) > 20 else ""),
            )

    return cache.get("characteristics", cached_chars)


async def _fetch_boards_one(session, ticker, bond_type, semaphore):
    async with semaphore:
        ep = "gsecs" if bond_type == "gsec" else "bonds"
        url = f"https://kase.kz/api/instruments/{ep}/{ticker}/?language=ru"
        data = await _get_json_with_retry(session, url)
        if data is None:
            return ticker, None
        return ticker, data.get("boards_en", {})


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
    failed = [t for t, v in results if v is None]
    if failed:
        logger.warning(
            "KASE: %d/%d bonds missing trading-regime data after retries "
            "(may be wrongly flagged not_currently_traded): %s",
            len(failed),
            len(bonds),
            ", ".join(failed[:20]) + (" ..." if len(failed) > 20 else ""),
        )
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


def _get_sync_with_retry(url, params=None, timeout=30, attempts=3, base_delay=0.8):
    """Same rationale as _get_json_with_retry, for the sync `requests` calls
    (the bulk securities list, HTML fallback) - these failing silently would
    drop half the universe (one whole category) rather than one bond."""
    last_exc = None
    for attempt in range(attempts):
        try:
            r = requests.get(url, params=params, timeout=timeout)
            r.raise_for_status()
            return r
        except Exception as e:
            last_exc = e
            if attempt < attempts - 1:
                time.sleep(base_delay * (attempt + 1))
    logger.warning("GET %s failed after %d attempts: %s", url, attempts, last_exc)
    return None


def _get_all_bonds() -> list[dict]:
    # requests.Response.json() re-parses the body into fresh objects on every
    # call - each category's response must be parsed exactly once into a
    # variable before tagging, or the "_bond_type" mutation lands on
    # throwaway dicts and every bond silently ends up untagged.
    all_bonds = []
    r = _get_sync_with_retry(SECURITIES_URL, params=CORP_BONDS_PARAMS)
    if r is not None:
        corp_bonds = r.json()
        for b in corp_bonds:
            b["_bond_type"] = "corp"
        all_bonds.extend(corp_bonds)
    r = _get_sync_with_retry(SECURITIES_URL, params=GSEC_PARAMS)
    if r is not None:
        gsec_bonds = r.json()
        for b in gsec_bonds:
            b["_bond_type"] = "gsec"
        all_bonds.extend(gsec_bonds)
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


def _coupon_schedule(settlement_date, maturity_date, frequency):
    """(previous coupon date, payment dates after settlement), stepping back from maturity."""
    months = 12 // frequency
    future, i = [maturity_date], 1
    while True:
        d = maturity_date - relativedelta(months=months * i)
        if d <= settlement_date:
            return d, sorted(future)
        future.append(d)
        i += 1


def _bond_flows(clean_price, coupon_rate, settlement_date, maturity_date, frequency, basis, accrued=None):
    """Dirty price and cash flows as (periods from settlement, years from settlement, amount).

    Periods are fractional: the first coupon is discounted over the time actually
    left to it, and the price paid includes accrued interest. Checked against the
    yield KASE itself publishes for the last deal: 109 of 112 issues within 0.05 pp.
    """
    frequency = int(frequency or 2)
    if basis in (360, "360"):
        year_days = 360

        def day_count(d1, d2):
            return _days_30_360(d1, d2)
    else:
        year_days = 365

        def day_count(d1, d2):
            return (d2 - d1).days

    prev_coupon, dates = _coupon_schedule(settlement_date, maturity_date, frequency)
    period_days = year_days / frequency
    if accrued is None:
        accrued = (coupon_rate / frequency) * day_count(prev_coupon, settlement_date) / period_days
    flows = []
    for d in dates:
        days = day_count(settlement_date, d)
        amount = coupon_rate / frequency + (100 if d == maturity_date else 0)
        flows.append((days / period_days, days / year_days, amount))
    return clean_price + accrued, flows


def _calculate_ytm_irr(
    clean_price,
    coupon_rate,
    settlement_date,
    maturity_date,
    frequency,
    basis,
    face_value=100,
    accrued=None,
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

        if not coupon_rate:
            # Discount bond: single redemption at maturity - closed-form
            # bond-equivalent yield (same convention as the AIX source).
            if basis in (360, "360"):
                years_to_maturity = _days_30_360(settlement_date, maturity_date) / 360
            else:
                years_to_maturity = (maturity_date - settlement_date).days / 365
            if years_to_maturity <= 0:
                return None
            return round((2 * ((100 / clean_price) ** (1.0 / (2 * years_to_maturity)) - 1)) * 100, 4)

        frequency = int(frequency or 2)
        dirty, flows = _bond_flows(
            clean_price, coupon_rate, settlement_date, maturity_date, frequency, basis, accrued
        )

        def npv(ytm):
            return sum(cf / (1 + ytm / frequency) ** n for n, _, cf in flows) - dirty

        try:
            return round(brentq(npv, -0.95 * frequency, 100.0, maxiter=1000) * 100, 4)
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
    accrued=None,
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

        if not coupon_rate:
            # Discount bond: the whole cash flow is the redemption at
            # maturity, so Macaulay duration is just time to maturity.
            if basis in (360, "360"):
                return round(_days_30_360(settlement_date, maturity_date) / 360, 4)
            return round((maturity_date - settlement_date).days / 365, 4)

        frequency = int(frequency or 2)
        _, flows = _bond_flows(
            clean_price, coupon_rate, settlement_date, maturity_date, frequency, basis, accrued
        )
        weighted_sum, total_pv = 0.0, 0.0
        for n, years, cf in flows:
            pv = cf / (1 + ytm / 100 / frequency) ** n
            weighted_sum += years * pv
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


def _has_term_gaps(bond: dict, characteristics: dict) -> bool:
    ticker_data = bond.get("ticker") or {}
    if ticker_data.get("typesec_en") == "discount":
        return not ticker_data.get("finish_date")
    return (
        not ticker_data.get("cupon")
        or not ticker_data.get("finish_date")
        or not (
            _extract_characteristic_value(characteristics, "coupon_prev_date")
            and _extract_characteristic_value(characteristics, "coupon_near_pay_date")
        )
    )


def _derive_maturity(ticker, bond_type_flag, characteristics) -> date | None:
    # KASE leaves maturity blank on some issues placed directly on the exchange.
    # Per their prospectuses circulation starts at the first placement trade,
    # so start-of-circulation (or, failing that, trading open date) + term.
    start = _parse_date(
        _extract_characteristic_by_label(characteristics, "Дата начала обращения")
    ) or _parse_date(_extract_characteristic_by_label(characteristics, "Дата открытия торгов"))
    if not start:
        return None
    months = (_get_bond_main_api(ticker, bond_type_flag).get("characteristics") or {}).get(
        "circulation_months"
    )
    if not months:
        return None
    return start + relativedelta(months=int(round(float(months))))


def _kase_bond_kind(ticker_data: dict, characteristics: dict) -> str:
    # ticker.typesec only knows coupon/discount/islamic; floating (indexed) issues
    # show up solely in the characteristics' free-text bond name / rate kind.
    typesec = (ticker_data.get("typesec_ru") or "").lower()
    if ticker_data.get("typesec_en") == "discount" or "дисконт" in typesec:
        return "Дисконтная"
    if "ислам" in typesec:
        return "Сукук"
    name = (
        _extract_characteristic_by_label(characteristics, "Наименование облигации") or ""
    ).lower()
    rate_kind = (
        _extract_characteristic_by_label(characteristics, "Вид купонной ставки") or ""
    ).lower()
    if (
        "индекс" in name
        or "индекс" in rate_kind
        or "плава" in rate_kind
        or _extract_characteristic_by_label(characteristics, "маржа + индекс") is not None
    ):
        return "Купонная, плавающая ставка"
    return "Купонная, фиксированная ставка"


def _leading_number(text) -> float | None:
    """The number a KASE characteristic starts with: '1 000,0' -> 1000.0.

    KASE appends a note to some values ('1,00 (минимальный размер долга в
    номинальном выражении, доступный для торговли на KASE)'); reading the whole
    string as a number failed there and the nominal silently became 100.
    """
    m = re.match(r"\s*(\d[\d\s\u00a0]*(?:[.,]\d+)?)", str(text or ""))
    if not m:
        return None
    return float(re.sub(r"[\s\u00a0]", "", m.group(1)).replace(",", "."))


def _listing_board_and_category(characteristics) -> tuple[str | None, str | None]:
    """Board and category from KASE's "Список ценных бумаг" line, in KASE's own words.

    'официальный, основная площадка, категория "облигации"' -> ("основная", "облигации");
    'официальный, частное размещение площадка' -> ("частное размещение", None).
    """
    text = _extract_characteristic_by_label(characteristics, "Список ценных бумаг") or ""
    m = re.search(r",\s*(.+?)\s+площадка(?:,\s*категория\s*\"(.+?)\")?\s*$", text)
    return (m.group(1), m.group(2)) if m else (None, None)


def _process_bond(
    bond: dict,
    settlement_date: date,
    all_characteristics: dict,
    all_boards: dict,
    prospectus_terms: dict | None = None,
) -> dict:
    ticker = bond.get("code")
    bond_type_flag = bond.get("_bond_type", "corp")

    currency = bond.get("currency_type")
    clean_price = bond.get("price") or bond.get("close_price")

    # GSEC bonds are quoted as an absolute fraction of par (0.xx) - normalize to %.
    if clean_price and clean_price < 2 and bond_type_flag == "gsec":
        clean_price = clean_price * 100

    # KASE's own yield for the last deal (NOT the 'ytm' field, which is DTM/365).
    # It reports 0 when it has no yield, which is not a yield of zero.
    ytm_kase_dohod = bond.get("dohod") or None
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
    listing_board, listing_category = _listing_board_and_category(characteristics)

    # KASE's characteristics API exposes a nominal-value label on at least
    # some issues; label text unconfirmed against a live response, so this
    # is a best-effort lookup with the same 100 fallback the YTM/duration
    # math below already assumes (corp bonds are quoted per 100 of par).
    face_value = _leading_number(_extract_characteristic_by_label(characteristics, "Номинал"))
    if not face_value or face_value <= 0:
        face_value = 100.0
    lot_size = _leading_number(
        _extract_characteristic_by_label(characteristics, "Количество ценных бумаг в одном лоте")
    )

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
    pterms = (prospectus_terms or {}).get(ticker, {})
    if not is_discount and not (last_coupon_date_str and next_coupon_date_str):
        if pterms.get("coupon_freq"):
            frequency = pterms["coupon_freq"]
        else:
            quality_flags.append("coupon_freq_assumed")

    if not last_coupon_date and next_coupon_date and frequency:
        calc_prev = next_coupon_date - relativedelta(months=12 // frequency)
        if calc_prev < settlement_date:
            last_coupon_date = calc_prev
            last_coupon_date_str = calc_prev.isoformat()

    if not coupon_rate and not is_discount and pterms.get("coupon_rate"):
        coupon_rate = pterms["coupon_rate"]

    if not coupon_rate and not is_discount:
        main_api_characteristics = _get_bond_main_api(ticker, bond_type_flag).get(
            "characteristics", {}
        )
        coupon_from_desc = _extract_coupon_from_description(main_api_characteristics)
        if coupon_from_desc:
            coupon_rate = coupon_from_desc
            quality_flags.append("coupon_from_description")

    # The HTML coupon scrape is not used: the page embeds other bonds' news, and
    # it once read MFMOb42's coupon (11%) as 23% from a news item about MFMOb38.
    if not maturity_date:
        html_data = _parse_bond_html_fallback(ticker)
        if html_data:
            if not maturity_date and "finish_date" in html_data:
                maturity_date = html_data["finish_date"]
                quality_flags.append("html_fallback")
            if frequency == 2 and "frequency" in html_data:
                frequency = html_data["frequency"]

    if not maturity_date:
        derived = _derive_maturity(ticker, bond_type_flag, characteristics)
        if derived:
            maturity_date = derived.isoformat()
            quality_flags.append("maturity_derived")
            if dtm is None:
                dtm = (derived - settlement_date).days

    nkd_percent = _calculate_accrued_interest(
        coupon_rate, last_coupon_date, settlement_date, basis, frequency
    )
    # Discount bonds accrue nothing (nkd_percent is None), so dirty == clean,
    # same convention the AIX source uses.
    dirty_price = (
        clean_price + nkd_percent if clean_price and nkd_percent else clean_price
    )

    ytm_calc_irr = _calculate_ytm_irr(
        clean_price, coupon_rate, settlement_date, maturity_date, frequency, basis, accrued=nkd_percent
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
        accrued=nkd_percent,
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
        "bond_kind": _kase_bond_kind(ticker_data, characteristics),
        # Every live KASE bond reports "срочные"; "бессрочные" is the assumed
        # counterpart, not yet seen in a live response.
        "is_perpetual": (ticker_data.get("bond_type_ru") or "").lower().startswith("бессроч"),
        "market_segment": category or "unknown",
        # The four levels of KASE's official-list structure, in KASE's wording:
        # list -> board -> sector -> category.
        "listing_list": ticker_data.get("securities_list_ru") or None,
        "listing_board": listing_board,
        "listing_sector": bond.get("fin_sec_ru") or None,
        # Only stated where the exchange states it: KASE marks a private placement
        # by its board; nothing else in its data says how an issue was placed.
        "placement_type": listing_board if listing_board == "частное размещение" else None,
        "listing_category": listing_category,
        "face_value": face_value,
        "lot_size": lot_size,
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
        "last_trade_date": str(date0)[:10] if date0 else None,
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
    gaps = [
        b.get("code")
        for b in fx_bonds
        if _has_term_gaps(b, all_characteristics.get(b.get("code"), {}))
    ]
    prospectus_terms = await asyncio.to_thread(prospectus.kase_prospectus_terms, gaps)
    logger.info("KASE prospectus terms: %d of %d bonds with gaps", len(prospectus_terms), len(gaps))

    rows = []
    for bond in fx_bonds:
        try:
            rows.append(
                _process_bond(
                    bond, settlement_date, all_characteristics, all_boards, prospectus_terms
                )
            )
        except Exception:
            continue
    return pd.DataFrame(rows)


def fetch_kase_bonds(settlement_date: date | None = None) -> pd.DataFrame:
    """Sync wrapper around fetch_kase_bonds_async for non-async callers."""
    return asyncio.run(fetch_kase_bonds_async(settlement_date))
