"""
Generates the analytical note: a short, data-driven markdown writeup with
one concrete conclusion, recomputed from the latest snapshot on every
pipeline run (never hand-written/hardcoded numbers).

Current angle: where does one exchange pay a materially different yield
than the other for genuinely comparable risk (same currency, same
duration bucket), using only price_quality == 'OK' bonds so fallback data
and untradeable bonds don't distort it. This is a starting angle, not a
fixed one - swap _duration_bucket / the grouping keys here as the field
set and filters solidify.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pandas as pd

from kzbonds import config

DURATION_BUCKETS = [
    (0, 1, "0-1y"),
    (1, 3, "1-3y"),
    (3, 5, "3-5y"),
    (5, 100, "5y+"),
]

MIN_BONDS_PER_GROUP = 2  # below this, a "median" is noise, not a signal


def _duration_bucket(years: float | None) -> str | None:
    if years is None or pd.isna(years):
        return None
    for lo, hi, label in DURATION_BUCKETS:
        if lo <= years < hi:
            return label
    return None


def _quality_breakdown(df: pd.DataFrame) -> pd.DataFrame:
    return (
        df.groupby(["exchange", "price_quality"], dropna=False)
        .size()
        .rename("n")
        .reset_index()
        .sort_values(["exchange", "n"], ascending=[True, False])
    )


def _yield_spread_table(clean: pd.DataFrame) -> pd.DataFrame:
    clean = clean.copy()
    clean["duration_bucket"] = clean["modified_duration"].apply(_duration_bucket)
    clean = clean.dropna(subset=["duration_bucket", "ytm"])

    grouped = (
        clean.groupby(["currency", "duration_bucket", "exchange"])
        .agg(median_ytm=("ytm", "median"), n=("ytm", "size"))
        .reset_index()
    )
    wide = grouped.pivot_table(
        index=["currency", "duration_bucket"],
        columns="exchange",
        values=["median_ytm", "n"],
    )
    wide.columns = [f"{a}_{b}" for a, b in wide.columns]
    wide = wide.reset_index()

    for col in ("median_ytm_KASE", "median_ytm_AIX", "n_KASE", "n_AIX"):
        if col not in wide.columns:
            wide[col] = None

    wide = wide.dropna(subset=["median_ytm_KASE", "median_ytm_AIX"])
    wide = wide[
        (wide["n_KASE"].fillna(0) >= MIN_BONDS_PER_GROUP)
        & (wide["n_AIX"].fillna(0) >= MIN_BONDS_PER_GROUP)
    ]
    wide["spread_kase_minus_aix_bp"] = (
        wide["median_ytm_KASE"] - wide["median_ytm_AIX"]
    ) * 100
    return wide.sort_values(
        "spread_kase_minus_aix_bp", key=lambda s: s.abs(), ascending=False
    )


def generate_note(df: pd.DataFrame) -> str:
    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")

    if df.empty:
        return f"# KZ Bond Monitor — аналитическая записка\n\n_{now}_\n\nДанных нет (пайплайн ещё не запускался)."

    total = len(df)
    by_exchange = df.groupby("exchange").size().to_dict()
    quality_bd = _quality_breakdown(df)
    ok_share = (df["price_quality"] == "OK").mean() * 100

    clean = df[df["price_quality"] == "OK"].dropna(
        subset=["ytm", "modified_duration", "currency"]
    )
    spread_table = _yield_spread_table(clean) if not clean.empty else pd.DataFrame()

    lines = [
        "# KZ Bond Monitor — аналитическая записка",
        f"_Сформировано автоматически: {now}, по {total} бумагам (KASE: {by_exchange.get('KASE', 0)}, AIX: {by_exchange.get('AIX', 0)})_",
        "",
        "## Качество данных",
        f"- С надёжной ценой и YTM (`price_quality = OK`): **{ok_share:.0f}%** от всех найденных валютных облигаций.",
    ]
    for _, row in quality_bd[quality_bd["price_quality"] != "OK"].head(8).iterrows():
        lines.append(
            f"  - {row['exchange']} / {row['price_quality']}: {row['n']} бумаг"
        )

    lines.append("")
    lines.append("## Вывод: где YTM отличается сильнее всего при сопоставимой дюрации")

    if spread_table.empty:
        lines.append(
            "Недостаточно бумаг с надёжной ценой в одинаковой валюте и корзине дюрации "
            f"(минимум {MIN_BONDS_PER_GROUP} на биржу в группе) для сравнения KASE и AIX в этом снапшоте."
        )
    else:
        top = spread_table.iloc[0]
        direction = "выше" if top["spread_kase_minus_aix_bp"] > 0 else "ниже"
        lines.append(
            f"**Самое большое расхождение**: облигации в **{top['currency']}** с модифицированной дюрацией "
            f"**{top['duration_bucket']}** — медианный YTM на KASE ({top['median_ytm_KASE']:.2f}%, "
            f"n={int(top['n_KASE'])}) на **{abs(top['spread_kase_minus_aix_bp']):.0f} б.п. {direction}**, "
            f"чем на AIX ({top['median_ytm_AIX']:.2f}%, n={int(top['n_AIX'])})."
        )
        lines.append("")
        lines.append(
            "Полная таблица расхождений (валюта × корзина дюрации, только надёжные цены):"
        )
        lines.append("")
        lines.append("| Валюта | Дюрация | YTM KASE | n | YTM AIX | n | Спред (б.п.) |")
        lines.append("|---|---|---|---|---|---|---|")
        for _, row in spread_table.head(15).iterrows():
            lines.append(
                f"| {row['currency']} | {row['duration_bucket']} | {row['median_ytm_KASE']:.2f}% | "
                f"{int(row['n_KASE'])} | {row['median_ytm_AIX']:.2f}% | {int(row['n_AIX'])} | "
                f"{row['spread_kase_minus_aix_bp']:+.0f} |"
            )

    if not clean.empty:
        lines.append("")
        lines.append("## Топ доходностей среди надёжных цен (по валютам)")
        for currency, group in clean.groupby("currency"):
            top5 = group.sort_values("ytm", ascending=False).head(5)
            lines.append(f"\n**{currency}:**")
            for _, r in top5.iterrows():
                mat = (
                    r["maturity_date"].strftime("%Y-%m-%d")
                    if pd.notna(r["maturity_date"])
                    else "—"
                )
                lines.append(
                    f"- {r['exchange']} {r['ticker']} — YTM {r['ytm']:.2f}%, дюрация {r['modified_duration']:.2f}л, погашение {mat}"
                )

    return "\n".join(lines)


def generate_and_save(df: pd.DataFrame) -> str:
    note = generate_note(df)
    config.ANALYSIS_NOTE_PATH.write_text(note, encoding="utf-8")
    return note
