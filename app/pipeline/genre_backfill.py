"""Genre lookup-failed backfill — artist profilinden GÜVENLİ geriye dönük doldurma.

Ana enrichment (genre_runner.run_one_genre_batch) her track'i anlık işler;
bir track işlendiği anda sanatçının profili henüz olgunlaşmamış olabilir.
Katalog ilerledikçe (artistler tracklerden önde gider) aynı sanatçının
profili sonradan zenginleşebilir — ama zaten lookup_failed yazılmış track'e
bu yeni bilgi hiç yansımaz. Bu modül, SADECE mevcut artist profil cache'ini
okuyarak (yeniden inşa etmeden) bu track'leri güvenli şekilde doldurur.

Güvenlik kuralı (Efendim onayı, 2026-07-02): backfill iki şart birden
sağlanırsa çalışır:
  1. Artist profilinde TEK baskın tür var (birden fazla tür = isim
     çakışması riski, örn. "Azer Bülbül" profilinde hem arabesk hem
     black metal — dokunulmaz).
  2. O türün kaynak listesinde db_tracks var — yani sanatçının DB'deki
     başka bir track'i zaten bu türle etiketlenmiş (track-seviyesinde
     doğrulanmış demektir, kör bir artist-tag'e güvenilmiyor).

Bilerek DEVREDE DEĞİL: bu modül bir Railway cron'una bağlanmadı, sadece
kod olarak hazır. Devreye alma ayrı bir karar (ana enrichment bitince).
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("rosso.worker.genre_backfill")


def resolve_backfill_genre(artist_profile: dict) -> str | None:
    """Artist profili iki güvenlik şartını sağlıyorsa tek türü döner, yoksa None."""
    slots = artist_profile.get("slots") or []
    if len(slots) != 1:
        return None

    genre = slots[0]
    sources = artist_profile.get("sources") or {}
    if "db_tracks" in (sources.get(genre) or []):
        return genre
    return None


def _normalize_artist_name(name: str) -> str:
    return (name or "").strip().lower()


def run_one_genre_backfill_batch(client: Any, *, batch_limit: int = 50) -> dict[str, Any]:
    """genre_lookup_failed_at dolu track'leri, CACHE'DENmiş artist profiliyle doldur.

    get_or_build_artist_profile ÇAĞIRMAZ — API'ye gitmez, sadece artists
    tablosundaki mevcut genre_data'yı okur. Profil yoksa/uygun değilse
    track'e dokunmaz (bir sonraki batch'te tekrar denenebilir, kalıcı
    fail yazmaz — ana enrichment'in aksine).
    """
    res = (
        client.table("tracks")
        .select("id, title, artists")
        .not_.is_("genre_lookup_failed_at", "null")
        .is_("genres", "null")
        .limit(batch_limit)
        .execute()
    )
    tracks = res.data or []
    if not tracks:
        return {"outcome": "empty", "processed": 0, "filled": 0}

    filled = 0
    for t in tracks:
        artists = t.get("artists") or []
        artist = artists[0] if artists else ""
        if not artist:
            continue

        key = _normalize_artist_name(artist)
        profile_res = (
            client.table("artists")
            .select("genre_data")
            .eq("name_normalized", key)
            .limit(1)
            .execute()
        )
        rows = profile_res.data or []
        if not rows or not rows[0].get("genre_data"):
            continue

        genre = resolve_backfill_genre(rows[0]["genre_data"])
        if not genre:
            continue

        try:
            client.table("tracks").update({
                "genres": [genre],
                "genre_source": "artist_profile_backfill",
                "genre_lookup_failed_at": None,
            }).eq("id", t["id"]).execute()
            filled += 1
        except Exception:  # noqa: BLE001
            logger.warning("Backfill güncelleme başarısız: track=%s", t["id"])

    return {"outcome": "success", "processed": len(tracks), "filled": filled}
