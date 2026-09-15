"""Spotify Extended Streaming History parser — saf fonksiyonlar.

Kaynak kuralları: docs/Developer-docs/spotify-export-research.md
DB/Storage bilmez; tamamen test-edilebilir. Worker bunları çağırır.
"""
from __future__ import annotations

from typing import Any, Iterable

MIN_MS_PLAYED = 5000  # research §2: 5 saniyeden kısa → at

# Asla DB'ye yazılmayacak hassas alanlar (research özet: ip_addr)
_SENSITIVE_KEYS = {"ip_addr", "ip_addr_decrypted", "user_agent_decrypted"}

# content_type CHECK ile uyumlu (migration 0001)
ContentType = str  # "music" | "podcast" | "audiobook" | "unknown"


def classify_event(raw: dict[str, Any]) -> ContentType:
    """İçerik tipini sınıflandır (research §5 — öncelik kritik).

    audiobook_title → audiobook, sonra episode_name → podcast,
    sonra master_metadata_track_name → music, hiçbiri yoksa → unknown.
    Audiobook önce gelir (eski/yeni export format farkı).
    """
    if raw.get("audiobook_title"):
        return "audiobook"
    if raw.get("episode_name"):
        return "podcast"
    if raw.get("master_metadata_track_name"):
        return "music"
    return "unknown"


def is_valid_play(raw: dict[str, Any]) -> bool:
    """Geçerli dinleme mi (research §2).

    ms_played = 0 → at; < 5000 → at.
    >= 5000 + skipped=True → SAKLA (skip istatistiği için gerekli).
    """
    ms = raw.get("ms_played", 0) or 0
    return ms >= MIN_MS_PLAYED


def _dedup_key(raw: dict[str, Any]) -> tuple[Any, Any]:
    """(ts, uri) çifti — uri yoksa episode/audiobook uri'sine düşer (research §4)."""
    uri = (
        raw.get("spotify_track_uri")
        or raw.get("spotify_episode_uri")
        or raw.get("audiobook_chapter_uri")
        or raw.get("audiobook_uri")
    )
    return (raw.get("ts"), uri)


def deduplicate(events: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Aynı (ts + uri) çiftini tek bırak; farklı uri = farklı kayıt (research §4)."""
    seen: set[tuple[Any, Any]] = set()
    out: list[dict[str, Any]] = []
    for ev in events:
        key = _dedup_key(ev)
        if key in seen:
            continue
        seen.add(key)
        out.append(ev)
    return out


def strip_sensitive(raw: dict[str, Any]) -> dict[str, Any]:
    """Hassas alanları (ip_addr vb.) çıkar — asla DB'ye yazılmaz."""
    return {k: v for k, v in raw.items() if k not in _SENSITIVE_KEYS}


def sort_events_chronologically(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """ts'e göre sırala (research §1: dosya adına/numarasına güvenme)."""
    return sorted(events, key=lambda e: e.get("ts") or "")


def normalize_event(
    raw: dict[str, Any], *, source: str = "spotify_export"
) -> dict[str, Any]:
    """Ham export kaydını play_events satırına dönüştür (migration 0001 şeması).

    - ip_addr asla taşınmaz.
    - reason_start/reason_end/platform TEXT korunur, enum yok (research §3, §7).
    - platform alanı KAYNAK platformu ("spotify"); cihaz bilgisi conn_country/
      raw cihaz alanından ayrıdır.
    """
    content_type = classify_event(raw)
    return {
        "played_at": raw.get("ts"),
        "content_type": content_type,
        "raw_track_name": raw.get("master_metadata_track_name"),
        "raw_artist_name": raw.get("master_metadata_album_artist_name"),
        "spotify_track_uri": raw.get("spotify_track_uri"),
        "episode_name": raw.get("episode_name"),
        "episode_show_name": raw.get("episode_show_name"),
        "spotify_episode_uri": raw.get("spotify_episode_uri"),
        "audiobook_title": raw.get("audiobook_title"),
        "audiobook_uri": raw.get("audiobook_uri"),
        "platform": "spotify",
        "source": source,
        "ms_played": raw.get("ms_played", 0) or 0,
        "conn_country": raw.get("conn_country"),
        "reason_start": raw.get("reason_start"),
        "reason_end": raw.get("reason_end"),
        "shuffle": raw.get("shuffle"),
        "skipped": bool(raw.get("skipped", False)),
        "offline": bool(raw.get("offline", False)),
        "incognito_mode": bool(raw.get("incognito_mode", False)),
    }


# ─────────────────── ZIP girdisi sınıflandırma (export_runner için) ───────────
# Önceden app/jobs/process_export.py içindeydi; cron-tabanlı export_runner'a
# taşındı (eski process_export.py silindi).

def classify_zip_entry(name: str) -> str:
    """ZIP içindeki dosyayı tipine göre sınıflandır.

    Önce dosya adına göre hızlı karar verilir (bilinen Spotify formatları).
    .json uzantılı ama adı tanınmayan dosyalar "unknown_json" döner —
    içerik taraması _iter_streaming_events'te yapılır.
    """
    base = name.rsplit("/", 1)[-1]

    if base.startswith("StreamingHistory_") or base.startswith("Streaming_History_"):
        return "streaming"

    if base.startswith("Playlist") and base.endswith(".json"):
        return "playlist"

    if base == "YourLibrary.json":
        return "library"

    if base == "identity.json":
        return "identity"  # işlenmez, silinir (güvenlik)

    if base.endswith(".json"):
        return "unknown_json"

    return "ignore"


# Bir JSON dosyasının streaming history olduğunu doğrulayan alan seti.
# Herhangi biri yeterliyse streaming sayılır.
_STREAMING_SIGNAL_KEYS = frozenset({
    "ts", "ms_played", "master_metadata_track_name",
    "spotify_track_uri", "reason_start", "reason_end",
})


def _looks_like_streaming_json(first_item: dict[str, Any]) -> bool:
    """İlk JSON objesinin streaming history formatında olup olmadığını kontrol et."""
    return bool(_STREAMING_SIGNAL_KEYS & first_item.keys())
