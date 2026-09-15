"""DB-tabanlı API cooldown — bellek bayrağı yerine kalıcı durum.

Felsefe (spec §3): worker 429 yiyince blocked_until damgası vurur ve ÇIKAR.
SLEEP YOK. Bir sonraki cron tetiklemesi is_blocked() ile kapıyı kontrol eder.
Retry-After absürt değer dönse bile MAX_COOLDOWN_S ile sınırlanır.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger("rosso.worker.cooldown")

MAX_COOLDOWN_S = 21600  # 6 saat — Retry-After ne derse desin üst sınır


def is_blocked(client: Any, provider: str) -> tuple[bool, int]:
    """provider için aktif cooldown var mı? (bloklu_mu, kalan_saniye) döndürür.

    Kayıt yoksa veya blocked_until geçmişteyse → (False, 0).
    """
    try:
        res = client.rpc("cooldown_get", {"p_provider": provider}).execute()
        rows = res.data or []
    except Exception:  # noqa: BLE001
        logger.warning("cooldown_get başarısız (%s) — bloklu değil varsayılıyor", provider)
        return False, 0

    if not rows:
        return False, 0

    blocked_until_raw = rows[0].get("blocked_until")
    if not blocked_until_raw:
        return False, 0

    try:
        blocked_until = datetime.fromisoformat(blocked_until_raw.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return False, 0

    now = datetime.now(timezone.utc)
    if blocked_until <= now:
        return False, 0

    remaining = int((blocked_until - now).total_seconds())
    return True, remaining


def set_cooldown(
    client: Any,
    provider: str,
    retry_after_s: float,
    reason: str,
    *,
    max_cap_s: int = MAX_COOLDOWN_S,
) -> int:
    """provider'a cooldown damgası vur. Retry-After cap'lenir; kullanılan saniyeyi döndürür.

    SLEEP YOK — sadece DB'ye blocked_until = now() + min(retry_after, cap) yazılır.
    """
    requested = int(max(retry_after_s, 0))
    used = min(requested, max_cap_s)
    blocked_until = (datetime.now(timezone.utc) + timedelta(seconds=used)).isoformat()
    try:
        client.rpc(
            "cooldown_set",
            {"p_provider": provider, "p_blocked_until": blocked_until, "p_reason": reason},
        ).execute()
        if requested > used:
            # Cap devreye girdi: blok, platformun istediğinden KISA. Tur bitince
            # muhtemelen yine 429 gelir — bu normaldir (cap bilinçli bir tercih),
            # ama log bunu söylemeli. Aksi hâlde "cooldown yazdık ama yine ceza
            # yiyoruz" tablosu sebepsiz görünür (2026-08-01 kapak dolgusu dersi).
            logger.warning(
                "Cooldown: %s %ds (%.1fh) bloklandı — sebep=%s ⚠ platform %ds (%.1fh) "
                "istemişti, %ds cap'i uygulandı; süre dolunca yine 429 gelebilir",
                provider, used, used / 3600, reason, requested, requested / 3600, max_cap_s,
            )
        else:
            logger.warning(
                "Cooldown: %s %ds (%.1fh) bloklandı — sebep=%s",
                provider, used, used / 3600, reason,
            )
    except Exception:  # noqa: BLE001
        logger.error("cooldown_set başarısız (%s) — sonraki cron yine deneyecek", provider)
    return used


def clear_cooldown(client: Any, provider: str) -> None:
    """provider'ın cooldown'unu temizle (başarılı çağrı sonrası — opsiyonel)."""
    try:
        past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        client.rpc(
            "cooldown_set",
            {"p_provider": provider, "p_blocked_until": past, "p_reason": "cleared"},
        ).execute()
    except Exception:  # noqa: BLE001
        logger.warning("cooldown temizlenemedi (%s)", provider)
