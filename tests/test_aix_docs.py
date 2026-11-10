"""AIX offering-document parsing, on fragments of the real PlanDeM and Alif documents."""

from kzbonds.aix_docs import _init_call, coupon_note, document_id, parse_aix_terms
from kzbonds.sources.aix import _has_term_gaps, _offering_urls

# OCR of PDM2.1254 offer terms (scanned): the schedule plus one misread "SY LPR" row.
PDM2_OCR = """
Coupon rate Floating interest rate, equal to the sum of the SY LPR and a margin 3,0% per
annum.
Schedule of Payment of Interests on Bonds
1 29.12.2035 5Y LPR + 3,0% 28.06.2036 29.06.2036
2 29.06.2036 5Y LPR + 3,0% 28.12.2036 29.12.2036
3 29.12.2036 5Y LPR + 3,0% 28.06.2037 29.06.2037
4 29.06.2037 SY LPR+3,0% 28.12.2037 29.12.2037
"""

PDM_SCHEDULE = """
1 20.12.2034 1Y SHIBOR + 1,7% 19.03.2035 20.03.2035
2 20.03.2035 lY SHIBOR + 1,7% 19.06.2035 20.06.2035
3 20.06.2035 1Y SHIBOR + 1,7% 19.09.2035 20.09.2035
"""

ALIF = (
    "The USD 10,000,000 aggregate principal amount of expected Periodic Distribution Amount rate "
    "of 10% per annum ... PDA Rate Target PDA Rate of 10% per annum Rights attached to the Sukuk"
)


def test_floating_terms_from_schedule_despite_ocr_misread():
    t = parse_aix_terms(PDM2_OCR)
    assert t["coupon_freq"] == 2
    assert t["accrual_start"] == "2035-12-29"
    assert t["coupon_formula"] == "5Y LPR + 3,0%"
    assert t["coupon_status"] == "floating"
    assert coupon_note(t) == "Плавающий купон: 5Y LPR + 3,0%, начисление с 29.12.2035"


def test_quarterly_schedule():
    t = parse_aix_terms(PDM_SCHEDULE)
    assert (t["coupon_freq"], t["coupon_formula"]) == (4, "1Y SHIBOR + 1,7%")


def test_sukuk_target_rate():
    t = parse_aix_terms(ALIF)
    assert (t["coupon_rate"], t["coupon_status"]) == (10.0, "sukuk_target")
    assert "не гарантирована" in coupon_note(t)


def test_init_call_found_in_controllers():
    js = (
        '<Action Type="Execute" ObjectID="baaae97c-a94f-47d1-8e7a-b3e9e740a44a" Method="QTzsQRlU-3zF9">'
        '<Parameters><Parameter SourceType="FormParameter" SourceID="documentid" TargetType="ObjectProperty"'
        ' TargetID="ID" IsRequired="false" /></Parameters><Results><Result SourceType="ObjectProperty"'
        ' SourceID="Document_EN_Memo" TargetType="FormParameter" TargetID="base64" /></Results>'
    )
    assert _init_call(js) == ("baaae97c-a94f-47d1-8e7a-b3e9e740a44a", "QTzsQRlU-3zF9")


def test_document_id():
    url = "https://k4txdyb.onk2.com/Runtime/Runtime/Form/MRA.Disclosure+Document+Display+Form?documentid=40339bc6"
    assert document_id(url) == "40339bc6"


def test_only_bonds_with_gaps_need_documents():
    assert _has_term_gaps({"couponType": "Floating", "couponRate": "0", "couponFreq": "0"})
    assert not _has_term_gaps({"couponType": "Fixed", "couponRate": "5.5", "couponFreq": "2"})
    assert not _has_term_gaps({"securityName": "Zero-Coupon Bonds", "couponType": "N/A", "couponRate": "0"})


def test_offering_urls_parsed_from_profile():
    p = {"offeringMaterials": '["https://k4txdyb.onk2.com/a?documentid=1", "not a url"]'}
    assert _offering_urls(p) == ["https://k4txdyb.onk2.com/a?documentid=1"]


def test_perpetual_notes():
    from kzbonds.sources.aix import _perpetual_note

    assert "первый колл 03.11.2030" in _perpetual_note("FRTBS.PERP")
    assert _perpetual_note("XYZ.PERP") == "Бессрочная; дата погашения у биржи условная"
