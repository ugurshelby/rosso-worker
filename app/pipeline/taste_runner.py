"""Taste modeli yeniden hesaplama akışı (FAZ P6.7).

play_events'i olan her kullanıcı için `refresh_user_taste` RPC'sini çağırır —
bu RPC tüm zinciri koşturur (weights → behavioral → genre_vector → mainstream →
identity → materialize). Haftalık cron (§1.9) + ZIP-sonrası tetiklenebilir.

Tasarım: docs/vision/rosso-social-media.md §1.9, §1.13-14.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("rosso.worker.taste_runner")


def run_taste_refresh(client: Any) -> dict[str, Any]:
    """Taste verisi olan tüm kullanıcılar için refresh_user_taste çağırır.

    {outcome, users_processed, errors} döner. Bir kullanıcının hatası diğerlerini
    durdurmaz (izole); hepsi patlarsa outcome='error', kısmi ise 'partial'.
    """
    # Taste verisi olan kullanıcılar = play_events'te en az 1 kaydı olanlar.
    # Tekil liste DB tarafında (recap_user_ids RPC, migration 0102): eski
    # `.select("user_id").limit(100000)` yolu Supabase REST'in ~1000 satır
    # kırpması yüzünden 253k satırlık tabloda yalnız EN ESKİ kullanıcıları
    # görüyordu — Ferzan'ın taste'i hiç üretilmemişti (2026-07-17 canlı bulgu,
    # recap_runner'daki bug'ın aynısı). RPC başarısızsa eski yola düşer.
    try:
        res = client.rpc("recap_user_ids", {}).execute()
        user_ids = sorted({u for u in (res.data or []) if u})
    except Exception:  # noqa: BLE001
        logger.warning("recap_user_ids RPC başarısız — eski tarama yoluna düşüldü")
        res = (
            client.table("play_events")
            .select("user_id")
            .limit(100000)
            .execute()
        )
        rows = res.data or []
        user_ids = sorted({r["user_id"] for r in rows if r.get("user_id")})

    if not user_ids:
        return {"outcome": "empty", "users_processed": 0, "errors": 0}

    processed = 0
    errors = 0
    for uid in user_ids:
        try:
            client.rpc("refresh_user_taste", {"p_user_id": uid}).execute()
            processed += 1
        except Exception as exc:  # noqa: BLE001
            errors += 1
            logger.warning("taste refresh başarısız user=%s: %s", uid, str(exc)[:200])

    if processed == 0 and errors > 0:
        outcome = "error"
    elif errors > 0:
        outcome = "partial"
    else:
        outcome = "success"

    return {"outcome": outcome, "users_processed": processed, "errors": errors}
