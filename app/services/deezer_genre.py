"""Deezer album-level genre çekimi — API key yok, ISRC yok, artist+title yeter.

Akış (canlı test 2026-06-30 ile doğrulandı):
  1. GET /search?q=artist:"X" track:"Y"  → ilk sonucun album.id
  2. GET /album/{id}                      → album.genres.data[].name

Deezer genre album-level ve kaba ('Pop', 'Rap/Hip Hop'); normalize_genres ile
kanonik türe çevrilir. TR albümlerde sık boş döner → Last.fm fallback (genre_runner).

Rate-limit (CANLI ÖLÇÜM 2026-07-16, 100 istekli sınama): **50 istek / 5 saniye,
kayan pencere** — 51. istek 429, ~2 sn sonra pencere açılıyor. Eski "50 req/sn,
429 nadir" notu YANLIŞTI (canlıda hit_count 505'e çıktı). Bu yüzden tüm Deezer
HTTP çağrıları _paced_get'ten geçer: istekler arası küresel ≥250ms (~4 istek/sn,
limitin yarısının altı — CLAUDE.md §1.6). Tek track hatası batch'i durdurmaz.

Deezer kota aşımını İKİ biçimde döndürür (canlı sınamada ikisi de görüldü):
HTTP 429 VE HTTP 200 + gövdede {"error": {"code": 4}}. İkisi de RateLimitError.
"""
from __future__ import annotations

import logging
import threading
import time
import unicodedata
import urllib.parse
from difflib import SequenceMatcher
from typing import Any

import httpx

from app.services.genre_errors import RateLimitError

logger = logging.getLogger("rosso.worker.deezer_genre")

_SEARCH_URL = "https://api.deezer.com/search"
_ALBUM_URL = "https://api.deezer.com/album/{id}"

# ── Küresel hız sınırlayıcı: iki Deezer isteği arası en az bu kadar geçer. ──
# 8 eşzamanlı worker thread'i de buradan geçer; kilit içindeki sleep kasıtlı —
# bekleyenler sıraya girer, süreç geneli ~4 istek/sn'yi aşamaz (limit 10/sn).
_PACE_GAP_S = 0.25
_pace_lock = threading.Lock()
_last_request_at = 0.0


def _paced_get(http_client: Any, url: str, timeout: float = 10) -> Any:
    """Tüm Deezer HTTP çağrılarının tek geçidi — küresel 250ms aralık."""
    global _last_request_at
    with _pace_lock:
        wait = _last_request_at + _PACE_GAP_S - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_request_at = time.monotonic()
    return http_client.get(url, timeout=timeout)


# Deezer hata kodları: 4=Quota, 700=ServiceBusy → geçici, RateLimitError.
# 800=DataException (veri yok) → kalıcı, sessizce boş sonuç doğru davranış.
_TRANSIENT_ERROR_CODES = {4, 700}


def _raise_if_quota(payload: Any) -> None:
    """HTTP 200 + gövde-hatası biçimindeki kota aşımını RateLimitError'a çevir.

    Bu kontrol yoksa kota fırtınasında arama sessizce [] döner ve track haksız
    yere kalıcı lookup_failed damgası yer (canlı sınama bulgusu, 2026-07-16).
    """
    if not isinstance(payload, dict):
        return
    err = payload.get("error")
    if isinstance(err, dict) and err.get("code") in _TRANSIENT_ERROR_CODES:
        raise RateLimitError("deezer", None)

# Aşama B (düz arama) doğrulama eşikleri — fold'lu difflib (Fable 5, 2026-07-03).
_ARTIST_MATCH_MIN = 0.85
_TITLE_MATCH_MIN = 0.60


def fold(text: str | None) -> str:
    """Unicode-fold: İ→I ön-değişim + NFD + combining işaretleri at + lower.

    Deezer aramasında Türkçe İ/aksan sorunu için (canlı: İ'li track'lerde 5/5
    kurtarma). Örn: DAİM→daim, Gülşen→gulsen. Türkçe 'İ' (U+0130) NFD'de
    beklenmedik ayrıştığı için önce düz 'I'ya çevrilir.
    """
    if not text:
        return ""
    pre = text.replace("İ", "I").replace("ı", "i")
    decomposed = unicodedata.normalize("NFD", pre)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return stripped.lower().strip()


def _similar(a: str, b: str) -> float:
    """fold'lanmış iki metnin difflib benzerliği (0-1)."""
    return SequenceMatcher(None, fold(a), fold(b)).ratio()


