"""
Generates the note shown in the dashboard: the highest yields per currency,
recomputed from the latest snapshot on every pipeline run (never hand-written
or hardcoded numbers).

Only bonds with price_quality == 'OK' are listed, so a stale price, a
reference price nobody dealt at, or a fallback value cannot put a bond at the
top of the list.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pandas as pd

from kzbonds import config

TOP_PER_CURRENCY = 5


def generate_note(df: pd.DataFrame) -> str:
    if df.empty:
        now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
        return f"# KZ Bond Monitor — аналитическая записка\n\n_{now}_\n\nДанных нет (пайплайн ещё не запускался)."

    clean = df[df["price_quality"] == "OK"].dropna(
        subset=["ytm", "modified_duration", "currency"]
    )

    lines = ["## Топ доходностей среди надёжных цен (по валютам)"]
    if clean.empty:
        lines.append("")
        lines.append("Бумаг с надёжной ценой и YTM в этом снапшоте нет.")
        return "\n".join(lines)

    for currency, group in clean.groupby("currency"):
        top = group.sort_values("ytm", ascending=False).head(TOP_PER_CURRENCY)
        lines.append(f"\n**{currency}:**")
        for _, r in top.iterrows():
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
