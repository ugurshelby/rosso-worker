"""Match normalize testleri — track-matching-sync-research.md §3, §4, §5."""
from app.matching.normalize import (
    normalize_title,
    extract_featuring,
    calculate_match_score,
)


# ─── normalize_title (research §4: lowercase, noktalama, Türkçe ASCII) ───

def test_lowercase_and_punctuation():
    assert normalize_title("Don't!") == "dont"


def test_turkish_chars_to_ascii():
    assert normalize_title("Şarkı Güneş") == "sarki gunes"


def test_collapse_whitespace():
    assert normalize_title("  hello    world  ") == "hello world"


def test_accents_removed():
    assert normalize_title("Café") == "cafe"


# ─── extract_featuring (research §3: feat. title'dan çıkar) ───

def test_extract_feat_parens():
    title, feats = extract_featuring("Song Name (feat. Artist2, Artist3)")
    assert title == "Song Name"
    assert feats == ["Artist2", "Artist3"]


def test_extract_ft_brackets():
    title, feats = extract_featuring("Track [ft. Someone]")
    assert title == "Track"
    assert feats == ["Someone"]


def test_no_featuring():
    title, feats = extract_featuring("Plain Title")
    assert title == "Plain Title"
    assert feats == []


# ─── calculate_match_score (research §3: 0.5 title + 0.4 artist + 0.1 dur) ───

def test_perfect_match_scores_high():
    source = {"title": "Song", "artists": ["Artist"], "duration_ms": 200000}
    cand = {"title": "Song", "artists": ["Artist"], "duration_ms": 200000}
    assert calculate_match_score(source, cand) >= 0.99


def test_different_title_scores_low():
    source = {"title": "Completely Different", "artists": ["X"], "duration_ms": 100000}
    cand = {"title": "Nothing Alike", "artists": ["Y"], "duration_ms": 300000}
    assert calculate_match_score(source, cand) < 0.85


def test_duration_tolerance_10s():
    # title+artist aynı, süre 8sn fark → duration puanı tam
    source = {"title": "Song", "artists": ["A"], "duration_ms": 200000}
    cand = {"title": "Song", "artists": ["A"], "duration_ms": 208000}
    assert calculate_match_score(source, cand) >= 0.99
    # 12sn fark → duration puanı 0 ama title+artist hâlâ yüksek
    cand2 = {"title": "Song", "artists": ["A"], "duration_ms": 212000}
    assert 0.85 <= calculate_match_score(source, cand2) < 1.0
