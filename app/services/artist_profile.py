"""Sanatçı bazlı genre DNA profili — 4 kaynaktan ağırlıklı, 30 gün TTL cache.

Track enrichment'ta iki iş için kullanılır:
  1. Track'in boş slot'larını doldurmak (track'te olmayan sanatçı türü).
  2. Track tamamen boşsa artist profilinden 3 tür koymak (genre_source='artist_profile').

Kaynaklar (güvenilirlik sırası — bkz. genre_normalize.SOURCE_WEIGHTS):
  - lastfm_artist     : artist.getTopTags
  - musicbrainz_artist: MB artist genres/tags
  - db_tracks         : bu sanatçının DB'deki track'lerinden tür frekansı
  - deezer_artist     : Deezer fallback — İSİM DOĞRULAMALI (difflib > 0.85)

Cache: artists tablosu, refreshed_at 30 günden eskiyse yeniden inşa. 4 kaynak
da boşsa genre_lookup_failed_at yazılır (kalıcı, retry yok).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from typing import Any

from app.services.deezer_genre import (
    get_deezer_artist_genres_by_id,
    get_deezer_artist_genres_scored,
)
from app.services.genre_normalize import (
    are_related,
    canonical_for,
    merge_scores,
    normalize_genres,
    select_slots,
)
from app.services.lastfm_genre import get_lastfm_artist_tags_scored
from app.services.musicbrainz_genre import get_musicbrainz_artist_genres

logger = logging.getLogger("rosso.worker.artist_profile")

_TTL_DAYS = 30
_FAILED_RETRY_DAYS = 7  # lookup_failed sanatçı 7 gün sonra yeniden denenir (kalıcı kilit yok)
_ARTIST_SLOTS = 5
_NAME_MATCH_THRESHOLD = 0.85
_TRUST_CONSISTENCY_MIN = 0.50  # ikili are_related tutarlılık eşiği
_TRUST_DB_ARBITER_MIN = 5      # db_tracks hakemliği için gereken min genre'lı track


def _pairwise_consistency(slots: list[str]) -> float:
    """Slot listesindeki tüm ikili çiftlerin are_related oranı (0-1).

    Tek/boş slot → 1.0 (çakışma yok). CLANN (100%) ile Manifest (0%) ayrımı için.
    """
    if len(slots) < 2:
        return 1.0
    total = 0
    related = 0
    for i in range(len(slots)):
        for j in range(i + 1, len(slots)):
            total += 1
            if are_related(slots[i], slots[j]):
                related += 1
    return related / total if total else 1.0


def resolve_lastfm_trust(
    lastfm_slots: list[str], db_genres: list[str]
) -> tuple[str, list[str]]:
    """Last.fm artist tag'lerine güvenilir mi? (BÖLÜM 2, Fable 5 2026-07-03).

    Args:
        lastfm_slots: Last.fm'den normalize edilmiş kanonik türler.
        db_genres: Bu sanatçının DB track'lerindeki ham genre listesi (frekans için).

    Döner: (status, kept_slots)
        - "EMPTY"   : slot yok.
        - "TRUSTED" : güvenilir; kept_slots merge'e girecek türler.
        - "SUSPECT" : güvenilmez; kept_slots=[] (Last.fm merge'den hariç).

    Kural:
      1. slots boş → EMPTY.
      2. tek tür → TRUSTED (Şehinşah/Ceza).
      3. ikili tutarlılık ≥ %50 → TRUSTED (CLANN).
      4. tutarlılık < %50 → db_tracks hakemliği:
         a. DB'de ≥5 genre'lı track + DB çoğunluk türü lastfm slot'larından
            biriyle akraba → sadece o aile korunur (daraltılmış TRUSTED, Motive).
         b. aksi → SUSPECT (Azer Bülbül / profil zehirlenmesi koruması).
    """
    slots = list(lastfm_slots or [])
    if not slots:
        return "EMPTY", []
    if len(slots) == 1:
        return "TRUSTED", slots
    if _pairwise_consistency(slots) >= _TRUST_CONSISTENCY_MIN:
        return "TRUSTED", slots

    # ── Çakışma var → db_tracks hakemliği ──
    canon_db = [c for c in (canonical_for(g) for g in (db_genres or [])) if c]
    if len(canon_db) < _TRUST_DB_ARBITER_MIN:
        return "SUSPECT", []

    # DB çoğunluk türü
    freq: dict[str, int] = {}
    for c in canon_db:
        freq[c] = freq.get(c, 0) + 1
    db_majority = max(freq, key=freq.get)

    # DB çoğunluğu lastfm slot'larından biriyle akraba mı?
    related_slots = [s for s in slots if are_related(db_majority, s)]
    if related_slots:
        return "TRUSTED", related_slots  # daraltılmış: yabancı slot'lar atıldı
    return "SUSPECT", []


def normalize_artist_name(name: str) -> str:
    """Sanatçı adı eşleştirme anahtarı: trim + lowercase."""
    return (name or "").strip().lower()


def is_artist_match(query: str, found: str | None, threshold: float = _NAME_MATCH_THRESHOLD) -> bool:
    """Deezer'ın döndürdüğü sanatçı, aradığımız sanatçı mı? (difflib benzerlik).

    Drake → 'Drake Milligan' veya SALİ → 'Saliva' gibi yanlış eşleşmeleri eler.
    """
    if not found:
        return False
    ratio = SequenceMatcher(None, (query or "").lower(), found.lower()).ratio()
    return ratio >= threshold


def get_db_artist_track_count(artist: str, client: Any) -> int:
    """Bu sanatçının DB'deki toplam track sayısı (isim-çakışması eşiği için).

    <5 track'li sanatçılarda artist-level tür güvenilmez (Mahmut Tuncer/Yıldız
    Tilbe gibi tek-track sanatçılar aynı adlı yabancı bir sanatçıyla karışıp
    yanlış 'black metal' profili alabiliyor). Eşik geçilene kadar profil kurulmaz;
    ileride yeni kullanıcılar track ekleyip sayı ≥5 olunca güvenle çözülür.
    """
    if not artist:
        return 0
    try:
        res = (
            client.table("tracks")
            .select("id", count="exact")
            .contains("artists", [artist])
            .execute()
        )
        return int(getattr(res, "count", 0) or 0)
    except Exception:  # noqa: BLE001
        logger.warning("DB artist track-count sorgusu başarısız: %s", artist)
        return 0


def get_db_artist_genres(artist: str, client: Any) -> list[tuple[str, int]]:
    """Bu sanatçının DB'deki track'lerinden tür frekansı (name, count).

    tracks.genres array'lerini toplar, en sık geçen türleri count'uyla döner.
    Kendi verimiz — orta güvenilirlik (db_tracks weight).
    """
    if not artist:
        return []
    try:
        res = (
            client.table("tracks")
            .select("genres")
            .contains("artists", [artist])
            .not_.is_("genres", "null")
            .limit(200)
            .execute()
        )
    except Exception:  # noqa: BLE001
        logger.warning("DB artist genre sorgusu başarısız: %s", artist)
        return []

    freq: dict[str, int] = {}
    for row in res.data or []:
        for g in row.get("genres") or []:
            freq[g] = freq.get(g, 0) + 1
    # Frekansı count olarak kullan (0-100 clamp; normalize count/100 yapar)
    return [(name, min(count, 100)) for name, count in freq.items()]


def _build_profile(
    artist: str, client: Any, http: Any, lastfm_key: str,
    query_name: str | None = None, query_id: int | None = None,
    id_only: bool = False,
) -> dict:
    """4 kaynaktan ham tür topla, isim doğrula, ağırlıklı skorla → genre_data dict.

    query_name (çapa) verilirse Last.fm/MB/Deezer artist sorguları onunla yapılır
    (isim çakışması koruması). db_tracks her zaman gerçek `artist` (DB adı) ile.

    query_id (2026-07-04): track çapasının doğrulanmış Deezer artist.id'si. Verilirse
    Deezer DNA'sı isimle ARAMADAN doğrudan bu id'nin albümlerinden çözülür — Deezer
    isim araması yanlış sanatçı buluyor (Motive→Harp sanatçısı, Ceza→Brezilyalı).

    id_only (2026-07-16, az-track sanatçı çözümü): yalnızca İSİMSİZ kaynaklar —
    Deezer by-id (doğrulanmış çapa) + db_tracks. Last.fm/MB isim aramaları ATLANIR:
    az-track'li sanatçıda Last.fm güven hakemi çalışamaz (db_tracks'te ≥5 genre'lı
    track ister) ve tek-tag'lik yanlış profil otomatik TRUSTED sayılırdı (Mahmut
    Tuncer/black metal senaryosu). id'li Deezer yolu isim araması yapmadığı için
    çakışma riski taşımaz — sanatçının GERÇEK diskografisinden çoğunluk türü gelir.
    """
    q = query_name or artist
    scored_lists = []

    # 3. DB tracks (DB adıyla — kendi verimiz). Last.fm güven kuralının hakemi
    #    olduğu için önce çekilir (ham genre listesi resolve_lastfm_trust'a girer).
    db_raw = get_db_artist_genres(artist, client)
    db = normalize_genres(db_raw, "db_tracks")

    if not id_only:
        # 1. Last.fm artist (çapa adıyla) — GÜVEN KURALINDAN geçir (BÖLÜM 2).
        #    Track-seviyesi Last.fm bu katalogda ölü; artist-seviyesi zengin ama
        #    isim çakışmasına açık (Motive → ABD metalcore grubu). SUSPECT ise
        #    merge'e hiç girmez; TRUSTED ise (gerekirse daraltılmış slot'larla) girer.
        lastfm_scored = normalize_genres(
            get_lastfm_artist_tags_scored(q, lastfm_key, http), "lastfm_artist"
        )
        lastfm_slots = [sg.canonical for sg in lastfm_scored]
        db_ham = [name for name, _ in (db_raw or [])]
        status, kept = resolve_lastfm_trust(lastfm_slots, db_ham)
        if status == "TRUSTED":
            lastfm = [sg for sg in lastfm_scored if sg.canonical in set(kept)]
            scored_lists.append(lastfm)
        elif status == "SUSPECT":
            logger.info("Last.fm SUSPECT — merge'den hariç: %s (%s)", q, lastfm_slots)

        # 2. MusicBrainz artist (çapa adıyla)
        mb = normalize_genres(get_musicbrainz_artist_genres(q, http), "musicbrainz_artist")
        scored_lists.append(mb)

    scored_lists.append(db)

    # 4. Deezer artist DNA. query_id varsa (track çapasından doğrulanmış artist.id)
    #    doğrudan id'nin albümlerinden çözülür — isim çakışması imkânsız. Yoksa
    #    (geriye uyum) isimle ara + is_artist_match ile doğrula.
    if query_id:
        deezer_raw = get_deezer_artist_genres_by_id(query_id, http)
        if deezer_raw:
            scored_lists.append(normalize_genres(deezer_raw, "deezer_artist"))
    else:
        deezer_raw, found_name = get_deezer_artist_genres_scored(q, http)
        if deezer_raw and is_artist_match(q, found_name):
            scored_lists.append(normalize_genres(deezer_raw, "deezer_artist"))
        elif deezer_raw:
            logger.info("Deezer artist elendi (isim eşleşmedi): %s → %s", q, found_name)

    merged = merge_scores(scored_lists)
    return select_slots(merged, max_slots=_ARTIST_SLOTS)


_MIN_TRACKS_FOR_PROFILE = 5  # isim-çakışması eşiği (2026-07-05, Efendim kararı)


def get_or_build_artist_profile(
    artist: str, client: Any, http: Any, lastfm_key: str,
    query_name: str | None = None, query_id: int | None = None,
    min_tracks: int = 0,
    id_only: bool = False,
) -> dict:
    """Sanatçı DNA profilini cache'ten al ya da inşa et.

    Döner: genre_data dict (slots/weights/raw_scores/sources). Bulunamazsa
    slots=[] ve DB'de genre_lookup_failed_at işaretlenir.

    query_id: track çapasından doğrulanmış Deezer artist.id (isim çakışmasını önler).
    min_tracks: sanatçının bu sayının altında DB track'i varsa profil KURULMAZ
      (isim-çakışması koruması). 0 = kontrol yok (geriye uyum). Enrichment akışı
      _MIN_TRACKS_FOR_PROFILE (5) ile çağırır. Eşik altı → boş döner, cache'e
      yazılmaz; ileride track sayısı artınca yeniden denenir.
    id_only: yalnızca isimsiz kaynaklar (Deezer by-id + db_tracks) — az-track'li
      sanatçı için güvenli yol (bkz. _build_profile). query_id ZORUNLU; yoksa
      hiçbir şey yapmadan boş döner (cache de kirlenmez).
    """
    key = normalize_artist_name(artist)
    if not key:
        return {"slots": [], "weights": [], "raw_scores": {}, "sources": {}}

    # id_only yolun tek meşru girişi doğrulanmış id'dir — id yoksa sessizce boş
    # dön; cache'e lookup_failed YAZMA (sanatçı büyüyünce tam akış denesin).
    if id_only and not query_id:
        return {"slots": [], "weights": [], "raw_scores": {}, "sources": {}}

    # ── İsim-çakışması eşiği: az-track'li sanatçıda profil kurma ──────────
    if min_tracks > 0 and get_db_artist_track_count(artist, client) < min_tracks:
        logger.info(
            "Artist profil atlandı (yetersiz track, isim-çakışması koruması): %s", artist
        )
        return {"slots": [], "weights": [], "raw_scores": {}, "sources": {}}

    # ── Cache kontrol ──────────────────────────────────────────────────
    cached = None
    try:
        res = (
            client.table("artists")
            .select("name_normalized, genre_data, genres, refreshed_at, genre_lookup_failed_at")
            .eq("name_normalized", key)
            .limit(1)
            .execute()
        )
        if res.data:
            cached = res.data[0]
    except Exception:  # noqa: BLE001
        logger.warning("Artist cache okuma başarısız: %s", artist)

    now = datetime.now(timezone.utc)
    is_fresh = False
    if cached and cached.get("refreshed_at"):
        try:
            refreshed = datetime.fromisoformat(cached["refreshed_at"])
            if refreshed.tzinfo is None:
                refreshed = refreshed.replace(tzinfo=timezone.utc)
            # lookup_failed sanatçılar daha kısa TTL ile yeniden denenir (kalıcı
            # kilit yok): başarılı profil 30 gün, başarısız 7 gün (Fable 5).
            ttl = _FAILED_RETRY_DAYS if cached.get("genre_lookup_failed_at") else _TTL_DAYS
            is_fresh = (now - refreshed) < timedelta(days=ttl)
        except (ValueError, TypeError):
            is_fresh = False

    if cached and is_fresh:
        gd = cached.get("genre_data") or {}
        return {
            "slots": gd.get("slots", []),
            "weights": gd.get("weights", []),
            "raw_scores": gd.get("raw_scores", {}),
            "sources": gd.get("sources", {}),
        }

    # ── İnşa et (yeni veya bayat) ──────────────────────────────────────
    profile = _build_profile(
        artist, client, http, lastfm_key,
        query_name=query_name, query_id=query_id, id_only=id_only,
    )
    has_result = bool(profile["slots"])

    row = {
        "name": artist,
        "name_normalized": key,
        "genre_data": profile if has_result else None,
        "genres": profile["slots"] if has_result else None,
        "genre_source": "multi_source" if has_result else "lookup_failed",
        "genre_lookup_failed_at": None if has_result else now.isoformat(),
        "refreshed_at": now.isoformat(),
    }

    try:
        if cached:
            client.table("artists").update(row).eq("name_normalized", key).execute()
        else:
            # upsert (plain insert değil): `name_normalized` artık UNIQUE
            # (migration 0302) — check-then-insert (yukarıdaki select) ile
            # bu satır arasında nadir bir yarış olursa plain insert constraint
            # ihlaliyle patlardı; upsert aynı yarışta güvenle günceller.
            client.table("artists").upsert(row, on_conflict="name_normalized").execute()
    except Exception:  # noqa: BLE001
        logger.warning("Artist profil yazma başarısız: %s", artist)

    return profile
