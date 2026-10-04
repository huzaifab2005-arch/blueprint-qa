from backend.services.grounding import (
    assess_finding, cap_confidence, confirm_quote, identifiers, ungrounded_identifiers,
)

PAGE = "RTU-1  TRANE YHC074  7.5 TON\nMAIN SUPPLY DUCT 24\"x12\"\nL1 2x4 LED 24LED-4000 QTY 14"


def test_identifiers_ignore_words_and_plain_numbers():
    assert identifiers("There are 14 fixtures; RTU-1 is a TRANE YHC074 at 24x12") == ["RTU-1", "YHC074", "24X12"]


def test_dimensions_match_across_formats():
    assert ungrounded_identifiers("duct is 24x12", PAGE) == []
    assert ungrounded_identifiers("duct is 30x18", PAGE) == ["30X18"]


def test_question_tags_do_not_count_as_ungrounded():
    assert ungrounded_identifiers("RTU-9 is not shown", "nothing here", "where is RTU-9?") == []


def test_confirm_quote_ignores_spacing_and_case():
    assert confirm_quote("rtu-1 trane yhc074", PAGE)
    assert not confirm_quote("CARRIER 50XC", PAGE)


def test_assess_rejects_unsupported_claims():
    v = assess_finding("RTU-1 is a CARRIER 50XC060", [False], PAGE, "what model is RTU-1")
    assert not v.accepted and "50XC060" in v.reason


def test_assess_accepts_supported_claims_and_caps_weak_ones():
    ok = assess_finding("RTU-1 is a TRANE YHC074", [True], PAGE, "model of RTU-1")
    assert ok.accepted and ok.confidence_cap is None
    weak = assess_finding("RTU-1 is a TRANE YHC074", [False], PAGE, "model of RTU-1")
    assert weak.accepted and weak.confidence_cap == "low"
    none = assess_finding("RTU-1 is a TRANE YHC074", [], PAGE, "model of RTU-1")
    assert none.confidence_cap == "low"


def test_image_only_sheet_is_capped_not_rejected():
    v = assess_finding("RTU-1 is a TRANE YHC074", [], "", "model of RTU-1")
    assert v.accepted and v.confidence_cap == "low"


def test_cap_confidence():
    assert cap_confidence("high", "low") == "low"
    assert cap_confidence("low", "medium") == "low"
    assert cap_confidence("high", None) == "high"
