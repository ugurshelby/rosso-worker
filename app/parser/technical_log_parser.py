"""Technical Log ZIP parser — saf fonksiyonlar.

İşlenen dosyalar (Spotify Technical Log ZIP):
  - AddedToCollection.json     → liked_songs_events ('liked')
  - RemovedFromCollection.json → liked_songs_events ('unliked')
  - AddedToPlaylist.json       → playlist_track_events
  - AddToPlaylist.json         → playlist_track_events (alternatif format)
  - PlaylistCreated.json       → playlists.source_created_at (signal olarak saklanır)
  - DaylistGenerated.json      → user_export_signals ('daylist_aggregate')
  - OnRepeatContents.json      → user_export_signals ('on_repeat')
  - ShuffleSequenceEvent*.json → user_export_signals ('shuffle_behavior')
  - CarDetectionEvent.json     → car_sessions
  - HomeSectionResponse.json   → user_export_signals ('home_section_categories')

Hard-discard:
  - DeviceIdentifier.json

Referans: docs/specs/spotify-data-spec.md §4.3
"""
from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger("rosso.worker.technical_log")

# ─── hard-discard ────────────────────────────────────────────────────────────
HARD_DISCARD_FILES = frozenset({"DeviceIdentifier.json"})

# ─── technical_log whitelist ─────────────────────────────────────────────────
TECHNICAL_LOG_WHITELIST = frozenset({
    "AddedToCollection.json",
    "RemovedFromCollection.json",
    "AddedToPlaylist.json",
    "AddToPlaylist.json",
    "PlaylistCreated.json",
    "DaylistGenerated.json",
    "OnRepeatContents.json",
    "CarDetectionEvent.json",
    "HomeSectionResponse.json",
})
# ShuffleSequenceEvent dosyaları prefix ile eşleştirilir (*.json)


def classify_technical_entry(basename: str) -> str:
    """Technical Log dosyasını işleme tipine göre sınıflandır."""
    if basename in HARD_DISCARD_FILES:
        return "hard_discard"
    if basename == "AddedToCollection.json":
        return "added_to_collection"
    if basename == "RemovedFromCollection.json":
        return "removed_from_collection"
    if basename in ("AddedToPlaylist.json", "AddToPlaylist.json"):
        return "added_to_playlist"
    if basename == "PlaylistCreated.json":
        return "playlist_created"
    if basename == "DaylistGenerated.json":
        return "daylist_generated"
    if basename == "OnRepeatContents.json":
        return "on_repeat"
    if basename.startswith("ShuffleSequenceEvent") and basename.endswith(".json"):
        return "shuffle_sequence"
    if basename == "CarDetectionEvent.json":
        return "car_detection"
    if basename == "HomeSectionResponse.json":
        return "home_section"
    return "skip"


def parse_collection_events(
    data: Any, *, event_type: str
) -> list[dict[str, Any]]:
    """AddedToCollection / RemovedFromCollection → liked_songs_events satırları.

    Gerçek format:
    [{"message_item_uri": "spotify:track:xxx", "message_set": "collection",
      "timestamp_utc": "2026-05-21T06:47:47.718Z"}, ...]
    Yalnızca spotify:track: URI'leri işlenir (album/playlist atlanır).
    Eski alan isimleri (uri/timestamp) fallback olarak korunur.
    """
    rows: list[dict[str, Any]] = []
    if not isinstance(data, list):
        return rows

    for item in data:
        if not isinstance(item, dict):
            continue
        uri = item.get("message_item_uri") or item.get("uri") or item.get("spotify_uri")
        ts = (
            item.get("timestamp_utc")
            or item.get("timestamp")
            or item.get("ts")
            or item.get("addedAt")
        )

        # Yalnızca müzik track'leri işle
        if not uri or not uri.startswith("spotify:track:"):
            continue
        if not ts:
            logger.debug("Timestamp eksik, atlanıyor: %s", uri)
            continue

        rows.append({
            "spotify_uri": uri,
            "event_type": event_type,
            "occurred_at": ts,
        })
    return rows