def get_deezer_genres(artist: str, title: str, http_client: Any) -> list[str]:
    """artist + title → Deezer album genre listesi (ham, normalize edilmemiş).

    Bulunamazsa veya genre boşsa [] döner.
    """
    if not artist or not title:
        return []

    q = urllib.parse.quote(f'artist:"{artist}" track:"{title}"')
    try:
        resp = _paced_get(http_client, f"{_SEARCH_URL}?q={q}&limit=1")
        resp.raise_for_status()
        data = resp.json().get("data", [])
    except Exception:  # noqa: BLE001
        logger.warning("Deezer search başarısız: %s - %s", artist, title)
        return []

    if not data:
        return []

    album = data[0].get("album") or {}
    album_id = album.get("id")
    if not album_id:
        return []

    try:
        resp = _paced_get(http_client, _ALBUM_URL.format(id=album_id))
        resp.raise_for_status()
        genres_data = resp.json().get("genres", {}).get("data", [])
    except Exception:  # noqa: BLE001
        logger.warning("Deezer album başarısız: album=%s", album_id)
        return []

    return [g["name"] for g in genres_data if g.get("name")]


def get_deezer_track_scored(
    artist: str, title: str, http_client: Any
) -> list[tuple[str, int]]:
    """artist + title → Deezer album genre (name, count). count=0 → pozisyon-count.

    genre DNA skor sistemi için get_deezer_genres'in çift döndüren sürümü.
    """
    return [(name, 0) for name in get_deezer_genres(artist, title, http_client)]


def _deezer_search(query: str, http_client: Any) -> list[dict]:
    """Ham Deezer /search çağrısı → data listesi. 429 / 200+kota → RateLimitError."""
    q = urllib.parse.quote(query)
    try:
        resp = _paced_get(http_client, f"{_SEARCH_URL}?q={q}&limit=1")
        resp.raise_for_status()
        payload = resp.json()
    except httpx.HTTPStatusError as exc:
        if exc.response is not None and exc.response.status_code == 429:
            ra = exc.response.headers.get("Retry-After")
            raise RateLimitError("deezer", float(ra) if ra else None) from exc
        logger.warning("Deezer search başarısız: %s", query)
        return []
    except Exception:  # noqa: BLE001
        logger.warning("Deezer search başarısız: %s", query)
        return []
    _raise_if_quota(payload)
    return payload.get("data", []) if isinstance(payload, dict) else []


def _album_genres_scored(album_id: Any, http_client: Any) -> list[tuple[str, int]]:
    """album_id → [(genre_name, 0), ...]. Hata/boş → []. Kota → RateLimitError."""
    try:
        resp = _paced_get(http_client, _ALBUM_URL.format(id=album_id))
        resp.raise_for_status()
        payload = resp.json()
    except httpx.HTTPStatusError as exc:
        if exc.response is not None and exc.response.status_code == 429:
            ra = exc.response.headers.get("Retry-After")
            raise RateLimitError("deezer", float(ra) if ra else None) from exc
        logger.warning("Deezer album başarısız: album=%s", album_id)
        return []
    except Exception:  # noqa: BLE001
        logger.warning("Deezer album başarısız: album=%s", album_id)
        return []
    _raise_if_quota(payload)
    genres_data = payload.get("genres", {}).get("data", []) if isinstance(payload, dict) else []
    return [(g["name"], 0) for g in genres_data if g.get("name")]


