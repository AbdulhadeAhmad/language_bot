from app.services.collections import normalize_german


def test_duplicate_normalization_preserves_spelling_case_and_punctuation():
    assert normalize_german("  Ich\t komme.\n") == "Ich komme."
    assert normalize_german("Gru\u0308ße\u00a0aus Berlin.") == "Grüße aus Berlin."
    assert normalize_german("ich komme.") != normalize_german("Ich komme.")
    assert normalize_german("Ich komme!") != normalize_german("Ich komme.")
