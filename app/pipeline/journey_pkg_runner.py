"""Journey yıl paketi üretimi (Aşama 3 · paket #1).

Belge: docs/reference/performans-olcumleri.md §12.4

`journey_year_pkg` tablosunu doldurur. `/journey` sayfası artık YALNIZ bu
paketi okur (0,13 ms); paket yoksa "hazırlanıyor" gösterir — canlı hesaba
ASLA düşmez.

TAZELİK KURALI (Efendim, 2026-07-31):
    *"İçinde bulunduğumuz yıl bile her gün tazelenmeye ihtiyaç duymuyor.
     Aylık bile yeter; sadece 'bugün' kısmı günlük tazelenir zaten."*

Ölçüm kararı destekliyor: kapanmış yılların son kaydı HEP 31 Aralık.

    Açık yıl      → AYLIK tazelenir (30 günden eskiyse)
    Kapanmış yıl  → bir kez hesaplanır, DONAR (`is_closed = true`)
    "Bugün" kartı → A·Canlı, paketlenmez (`get_journey_last_played`)

Cron etkisi: kullanıcı başına ayda 1 üretim.
    3 kullanıcı → günde 0,1  ·  10.000 kullanıcı → günde 333 (~18 dk)
Günlük seçilseydi 10.000 kullanıcıda günde 10.000 olurdu — 30 kat fazla.

⚠ `is_test` politikası (Efendim): test profilleri paketlerini BİR KEZ alır,
cron onlara DOKUNMAZ. Bu yüzden aday listesi `recap_real_user_ids()` — test
profillerini dışlayan RPC (migration 0129).
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("rosso.worker.journey_pkg_runner")

#: Açık yıl paketi kaç günden sonra bayat sayılır (Efendim: "aylık yeter").
STALE_AFTER_DAYS = 30

#: Paket başına saklanan kapak sayısı. Sayfa 12 gösteriyor; fazlası saklanıyor
#: ki gösterim sayısı değişince paket yeniden üretilmesin (§"paket ne tutmalı").
COVERS_PER_YEAR = 20


def run_journey_pkg(client: Any, force: bool = False) -> dict[str, Any]:
    """Bayat paketi olan kullanıcılar için `build_journey_year_pkg` çağırır.

    {outcome, users_processed, years_written, skipped, errors} döner.
    Bir kullanıcının hatası diğerlerini durdurmaz (izole) — `taste_runner`
    ile aynı desen.

    Args:
        force: True ise tazelik kontrolü atlanır, herkes yeniden üretilir.
               (Şema değişikliği sonrası toplu yeniden üretim için.)
    """
    # ⚠ Aday listesi DB tarafında (CLAUDE.md §4.3): Supabase REST ~1000 satırda
    # sessizce kırpar. `recap_real_user_ids()` hem DISTINCT yapar hem test
    # profillerini dışlar (migration 0129 + Efendim'in is_test kararı).
    try:
        res = client.rpc("recap_real_user_ids", {}).execute()
        user_ids = sorted({u for u in (res.data or []) if u})
    except Exception as exc:  # noqa: BLE001
        logger.exception("journey_pkg: aday sorgusu başarısız")
        return {"outcome": "error", "users_processed": 0, "years_written": 0,
                "skipped": 0, "errors": 0, "error": str(exc)[:300]}

    if not user_ids:
        return {"outcome": "empty", "users_processed": 0, "years_written": 0,
                "skipped": 0, "errors": 0}

    processed = 0
    years_written = 0
    skipped = 0
    errors = 0

    for uid in user_ids:
        try:
            if not force and not _needs_refresh(client, uid):
                skipped += 1
                continue

            res = client.rpc(
                "build_journey_year_pkg",
                {"p_user_id": uid, "p_covers": COVERS_PER_YEAR},
            ).execute()

            written = res.data if isinstance(res.data, int) else 0
            years_written += written
            processed += 1

            # Enrich package with 5 latent analytical dimensions & chromatic palette
            enrich_journey_latent_dimensions(client, uid)
        except Exception as exc:  # noqa: BLE001
            errors += 1
            logger.warning("journey_pkg başarısız user=%s: %s", uid, str(exc)[:200])

    if processed == 0 and errors > 0:
        outcome = "error"
    elif errors > 0:
        outcome = "partial"
    elif processed == 0:
        # Hepsi taze — normal durum, hata değil.
        outcome = "empty"
    else:
        outcome = "success"

    return {
        "outcome": outcome,
        "users_processed": processed,
        "years_written": years_written,
        "skipped": skipped,
        "errors": errors,
    }


def _needs_refresh(client: Any, user_id: str) -> bool:
    """Bu kullanıcının AÇIK yıl paketi bayat mı (30 günden eski) ya da hiç yok mu?

    Kapanmış yıllar (`is_closed = true`) sorgu dışında — onlar bir daha
    hesaplanmaz. Paket hiç yoksa True (ilk üretim).
    """
    from datetime import datetime, timedelta, timezone

    cutoff = (datetime.now(timezone.utc) - timedelta(days=STALE_AFTER_DAYS)).isoformat()

    try:
        res = (
            client.table("journey_year_pkg")
            .select("year, generated_at")
            .eq("user_id", user_id)
            .eq("is_closed", False)
            .gte("generated_at", cutoff)
            .limit(1)
            .execute()
        )
    except Exception:  # noqa: BLE001
        # Kontrol edilemiyorsa ÜRET — bayat veri göstermektense fazladan
        # hesaplamak yeğdir (kullanıcı sayısı bugün 3).
        logger.warning("journey_pkg: tazelik kontrolü başarısız user=%s → üretilecek", user_id)
        return True

    # Taze bir açık-yıl satırı varsa atla.
    return not (res.data or [])


def enrich_journey_latent_dimensions(client: Any, user_id: str) -> None:
    """Enriches journey_year_pkg payload with 5 latent analytical dimensions & 4-color palette."""
    from app.pipeline.journey_mining import mine_year_latent_dimensions

    try:
        pillar_candidates: list[dict[str, Any]] = []
        try:
            pc_res = client.rpc("get_journey_pillar_candidates", {"p_user_id": user_id}).execute()
            pillar_candidates = (pc_res.data or []) if hasattr(pc_res, "data") else []
        except Exception:
            pillar_candidates = []

        # Fetch years written for this user
        pkg_res = client.table("journey_year_pkg").select("year, payload").eq("user_id", user_id).execute()
        rows = (pkg_res.data or []) if hasattr(pkg_res, "data") else []

        for row in rows:
            year_val = row.get("year")
            if not year_val:
                continue
            year = int(year_val)
            payload = dict(row.get("payload") or {})

            # Fetch events for this year
            events: list[dict[str, Any]] = []
            try:
                ev_res = client.rpc(
                    "get_journey_mining_events",
                    {"p_user_id": user_id, "p_year": year},
                ).execute()
                events = (ev_res.data or []) if hasattr(ev_res, "data") else []
            except Exception:
                events = []

            covers = payload.get("covers") or []
            dominant_genre = str(payload.get("genre_label") or "")

            mined = mine_year_latent_dimensions(
                year=year,
                events=events,
                covers=covers,
                pillar_candidates=pillar_candidates,
                dominant_genre=dominant_genre,
            )

            payload["latent"] = mined["latent"]
            payload["palette"] = mined["palette"]

            try:
                client.table("journey_year_pkg").update({"payload": payload}).eq("user_id", user_id).eq("year", year).execute()
            except Exception:
                pass
    except Exception as exc:  # noqa: BLE001
        logger.warning("journey_pkg latent enrichment failed user=%s: %s", user_id, str(exc)[:200])
