"""Account Data ZIP parser — saf fonksiyonlar.

İşlenen dosyalar (Spotify Account Data ZIP):
  - YourLibrary.json      → liked_songs_events (snapshot 'liked')
  - Inferences.json       → user_export_signals ('inferences')
  - Wrapped2025.json      → user_export_signals ('wrapped_2025')
  - YourSoundCapsule.json → user_export_signals ('sound_capsule')
  - Playlist1.json        → playlist_track_events (addedDate → recap küratörlük/gap sinyali)
  - PlaylistCreated.json  → playlists.source_created_at (yeni bilgi)

Hard-discard (içerik parse edilmez):
  - Identifiers.json, Identity.json, UserAttributes.json,
    AdsIdentitySecondPartyIdentifiers.json

Referans: docs/specs/spotify-data-spec.md §4.2
"""
from __future__ import annotations

import json
import logging
from typing import Any

from app.services.chunking import chunked

logger = logging.getLogger("rosso.worker.account_data")

# ─── hard-discard dosyaları ────────────────────────────────────────────────
HARD_DISCARD_FILES = frozenset({
    "Identifiers.json",
    "Identity.json",
    "UserAttributes.json",
    "AdsIdentitySecondPartyIdentifiers.json",
})

# ─── account_data whitelist ────────────────────────────────────────────────
ACCOUNT_DATA_WHITELIST = frozenset({
    "YourLibrary.json",
    "Inferences.json",
    "Wrapped2025.json",
    "YourSoundCapsule.json",
    "Playlist1.json",
})