def parse_playlist_track_events(data: Any) -> list[dict[str, Any]]:
    """AddedToPlaylist / AddToPlaylist → playlist_track_events satırları.

    Gerçek format (iki varyant):
      AddedToPlaylist: tekil {"message_item_uri", "message_playlist_uri", "timestamp_utc"}
      AddToPlaylist:   çoğul {"message_item_uris": [...], "message_playlist_uri", "timestamp_utc"}
    Yalnızca spotify:track: URI'leri işlenir. Eski alanlar fallback'te.
    """
    rows: list[dict[str, Any]] = []
    if not isinstance(data, list):
        return rows

    for item in data:
        if not isinstance(item, dict):
            continue
        playlist_uri = (
            item.get("message_playlist_uri")
            or item.get("playlistUri")
            or item.get("playlist_uri")
        )
        added_at = (
            item.get("timestamp_utc")
            or item.get("addedAt")
            or item.get("added_at")
            or item.get("timestamp")
        )
        platform = (
            item.get("message_client_platform")
            or item.get("context_os_name")
            or item.get("platform")
            or item.get("source")
        )

        if not playlist_uri or not added_at:
            continue

        # Tekil (message_item_uri) veya çoğul (message_item_uris[]) track URI'leri
        uris = item.get("message_item_uris")
        if not uris:
            single = (
                item.get("message_item_uri")
                or item.get("trackUri")
                or item.get("track_uri")
                or item.get("uri")
            )
            uris = [single] if single else []

        for track_uri in uris:
            if not track_uri or not track_uri.startswith("spotify:track:"):
                continue
            rows.append({
                "track_uri": track_uri,
                "playlist_uri": playlist_uri,
                "added_at": added_at,
                "platform": str(platform) if platform is not None else None,
            })
    return rows


def parse_car_detection_events(data: Any) -> list[dict[str, Any]]:
    """CarDetectionEvent → car_sessions satırları.

    Gerçek format:
    [{"message_is_car_connected": true/false, "timestamp_utc": "...Z"}, ...]
    Connect (true) + bir sonraki disconnect (false) çifti bir seans oluşturur.
    Eşleşmeyen connect → disconnected_at=None, duration_seconds=None olarak yazılır.
    Eski alanlar (timestamp/connected) fallback'te.
    """
    if not isinstance(data, list):
        return []

    def _ts(e: dict[str, Any]) -> str:
        return e.get("timestamp_utc") or e.get("timestamp") or ""

    def _connected(e: dict[str, Any]) -> Any:
        v = e.get("message_is_car_connected")
        if v is None:
            v = e.get("connected")
        return v

    # Önce zaman sırası
    events = sorted(
        [e for e in data if isinstance(e, dict) and _ts(e)],
        key=_ts,
    )

    sessions: list[dict[str, Any]] = []
    pending_connect: str | None = None

    for event in events:
        connected = _connected(event)
        ts = _ts(event)

        if connected is True:
            # Yeni bağlantı başladı (önceki bağlantı kapanmadan yenisi geldiyse sıfırla)
            pending_connect = ts
        elif connected is False and pending_connect is not None:
            # Bağlantı kapandı — seans tamamlandı
            try:
                from datetime import datetime, timezone
                start = datetime.fromisoformat(pending_connect.replace("Z", "+00:00"))
                end = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                duration = max(0, int((end - start).total_seconds()))
            except Exception:
                duration = None

            sessions.append({
                "connected_at": pending_connect,
                "disconnected_at": ts,
                "duration_seconds": duration,
            })
            pending_connect = None

    # Eşleşmeyen connect (kapanış event'i yok)
    if pending_connect is not None:
        sessions.append({
            "connected_at": pending_connect,
            "disconnected_at": None,
            "duration_seconds": None,
        })

    return sessions


