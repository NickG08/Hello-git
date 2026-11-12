"""
Generates the note shown in the dashboard: the highest yields per currency,
recomputed from the latest snapshot on every pipeline run (never hand-written
or hardcoded numbers).

The list is drawn from every bond whose YTM the table shows. normalize()
already blanks the YTM of a bond whose last deal is too old or that never
traded, so the note and the table follow one rule on both exchanges.

Bonds closer to maturity than config.NOTE_MIN_DAYS_TO_MATURITY are left out of
the list (the table keeps them): their yield is an old price annualised over a
few weeks. Perpetual bonds have no maturity and stay in.
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

    with_ytm = df.dropna(subset=["ytm", "currency"])
    near_maturity = with_ytm["days_to_maturity"] < config.NOTE_MIN_DAYS_TO_MATURITY
    with_ytm = with_ytm[~near_maturity]

    lines = ["## Топ доходностей по валютам"]
    if with_ytm.empty:
        lines.append("")
        lines.append("Бумаг с YTM в этом снапшоте нет.")
        return "\n".join(lines)

    lines.append("")
    lines.append(f"В список не входят бумаги, до погашения которых меньше {config.NOTE_MIN_DAYS_TO_MATURITY} дней.")

    for currency, group in with_ytm.groupby("currency"):
        top = group.sort_values("ytm", ascending=False).head(TOP_PER_CURRENCY)
        lines.append(f"\n**{currency}:**")
        for _, r in top.iterrows():
            mat = r["maturity_date"].strftime("%Y-%m-%d") if pd.notna(r["maturity_date"]) else "—"
            duration = f"{r['modified_duration']:.2f}л" if pd.notna(r["modified_duration"]) else "—"
            lines.append(f"- {r['exchange']} {r['ticker']} — YTM {r['ytm']:.2f}%, дюрация {duration}, погашение {mat}")

    return "\n".join(lines)


def generate_and_save(df: pd.DataFrame) -> str:
    note = generate_note(df)
    config.ANALYSIS_NOTE_PATH.write_text(note, encoding="utf-8")
    return note