def parse_your_library(data: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """YourLibrary.json → liked_songs_events satırları (statik snapshot).

    Format: [{"uri": "spotify:track:xxx", ...}, ...]
    Her kayıt 'liked' event_type ile döner; occurred_at bilinmiyor →
    AddedToCollection zaten olmadan da snapshot olarak kullanılabilir.
    occurred_at None olarak döner; worker import_at damgasını kullanır.
    """
    rows: list[dict[str, Any]] = []
    # YourLibrary.json formatı: {"tracks": [...], "albums": [...], ...}
    if isinstance(data, dict):
        items = data.get("tracks", [])
    elif isinstance(data, list):
        items = data
    else:
        return rows

    for item in items:
        uri = item.get("uri") or item.get("spotify_uri")
        if not uri or not uri.startswith("spotify:track:"):
            continue
        rows.append({
            "spotify_uri": uri,
            "event_type": "liked",
            "occurred_at": None,  # snapshot: zaman bilinmiyor
        })
    return rows


def parse_saved_library(data: Any) -> list[dict[str, Any]]:
    """YourLibrary.json → `albums` + `artists` bölümleri (A7).

    Bugüne kadar YALNIZ `tracks` okunuyordu; albüm ve sanatçı kayıtları
    sessizce atılıyordu. Plan (P4): *"albüm kaydetmek beğenmekten güçlü niyet
    sinyali"* — tek şarkı anlık tepkidir, albüm "bunu bütün olarak istiyorum"
    demektir.

    Track'ler BURAYA girmez; onlar `liked_songs_events`'te kalır — o tablonun
    tüm tüketicileri (A2, A4, taste) uri'yi track varsayıyor.

    Dönüş: [{"item_type": "album"|"artist", "spotify_uri": str, "name": str|None}]
    """
    rows: list[dict[str, Any]] = []
    if not isinstance(data, dict):
        return rows

    for key, item_type, uri_prefix in (
        ("albums", "album", "spotify:album:"),
        ("artists", "artist", "spotify:artist:"),
    ):
        for item in data.get(key) or []:
            if not isinstance(item, dict):
                continue
            uri = item.get("uri") or item.get("spotify_uri")
            if not isinstance(uri, str) or not uri.startswith(uri_prefix):
                continue
            # Albümde "album" + "artist", sanatçıda yalnız "name" gelir.
            name = item.get("name") or item.get("album") or item.get("artist")
            rows.append({
                "item_type": item_type,
                "spotify_uri": uri,
                "name": name if isinstance(name, str) else None,
            })
    return rows


def parse_inferences(data: Any) -> dict[str, Any]:
    """Inferences.json → user_export_signals signal_data.

    Gerçek format: {"inferences": ["Interest | Music | Pop | Pop(1P)", ...]}
    Yalnızca "Interest | Music |" prefix'li etiketler müzik sayılır (KVKK §4:
    demografik/Education/Hobbies/1P_Custom/3P_Custom etiketleri saklanmaz).
    Eski keyword araması yanlış pozitif veriyordu (ör. "Musical Instruments"
    içinde "music" geçer ama bu bir hobi etiketidir, müzik zevki değil).
    """
    if isinstance(data, dict):
        raw_labels = data.get("inferences", [])
    elif isinstance(data, list):
        raw_labels = data
    else:
        raw_labels = []

    music_labels = [
        lbl for lbl in raw_labels
        if isinstance(lbl, str) and lbl.strip().startswith("Interest | Music |")
    ]

    return {
        "all_count": len(raw_labels),
        "music_labels": music_labels,
        "music_count": len(music_labels),
        # A12: motora giren normalize hâli. Ham etiket saklanmaya devam eder
        # (kaynağı kaybetmeyelim), ama tüketiciler bunu okur.
        "normalized_genres": normalize_inference_genres(music_labels),
    }


# A12 — Spotify tür etiketi → Rosso tür anahtarı.
# Yalnız ANLAMLI olanlar; tautoloji ("Music | Music") ve jenerik kova atılır.
# Katalog: docs/decisions/inferences-katalog.md
_INFERENCE_GENRE_MAP = {
    "pop": "pop",
    "rock": "rock",
    "metal": "metal",
    "punk": "punk",
    "indie": "indie",
    "trap": "trap",
    "hip-hop": "hip-hop",
    "r&b": "r&b",
    "soul": "soul",
    "blues": "blues",
    "jazz": "jazz",
    "folk": "folk",
    "classical": "classical",
    "edm": "electronic",
    "emo": "emo",
    "soundtrack": "soundtrack",
    "eras oldies": "oldies",
}


def normalize_inference_genres(music_labels: list[str]) -> list[str]:
    """A12 — ham Spotify etiketlerini Rosso tür anahtarlarına indirger.

    Girdi formatı (canlıda doğrulandı, 18 etiket):
        "Interest | Music | Hip-hop | Hip-hop(1P)"

    Yapılan üç iş:
      1. Üçüncü parçayı (asıl tür) al — dördüncü yalnız "(1P)" tekrarı.
      2. Sözlükten Rosso anahtarına çevir ("EDM" → "electronic").
      3. Tautoloji ve bilinmeyeni AT ("Music | Music" bir tür değil, kova).

    ⚠ Bu etiketler kullanıcıya GÖSTERİLMEZ (plan §Değişmez ilkeler:
    *"Spotify verisi kullanıcıya 'Spotify böyle dedi' diye gösterilmez"*).
    Eşleşme ve öneri motoruna yakıttır.

    Dönüş: sıralı, tekrarsız Rosso tür anahtarları.
    """
    out: set[str] = set()
    for label in music_labels or []:
        if not isinstance(label, str):
            continue
        parts = [p.strip() for p in label.split("|")]
        # "Interest | Music | <tür> | <tür>(1P)" → en az 3 parça şart
        if len(parts) < 3:
            continue
        raw = parts[2].lower()
        mapped = _INFERENCE_GENRE_MAP.get(raw)
        if mapped:
            out.add(mapped)
    return sorted(out)


def parse_wrapped(data: Any) -> dict[str, Any]:
    """Wrapped2025.json → user_export_signals signal_data.

    Yapıyı JSONB olarak olduğu gibi depolar; recap motoru bu veriyi okur.
    Bilinmeyen alanlar da korunur — gelecekteki kullanım için.
    """
    if not isinstance(data, dict):
        return {"raw": data}
    return data


def parse_sound_capsule(data: Any) -> dict[str, Any]:
    """YourSoundCapsule.json → user_export_signals signal_data.

    Haftalık kapsüller, Türkiye ortalaması karşılaştırması, FIRST_TO_DISCOVER.
    Tüm yapı korunur.
    """
    if not isinstance(data, dict):
        return {"raw": data}
    return data


def parse_playlist_track_events(data: Any) -> list[dict[str, Any]]:
    """Playlist1.json → playlist_track_events satırları.

    Format: {"playlists": [{"name": ..., "items": [{"track": {"trackUri": ...},
             "addedDate": "2026-04-06"}, ...]}, ...]}
    Her (track_uri, playlist, addedDate) bir küratörlük event'i. Playlist URI
    export'ta yok → playlist adı 'playlist_uri' olarak kullanılır (dedup için yeter).
    addedDate yoksa veya track_uri geçersizse satır atlanır.
    """
    rows: list[dict[str, Any]] = []
    if not isinstance(data, dict):
        return rows

    for pl in data.get("playlists", []) or []:
        playlist_name = pl.get("name")
        for item in pl.get("items", []) or []:
            tr = item.get("track")
            added = item.get("addedDate")
            if not tr or not added:
                continue
            uri = tr.get("trackUri")
            if not uri or not uri.startswith("spotify:track:"):
                continue
            rows.append({
                "track_uri": uri,
                "playlist_uri": playlist_name or "unknown",
                "added_at": added,  # 'YYYY-MM-DD' — Postgres date→timestamptz cast eder
            })
    return rows


def read_json_from_zip(zf: Any, name: str) -> Any:
    """ZIP içindeki JSON dosyasını oku ve parse et."""
    with zf.open(name) as fp:
        return json.load(fp)


# ─── dosya tipi → parser eşleştirme ─────────────────────────────────────────

def classify_account_entry(basename: str) -> str:
    """Account Data dosyasını işleme tipine göre sınıflandır."""
    if basename in HARD_DISCARD_FILES:
        return "hard_discard"
    if basename == "YourLibrary.json":
        return "your_library"
    if basename == "Inferences.json":
        return "inferences"
    if basename == "Wrapped2025.json":
        return "wrapped"
    if basename == "YourSoundCapsule.json":
        return "sound_capsule"
    if basename == "Playlist1.json":
        return "playlist_events"
    if basename.startswith("StreamingHistory_music"):
        return "streaming"  # mevcut pipeline'a devredilir
    return "skip"


def process_account_data(
    zf: Any,
    *,
    user_id: str,
    job_id: str,
    client: Any,
    import_at: str,
) -> dict[str, int]:
    """Account Data ZIP'ini işle, ilgili tabloları doldur.

    Dönüş: her işlem türünün sayısını içeren dict.
    """
    from datetime import timezone
    counts: dict[str, int] = {
        "liked_songs_inserted": 0,
        "signals_upserted": 0,
        "playlist_events_inserted": 0,
        "hard_discarded": 0,
        "skipped": 0,
        "errors": 0,
    }

    namelist = zf.namelist()

    for name in namelist:
        basename = name.rsplit("/", 1)[-1]
        entry_type = classify_account_entry(basename)

        if entry_type == "skip":
            counts["skipped"] += 1
            logger.debug("Atlanıyor: %s", name)
            continue

        if entry_type == "hard_discard":
            counts["hard_discarded"] += 1
            logger.info("Hard-discard: %s (içerik okunmadı)", basename)
            continue

        if entry_type == "streaming":
            counts["skipped"] += 1
            logger.debug("Streaming dosyası mevcut pipeline'a devrediliyor: %s", name)
            continue

        try:
            data = read_json_from_zip(zf, name)
        except Exception:
            counts["errors"] += 1
            logger.warning("JSON parse hatası, atlanıyor: %s", name)
            continue

        if entry_type == "your_library":
            rows = parse_your_library(data)
            _insert_liked_songs_snapshot(client, user_id, job_id, rows, import_at, counts)
            # A7: aynı dosyanın albums/artists bölümleri (ayrı tabloya).
            _upsert_saved_library(
                client, user_id, job_id, parse_saved_library(data), import_at, counts
            )

        elif entry_type == "inferences":
            signal_data = parse_inferences(data)
            _upsert_signal(client, user_id, job_id, "inferences", signal_data, import_at, counts)

        elif entry_type == "wrapped":
            signal_data = parse_wrapped(data)
            _upsert_signal(client, user_id, job_id, "wrapped_2025", signal_data, import_at, counts)

        elif entry_type == "sound_capsule":
            signal_data = parse_sound_capsule(data)
            _upsert_signal(client, user_id, job_id, "sound_capsule", signal_data, import_at, counts)

        elif entry_type == "playlist_events":
            rows = parse_playlist_track_events(data)
            _insert_playlist_track_events(client, user_id, job_id, rows, counts)

    return counts


def _insert_liked_songs_snapshot(
    client: Any,
    user_id: str,
    job_id: str,
    rows: list[dict[str, Any]],
    import_at: str,
    counts: dict[str, int],
) -> None:
    """YourLibrary snapshot'ından liked_songs_events yaz (occurred_at=import_at)."""
    if not rows:
        return

    db_rows = []
    for row in rows:
        db_rows.append({
            "user_id": user_id,
            "spotify_uri": row["spotify_uri"],
            "event_type": row["event_type"],
            "occurred_at": import_at,  # snapshot — gerçek zaman bilinmiyor
            "import_job_id": job_id,
        })

    from app.config import get_settings
    batch_size = get_settings().batch_size

    try:
        for batch in chunked(db_rows, batch_size):
            if not batch:
                continue
            client.table("liked_songs_events").upsert(
                batch,
                on_conflict="user_id,spotify_uri,occurred_at,event_type",
                ignore_duplicates=True,
            ).execute()
        counts["liked_songs_inserted"] += len(db_rows)
        logger.info("liked_songs_events snapshot: %d kayıt eklendi (job=%s)", len(db_rows), job_id)
    except Exception:
        counts["errors"] += len(db_rows)
        logger.exception("liked_songs_events insert hatası (job=%s)", job_id)


def _upsert_saved_library(
    client: Any,
    user_id: str,
    job_id: str,
    rows: list[dict[str, Any]],
    import_at: str,
    counts: dict[str, int],
) -> None:
    """A7 — kaydedilen albüm/sanatçıları `user_saved_library`'ye yaz (0179).

    SNAPSHOT semantiği: ZIP "şu an kütüphanemde bunlar var" der, ne zaman
    eklendiğini söylemez. Bu yüzden upsert — aynı öğe tekrar gelirse
    çoğaltılmaz (`unique(user_id, item_type, spotify_uri)`).

    ⚠ Kullanıcı kütüphaneden bir albümü ÇIKARIRSA bu tablo bunu bilmez
    (sonraki ZIP'te o satır gelmez ama eski kayıt durur). Silme senaryosu
    bugün kapsam dışı: ZIP zaten seyrek yükleniyor ve "eskiden kaydetmiştin"
    bilgisi de bir sinyal. Gerçek bir sorun olursa import başına temizlik
    (delete-then-insert) eklenir.
    """
    if not rows:
        return

    db_rows = [
        {
            "user_id": user_id,
            "item_type": r["item_type"],
            "spotify_uri": r["spotify_uri"],
            "name": r.get("name"),
            "import_job_id": job_id,
            "imported_at": import_at,
        }
        for r in rows
    ]

    from app.config import get_settings
    batch_size = get_settings().batch_size

    try:
        for batch in chunked(db_rows, batch_size):
            if not batch:
                continue
            client.table("user_saved_library").upsert(
                batch,
                on_conflict="user_id,item_type,spotify_uri",
                ignore_duplicates=True,
            ).execute()
        counts["saved_library_inserted"] = counts.get("saved_library_inserted", 0) + len(db_rows)
        logger.info(
            "user_saved_library: %d kayit (album+sanatci) yazildi (job=%s)",
            len(db_rows), job_id,
        )
    except Exception:
        counts["errors"] += len(db_rows)
        logger.exception("user_saved_library upsert hatasi (job=%s)", job_id)


def _insert_playlist_track_events(
    client: Any,
    user_id: str,
    job_id: str,
    rows: list[dict[str, Any]],
    counts: dict[str, int],
) -> None:
    """Playlist1.json addedDate event'lerini playlist_track_events'e yaz.

    Dedup: unique index (user_id, track_uri, playlist_uri, added_at) → çakışma yok say.
    """
    if not rows:
        return

    db_rows = [
        {
            "user_id": user_id,
            "track_uri": row["track_uri"],
            "playlist_uri": row["playlist_uri"],
            "added_at": row["added_at"],
            "platform": "spotify",
            "import_job_id": job_id,
        }
        for row in rows
    ]

    try:
        client.table("playlist_track_events").upsert(
            db_rows,
            on_conflict="user_id,track_uri,playlist_uri,added_at",
            ignore_duplicates=True,
        ).execute()
        counts["playlist_events_inserted"] += len(db_rows)
        logger.info("playlist_track_events: %d kayıt eklendi (job=%s)", len(db_rows), job_id)
    except Exception:
        counts["errors"] += len(db_rows)
        logger.exception("playlist_track_events insert hatası (job=%s)", job_id)


def _upsert_signal(
    client: Any,
    user_id: str,
    job_id: str,
    signal_source: str,
    signal_data: dict[str, Any],
    import_at: str,
    counts: dict[str, int],
) -> None:
    """user_export_signals tablosuna upsert (aynı source → override)."""
    try:
        client.table("user_export_signals").upsert(
            {
                "user_id": user_id,
                "signal_source": signal_source,
                "signal_data": signal_data,
                "imported_at": import_at,
                "export_job_id": job_id,
            },
            on_conflict="user_id,signal_source",
        ).execute()
        counts["signals_upserted"] += 1
        logger.info("Signal upserted: %s (job=%s)", signal_source, job_id)
    except Exception:
        counts["errors"] += 1
        logger.exception("Signal upsert hatası (%s, job=%s)", signal_source, job_id)
