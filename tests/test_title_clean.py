"""title_clean — feat. eki temizleme testleri."""
from app.services.title_clean import clean_title


def test_paren_feat_removed():
    assert clean_title("Spicy (feat. Post Malone)") == "Spicy"


def test_bracket_feat_removed():
    assert clean_title("Woo Baby [feat. Chris Brown]") == "Woo Baby"


def test_bare_feat_removed():
    assert clean_title("Gece Gündüz feat. MERO") == "Gece Gündüz"


def test_multi_feat_removed():
    assert clean_title("Dorado (feat. Sfera Ebbasta & Feid)") == "Dorado"


def test_no_feat_unchanged():
    assert clean_title("Yaşanacaksa") == "Yaşanacaksa"


def test_title_with_paren_but_no_feat_unchanged():
    # feat. içermeyen parantez korunur
    assert clean_title("Matmazel - Hoodtrap") == "Matmazel - Hoodtrap"


def test_empty_string():
    assert clean_title("") == ""


def test_idempotent():
    once = clean_title("Spicy (feat. X)")
    assert clean_title(once) == once
