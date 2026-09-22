"""Export akışı: ZIP → tracks (artist ID'li) → yalın play_events + podcast_events.

Yeni akış (spec §5), 2 adım:
  1. Event'leri normalize et (bellekte, ijson akış).
  2. Benzersiz spotify_id → /tracks lookup → insert_tracks_batch RPC → {spotify_id: track_id}.
     play_events'e SADECE track_id yaz (raw_* YOK). Podcast'ler ayrı tabloya.
Kota dolarsa artist ID'siz devam edilir; genre cron sonradan toplar.
"""
from __future__ import annotations

import io
import logging
import sys
import time
import zipfile
from bisect import bisect_left
from datetime import datetime, timezone
from typing import Any, Iterator

from app.parser.spotify_export import (
    classify_zip_entry,
    deduplicate,
    is_valid_play,
    normalize_event,
    sort_events_chronologically,
    strip_sensitive,
    _looks_like_streaming_json,
)
from app.services.chunking import chunked as _chunked

logger = logging.getLogger("rosso.worker.export_runner")


# ─────────────────────────── saf helper'lar ───────────────────────────

# recently_played_runner._NEARBY_WINDOW_SECONDS ile AYNI pencere: iki yön
# aynı "aynı çalma" tanımını kullanmalı.
NEARBY_WINDOW_SECONDS = 5


def _epoch(ts: Any) -> float | None:
    """ISO zaman damgası → epoch saniye (Z ve saniye-altı destekli)."""
    if not isinstance(ts, str) or not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def drop_near_realtime(
    rows: list[dict[str, Any]], realtime: dict[str, list[float]],
) -> tuple[list[dict[str, Any]], int]:
    """Canlı dinlemede (api_realtime) ±5 sn içinde zaten olan çalmaları at.

    ÖLÇÜLDÜ 2026-09-21: ZIP'in `ts`'i tam saniye (`10:18:03`), canlı
    dinlemenin `played_at`'i milisaniyeli (`10:18:03.816`). Aynı çalma iki
    farklı satır olur; `play_events_dedup_idx` (user, played_at, track) tam
    eşleşme aradığı için yakalamaz. Canlı runner bu korumayı ZIP'e karşı
    zaten yapıyordu (0104 bulgusu); ters yön — canlı satırlar ÖNCE gelip ZIP
    SONRA yüklendiğinde — korumasızdı ve dinlemeleri ikiye katlayacaktı.

    Canlı satır SİLİNMEZ, ZIP satırı ATLANIR: silmek geri alınamaz bir iş,
    atlamak değil. Bedeli: o çalmalarda ms_played canlı runner'ın tam-süre
    tahmini olarak kalır.

    `realtime`: track_id → SIRALI epoch listesi. Dönüş: (kalanlar, atılan sayısı).
    """
    kept: list[dict[str, Any]] = []
    dropped = 0
    for row in rows:
        times = realtime.get(row.get("track_id") or "")
        t = _epoch(row.get("played_at")) if times else None
        if times and t is not None:
            i = bisect_left(times, t - NEARBY_WINDOW_SECONDS)
            if i < len(times) and times[i] <= t + NEARBY_WINDOW_SECONDS:
                dropped += 1
                continue
        kept.append(row)
    return kept, dropped


def _fetch_realtime_plays(
    client: Any, user_id: str, period_start: str | None, period_end: str | None,
) -> dict[str, list[float]]:
    """ZIP döneminin içindeki api_realtime çalmaları: track_id → sıralı epoch.

    Yalnız api_realtime çekilir: ZIP↔ZIP örtüşmesi aynı hassasiyette olduğu
    için tam eşleşme index'i onu zaten yakalar. Sorgu düşerse boş döner ve
    yükleme devam eder (tek bir ZIP'i kaybetmek, birkaç çift satırdan kötü);
    uyarı loglanır.
    """
    out: dict[str, list[float]] = {}
    lo, hi = _epoch(period_start), _epoch(period_end)
    if lo is None or hi is None:
        return out
    lo_iso = datetime.fromtimestamp(lo - NEARBY_WINDOW_SECONDS, tz=timezone.utc).isoformat()
    hi_iso = datetime.fromtimestamp(hi + NEARBY_WINDOW_SECONDS, tz=timezone.utc).isoformat()
    page = 1000
    start = 0
    try:
        while True:
            res = (
                client.table("play_events")
                .select("track_id,played_at")
                .eq("user_id", user_id)
                .eq("source", "api_realtime")
                .gte("played_at", lo_iso)
                .lte("played_at", hi_iso)
                .order("played_at")
                .range(start, start + page - 1)
                .execute()
            )
            data = res.data or []
            for r in data:
                t = _epoch(r.get("played_at"))
                if t is not None and r.get("track_id"):
                    out.setdefault(r["track_id"], []).append(t)
            if len(data) < page:
                break
            start += page
    except Exception:  # noqa: BLE001
        logger.warning("api_realtime örtüşme sorgusu düştü: user=%s", user_id, exc_info=True)
        return {}
    for times in out.values():
        times.sort()
    return out


