"""FAZ 6 — Spotify Dev Mode kabul kuyruğunun DOĞRULAMA ucu.

Allowlist'e ekleme elle yapılır (API yok — B17). Admin panelde "Eklendi" der ve
kayıt `approved` olur; ama gerçekten eklendi mi, bunu ancak Spotify'ın kendisi
söyler. Bu modül o cevabı okur:

  - senkron başarılı  → `active`  (erişim kanıtlandı)
  - senkron 403 aldı  → `pending` (hâlâ allowlist dışı; admin'in "eklendi"si tutmadı)

Böylece admin panelindeki durum yalan söylemez.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

logger = logging.getLogger("rosso.worker.spotify_allowlist")

_TABLE = "spotify_allowlist_requests"


def mark_access(client, user_id: str, *, granted: bool) -> None:
    """Kullanıcının Spotify erişim durumunu gözlenen gerçeğe göre günceller.

    `rejected` kayıtlara dokunmaz — o admin'in bilinçli kararıdır.
    """
    try:
        existing = (
            client.table(_TABLE)
            .select("status")
            .eq("user_id", user_id)
            .limit(1)
            .execute()
        )
        rows = existing.data or []
        current = rows[0]["status"] if rows else None

        if current == "rejected":
            return
        if granted and current == "active":
            return  # zaten doğru
        if not granted and current == "pending":
            return  # zaten doğru

        payload = {
            "user_id": user_id,
            "status": "active" if granted else "pending",
        }
        if granted:
            payload["activated_at"] = datetime.now(timezone.utc).isoformat()

        client.table(_TABLE).upsert(payload, on_conflict="user_id").execute()
        logger.info(
            "Spotify erişim durumu güncellendi: user=%s → %s",
            user_id,
            payload["status"],
        )
    except Exception:  # noqa: BLE001
        # Yan-defter; ana senkronu çöktürmez. Ama sessiz de kalmaz (B19).
        logger.warning(
            "Spotify allowlist durumu güncellenemedi: user=%s granted=%s",
            user_id,
            granted,
            exc_info=True,
        )
