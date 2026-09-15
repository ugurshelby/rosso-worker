"""YouTube Data API v3 search.list günlük kota sayacı — DB-tabanlı, kalıcı.

Google, search.list için proje geneli günlük 100 çağrı sınırı koyuyor (tüm
kullanıcılar paylaşır). Worker her arama öncesi bu sayacı artırıp kontrol eder.
RPC başarısız olursa güvenli tarafta kalınır — arama yapılmaz (limit aşılmış
sayılır) ki sessizce kotayı aşıp Google'dan 403 yemeyelim.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("rosso.worker.yt_search_quota")

DAILY_LIMIT = 100


def increment_and_check(client: Any) -> int:
    """Bugünün arama sayacını 1 artırır, yeni değeri döner."""
    try:
        res = client.rpc("yt_search_quota_increment", {}).execute()
        return int(res.data)
    except Exception:  # noqa: BLE001
        logger.warning("yt_search_quota_increment başarısız — limit aşılmış varsayılıyor")
        return DAILY_LIMIT + 1


def is_quota_exceeded(used_count: int) -> bool:
    """used_count günlük limite ulaştı/geçti mi?"""
    return used_count >= DAILY_LIMIT