def to_play_event_row(normalized: dict[str, Any], *, user_id: str, track_id: str) -> dict[str, Any]:
    """Normalize edilmiş event'i YALIN play_events satırına çevir (raw_* YOK)."""
    return {
        "played_at": normalized["played_at"],
        "user_id": user_id,
        "track_id": track_id,
        "platform": normalized.get("platform", "spotify"),
        "source": normalized.get("source", "spotify_export"),
        "ms_played": normalized.get("ms_played", 0) or 0,
        "conn_country": normalized.get("conn_country"),
        "reason_start": normalized.get("reason_start"),
        "reason_end": normalized.get("reason_end"),
        "shuffle": normalized.get("shuffle"),
        "skipped": bool(normalized.get("skipped", False)),
        "offline": bool(normalized.get("offline", False)),
        "incognito_mode": bool(normalized.get("incognito_mode", False)),
    }


def to_podcast_event_row(normalized: dict[str, Any], *, user_id: str) -> dict[str, Any]:
    """Podcast event'i podcast_events satırına çevir."""
    return {
        "played_at": normalized["played_at"],
        "user_id": user_id,
        "ms_played": normalized.get("ms_played", 0) or 0,
        "episode_name": normalized.get("episode_name"),
        "episode_show_name": normalized.get("episode_show_name"),
        "spotify_episode_uri": normalized.get("spotify_episode_uri"),
        "skipped": bool(normalized.get("skipped", False)),
        "offline": bool(normalized.get("offline", False)),
    }


