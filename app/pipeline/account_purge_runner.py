"""Otomatik kalıcı silme (purge) — soft-delete süresi (30 gün) dolan hesapları siler.

Bağlam (Aşama D / D1, migration 0100 + admin purge route):
  Hesap silme İKİ aşamalı. (1) Kullanıcı/admin soft-delete eder →
  social_profiles.deleted_at damgalanır, veri 30 gün DURUR (geri alınabilir).
  (2) 30 gün dolunca KALICI silme. Bugüne kadar bu ikinci aşama YALNIZ admin
  panelinden ELLE yapılıyordu (admin .../purge/route.ts). Bu runner onu
  otomatikleştirir — süresi dolmuş hesapları kendiliğinden siler.

Neden güvenli (admin route ile AYNI kapılar, e-posta onayı hariç):
  - Kapı 1: yalnız deleted_at DOLU (soft-delete edilmiş) hesaplar aday.
  - Kapı 2: deleted_at + 30 gün < now — süresi GERÇEKTEN dolmuş olmalı.
  - Admin route'un 3. kapısı (e-posta birebir) bir İNSAN yanlış kullanıcıyı
    silmesin diyeydi; burada seçim DB filtresiyle otomatik ve dar, ekstra
    insan-hatası riski yok. Yine de her turda AZ hesap (batch_limit) sileriz —
    bir bug tüm silinmişleri tek turda süpürmesin (§1.7 küçük adım).

Ne siler (admin route ile aynı sıra):
  1. profile-photos/{user_id}/ altındaki yetim dosyalar (Storage).
  2. auth.users kaydı → CASCADE ile play_events/playlists/taste/mesajlar/…
     hepsi gider. Bu geri DÖNÜŞSÜZ; ama 30 gün grace + soft-delete guard'ı
     zaten kullanıcıya kurtarma penceresi verdi.

⚠ Bu cron dış API'ye (Spotify vb.) İSTEK ATMAZ — yalnız Supabase (kendi DB +
Storage + auth admin). Rate-limit (§1.6) konusu yok. Ama yıkıcı olduğu için
batch küçük ve her silme tek tek, hata izole.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger("rosso.worker.account_purge_runner")

# migration 0100 ve admin purge route ile BİREBİR aynı — tek kaynak burası değil,
# ama ikisi de 30 kullanıyor. Değişirse üçü birden (SQL yorumu + TS + burası).
PURGE_AFTER_DAYS = 30
_BUCKET = "profile-photos"


def _delete_storage_folder(client: Any, user_id: str) -> int:
    """profile-photos/{user_id}/ altındaki dosyaları siler, silinen sayısını döner.

    Hata yutulur ve loglanır — Storage temizliği CASCADE silmeyi ENGELLEMEMELİ
    (yetim dosya kalması, kullanıcının hâlâ silinememesinden iyidir).
    """
    try:
        files = client.storage.from_(_BUCKET).list(user_id)
    except Exception:  # noqa: BLE001
        logger.warning("Purge: Storage list başarısız user=%s", user_id)
        return 0
    if not files:
        return 0
    paths = [f"{user_id}/{f['name']}" for f in files if f.get("name")]
    if not paths:
        return 0
    try:
        client.storage.from_(_BUCKET).remove(paths)
        return len(paths)
    except Exception:  # noqa: BLE001
        logger.warning("Purge: Storage remove başarısız user=%s (%d dosya)", user_id, len(paths))
        return 0


def run_account_purge(
    client: Any,
    batch_limit: int = 25,
) -> dict[str, Any]:
    """Soft-delete süresi dolmuş hesapları kalıcı siler.

    {outcome, eligible, purged, failed, storage_files}. outcome:
      'empty'   — süresi dolmuş hesap yok (normal durum — bakım modu).
      'success' — en az bir hesap silindi, hata yok.
      'partial' — bazıları silindi ama en az bir silme başarısız.
      'error'   — aday sorgusu bile alınamadı.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=PURGE_AFTER_DAYS)).isoformat()

    # Aday: deleted_at DOLU ve cutoff'tan ESKİ (30 günü geçmiş). En eskiler önce.
    try:
        res = (
            client.table("social_profiles")
            .select("user_id, deleted_at")
            .not_.is_("deleted_at", "null")
            .lt("deleted_at", cutoff)
            .order("deleted_at")
            .limit(batch_limit)
            .execute()
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Purge: aday sorgusu başarısız")
        return {"outcome": "error", "eligible": 0, "purged": 0, "failed": 0,
                "storage_files": 0, "error": str(exc)[:300]}

    rows = res.data or []
    if not rows:
        return {"outcome": "empty", "eligible": 0, "purged": 0, "failed": 0, "storage_files": 0}

    purged = 0
    failed = 0
    storage_files = 0

    for row in rows:
        user_id = row["user_id"]
        try:
            # 1) Storage yetim dosyaları (CASCADE bunları temizlemez — bucket ayrı).
            storage_files += _delete_storage_folder(client, user_id)

            # 2) auth.users → CASCADE. Python supabase admin API.
            client.auth.admin.delete_user(user_id)
            purged += 1
            logger.info(
                '{"run":"account_purge","status":"purged","user":"%s","deleted_at":"%s"}',
                user_id, row.get("deleted_at"),
            )
        except Exception:  # noqa: BLE001
            failed += 1
            logger.exception("Purge: kullanıcı silinemedi user=%s", user_id)
            continue

    if failed and purged:
        outcome = "partial"
    elif failed:
        outcome = "error"
    else:
        outcome = "success"

    return {
        "outcome": outcome,
        "eligible": len(rows),
        "purged": purged,
        "failed": failed,
        "storage_files": storage_files,
    }