def parse_daylist(data: Any) -> dict[str, Any]:
    """DaylistGenerated → aggregate signal (tür + zaman × ruh hali etiketleri).

    Ham veriyi aggregate ederek saklar: bireysel daylist log yerine
    tür ve mood etiketlerinin frekans dağılımı.
    """
    if not isinstance(data, list):
        return {"raw_count": 0}

    mood_counts: dict[str, int] = {}
    genre_counts: dict[str, int] = {}

    for item in data:
        if not isinstance(item, dict):
            continue
        # Daylist başlığından mood/genre keyword'leri çıkar (gerçek:
        # message_playlist_title, ör. "angst rock-ish thursday morning")
        title = (
            item.get("message_playlist_title")
            or item.get("daylistTitle")
            or item.get("title")
            or ""
        )
        for word in title.lower().split():
            clean = word.strip(".,;:!?")
            if len(clean) > 2:
                genre_counts[clean] = genre_counts.get(clean, 0) + 1

        # Zaman dilimi etiketi (gerçek: message_daypart "morning"/"afternoon"/...)
        time_of_day = (
            item.get("message_daypart")
            or item.get("timeOfDay")
            or item.get("time_of_day")
        )
        if time_of_day:
            mood_counts[time_of_day] = mood_counts.get(time_of_day, 0) + 1

    return {
        "raw_count": len(data),
        "time_of_day_distribution": mood_counts,
        "title_word_frequency": dict(
            sorted(genre_counts.items(), key=lambda x: x[1], reverse=True)[:50]
        ),
    }


def parse_home_section(data: Any) -> dict[str, Any]:
    """HomeSectionResponse → section başlıklarının listesi (genre algoritması sinyali)."""
    if not isinstance(data, list):
        return {"titles": [], "count": 0}

    titles: list[str] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        title = (
            item.get("message_title")
            or item.get("sectionTitle")
            or item.get("title")
            or item.get("message_content_uri")
            or item.get("sectionId")
        )
        if title and isinstance(title, str):
            titles.append(title)

    return {
        "titles": list(dict.fromkeys(titles)),  # dedup, sıra korunur
        "count": len(titles),
    }


def read_json_from_zip(zf: Any, name: str) -> Any:
    """ZIP içindeki JSON dosyasını oku ve parse et."""
    with zf.open(name) as fp:
        return json.load(fp)


def process_technical_log(
    zf: Any,
    *,
    user_id: str,
    job_id: str,
    client: Any,
    import_at: str,
) -> dict[str, int]:
    """Technical Log ZIP'ini işle, ilgili tabloları doldur.

    Dönüş: her işlem türünün sayısını içeren dict.
    """
    counts: dict[str, int] = {
        "liked_songs_inserted": 0,
        "playlist_track_events_inserted": 0,
        "car_sessions_inserted": 0,
        "signals_upserted": 0,
        "hard_discarded": 0,
        "skipped": 0,
        "errors": 0,
    }

    # ShuffleSequence'ın birden fazla dosyası olabilir → aggregate topla
    shuffle_aggregate: list[Any] = []

    namelist = zf.namelist()

    for name in namelist:
        basename = name.rsplit("/", 1)[-1]
        entry_type = classify_technical_entry(basename)

        if entry_type == "skip":
            counts["skipped"] += 1
            continue

        if entry_type == "hard_discard":
            counts["hard_discarded"] += 1
            logger.info("Hard-discard: %s (içerik okunmadı)", basename)
            continue

        try:
            data = read_json_from_zip(zf, name)
        except Exception:
            counts["errors"] += 1
            logger.warning("JSON parse hatası, atlanıyor: %s", name)
            continue

        if entry_type == "added_to_collection":
            rows = parse_collection_events(data, event_type="liked")
            _insert_liked_events(client, user_id, job_id, rows, counts)

        elif entry_type == "removed_from_collection":
            rows = parse_collection_events(data, event_type="unliked")
            _insert_liked_events(client, user_id, job_id, rows, counts)

        elif entry_type == "added_to_playlist":
            rows = parse_playlist_track_events(data)
            _insert_playlist_track_events(client, user_id, job_id, rows, counts)

        elif entry_type == "playlist_created":
            # PlaylistCreated → signal olarak sakla (playlists.created_at güncellemesi
            # için UI/API katmanı bu sinyali okur)
            if isinstance(data, list):
                _upsert_signal(client, user_id, job_id, "playlist_created", {"events": data}, import_at, counts)
            else:
                _upsert_signal(client, user_id, job_id, "playlist_created", data or {}, import_at, counts)

        elif entry_type == "daylist_generated":
            signal_data = parse_daylist(data)
            _upsert_signal(client, user_id, job_id, "daylist_aggregate", signal_data, import_at, counts)

        elif entry_type == "on_repeat":
            _upsert_signal(client, user_id, job_id, "on_repeat", data if isinstance(data, dict) else {"items": data}, import_at, counts)

        elif entry_type == "shuffle_sequence":
            # Tüm ShuffleSequence dosyalarını birleştir, tek signal olarak yaz
            if isinstance(data, list):
                shuffle_aggregate.extend(data)
            elif isinstance(data, dict):
                shuffle_aggregate.append(data)

        elif entry_type == "car_detection":
            sessions = parse_car_detection_events(data)
            _insert_car_sessions(client, user_id, job_id, sessions, counts)

        elif entry_type == "home_section":
            signal_data = parse_home_section(data)
            _upsert_signal(client, user_id, job_id, "home_section_categories", signal_data, import_at, counts)

    # ShuffleSequence aggregate → tek signal
    if shuffle_aggregate:
        _upsert_signal(
            client, user_id, job_id,
            "shuffle_behavior",
            {"events": shuffle_aggregate, "count": len(shuffle_aggregate)},
            import_at, counts,
        )

    return counts


