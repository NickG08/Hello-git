"""
Unified schema across KASE + AIX, and the price/YTM quality flag.

Both source modules already emit rows in a common shape (see
sources/kase.py::_process_bond and sources/aix.py fetch loop) plus an
internal '_quality_flags' list of low-level issues found while building
that row. This module turns those low-level flags into one overall
`price_quality` verdict + a human-readable `price_quality_detail`, and is
the single place to touch when the exact field set changes later.

price_quality values, worst first (first match on a bond's flags wins):
  NO_PRICE       - bond has no tradable/quoted price at all; YTM is not meaningful.
  NO_YTM         - price exists but YTM could not be computed (bad/missing coupon data).
  CAPITALIZING   - coupon capitalizes into principal; our YTM math doesn't model that.
  FLOATING       - floating-rate coupon with unknown current rate.
  STALE_OR_ESTIMATED - price/coupon came from a fallback (HTML scrape, cached
                   profile, description-parsed coupon) rather than the primary API.
  ILLIQUID       - has a price but no trades today / not currently in a trading regime.
  OK             - nothing flagged.
"""

from __future__ import annotations

import pandas as pd

UNIFIED_COLUMNS = [
    "exchange",
    "ticker",
    "isin",
    "issuer",
    "currency",
    "instrument_type",
    "market_segment",
    "clean_price",
    "accrued_interest",
    "dirty_price",
    "coupon_rate",
    "coupon_freq",
    "day_count_basis",
    "last_coupon_date",
    "maturity_date",
    "days_to_maturity",
    "ytm",
    "ytm_exchange_reported",
    "ytm_total_return_reported",
    "macaulay_duration",
    "modified_duration",
    "issue_volume",
    "price_quality",
    "price_quality_detail",
]

_FLAG_TO_QUALITY = {
    "no_price": ("NO_PRICE", "Нет котировки/цены по бумаге."),
    "ytm_calc_failed": (
        "NO_YTM",
        "Цена есть, но YTM не рассчитывается (некорректные/неполные данные по купону).",
    ),
    "calc_unavailable": (
        "NO_YTM",
        "Показатели не рассчитаны (см. тип облигации в исходных данных).",
    ),
    "capitalizing_coupon": (
        "CAPITALIZING",
        "Купон капитализируется в тело долга — расчёт YTM не учитывает это, доверять цифре AIX.",
    ),
    "floating_unknown_rate": (
        "FLOATING",
        "Плавающая ставка, текущее значение неизвестно — YTM не рассчитан.",
    ),
    "html_fallback": (
        "STALE_OR_ESTIMATED",
        "Купон/дата погашения получены HTML fallback-парсингом (нет в API).",
    ),
    "coupon_from_description": (
        "STALE_OR_ESTIMATED",
        "Купонная ставка извлечена из текстового описания, не из структурированного поля API.",
    ),
    "profile_fallback_anomalous": (
        "STALE_OR_ESTIMATED",
        "Рыночная цена выглядела аномальной, использована цена из профиля.",
    ),
    "profile_fallback_no_market_price": (
        "STALE_OR_ESTIMATED",
        "Рыночная цена отсутствовала, использована цена из профиля.",
    ),
    "not_currently_traded": ("ILLIQUID", "Бумага сейчас не в активном режиме торгов."),
    "no_trades_today": ("ILLIQUID", "Сегодня не было сделок по бумаге."),
    "no_coupon_rate": (
        "STALE_OR_ESTIMATED",
        "Купонная ставка не найдена ни в одном источнике.",
    ),
}

# Priority order when a bond has several flags - most severe wins.
_QUALITY_PRIORITY = [
    "NO_PRICE",
    "NO_YTM",
    "CAPITALIZING",
    "FLOATING",
    "STALE_OR_ESTIMATED",
    "ILLIQUID",
    "OK",
]


def _resolve_quality(flags: list[str]) -> tuple[str, str]:
    if not flags:
        return "OK", ""
    matches = [_FLAG_TO_QUALITY[f] for f in flags if f in _FLAG_TO_QUALITY]
    if not matches:
        return "OK", ""
    matches.sort(key=lambda qd: _QUALITY_PRIORITY.index(qd[0]))
    best_quality = matches[0][0]
    details = " | ".join(dict.fromkeys(d for q, d in matches if q == best_quality))
    return best_quality, details


def normalize(df: pd.DataFrame) -> pd.DataFrame:
    """Add price_quality/price_quality_detail and coerce dtypes. Idempotent
    and safe to call on an already-normalized frame (re-derives the flag)."""
    if df.empty:
        return pd.DataFrame(columns=UNIFIED_COLUMNS)

    df = df.copy()
    flags_col = (
        df["_quality_flags"]
        if "_quality_flags" in df.columns
        else pd.Series([[]] * len(df), index=df.index)
    )
    resolved = flags_col.apply(_resolve_quality)
    df["price_quality"] = resolved.apply(lambda t: t[0])
    df["price_quality_detail"] = resolved.apply(lambda t: t[1])

    df["maturity_date"] = pd.to_datetime(df["maturity_date"], errors="coerce")
    for col in (
        "clean_price",
        "dirty_price",
        "accrued_interest",
        "coupon_rate",
        "coupon_freq",
        "ytm",
        "ytm_exchange_reported",
        "ytm_total_return_reported",
        "macaulay_duration",
        "modified_duration",
        "issue_volume",
        "days_to_maturity",
    ):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    for col in UNIFIED_COLUMNS:
        if col not in df.columns:
            df[col] = None

    return df[UNIFIED_COLUMNS]


def combine(kase_df: pd.DataFrame, aix_df: pd.DataFrame) -> pd.DataFrame:
    """Normalize both source frames and stack them into one unified table."""
    parts = [
        normalize(df) for df in (kase_df, aix_df) if df is not None and not df.empty
    ]
    if not parts:
        return pd.DataFrame(columns=UNIFIED_COLUMNS)
    return pd.concat(parts, ignore_index=True)
