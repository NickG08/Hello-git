"""
KZ Bond Monitor - Streamlit dashboard.

Unifies KASE + AIX foreign-currency bonds into one filterable table with
YTM/duration and a price-quality flag, plus an auto-regenerated analytical
note. Run with: `streamlit run app.py`.

Reads whatever the last pipeline run wrote to the DB (kzbonds/db.py); use
the sidebar button to trigger a fresh run on demand, or let the scheduler
(docker/scheduler_loop.py) keep it current in the background.
"""

from __future__ import annotations

import altair as alt
import pandas as pd
import streamlit as st

from kzbonds import config, db, pipeline

st.set_page_config(page_title="KZ Bond Monitor", page_icon="📈", layout="wide")

# Fixed categorical colors (never cycled/reassigned) so "KASE" and "AIX"
# always read as the same color anywhere in the app.
EXCHANGE_COLORS = {"KASE": "#4E79A7", "AIX": "#F28E2B"}
# Status-style palette for price_quality, worst -> best.
QUALITY_COLORS = {
    "NO_PRICE": "#E15759",
    "NO_YTM": "#E15759",
    "CAPITALIZING": "#B07AA1",
    "FLOATING": "#9C755F",
    "STALE_OR_ESTIMATED": "#F1CE63",
    "ILLIQUID": "#B0B0B0",
    "OK": "#59A14F",
}
QUALITY_BADGE = {
    "OK": "🟢 OK",
    "STALE_OR_ESTIMATED": "🟡 оценочно",
    "ILLIQUID": "⚪ неликвидно",
    "NO_PRICE": "🔴 нет цены",
    "NO_YTM": "🔴 нет YTM",
    "CAPITALIZING": "🟣 капитализация купона",
    "FLOATING": "🟤 плавающая ставка",
}


@st.cache_data(ttl=300)
def _load_latest() -> pd.DataFrame:
    return db.load_latest()


def _run_pipeline_now():
    with st.spinner("Собираем свежие данные с KASE и AIX (обычно 1-3 минуты)..."):
        result = pipeline.run()
    _load_latest.clear()
    if result.get("n_bonds"):
        st.success(f"Готово: {result['n_bonds']} бумаг, run_id={result['run_id']}")
    else:
        st.error(
            "Обе биржи вернули пустой результат - проверьте сетевой доступ к kase.kz и market-backend.aixkz.com."
        )


st.title("📈 KZ Bond Monitor")
st.caption(
    "Валютные облигации KASE + AIX в одной таблице: YTM, дюрация, флаг качества цены"
)

df = _load_latest()

with st.sidebar:
    st.header("Данные")
    if df.empty:
        st.warning("Снапшотов ещё нет.")
        if st.button("Собрать данные сейчас", type="primary"):
            _run_pipeline_now()
            st.rerun()
        st.stop()

    fetched_at = (
        pd.to_datetime(db.list_runs().iloc[0]["fetched_at"])
        if not db.list_runs().empty
        else None
    )
    if fetched_at is not None:
        st.caption(f"Обновлено: {fetched_at.strftime('%Y-%m-%d %H:%M UTC')}")
    if st.button("🔄 Обновить сейчас"):
        _run_pipeline_now()
        st.rerun()

    st.divider()
    st.header("Фильтры")

    exchanges = st.multiselect(
        "Биржа",
        sorted(df["exchange"].dropna().unique()),
        default=sorted(df["exchange"].dropna().unique()),
    )
    currencies = st.multiselect(
        "Валюта",
        sorted(df["currency"].dropna().unique()),
        default=sorted(df["currency"].dropna().unique()),
    )

    quality_options = [q for q in QUALITY_COLORS if q in df["price_quality"].unique()]
    quality_default = [q for q in quality_options if q != "NO_PRICE"]
    quality = st.multiselect(
        "Качество цены/YTM",
        quality_options,
        default=quality_default,
        format_func=lambda q: QUALITY_BADGE.get(q, q),
        help="По умолчанию скрыты бумаги без цены (NO_PRICE) - остальные показаны с пометкой качества.",
    )

    instrument_types = sorted(df["instrument_type"].dropna().unique())
    selected_types = st.multiselect(
        "Тип облигации", instrument_types, default=instrument_types
    )

    ytm_vals = df["ytm"].dropna()
    if not ytm_vals.empty:
        ytm_lo, ytm_hi = float(ytm_vals.min()), float(ytm_vals.max())
        ytm_range = st.slider(
            "YTM, %",
            min_value=round(ytm_lo, 1),
            max_value=round(ytm_hi, 1),
            value=(round(ytm_lo, 1), round(ytm_hi, 1)),
        )
    else:
        ytm_range = None

    dur_vals = df["modified_duration"].dropna()
    if not dur_vals.empty:
        dur_lo, dur_hi = float(dur_vals.min()), float(dur_vals.max())
        dur_range = st.slider(
            "Модифицированная дюрация, лет",
            min_value=round(dur_lo, 1),
            max_value=round(dur_hi, 1),
            value=(round(dur_lo, 1), round(dur_hi, 1)),
        )
    else:
        dur_range = None

    search = st.text_input("Поиск по тикеру/эмитенту/ISIN")

