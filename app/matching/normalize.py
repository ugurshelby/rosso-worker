"""Track eşleştirme normalizasyonu — saf fonksiyonlar.

Kaynak: track-matching-sync-research.md §3, §4, §5.
Platformlar arası başlık/sanatçı tutarsızlıklarını giderir.
"""
from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from typing import Any

# Türkçe özel karakter haritası (research §4 / platform-limits §5)
_TR_MAP = str.maketrans("şğıöüçŞĞİÖÜÇ", "sgioucSGIOUC")

_FEAT_RE = re.compile(
    r"\s*[\(\[]?\s*(?:feat|ft|featuring)\.?\s+([^\)\]]+)[\)\]]?\s*$",
    re.IGNORECASE,
)

MATCH_THRESHOLD = 0.85  # research §3


def normalize_title(title: str) -> str:
    """lowercase → Türkçe ASCII → aksan kaldır → noktalama sil → tek boşluk."""
    if not title:
        return ""
    t = title.translate(_TR_MAP).lower()
    # Unicode aksanları kaldır (café → cafe)
    t = unicodedata.normalize("NFKD", t)
    t = "".join(c for c in t if not unicodedata.combining(c))
    # Noktalama → boşluk, sonra harf/rakam dışını ele
    t = re.sub(r"[^\w\s]", "", t, flags=re.UNICODE)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def extract_featuring(title: str) -> tuple[str, list[str]]:
    """"Song (feat. X, Y)" → ("Song", ["X", "Y"]); yoksa (title, [])."""
    m = _FEAT_RE.search(title)
    if not m:
        return title.strip(), []
    feats = [a.strip() for a in re.split(r",|&|/| and ", m.group(1)) if a.strip()]
    clean = _FEAT_RE.sub("", title).strip()
    return clean, feats


def calculate_match_score(source: dict[str, Any], candidate: dict[str, Any]) -> float:
    """0.0-1.0 arası eşleşme skoru (research §3).

    title benzerliği 0.5 + artist örtüşmesi 0.4 + süre toleransı 0.1.
    """
    title_score = SequenceMatcher(
        None,
        normalize_title(source.get("title", "")),
        normalize_title(candidate.get("title", "")),
    ).ratio()

    src_artists = {normalize_title(a) for a in source.get("artists", []) if a}
    cand_artists = {normalize_title(a) for a in candidate.get("artists", []) if a}
    overlap = len(src_artists & cand_artists)
    artist_score = overlap / max(len(src_artists), 1)

    dur_diff = abs(
        (source.get("duration_ms") or 0) - (candidate.get("duration_ms") or 0)
    )
    duration_score = 1.0 if dur_diff < 10000 else 0.0

    return (title_score * 0.5) + (artist_score * 0.4) + (duration_score * 0.1)