def get_deezer_track_scored_with_anchor(
    artist: str, title: str, http_client: Any
) -> tuple[list[tuple[str, int]], str | None, int | None]:
    """artist + title → (Deezer albüm türleri [(name,0)], sanatçı adı=çapa, artist.id).

    İki aşamalı (Fable 5, 2026-07-03):
      Aşama A — fold'lu kesin sorgu: artist:"fold(A)" track:"fold(T)".
        İ/aksan sorununu çözer (İ'li track'lerde 5/5 kurtarma).
      Aşama B — A boşsa düz "A T" araması + ZORUNLU sanatçı+başlık doğrulaması.
        Yanlış sanatçının track'ini kabul etmeyi engeller (Stabil→Ati242 senaryosu).

    Sanatçı doğrulaması HER İKİ aşamada da geçerli: Aşama A'da çapa çağırana
    döner (genre_runner is_artist_match ile süzer); Aşama B'de bu fonksiyon
    içinde artist ≥0.85 VE title ≥0.6 doğrulaması yapılır, geçmezse [] döner.

    **artist.id (2026-07-04):** Track hit'i doğru artist.id'yi içerir. Bu ID,
    artist DNA'sının isimle yeniden aranmadan (Deezer isim araması yanlış sanatçı
    buluyor: Motive→Harp, Ceza→Brezilya) doğrudan /artist/{id}/albums'tan
    çözülmesini sağlar. Doğrulama geçen hit'lerde döner; aksi None.
    """
    if not artist or not title:
        return [], None, None

    # Tamamen sayısal başlık (örn. "129") Deezer arama ayrıştırıcısını kırıyor
    # (canlı doğrulandı, 2026-07-02). Sorguyu hiç atmadan boş dön.
    if title.strip().isdigit():
        return [], None, None

    # ── Aşama A: fold'lu kesin sorgu ──
    data = _deezer_search(f'artist:"{fold(artist)}" track:"{fold(title)}"', http_client)
    if data:
        hit = data[0]
        found = hit.get("artist") or {}
        found_name = found.get("name")
        found_id = found.get("id")
        album_id = (hit.get("album") or {}).get("id")
        if album_id:
            return _album_genres_scored(album_id, http_client), found_name, found_id
        return [], found_name, found_id

    # ── Aşama B: düz arama + zorunlu doğrulama ──
    data = _deezer_search(f"{artist} {title}", http_client)
    if not data:
        return [], None, None

    hit = data[0]
    found = hit.get("artist") or {}
    found_name = found.get("name")
    found_id = found.get("id")
    found_title = hit.get("title") or hit.get("title_short") or ""

    # Zorunlu doğrulama: yanlış sanatçı/başlık track'i kabul etme.
    if _similar(artist, found_name or "") < _ARTIST_MATCH_MIN:
        logger.info("Deezer aşama B elendi (sanatçı): %s → %s", artist, found_name)
        return [], None, None
    if _similar(title, found_title) < _TITLE_MATCH_MIN:
        logger.info("Deezer aşama B elendi (başlık): %s → %s", title, found_title)
        return [], None, None

    album_id = (hit.get("album") or {}).get("id")
    if not album_id:
        return [], found_name, found_id
    return _album_genres_scored(album_id, http_client), found_name, found_id


_ARTIST_TOP_URL = "https://api.deezer.com/artist/{id}/top?limit=1"
_ARTIST_ALBUMS_URL = "https://api.deezer.com/artist/{id}/albums?limit=50"

# Deezer sabit üst-tür ID haritası (GET /genre ile alındı, 2026-07-04).
# Deezer albüm nesnesinde asıl tür `genre_id`'de; `genres.data` TR'de sık boş.
# İsimler Deezer'ın kendi Türkçe etiketleri (normalize_genres'e girer).
_DEEZER_GENRE_MAP: dict[int, str] = {
    132: "Pop",
    116: "Rap/Hip Hop",
    152: "Rock",
    113: "Dans",
    165: "R&B",
    85: "Alternatif",
    106: "Elektronik",
    466: "Folk",
    144: "Reggae",
    129: "Caz",
    98: "Klasik",
    173: "Film/Oyun",
    464: "Metal",
    169: "Soul & Funk",
    2: "Afrika müziği",
    12: "Arap müziği",
    16: "Asya müziği",
    153: "Blues",
    75: "Brezilya müziği",
    81: "Hint müziği",
    197: "Latin Müzik",
}


def get_deezer_artist_genres(artist: str, http_client: Any) -> list[str]:
    """Sanatçı adıyla Deezer genre ara — şarkı bulunamayınca fallback.

    Akış: /search?q=artist:"X"&limit=1 → artist.id → /artist/{id}/top → album.id → /album/{id} → genres
    Bulamazsa [] döner.
    """
    if not artist:
        return []

    q = urllib.parse.quote(f'artist:"{artist}"')
    try:
        resp = _paced_get(http_client, f"{_SEARCH_URL}?q={q}&limit=1")
        resp.raise_for_status()
        data = resp.json().get("data", [])
    except Exception:  # noqa: BLE001
        logger.warning("Deezer artist search başarısız: %s", artist)
        return []

    if not data:
        return []

    artist_id = (data[0].get("artist") or {}).get("id")
    if not artist_id:
        return []

    try:
        resp = _paced_get(http_client, _ARTIST_TOP_URL.format(id=artist_id))
        resp.raise_for_status()
        top_data = resp.json().get("data", [])
    except Exception:  # noqa: BLE001
        logger.warning("Deezer artist/top başarısız: artist_id=%s", artist_id)
        return []

    if not top_data:
        return []

    album_id = (top_data[0].get("album") or {}).get("id")
    if not album_id:
        return []

    try:
        resp = _paced_get(http_client, _ALBUM_URL.format(id=album_id))
        resp.raise_for_status()
        genres_data = resp.json().get("genres", {}).get("data", [])
    except Exception:  # noqa: BLE001
        logger.warning("Deezer artist album başarısız: album=%s", album_id)
        return []

    return [g["name"] for g in genres_data if g.get("name")]


