"""AIX reports issue size as a bond count; volume is count x face value."""

from kzbonds.sources.aix import _issue_volume


def test_volume_uses_current_issued_quantity():
    # Live MFKZ.1030 profile values (USD 1.5bn sovereign Eurobond).
    profile = {"faceValue": "1000", "issuedQuantity": "1500000", "originalIssuedQuantity": "1500000"}
    assert _issue_volume(profile) == 1_500_000_000


def test_current_quantity_wins_over_original_after_buyback():
    # Live KMG.0427: partially bought back, outstanding < placed.
    profile = {"faceValue": "1000", "issuedQuantity": "250093", "originalIssuedQuantity": "1000000"}
    assert _issue_volume(profile) == 250_093_000


def test_falls_back_to_original_quantity():
    profile = {"faceValue": "1000", "issuedQuantity": None, "originalIssuedQuantity": "2000"}
    assert _issue_volume(profile) == 2_000_000


def test_missing_face_value_gives_none():
    assert _issue_volume({"issuedQuantity": "100"}) is None
