"""Bond-kind classification, using field values seen in live KASE/AIX responses."""

import pandas as pd
from sqlalchemy import create_engine, inspect

from kzbonds import db
from kzbonds.sources.aix import _aix_bond_kind, _is_perpetual
from kzbonds.sources.kase import _kase_bond_kind


def _chars(**labels):
    return {str(i): {"label": k, "value": v} for i, (k, v) in enumerate(labels.items())}


def test_kase_indexed_bond_is_floating():
    # IDFRpp5: ticker.typesec says plain "купонные", only the bond name says indexed.
    ticker = {"typesec_ru": "купонные", "typesec_en": "coupon"}
    chars = _chars(**{"Наименование облигации": "индексированные купонные облигации"})
    assert _kase_bond_kind(ticker, chars) == "Купонная, плавающая ставка"


def test_kase_discount_and_islamic():
    assert _kase_bond_kind({"typesec_ru": "дисконтные", "typesec_en": "discount"}, {}) == "Дисконтная"
    assert _kase_bond_kind({"typesec_ru": "исламские облигации"}, {}) == "Сукук"


def test_kase_plain_coupon_is_fixed():
    ticker = {"typesec_ru": "купонные", "typesec_en": "coupon"}
    chars = _chars(**{"Наименование облигации": "купонные облигации", "Вид купонной ставки": "фиксированная"})
    assert _kase_bond_kind(ticker, chars) == "Купонная, фиксированная ставка"


def test_aix_sukuk_is_not_discount():
    # ALIF.1028: couponType "N/A", couponRate 0 - previously read as a discount bond.
    p = {"securityName": "Sukuk of Alif Moliya SPC Limited", "couponType": "N/A", "couponRate": "0"}
    assert _aix_bond_kind(p) == "Сукук"


def test_aix_zero_coupon_with_na_type_is_discount():
    p = {"securityName": 'Zero-Coupon Bonds of JSC "Home Credit Bank"', "couponType": "N/A", "couponRate": "0"}
    assert _aix_bond_kind(p) == "Дисконтная"


def test_aix_coupon_kinds():
    assert _aix_bond_kind({"couponType": "Floating", "couponRate": "18"}) == "Купонная, плавающая ставка"
    assert _aix_bond_kind({"couponType": "Capitalized", "couponRate": "12"}) == "Купонная, капитализация купона"
    assert _aix_bond_kind({"couponType": "Fixed", "couponRate": "5.5"}) == "Купонная, фиксированная ставка"


def test_aix_floating_with_unknown_rate_is_not_discount():
    assert _aix_bond_kind({"couponType": "Floating", "couponRate": "0"}) == "Купонная, плавающая ставка"


def test_aix_perpetual_detection():
    assert _is_perpetual({"securityName": "Perpetual Bonds of ForteBank"}, "FRTB.PERP")
    assert not _is_perpetual({"securityName": "Bonds of Qarmet JSC"}, "QAR3.0636")


def test_new_column_is_added_to_existing_table(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 't.db'}")
    pd.DataFrame({"run_id": ["a"], "ticker": ["X"]}).to_sql(db.TABLE, engine, index=False)
    new = pd.DataFrame({"run_id": ["b"], "ticker": ["Y"], "bond_kind": ["Сукук"], "is_perpetual": [True]})
    db._add_missing_columns(engine, new)
    new.to_sql(db.TABLE, engine, if_exists="append", index=False)
    cols = {c["name"] for c in inspect(engine).get_columns(db.TABLE)}
    assert {"bond_kind", "is_perpetual"} <= cols