# ─── DB yardımcı fonksiyonlar ────────────────────────────────────────────────

def _insert_liked_events(
    client: Any,
    user_id: str,
    job_id: str,
    rows: list[dict[str, Any]],
    counts: dict[str, int],
) -> None:
    if not rows:
        return

    db_rows = [
        {
            "user_id": user_id,
            "spotify_uri": r["spotify_uri"],
            "event_type": r["event_type"],
            "occurred_at": r["occurred_at"],
            "import_job_id": job_id,
        }
        for r in rows
    ]

    try:
        client.table("liked_songs_events").upsert(
            db_rows,
            on_conflict="user_id,spotify_uri,occurred_at,event_type",
            ignore_duplicates=True,
        ).execute()
        counts["liked_songs_inserted"] += len(db_rows)
        logger.info("liked_songs_events: %d kayıt eklendi (job=%s)", len(db_rows), job_id)
    except Exception:
        counts["errors"] += len(db_rows)
        logger.exception("liked_songs_events insert hatası (job=%s)", job_id)


def _insert_playlist_track_events(
    client: Any,
    user_id: str,
    job_id: str,
    rows: list[dict[str, Any]],
    counts: dict[str, int],
) -> None:
    if not rows:
        return

    db_rows = [
        {
            "user_id": user_id,
            "track_uri": r["track_uri"],
            "playlist_uri": r["playlist_uri"],
            "added_at": r["added_at"],
            "platform": r.get("platform"),
            "import_job_id": job_id,
        }
        for r in rows
    ]

    try:
        client.table("playlist_track_events").upsert(
            db_rows,
            on_conflict="user_id,track_uri,playlist_uri,added_at",
            ignore_duplicates=True,
        ).execute()
        counts["playlist_track_events_inserted"] += len(db_rows)
        logger.info("playlist_track_events: %d kayıt eklendi (job=%s)", len(db_rows), job_id)
    except Exception:
        counts["errors"] += len(db_rows)
        logger.exception("playlist_track_events insert hatası (job=%s)", job_id)


def _insert_car_sessions(
    client: Any,
    user_id: str,
    job_id: str,
    sessions: list[dict[str, Any]],
    counts: dict[str, int],
) -> None:
    if not sessions:
        return

    db_rows = [
        {
            "user_id": user_id,
            "connected_at": s["connected_at"],
            "disconnected_at": s.get("disconnected_at"),
            "duration_seconds": s.get("duration_seconds"),
            "import_job_id": job_id,
        }
        for s in sessions
    ]

    try:
        client.table("car_sessions").upsert(
            db_rows,
            on_conflict="user_id,connected_at",
            ignore_duplicates=True,
        ).execute()
        counts["car_sessions_inserted"] += len(db_rows)
        logger.info("car_sessions: %d seans eklendi (job=%s)", len(db_rows), job_id)
    except Exception:
        counts["errors"] += len(db_rows)
        logger.exception("car_sessions insert hatası (job=%s)", job_id)


def _upsert_signal(
    client: Any,
    user_id: str,
    job_id: str,
    signal_source: str,
    signal_data: dict[str, Any],
    import_at: str,
    counts: dict[str, int],
) -> None:
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
