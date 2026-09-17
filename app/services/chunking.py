"""Paylaşılan batching yardımcı fonksiyonu.

Supabase `.upsert()` / `.insert()` çağrılarını tek seferde binlerce satırla
yapmak (timeout, payload boyutu) risklidir. `export_runner.py` bunu zaten
`_chunked` ile çözüyordu; parser'lardaki (account_data_parser,
technical_log_parser) benzer upsert'ler de aynı sınırı aşmadan yazmalı.
Bu yüzden helper buraya taşındı, tek kaynaktan paylaşılır.
"""
from __future__ import annotations

from typing import Any, Iterator


def chunked(items: list[Any], size: int) -> Iterator[list[Any]]:
    """`items`'ı `size` boyutunda parçalara böler (son parça kısa olabilir)."""
    if size <= 0:
        size = len(items) or 1
    for i in range(0, len(items), size):
        yield items[i:i + size]