def split_music_podcast(
    rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """content_type'a göre (music, podcast) ayır. unknown/audiobook atılır."""
    music = [r for r in rows if r.get("content_type") == "music"]
    podcast = [r for r in rows if r.get("content_type") == "podcast"]
    return music, podcast


def _spotify_id_from_uri(uri: str | None) -> str | None:
    """spotify:track:XXXX → XXXX."""
    if not uri or not uri.startswith("spotify:track:"):
        return None
    return uri.rsplit(":", 1)[-1]


def _iter_streaming_events(zf: zipfile.ZipFile) -> Iterator[dict[str, Any]]:
    """Streaming JSON event'lerini akış halinde ver (ijson, bellek-dostu)."""
    import ijson  # geç import — test ortamı ijson gerektirmesin

    for name in zf.namelist():
        entry_type = classify_zip_entry(name)
        if entry_type == "streaming":
            with zf.open(name) as fp:
                try:
                    for event in ijson.items(fp, "item"):
                        yield event
                except Exception:  # noqa: BLE001
                    logger.warning("Streaming dosyası okunamadı: %s", name)
        elif entry_type == "unknown_json":
            with zf.open(name) as fp:
                try:
                    parser = ijson.items(fp, "item")
                    first = next(parser, None)
                    if first is None or not _looks_like_streaming_json(first):
                        continue
                    logger.info("İçerik taramasıyla streaming tespit edildi: %s", name)
                    yield first
                    for event in parser:
                        yield event
                except Exception:  # noqa: BLE001
                    logger.warning("Unknown JSON okunamadı: %s", name)


def _cleanup(client: Any, bucket: str, path: str) -> None:
    """İşlenen ZIP'i Storage API ile sil (ham kişisel veri tutulmaz)."""
    try:
        client.storage.from_(bucket).remove([path])
    except Exception:  # noqa: BLE001
        logger.warning("Storage temizliği başarısız: %s", path)


# ─────────────────────────── orkestrasyon ───────────────────────────

def _side_written(side_counts: dict[str, int]) -> int:
    """Yan-veri sayaçlarından DB'ye gerçekten yazılan kayıt toplamı.

    Parser'lar her yazımı `*_inserted` / `*_upserted` anahtarlarıyla raporlar;
    skipped/hard_discarded/errors yazım değildir, toplama girmez.
    """
    return sum(
        v for k, v in side_counts.items()
        if k.endswith("_inserted") or k.endswith("_upserted")
    )


def run_one_export(client: Any, settings: Any, job: dict[str, Any]) -> dict[str, Any]:
    """Tek bir export job'unu baştan sona işle. Sonuç dict döndürür.

    job: {"id", "user_id", "file_path"}
    Dönüş: {"outcome", "tracks", "events", "podcasts", "elapsed_ms",
            "zip_type", "matched_events", "skipped_events", "counts"}

    Karne sözleşmesi (export_jobs kolonlarıyla eşleşir, 2026-07-19):
      matched_events  → DB'ye gerçekten yazılan kayıt sayısı (play_events +
                        yan-veri tabloları). "ZIP'ten ne çıktı" sorusunun cevabı.
      skipped_events  → okunup YAZILMAYAN event sayısı (geçersiz/duplicate/
                        track eşleşmeyen) + yan-veri parse hataları.
    ZIP Storage'dan silindikten sonra iz bırakan tek kayıt bu ikilidir.
    """
    from datetime import datetime, timezone

    from app.services.zip_detect import ZipBombError, detect_zip_type, guard_zip_bomb

    t0 = time.time()
    job_id = job["id"]
    user_id = job["user_id"]
    path = job["file_path"]

    blob = client.storage.from_(settings.export_bucket).download(path)
    zf = zipfile.ZipFile(io.BytesIO(blob))

    # Zip-bomb guard: parse başlamadan önce açılmış-boyut/oran sınırlarını doğrula.
    # Aşılırsa job'u reddet + dosyayı temizle (cron'u OOM ile öldürmesini önle).
    try:
        guard_zip_bomb(zf)
    except ZipBombError as exc:
        logger.warning("ZIP-bomb reddedildi (job=%s): %s", job_id, exc)
        _cleanup(client, settings.export_bucket, path)
        return {"outcome": "error", "error": "zip_bomb",
                "elapsed_ms": int((time.time() - t0) * 1000)}

    zip_type = detect_zip_type(zf)
    logger.info("ZIP tipi: %s (job=%s)", zip_type, job_id)

    import_at = datetime.now(timezone.utc).isoformat()

    # ── Yan-veri (account/techlog) işleme ──────────────────────────────────────
    # account_data / technical_log → yalnız yan-veri (streaming yok).
    # mixed → hem yan-veri HEM streaming akışı (aşağıda devam eder).
    side_counts: dict[str, int] = {}
    if zip_type in ("account_data", "technical_log", "mixed"):
        if zip_type in ("account_data", "mixed"):
            from app.parser.account_data_parser import process_account_data
            for k, v in process_account_data(
                zf, user_id=user_id, job_id=job_id, client=client, import_at=import_at
            ).items():
                side_counts[k] = side_counts.get(k, 0) + v
        if zip_type in ("technical_log", "mixed"):
            from app.parser.technical_log_parser import process_technical_log
            for k, v in process_technical_log(
                zf, user_id=user_id, job_id=job_id, client=client, import_at=import_at
            ).items():
                side_counts[k] = side_counts.get(k, 0) + v

        if zip_type in ("account_data", "technical_log"):
            # Saf yan-veri ZIP'i: streaming akışı yok, burada bitir.
            _cleanup(client, settings.export_bucket, path)
            elapsed = int((time.time() - t0) * 1000)
            return {"outcome": "success", "tracks": 0, "events": 0, "podcasts": 0,
                    "elapsed_ms": elapsed, "counts": side_counts,
                    "zip_type": zip_type,
                    "matched_events": _side_written(side_counts),
                    "skipped_events": side_counts.get("errors", 0)}

    if zip_type == "unknown":
        return {"outcome": "error", "error": "unknown_zip", "zip_type": zip_type,
                "elapsed_ms": int((time.time() - t0) * 1000)}

    # ── streaming_history (yalın akış) ──────────────────────────────────────────
    raw_events = list(_iter_streaming_events(zf))
    cleaned = [strip_sensitive(e) for e in raw_events]
    valid = [e for e in cleaned if is_valid_play(e)]
    deduped = deduplicate(valid)
    ordered = sort_events_chronologically(deduped)
    normalized = [normalize_event(e, source="spotify_export") for e in ordered]

    music, podcast = split_music_podcast(normalized)

    # Dönem (period) metadata'sı — UI rozeti için min/max played_at
    _played = [n["played_at"] for n in normalized if n.get("played_at")]
    period_start = min(_played) if _played else None
    period_end = max(_played) if _played else None

    # ── 1. Benzersiz track'leri topla ───────────────────────────────────────────
    # Spotify artist-ID lookup'ı BİLEREK burada YAPILMAZ: büyük ZIP'te binlerce
    # tekil çağrı (1.5sn/çağrı) cron penceresini aşıp job'u öldürüyordu. Artist ID
    # toplama enrichment cron'una taşındı (spec 2026-06-30 §4.B; track'ler
    # spotify_artist_ids=NULL oluşur, enrichment Aşama A doldurur).
    uniq_ids: dict[str, dict[str, Any]] = {}
    for ev in music:
        sid = _spotify_id_from_uri(ev.get("spotify_track_uri"))
        if sid and sid not in uniq_ids:
            uniq_ids[sid] = ev

    # ── 2. insert_tracks_batch RPC → {spotify_id: track_id} ─────────────────────
    track_payload = []
    for sid, ev in uniq_ids.items():
        track_payload.append({
            "spotify_id": sid,
            "title": ev.get("raw_track_name") or "Unknown",
            "artists": [ev.get("raw_artist_name")] if ev.get("raw_artist_name") else ["Unknown"],
            "spotify_artist_ids": None,  # enrichment Aşama A dolduracak
        })
    sid_to_track_id: dict[str, str] = {}
    if track_payload:
        for batch in _chunked(track_payload, 500):
            res = client.rpc("insert_tracks_batch", {"p_tracks": batch}).execute()
            for row in (res.data or []):
                sid_to_track_id[row["spotify_id"]] = row["track_id"]

    # ── 3. play_events (yalın, track_id'li) batch upsert ────────────────────────
    play_rows = []
    for ev in music:
        sid = _spotify_id_from_uri(ev.get("spotify_track_uri"))
        tid = sid_to_track_id.get(sid) if sid else None
        if tid:
            play_rows.append(to_play_event_row(ev, user_id=user_id, track_id=tid))

    # Canlı dinleme (api_realtime) ile örtüşen çalmalar: aynı çalma iki kaynakta
    # farklı saniye-altı hassasiyetle durur, tam eşleşme arayan dedup index'i
    # yakalamaz. Bkz. `drop_near_realtime`.
    realtime = _fetch_realtime_plays(client, user_id, period_start, period_end)
    play_rows, realtime_overlap = drop_near_realtime(play_rows, realtime)

    inserted_plays = 0
    for batch in _chunked(play_rows, settings.batch_size):
        if batch:
            res = client.table("play_events").upsert(
                batch, on_conflict="user_id,played_at,track_id", ignore_duplicates=True,
            ).execute()
            # ignore_duplicates + return=representation → yalnız GERÇEKTEN
            # eklenen satırlar döner. Önceki ZIP'te olan çalmalar burada sayılmaz.
            inserted_plays += len(getattr(res, "data", None) or [])

    # ── 4. podcast_events ───────────────────────────────────────────────────────
    podcast_rows = [to_podcast_event_row(ev, user_id=user_id) for ev in podcast]
    for batch in _chunked(podcast_rows, settings.batch_size):
        if batch:
            client.table("podcast_events").upsert(batch, ignore_duplicates=True).execute()

    _cleanup(client, settings.export_bucket, path)
    elapsed = int((time.time() - t0) * 1000)

    # ── Karne (streaming + varsa mixed yan-veri) ────────────────────────────────
    # total_read: ZIP'ten okunan ham event sayısı. matched: DB'ye yazılanlar
    # (play + podcast + yan-veri). skipped: okunup yazılmayanlar (geçersiz/
    # duplicate/track'e eşleşmeyen) + yan-veri hataları.
    total_read = len(raw_events)
    #
    # ÖLÇÜLDÜ 2026-09-21: eskiden `written_streaming = len(play_rows)` idi —
    # yazmaya DENENEN satır sayısı. İkinci ZIP'te önceki ZIP'in tüm çalmaları
    # zaten DB'dedir ve ignore_duplicates onları sessizce atlar; karne yine
    # hepsini "yazıldı" gösteriyordu. Artık gerçekten eklenen sayılıyor.
    written_streaming = inserted_plays + len(podcast_rows)
    matched_events = written_streaming + _side_written(side_counts)
    skipped_events = max(0, total_read - written_streaming) + side_counts.get("errors", 0)

    return {
        "outcome": "success",
        "tracks": len(sid_to_track_id),
        "events": len(play_rows),
        "inserted_events": inserted_plays,
        "realtime_overlap": realtime_overlap,
        "podcasts": len(podcast_rows),
        "elapsed_ms": elapsed,
        "period_start": period_start,
        "period_end": period_end,
        "counts": side_counts,
        "zip_type": zip_type,
        "matched_events": matched_events,
        "skipped_events": skipped_events,
    }


# ─────────────────────────── on-demand giriş noktası ───────────────────────
#
# cron-system.md ADIM 3/4: GitHub Actions'ın `python -m app.pipeline.export_runner`
# ile çağırdığı gerçek entrypoint burada yaşar. Önceden bu dosyada `__main__`
# YOKTU — workflow bu modülü çalıştırınca hiçbir şey yapmadan sessizce exit 0
# veriyordu (bir fonksiyon/sınıf tanımlamak dışında top-level kod yok). Gerçek
# "kuyruk bitene kadar çalış" mantığı `app.cron.export.run_export_burst()`'te
# zaten vardı ama hiçbir __main__'e bağlı değildi. İçe aktarma burada
# FONKSİYON İÇİNDE (lazy) yapılır — `app.cron.export` da bu modülden
# `run_one_export`'u lazy import ediyor; top-level import döngüsel olurdu.
def main() -> int:
    from app.cron.export import run_export_burst

    result = run_export_burst()
    logger.info("[export_runner] on-demand tur bitti: %s", result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
