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
  STALE_PRICE    - the price is from a deal older than config.YTM_MAX_PRICE_AGE_DAYS, or the
                   bond never traded and the price is only the exchange's reference price;
                   YTM and duration are not shown (the raw figure is kept in `ytm_calc`).
  CAPITALIZING   - coupon capitalizes into principal; our YTM math doesn't model that.
  FLOATING       - floating-rate coupon with unknown current rate.
  STALE_OR_ESTIMATED - price/coupon came from a fallback (HTML scrape, cached
                   profile, description-parsed coupon) rather than the primary API.
  ILLIQUID       - has a price but no trades today / not currently in a trading regime.
  OK             - nothing flagged.
"""

from __future__ import annotations

import pandas as pd

from kzbonds import config

UNIFIED_COLUMNS = [
    "exchange",
    "ticker",
    "isin",
    "issuer",
    "currency",
    "instrument_type",
    "bond_kind",
    "is_perpetual",
    "market_segment",
    "listing_list",
    "listing_board",
    "listing_sector",
    "listing_category",
    "face_value",
    "face_currency",
    "clean_price",
    "last_trade_date",
    "price_age_days",
    "accrued_interest",
    "dirty_price",
    "coupon_rate",
    "coupon_freq",
    "coupon_note",
    "data_note",
    "day_count_basis",
    "last_coupon_date",
    "maturity_date",
    "days_to_maturity",
    "ytm",
    "ytm_calc",
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
    "price_stale": (
        "STALE_PRICE",
        f"Последняя сделка была больше {config.YTM_MAX_PRICE_AGE_DAYS} дней назад — YTM и дюрация не показываются.",
    ),
    "never_traded": (
        "STALE_PRICE",
        "По бумаге не было ни одной сделки на бирже, цена справочная — YTM и дюрация не показываются.",
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
    "sukuk_no_rate": (
        "NO_YTM",
        "Сукук: доход выплачивается из прибыли актива, ставка в данных биржи не указана — YTM не рассчитывается.",
    ),
    "maturity_derived": (
        "STALE_OR_ESTIMATED",
        "Дата погашения рассчитана: начало обращения (или дата открытия торгов) + срок обращения — биржа её не публикует.",
    ),
    "coupon_freq_assumed": (
        "STALE_OR_ESTIMATED",
        "Периодичность купона не опубликована ни биржей, ни в проспекте — принято 2 раза в год.",
    ),
    "sukuk_target_rate": (
        "NO_YTM",
        "Сукук мудараба: в проспекте только целевая ставка распределения, доход не гарантирован — YTM не рассчитывается.",
    ),
    "no_coupon_rate": (
        "STALE_OR_ESTIMATED",
        "Купонная ставка не найдена ни в одном источнике.",
    ),
}

# Priority order when a bond has several flags - most severe wins.
_QUALITY_PRIORITY = [
    "NO_PRICE",
    "NO_YTM",
    "STALE_PRICE",
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


def _apply_price_freshness(df: pd.DataFrame, flags_col: pd.Series, as_of=None) -> pd.Series:
    """Hide YTM and duration when the price behind them is not a recent deal.

    The yield is solved as of today from the last deal's price. An old price
    makes that meaningless (a T-note priced in 2024 showed 36%), and a bond
    that never traded only has the exchange's reference price, usually par.
    The raw figure stays in `ytm_calc`; returns the flags with the reason added.
    """
    as_of = pd.Timestamp(as_of) if as_of is not None else pd.Timestamp.now().normalize()
    if "last_trade_date" not in df.columns:
        df["last_trade_date"] = None
    last = pd.to_datetime(df["last_trade_date"], errors="coerce").dt.normalize()
    df["price_age_days"] = (as_of - last).dt.days.clip(lower=0)
    df["last_trade_date"] = last.dt.strftime("%Y-%m-%d").where(last.notna(), None)
    if "ytm_calc" not in df.columns:
        df["ytm_calc"] = df["ytm"]

    has_price = pd.to_numeric(df["clean_price"], errors="coerce").notna()
    never_traded = has_price & last.isna()
    stale = has_price & (df["price_age_days"] > config.YTM_MAX_PRICE_AGE_DAYS)
    for col in ("ytm", "macaulay_duration", "modified_duration"):
        df[col] = df[col].astype(object).where(~(never_traded | stale), None)
    return pd.Series(
        [
            list(flags) + (["never_traded"] if nt else ["price_stale"] if st else [])
            for flags, nt, st in zip(flags_col, never_traded, stale)
        ],
        index=df.index,
    )


def normalize(df: pd.DataFrame, as_of=None) -> pd.DataFrame:
    """Add price_quality/price_quality_detail, apply the price-freshness rule
    and coerce dtypes. `as_of` is the date price age is measured from (today)."""
    if df.empty:
        return pd.DataFrame(columns=UNIFIED_COLUMNS)

    df = df.copy()
    flags_col = (
        df["_quality_flags"]
        if "_quality_flags" in df.columns
        else pd.Series([[]] * len(df), index=df.index)
    )
    flags_col = _apply_price_freshness(df, flags_col, as_of)
    resolved = flags_col.apply(_resolve_quality)
    df["price_quality"] = resolved.apply(lambda t: t[0])
    df["price_quality_detail"] = resolved.apply(lambda t: t[1])

    df["maturity_date"] = pd.to_datetime(df["maturity_date"], errors="coerce")
    for col in (
        "face_value",
        "clean_price",
        "dirty_price",
        "accrued_interest",
        "coupon_rate",
        "coupon_freq",
        "ytm",
        "ytm_calc",
        "ytm_exchange_reported",
        "ytm_total_return_reported",
        "macaulay_duration",
        "modified_duration",
        "issue_volume",
        "days_to_maturity",
        "price_age_days",
    ):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    for col in UNIFIED_COLUMNS:
        if col not in df.columns:
            df[col] = None

    return df[UNIFIED_COLUMNS]


def combine(kase_df: pd.DataFrame, aix_df: pd.DataFrame, as_of=None) -> pd.DataFrame:
    """Normalize both source frames and stack them into one unified table."""
    parts = [
        normalize(df, as_of) for df in (kase_df, aix_df) if df is not None and not df.empty
    ]
    if not parts:
        return pd.DataFrame(columns=UNIFIED_COLUMNS)
    return pd.concat(parts, ignore_index=True)
