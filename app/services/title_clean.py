"""Şarkı başlığından feat. ekini temizler — Deezer/Last.fm track araması için.

Genre pipeline'da iki aşamalı arama kullanılır: önce ham başlık, boş dönerse
temiz başlık. Bu modül yalnızca sondaki feat. ekini atar; feat. içermeyen
başlığı değiştirmez (idempotent, güvenli).
"""
from __future__ import annotations

import re

# Sondaki feat. eki: opsiyonel açan parantez/köşeli ayraç + 'feat'/'ft' + kalan her şey.
_FEAT_RE = re.compile(r"\s*[\(\[]?\s*(?:feat|ft)\.?\s.*$", re.IGNORECASE)


def clean_title(title: str) -> str:
    """Başlıktan sondaki feat. ekini at. feat. yoksa girdiyi aynen döner."""
    if not title:
        return title
    return _FEAT_RE.sub("", title).strip()
