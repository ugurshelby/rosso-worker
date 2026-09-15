"""Günlük eşleşme partisi üretimi (2026-07-15 bulgusu — build_match_batch
hiçbir cron'a bağlı değildi, son parti elle üretilmişti).

Sosyal profili olan her kullanıcı için ÜÇ RPC'yi sırayla çağırır:
  1. `build_match_batch`        — günlük 5 eşleşme
  2. `persist_discover_contrast`— zıt kutup önerileri
  3. `build_suggestion_batch`   — öneri paketi (FAZ 6)

⚠ SIRA BAĞLAYICI: öneri paketi eşleşmeden SONRA koşar, çünkü "o günün
eşleşme slotundakiler hariç" kuralı var. Ters sırada aynı profil iki
yerde birden çıkardı.

Desen taste_runner ile aynı: bir kullanıcının hatası diğerlerini durdurmaz,
her adım kendi içinde izole.

⚠ 2026-08-10 (FAZ EŞLEŞME-V2): "üç-kulvar skorlama" artık GEÇERSİZ —
kulvarlar kaldırıldı, tek skor var (migration 0259).
Tasarım: docs/plans/eslesme-oneri-motoru-sadelestirme-plani.md
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("rosso.worker.match_batch_runner")


def run_match_batch(client: Any) -> dict[str, Any]:
    """Sosyal profili olan tüm kullanıcılar için build_match_batch çağırır.

    {outcome, users_processed, errors} döner. Bir kullanıcının hatası diğerlerini
    durdurmaz (izole); hepsi patlarsa outcome='error', kısmi ise 'partial'.
    """
    # Aday havuzu build_match_batch içinde zaten social_profiles'a bakıyor
    # (gender NULL ise 0 döner) — kullanıcı listesini de aynı tablodan çekmek
    # anlamsız RPC çağrısını (taste verisi olmayan kullanıcı için) önler.
    # ⚠ Supabase REST ~1000 satırda kırpar (2026-07-17 recap/taste bug'ı) —
    # social_profiles kullanıcı başına 1 satır olduğu için ~1000 KULLANICIYA
    # kadar güvenli; o eşiğe yaklaşınca RPC'ye geç (bkz. recap_user_ids).
    res = (
        client.table("social_profiles")
        .select("user_id")
        .limit(100000)
        .execute()
    )
    rows = res.data or []
    user_ids = sorted({r["user_id"] for r in rows if r.get("user_id")})

    # 2026-07-20: Erken uyarı. Bu sorgu ~1000 satırda SESSİZCE kırpılır — hata
    # yok, log yok, yalnız eksik kullanıcı (2026-07-17'de recap+taste tam bunu
    # yaşadı, Ferzan iki cron'dan da düştü). Ölçüm: bugün 32 kullanıcı, tavana
    # 968 var. Tavana yaklaşınca burada GÖRÜNÜR uyarı çıkar ki sessiz kayıp
    # yaşanmadan RPC'ye (bkz. recap_user_ids, migration 0102) geçilebilsin.
    if len(rows) >= 900:
        logger.error(
            "match_batch: social_profiles %d satır — PostgREST ~1000 tavanına "
            "yaklaşıldı, kullanıcılar SESSİZCE düşmek üzere. DB-taraflı DISTINCT "
            "RPC'ye geçilmeli (CLAUDE.md §1.65).",
            len(rows),
        )

    if not user_ids:
        return {"outcome": "empty", "users_processed": 0, "errors": 0}

    processed = 0
    errors = 0
    contrast_written = 0
    contrast_errors = 0
    suggestion_written = 0
    suggestion_errors = 0
    for uid in user_ids:
        try:
            client.rpc("build_match_batch", {"p_user_id": uid}).execute()
            processed += 1
        except Exception as exc:  # noqa: BLE001
            errors += 1
            logger.warning("match batch başarısız user=%s: %s", uid, str(exc)[:200])

        # Plan 10 FAZ 5 — zıt kutup önerileri kalıcı tabloya (0226).
        # ⚠ Efendim: "Zıt kutup da anlık hesaplanmamalı, performansı gözetmeliyiz."
        # Ölçüldü (2026-08-06): anlık yol 20,2 ms / 1.683 blok; kalıcı tablodan
        # okuma 0,36 ms / 8 blok — 56 kat hızlı, 210 kat az disk. Fark topluluk
        # büyüdükçe açılır (anlık yol HER adayı tek tek hesaplıyor).
        #
        # ⚠ AYRI CRON AÇILMADI: yeni Railway servisi CLAUDE.md §5 "dur ve sor"
        # listesinde. Ayrıca aynı kullanıcı listesini iki kez dolaşmak gereksiz;
        # zıt kutup zaten match_daily_slots'a bakıyor (aynı turda üretilmiş
        # olması DOĞRU sıralama).
        #
        # ⚠ İZOLE: zıt kutup hatası eşleşme turunu BOZMAZ. Eşleşme ana üründür;
        # zıt kutup keşif zenginleştirmesidir. Tablo boş kalırsa okuyucu RPC
        # anlık yola düşer (0226 fallback) — ekran boşalmaz.
        try:
            client.rpc(
                "persist_discover_contrast", {"p_user_id": uid, "p_limit": 10}
            ).execute()
            contrast_written += 1
        except Exception as exc:  # noqa: BLE001
            contrast_errors += 1
            logger.warning(
                "zıt kutup yazımı başarısız user=%s: %s", uid, str(exc)[:200]
            )

        # FAZ EŞLEŞME-V2 · FAZ 6 — öneri paketi (Efendim'in ana isteği):
        # "öneriler de gece bir paket halinde sunulsun; gün içinde uygulamaya
        # girdiğimde zaten hazır olarak sosyal sayfasını göreyim."
        #
        # 🔴 SIRA KRİTİK: build_match_batch'ten SONRA çağrılıyor. Öneri
        # üretimi "o günün eşleşme slotundakiler hariç" diyor — ters sırada
        # koşarsa aynı profil HEM eşleşmede HEM öneride çıkardı.
        #
        # 🚫 YENİ RAILWAY SERVİSİ AÇILMADI (Efendim'in şartı). Zıt kutupla
        # aynı desen: aynı döngü, aynı kullanıcı listesi.
        #
        # ⚠ İZOLE: öneri hatası eşleşme turunu BOZMAZ. Paket yazılamazsa
        # get_suggestions anlık yola düşer (0260 yedek yolu) — ekran boşalmaz.
        try:
            client.rpc("build_suggestion_batch", {"p_user_id": uid}).execute()
            suggestion_written += 1
        except Exception as exc:  # noqa: BLE001
            suggestion_errors += 1
            logger.warning(
                "öneri paketi başarısız user=%s: %s", uid, str(exc)[:200]
            )

    if processed == 0 and errors > 0:
        outcome = "error"
    elif errors > 0:
        outcome = "partial"
    else:
        outcome = "success"

    return {
        "outcome": outcome,
        "users_processed": processed,
        "errors": errors,
        "contrast_written": contrast_written,
        "contrast_errors": contrast_errors,
        "suggestion_written": suggestion_written,
        "suggestion_errors": suggestion_errors,
    }