filtered = df[
    df["exchange"].isin(exchanges)
    & df["currency"].isin(currencies)
    & df["price_quality"].isin(quality)
    & df["instrument_type"].isin(selected_types)
]
if ytm_range:
    filtered = filtered[filtered["ytm"].between(*ytm_range) | filtered["ytm"].isna()]
if dur_range:
    filtered = filtered[
        filtered["modified_duration"].between(*dur_range)
        | filtered["modified_duration"].isna()
    ]
if search:
    s = search.lower()
    mask = (
        filtered["ticker"].str.lower().str.contains(s, na=False)
        | filtered["issuer"].str.lower().str.contains(s, na=False)
        | filtered["isin"].str.lower().str.contains(s, na=False)
    )
    filtered = filtered[mask]

col1, col2, col3, col4 = st.columns(4)
col1.metric("Бумаг после фильтра", len(filtered))
col2.metric(
    "Медианный YTM",
    f"{filtered['ytm'].median():.2f}%" if filtered["ytm"].notna().any() else "—",
)
col3.metric("Из них 'OK' качество", int((filtered["price_quality"] == "OK").sum()))
col4.metric("Требуют внимания", int((~filtered["price_quality"].isin(["OK"])).sum()))

tab_table, tab_chart, tab_note, tab_history = st.tabs(
    ["Таблица", "YTM / Дюрация", "Аналитическая записка", "История"]
)

with tab_table:
    display = filtered.copy()
    display["Качество"] = (
        display["price_quality"].map(QUALITY_BADGE).fillna(display["price_quality"])
    )
    display["maturity_date"] = display["maturity_date"].dt.strftime("%Y-%m-%d")
    columns = [
        "exchange",
        "ticker",
        "isin",
        "issuer",
        "currency",
        "instrument_type",
        "face_value",
        "face_currency",
        "clean_price",
        "dirty_price",
        "accrued_interest",
        "coupon_rate",
        "coupon_freq",
        "maturity_date",
        "days_to_maturity",
        "ytm",
        "macaulay_duration",
        "modified_duration",
        "issue_volume",
        "Качество",
        "price_quality_detail",
    ]
    st.dataframe(
        display[columns].sort_values("ytm", ascending=False),
        use_container_width=True,
        height=560,
        column_config={
            "exchange": "Биржа",
            "ticker": "Тикер",
            "isin": "ISIN",
            "issuer": "Эмитент",
            "currency": "Валюта",
            "instrument_type": "Тип",
            "face_value": st.column_config.NumberColumn("Номинал", format="%.0f"),
            "face_currency": "Вал. номинала",
            "clean_price": st.column_config.NumberColumn(
                "Чистая цена, %", format="%.2f"
            ),
            "dirty_price": st.column_config.NumberColumn(
                "Грязная цена, %", format="%.2f"
            ),
            "accrued_interest": st.column_config.NumberColumn("НКД, %", format="%.3f"),
            "coupon_rate": st.column_config.NumberColumn("Купон, %", format="%.2f"),
            "coupon_freq": "Частота купонов",
            "maturity_date": "Погашение",
            "days_to_maturity": "Дней до погашения",
            "ytm": st.column_config.NumberColumn("YTM, %", format="%.2f"),
            "macaulay_duration": st.column_config.NumberColumn(
                "Дюр. Маколея, лет", format="%.2f"
            ),
            "modified_duration": st.column_config.NumberColumn(
                "Мод. дюрация, лет", format="%.2f"
            ),
            "issue_volume": st.column_config.NumberColumn(
                "Объём выпуска",
                format="%.0f",
                help="Не путать с номиналом одной бумаги",
            ),
            "price_quality_detail": "Детали качества",
        },
    )
    st.download_button(
        "Скачать CSV",
        display[columns].to_csv(index=False).encode("utf-8-sig"),
        file_name="kz_bond_monitor.csv",
        mime="text/csv",
    )

