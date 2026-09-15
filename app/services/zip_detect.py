"""İçerik tabanlı ZIP tipi tespiti (güven skoru) — spec §2.

İlke: dosya adı bir *sinyal*, içerik *kanıt*. Sadece isim asla yeterli değildir;
bir tipin doğrulanması için en az bir içerik kanıtı (skor ≥3) gerekir.

Bu sayede ZIP/dosya adı değiştirilmiş olsa bile (StreamingHistory_music_0.json →
music_data_0.json) içeriğe bakarak doğru tip tespit edilir.

İçerik okuma maliyeti minimum: her aday .json'ın yalnızca İLK objesi ijson ile
akış halinde okunur (tüm dosya değil).

Eski detect_zip_type (yalnızca isim) bu modüle taşındı + kanıt katmanı eklendi.
process_export.py bunu çağırır.
"""
from __future__ import annotations

import logging
import zipfile
from typing import Any

logger = logging.getLogger("rosso.worker.zip_detect")

# ── Zip-bomb koruması ─────────────────────────────────────────────────────────
# Yükleme sınırı 500MB SIKIŞTIRILMIŞ boyut (validate-upload.ts). Worker json.load
# ile girdileri belleğe açtığı için, yüksek oranda sıkışan bir ZIP (birkaç MB →
# birkaç GB) export cron'unu OOM ile düşürebilir → paylaşılan cron durur (DoS).
# İki eşik: (1) toplam açılmış boyut, (2) sıkıştırma oranı. İkisi de aşılırsa reddet.
_MAX_TOTAL_UNCOMPRESSED = 2 * 1024 * 1024 * 1024  # 2 GB — gerçek Spotify export'u çok altında
_MAX_COMPRESSION_RATIO = 100  # açılmış/sıkışmış > 100x → şüpheli (metin JSON ~10-20x sıkışır)
_MIN_COMPRESSED_FOR_RATIO = 1024 * 1024  # <1MB dosyalarda oran kontrolü atlanır (küçük dosya güvenli)


class ZipBombError(Exception):
    """ZIP açılmış boyutu/oranı güvenli sınırları aşıyor (olası zip-bomb)."""


def guard_zip_bomb(
    zf: zipfile.ZipFile,
    *,
    max_total: int = _MAX_TOTAL_UNCOMPRESSED,
    max_ratio: int = _MAX_COMPRESSION_RATIO,
    min_compressed_for_ratio: int = _MIN_COMPRESSED_FOR_RATIO,
) -> None:
    """ZIP'i işlemeden önce açılmış-boyut/oran sınırlarını doğrula.

    ZipInfo metadata'sı (file_size = açılmış boyut) diske/belleğe açmadan okunur,
    yani kontrolün kendisi ucuz ve güvenli. Sınır aşılırsa ZipBombError yükseltir.
    Eşikler test için parametreli; üretim varsayılanları modül sabitlerinden gelir.
    """
    total_uncompressed = 0
    total_compressed = 0

    for info in zf.infolist():
        total_uncompressed += info.file_size
        total_compressed += info.compress_size

        if total_uncompressed > max_total:
            raise ZipBombError(
                f"ZIP açılmış boyutu sınırı aştı: {total_uncompressed} > "
                f"{max_total} bayt"
            )

    # Sıkıştırma oranı: yalnızca anlamlı büyüklükteki ZIP'lerde (küçük dosyalarda
    # yüksek oran normal ve zararsızdır).
    if total_compressed >= min_compressed_for_ratio:
        ratio = total_uncompressed / total_compressed
        if ratio > max_ratio:
            raise ZipBombError(
                f"ZIP sıkıştırma oranı şüpheli: {ratio:.1f}x > {max_ratio}x "
                f"(açılmış={total_uncompressed}, sıkışmış={total_compressed})"
            )

# ── Sinyal alanları (içerik kanıtı) ──────────────────────────────────────────
_STREAMING_KEYS = frozenset({
    "ts", "ms_played", "master_metadata_track_name",
    "spotify_track_uri", "reason_start", "reason_end",
})
_STREAMING_MIN_KEYS = 3   # ilk objede bu kadar streaming alanı varsa kanıt sayılır

