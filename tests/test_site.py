import json

import pandas as pd
import pytest

from kzbonds import site


def _snapshot(**overrides):
    row = {
        "run_id": "abc123",
        "fetched_at": "2026-11-12T06:55:02.818081+00:00",
        "exchange": "KASE",
        "ticker": "TEST1",
        "isin": "KZ0000000001",
        "issuer": "Issuer",
        "currency": "USD",
        "instrument_type": "купонные облигации",
        "bond_kind": "Купонная, фиксированная ставка",
        "is_perpetual": 0,
        "face_value": 100.0,
        "face_currency": "USD",
        "clean_price": 98.0,
        "dirty_price": 98.3,
        "accrued_interest": 0.3,
        "coupon_rate": 5.0,
        "coupon_freq": 2.0,
        "day_count_basis": "360",
        "maturity_date": pd.Timestamp("2029-01-01"),
        "days_to_maturity": 780.0,
        "ytm": 6.1,
        "macaulay_duration": 2.0,
        "modified_duration": 1.9,
        "issue_volume": 1000.0,
        "price_quality": "OK",
        "price_quality_detail": "",
        "last_trade_date": "2026-11-11",
        "price_age_days": 1.0,
    }
    row.update(overrides)
    return pd.DataFrame([row])


def _bonds_in(page):
    start = page.index("const BONDS = ") + len("const BONDS = ")
    return json.loads(page[start : page.index(";\nconst NOTE_MD = ", start)])


def test_records_use_plain_values_the_page_can_read():
    df = _snapshot(clean_price=float("nan"), is_perpetual=1)
    record = site.snapshot_records(df)[0]
    assert list(record) == site.SITE_COLUMNS
    assert record["clean_price"] is None
    assert record["is_perpetual"] is True
    assert record["maturity_date"] == "2029-01-01"
    assert record["last_trade_date"] == "2026-11-11"
    # A column the snapshot does not have yet becomes an empty value, not an error.
    assert record["lot_size"] is None


def test_page_fills_in_every_placeholder():
    page = site.render(_snapshot(), mode="site")
    assert "__" not in page.replace("__proto__", "")
    assert "снапшот от" in page and "2026-11-12 06:55 GMT" in page
    # Both places that show the time carry it for the script to convert.
    assert page.count('data-utc="2026-11-12T06:55:02Z"') == 2
    assert "run_id abc123" in page
    assert _bonds_in(page)[0]["ticker"] == "TEST1"
    assert "Топ доходностей по валютам" in page


def test_site_mode_is_a_complete_document_and_artifact_mode_is_not():
    df = _snapshot()
    full = site.render(df, mode="site")
    fragment = site.render(df, mode="artifact")
    assert full.startswith("<!doctype html>") and full.rstrip().endswith("</html>")
    assert '<meta charset="utf-8">' in full and 'name="viewport"' in full
    assert fragment.startswith("<title>") and "<!doctype" not in fragment
    # Same page inside: the fragment is the document without its skeleton.
    assert fragment[: fragment.index("</style>")] in full
    assert fragment[fragment.index("</style>") + len("</style>\n") :] in full


def test_text_that_looks_like_a_closing_script_tag_cannot_break_the_page():
    page = site.render(_snapshot(issuer="A</script><b>B"), mode="site")
    assert "A</script>" not in page
    assert _bonds_in(page)[0]["issuer"] == "A</script><b>B"


def test_render_refuses_an_empty_snapshot_and_an_unknown_mode():
    with pytest.raises(ValueError):
        site.render(pd.DataFrame(), mode="site")
    with pytest.raises(ValueError):
        site.render(_snapshot(), mode="email")


def test_metrika_counter_goes_only_into_the_site_page(monkeypatch):
    monkeypatch.setattr(site.config, "METRIKA_ID", "12345678")
    full = site.render(_snapshot(), mode="site")
    fragment = site.render(_snapshot(), mode="artifact")
    assert "window.KZB_METRIKA_ID = 12345678;" in full
    assert "ym(12345678, 'init'" in full and "webvisor:true" in full
    assert "https://mc.yandex.ru/metrika/tag.js?id=12345678" in full
    assert "https://mc.yandex.ru/watch/12345678" in full
    # The loader sits in <head>, the fallback image right after <body>.
    assert full.index("mc.yandex.ru/metrika/tag.js") < full.index("</head>")
    assert full.index("<body>") < full.index("mc.yandex.ru/watch/")
    assert "mc.yandex.ru" not in fragment and "KZB_METRIKA_ID = " not in fragment


def test_no_counter_number_means_no_metrika(monkeypatch):
    for value in ("", "  ", "abc123"):
        monkeypatch.setattr(site.config, "METRIKA_ID", value)
        page = site.render(_snapshot(), mode="site")
        assert "mc.yandex.ru" not in page and "KZB_METRIKA_ID = " not in page


def test_page_script_sends_every_agreed_goal():
    page = site.render(_snapshot(), mode="site")
    for goal in (
        "tab_table",
        "tab_chart",
        "tab_note",
        "card_table",
        "card_chart",
        "card_search",
        "filter_exchange_",
        "filter_currency_",
        "filter_type_",
        "filter_maturity_",
        "filter_clear",
        "filter_combo",
        "search",
        "search_empty",
        "reset_filters",
        "sort_ytm",
        "sort_price",
        "sort_coupon",
        "sort_duration",
        "sort_maturity",
        "sort_face",
        "sort_exchange",
        "sort_currency",
        "sort_other",
    ):
        assert f"'{goal}" in page, goal


def test_site_page_carries_the_coming_refresh_times_and_the_artifact_does_not():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    friday_noon = datetime(2026, 11, 13, 12, 0, tzinfo=ZoneInfo("Asia/Almaty"))
    full = site.render(_snapshot(), mode="site", now=friday_noon)
    start = full.index("window.KZB_NEXT_RUNS = ") + len("window.KZB_NEXT_RUNS = ")
    runs = json.loads(full[start : full.index(";</script>", start)])
    assert runs[0] == "2026-11-13T07:45:00Z"  # 12:45 Astana
    assert full.index("KZB_NEXT_RUNS") < full.index("</head>")
    assert "KZB_NEXT_RUNS = " not in site.render(_snapshot(), mode="artifact")