def get_deezer_artist_genres_scored(
    artist: str, http_client: Any
) -> tuple[list[tuple[str, int]], str | None]:
    """Sanatçı adıyla Deezer genre ara; (name,count) çiftleri + BULUNAN sanatçı adı döner.

    Akış (2026-07-04 — çoklu-albüm genre_id çoğunluğu):
      1. /search?q=artist:"X" → doğru artist.id + bulunan ad (çapa doğrulaması için)
      2. /artist/{id}/albums?limit=50 → her albümün genre_id'si
      3. genre_id → isim (_DEEZER_GENRE_MAP) → çoğunluk sayımı (count=frekans)

    Eski tek-top-track-albümü yolu terk edildi: o albüm başkasının (feat/derleme)
    olabiliyor ve `genres.data` TR'de sık boş. Çoklu-albüm çoğunluğu Motive'i
    (42/48 = Rap/Hip Hop) doğru çözer. Bulunan isim, çağıranın difflib ile yanlış
    eşleşmeyi (Drake → Brezilyalı Drake) elemesi için gereklidir. Bulunamazsa ([], None).
    """
    if not artist:
        return [], None

    q = urllib.parse.quote(f'artist:"{artist}"')
    try:
        resp = _paced_get(http_client, f"{_SEARCH_URL}?q={q}&limit=1")
        resp.raise_for_status()
        payload = resp.json()
    except Exception:  # noqa: BLE001
        logger.warning("Deezer artist search (scored) başarısız: %s", artist)
        return [], None

    # Kota fırtınasında sessiz [] → profil Deezer'sız (zayıf) kurulup KALICI
    # yazılırdı; RateLimitError yukarı taşınır, sonraki tur doğru kurar.
    _raise_if_quota(payload)
    data = payload.get("data", []) if isinstance(payload, dict) else []

    if not data:
        return [], None

    found_artist = data[0].get("artist") or {}
    artist_id = found_artist.get("id")
    found_name = found_artist.get("name")
    if not artist_id:
        return [], found_name

    return get_deezer_artist_genres_by_id(artist_id, http_client), found_name


def get_deezer_artist_genres_by_id(
    artist_id: int | None, http_client: Any
) -> list[tuple[str, int]]:
    """artist.id → çoklu-albüm genre_id çoğunluğu [(name, count), ...].

    Doğrudan /artist/{id}/albums (isimle arama YOK). Track çapasından gelen
    doğrulanmış artist.id ile çağrılır → isim çakışması imkânsız (2026-07-04).
    Bulunamazsa []. genre_id haritada yoksa (-1, 0) sayıma girmez.
    """
    if not artist_id:
        return []

    try:
        resp = _paced_get(http_client, _ARTIST_ALBUMS_URL.format(id=artist_id))
        resp.raise_for_status()
        payload = resp.json()
    except httpx.HTTPStatusError as exc:
        # HTTP 429 → RateLimitError (sessizce [] DÖNME; profil yanlış türe düşer).
        if exc.response is not None and exc.response.status_code == 429:
            ra = exc.response.headers.get("Retry-After")
            raise RateLimitError("deezer", float(ra) if ra else None) from exc
        logger.warning("Deezer artist/albums başarısız: artist_id=%s", artist_id)
        return []
    except Exception:  # noqa: BLE001
        logger.warning("Deezer artist/albums başarısız: artist_id=%s", artist_id)
        return []

    # Kota (code 4/700) → RateLimitError; "veri yok" (800) → [] doğru davranış.
    _raise_if_quota(payload)

    albums = payload.get("data", []) if isinstance(payload, dict) else []

    # genre_id çoğunluğu: haritada olmayan (-1, 0) atlanır, frekans = count.
    freq: dict[str, int] = {}
    for alb in albums:
        name = _DEEZER_GENRE_MAP.get(alb.get("genre_id"))
        if name:
            freq[name] = freq.get(name, 0) + 1

    # Çoğunluktan aza sırala (en baskın tür ilk slot'a)
    return sorted(freq.items(), key=lambda kv: kv[1], reverse=True)