with tab_chart:
    chart_df = filtered.dropna(subset=["ytm", "modified_duration"])
    if chart_df.empty:
        st.info(
            "Нет бумаг с одновременно известными YTM и дюрацией при текущих фильтрах."
        )
    else:
        scatter = (
            alt.Chart(chart_df)
            .mark_circle(size=90, opacity=0.75)
            .encode(
                x=alt.X("modified_duration:Q", title="Модифицированная дюрация, лет"),
                y=alt.Y("ytm:Q", title="YTM, %"),
                color=alt.Color(
                    "exchange:N",
                    title="Биржа",
                    scale=alt.Scale(
                        domain=list(EXCHANGE_COLORS),
                        range=list(EXCHANGE_COLORS.values()),
                    ),
                ),
                tooltip=[
                    "exchange",
                    "ticker",
                    "issuer",
                    "currency",
                    "ytm",
                    "modified_duration",
                    "price_quality",
                ],
            )
            .properties(height=460)
            .interactive()
        )
        st.altair_chart(scatter, use_container_width=True)

        quality_counts = (
            filtered.groupby(["exchange", "price_quality"]).size().reset_index(name="n")
        )
        bar = (
            alt.Chart(quality_counts)
            .mark_bar()
            .encode(
                x=alt.X("exchange:N", title="Биржа"),
                y=alt.Y("n:Q", title="Бумаг"),
                color=alt.Color(
                    "price_quality:N",
                    title="Качество",
                    scale=alt.Scale(
                        domain=list(QUALITY_COLORS), range=list(QUALITY_COLORS.values())
                    ),
                ),
                tooltip=["exchange", "price_quality", "n"],
            )
            .properties(height=280)
        )
        st.altair_chart(bar, use_container_width=True)

with tab_note:
    if config.ANALYSIS_NOTE_PATH.exists():
        st.markdown(config.ANALYSIS_NOTE_PATH.read_text(encoding="utf-8"))
    else:
        st.info("Записка появится после первого запуска пайплайна.")

with tab_history:
    runs = db.list_runs()
    if runs.empty:
        st.info("История появится после нескольких запусков пайплайна.")
    else:
        st.dataframe(
            runs,
            use_container_width=True,
            hide_index=True,
            column_config={
                "run_id": "Run ID",
                "fetched_at": "Время",
                "n_bonds": "Бумаг",
            },
        )
        ticker_pick = st.selectbox(
            "Показать историю YTM по тикеру",
            sorted(filtered["ticker"].dropna().unique()),
        )
        if ticker_pick:
            hist = db.load_history(ticker_pick)
            if not hist.empty:
                line = (
                    alt.Chart(hist.dropna(subset=["ytm"]))
                    .mark_line(point=True)
                    .encode(
                        x=alt.X("fetched_at:T", title="Дата снапшота"),
                        y=alt.Y("ytm:Q", title="YTM, %"),
                        color=alt.Color(
                            "exchange:N",
                            title="Биржа",
                            scale=alt.Scale(
                                domain=list(EXCHANGE_COLORS),
                                range=list(EXCHANGE_COLORS.values()),
                            ),
                        ),
                        tooltip=["fetched_at", "ytm", "exchange"],
                    )
                    .properties(height=360)
                )
                st.altair_chart(line, use_container_width=True)