_ACCOUNT_NAMES = frozenset({
    "Inferences.json", "YourLibrary.json", "Wrapped2025.json",
    "Playlist1.json", "YourSoundCapsule.json",
})
_TECHLOG_NAMES = frozenset({
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

# Karar eşiği: bir tip için toplam skor bu değere ulaşırsa doğrulanmış sayılır.
# İsim (+2) tek başına yetmez; en az bir içerik kanıtı (+3) gerekir.
_CONFIDENCE_THRESHOLD = 3


def _basenames(namelist: list[str]) -> set[str]:
    return {n.rsplit("/", 1)[-1] for n in namelist}


def _first_streaming_item(zf: zipfile.ZipFile, name: str) -> dict[str, Any] | None:
    """Top-level DİZİ olan dosyanın ilk öğesini akış halinde oku (streaming history).

    Streaming dosyaları büyük olabildiği için tüm dosya parse edilmez —
    yalnızca ilk dizi öğesi (ijson). Bozuk/boş dosyada None döner.
    """
    import ijson  # geç import — test ortamı ijson gerektirmesin
    try:
        with zf.open(name) as fp:
            first = next(ijson.items(fp, "item"), None)
            return first if isinstance(first, dict) else None
    except Exception:  # noqa: BLE001
        return None


def _top_level_object(zf: zipfile.ZipFile, name: str) -> dict[str, Any] | None:
    """Top-level OBJE olan dosyayı oku (account/techlog: {"tracks": [...]} gibi).

    Bu dosyalar küçüktür (≤~400KB) — json.load güvenli. Bozuk/boş → None.
    Dosya top-level dizi ise (streaming) None döner; o ayrı yolla ele alınır.
    """
    import json as _json
    try:
        with zf.open(name) as fp:
            data = _json.load(fp)
        return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001
        return None


def _looks_like_streaming(obj: dict[str, Any]) -> bool:
    """Top-level dizinin ilk öğesi streaming event formatında mı."""
    return len(_STREAMING_KEYS & obj.keys()) >= _STREAMING_MIN_KEYS


def _account_evidence(name: str, top: dict[str, Any]) -> bool:
    """Account dosyasının içerik kanıtı (top-level OBJE).

    YourLibrary.json → {"tracks": [...], "albums": [...]} (parser §51).
    Inferences.json  → inferences yapısı. Diğer account dosyaları için isim+yapı.
    """
    if name == "YourLibrary.json":
        tracks = top.get("tracks")
        if isinstance(tracks, list):
            if not tracks:
                return True  # boş ama doğru anahtar → yine de YourLibrary
            return isinstance(tracks[0], dict) and (
                "uri" in tracks[0] or "track" in tracks[0] or "artist" in tracks[0]
            )
        return False
    if name == "Inferences.json":
        return "inferences" in top or "inference" in top
    if name in ("Wrapped2025.json", "YourSoundCapsule.json"):
        # Bu dosyalar her zaman top-level obje; varlığı + obje olması kanıt sayılır.
        return bool(top)
    return False


# Gerçek Spotify techlog event imzası: tüm techlog dosyaları message_* alanları
# + timestamp_utc paylaşır. Bir tane bile varsa techlog kanıtı sayılır.
# (Eski uydurma alanlar — uri/timestamp/connected — fallback olarak korunur.)
_TECHLOG_ITEM_KEYS = frozenset({
    "message_item_uri", "message_item_uris", "message_playlist_uri",
    "message_set", "message_is_car_connected", "message_playlist_title",
    "message_daypart", "message_content_uri", "message_title",
    "message_mix_id", "message_contents", "timestamp_utc",
    # eski/uydurma alanlar (geriye uyumluluk)
    "itemType", "uri", "connected", "daylistTitle", "trackUri",
    "playlistUri", "sectionTitle", "sectionId",
})
_TECHLOG_MIN_KEYS = 1


def _looks_like_techlog_item(name: str, obj: dict[str, Any]) -> bool:
    """İlk öğe gerçek techlog event formatında mı (message_* veya timestamp_utc).

    Tüm techlog dosyaları aynı message_* imzasını paylaşır; isim-bazlı özel
    kontrol gerekmiyor. OnRepeatContents gibi obje varyantları için herhangi
    bir bilinen techlog anahtarının varlığı yeterli.
    """
    if not isinstance(obj, dict):
        return False
    return len(_TECHLOG_ITEM_KEYS & obj.keys()) >= _TECHLOG_MIN_KEYS


def _score_types(zf: zipfile.ZipFile) -> dict[str, int]:
    """Her tip için güven skoru topla (isim +2, içerik kanıtı +3).

    Format farkları (parser'lardan doğrulandı):
      - streaming  → top-level DİZİ, ilk öğe streaming event
      - account    → top-level OBJE ({"tracks": [...]} vb.)
      - techlog    → top-level DİZİ, ilk öğe techlog event
    """
    names = zf.namelist()
    basenames = _basenames(names)
    scores = {"streaming_history": 0, "account_data": 0, "technical_log": 0}

    # ── İsim sinyalleri (+2) ──────────────────────────────────────────────────
    if any(b.startswith("StreamingHistory_music") for b in basenames):
        scores["streaming_history"] += 2
    if basenames & _ACCOUNT_NAMES:
        scores["account_data"] += 2
    if basenames & _TECHLOG_NAMES:
        scores["technical_log"] += 2

    # ── İçerik kanıtları (+3) ─────────────────────────────────────────────────
    for name in names:
        base = name.rsplit("/", 1)[-1]
        if not base.endswith(".json"):
            continue

        # account kanıtı (top-level obje)
        if base in _ACCOUNT_NAMES and scores["account_data"] < 5:
            top = _top_level_object(zf, name)
            if top and _account_evidence(base, top):
                scores["account_data"] += 3

        # techlog kanıtı: önce dizi formatı dene, olmadı top-level obje dene.
        # Spotify farklı versiyonlarda aynı dosyayı dizi veya obje olarak gönderebilir.
        if base in _TECHLOG_NAMES and scores["technical_log"] < 5:
            item = _first_streaming_item(zf, name)  # dizi → ilk öğe
            if item is None:
                # top-level obje formatı dene ({"items": [...]} veya düz obje)
                top = _top_level_object(zf, name)
                if isinstance(top, dict):
                    item = top  # objeyi direkt kanıt olarak kullan
            if item and _looks_like_techlog_item(base, item):
                scores["technical_log"] += 3

        # streaming kanıtı: adı StreamingHistory* OLAN ya da adı hiçbir bilinen
        # account/techlog/playlist/identity grubuna girmeyen .json (renamed dosya).
        is_candidate_stream = base.startswith("StreamingHistory") or (
            base not in _ACCOUNT_NAMES
            and base not in _TECHLOG_NAMES
            and not base.startswith("Playlist")
            and base not in ("identity.json",)
        )
        if is_candidate_stream and scores["streaming_history"] < 5:
            item = _first_streaming_item(zf, name)
            if item and _looks_like_streaming(item):
                scores["streaming_history"] += 3

    return scores


def detect_zip_type(zf: zipfile.ZipFile) -> str:
    """ZIP içeriğine güven skoruyla bakarak tipi tespit et (spec §2).

    Döndürdüğü değerler:
      'streaming_history' | 'account_data' | 'technical_log' | 'mixed' | 'unknown'

    Karar:
      - skor ≥3 (en az bir içerik kanıtı) → o tip doğrulanmış.
      - hem streaming hem account/techlog ≥3 → 'mixed'.
      - hiçbiri ≥3 → 'unknown'.
    """
    scores = _score_types(zf)
    logger.info("ZIP güven skorları: %s", scores)

    has_streaming = scores["streaming_history"] >= _CONFIDENCE_THRESHOLD
    has_account   = scores["account_data"] >= _CONFIDENCE_THRESHOLD
    has_techlog   = scores["technical_log"] >= _CONFIDENCE_THRESHOLD

    if has_streaming and (has_account or has_techlog):
        return "mixed"
    if has_streaming:
        return "streaming_history"
    if has_account:
        return "account_data"
    if has_techlog:
        return "technical_log"
    return "unknown"


def detect_zip_type_from_namelist(namelist: list[str]) -> str:
    """Geriye uyumluluk: yalnızca isimden tahmini tip (içerik okumadan).

    Eski detect_zip_type davranışı. Yeni kod içerik-tabanlı detect_zip_type(zf)
    kullanmalı; bu yalnızca hızlı/içeriksiz bir ön-tahmin gerektiğinde kullanılır.
    """
    basenames = {n.rsplit("/", 1)[-1] for n in namelist}
    has_streaming = any(b.startswith("StreamingHistory_music") for b in basenames)
    has_account = bool(basenames & _ACCOUNT_NAMES)
    has_techlog = bool(basenames & _TECHLOG_NAMES)

    if has_streaming and (has_account or has_techlog):
        return "mixed"
    if has_streaming:
        return "streaming_history"
    if has_account:
        return "account_data"
    if has_techlog:
        return "technical_log"
    return "unknown"
