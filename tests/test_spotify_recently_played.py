"""spotify_recently_played — pagination ile sıfır kayıp fetch testleri."""
from app.services.spotify_recently_played import fetch_recently_played


class _Resp:
    def __init__(self, payload):
        self._payload = payload
    def raise_for_status(self): pass
    def json(self):
        return self._payload


def _item(played_at, track_id, isrc=None, duration_ms=210000, images=None):
    track = {
        "id": track_id,
        "name": f"Track {track_id}",
        "artists": [{"name": "Artist"}],
        "external_ids": {"isrc": isrc} if isrc else {},
        "duration_ms": duration_ms,
    }
    if images is not None:
        track["album"] = {"images": images}
    return {"played_at": played_at, "track": track}


def test_single_page_no_pagination_needed():
    """50'den az track varsa tek çağrı yeterli, next None."""
    class _Http:
        def get(self, url, headers=None, params=None):
            return _Resp({
                "items": [_item("2026-07-02T10:00:00Z", "t1", "US1234567")],
                "next": None,
            })

    result = fetch_recently_played("token", None, _Http())
    assert len(result) == 1
    assert result[0]["spotify_id"] == "t1"
    assert result[0]["isrc"] == "US1234567"
    assert result[0]["duration_ms"] == 210000


def test_pagination_follows_next_until_null():
    """next dolu olduğu sürece takip edilir, sıfır kayıp."""
    calls = []

    class _Http:
        def get(self, url, headers=None, params=None):
            calls.append((url, params))
            if len(calls) == 1:
                return _Resp({
                    "items": [_item(f"2026-07-02T1{i}:00:00Z", f"t{i}") for i in range(50)],
                    "next": "https://api.spotify.com/v1/me/player/recently-played?before=X",
                })
            return _Resp({
                "items": [_item("2026-07-02T09:00:00Z", "t50")],
                "next": None,
            })

    result = fetch_recently_played("token", None, _Http())
    assert len(result) == 51  # 50 + 1 — sıfır kayıp
    assert len(calls) == 2
    # ikinci çağrı next URL'ini olduğu gibi kullanır (yeni params eklenmez)
    assert calls[1][0] == "https://api.spotify.com/v1/me/player/recently-played?before=X"


def test_after_param_used_when_provided():
    """after_ms verilirse ilk çağrıya after parametresi eklenir."""
    captured = {}

    class _Http:
        def get(self, url, headers=None, params=None):
            captured["params"] = params
            return _Resp({"items": [], "next": None})

    fetch_recently_played("token", 1719900000000, _Http())
    assert captured["params"]["after"] == 1719900000000


def test_401_raises_auth_error():
    """401 → AuthError yükseltilir (çağıran is_active=false yapabilsin diye)."""
    import httpx
    import pytest
    from app.services.spotify_recently_played import fetch_recently_played, SpotifyAuthError

    class _Resp:
        status_code = 401
        def raise_for_status(self):
            raise httpx.HTTPStatusError("401", request=None, response=self)
        def json(self): return {}

    class _Http:
        def get(self, url, headers=None, params=None): return _Resp()

    with pytest.raises(SpotifyAuthError):
        fetch_recently_played("token", None, _Http())


def test_429_raises_rate_limit_error_with_retry_after():
    """429 → SpotifyRateLimitError(retry_after) yükseltilir."""
    import httpx
    import pytest
    from app.services.spotify_recently_played import fetch_recently_played, SpotifyRateLimitError

    class _Resp:
        status_code = 429
        headers = {"Retry-After": "30"}
        def raise_for_status(self):
            raise httpx.HTTPStatusError("429", request=None, response=self)
        def json(self): return {}

    class _Http:
        def get(self, url, headers=None, params=None): return _Resp()

    with pytest.raises(SpotifyRateLimitError) as exc:
        fetch_recently_played("token", None, _Http())
    assert exc.value.retry_after == 30.0


# ── Albüm kapağı (plan 08, 2026-08-05) ────────────────────────────────────────
#
# Kapak yanıtta ZATEN geliyordu ama okunmuyordu; kör dolgu cron'u aynı görseli
# ayrı bir istekle topluyordu. Bu testler alanın gerçekten okunduğunu ve
# en BÜYÜK görselin seçildiğini sabitler.


def test_album_kapagi_okunur():
    """Yanıttaki album.images kapağı `image_url` olarak döner (ek istek YOK)."""
    class _Http:
        def get(self, url, headers=None, params=None):
            return _Resp({
                "items": [_item(
                    "2026-08-05T10:00:00Z", "t1",
                    images=[{"url": "https://i.scdn.co/kucuk", "width": 64}],
                )],
                "next": None,
            })

    result = fetch_recently_played("token", None, _Http())
    assert result[0]["image_url"] == "https://i.scdn.co/kucuk"


def test_en_buyuk_gorsel_secilir_siraya_guvenilmez():
    """Sıra değil GENİŞLİK belirler — Spotify sıralamayı garanti etmez."""
    class _Http:
        def get(self, url, headers=None, params=None):
            return _Resp({
                "items": [_item(
                    "2026-08-05T10:00:00Z", "t1",
                    images=[
                        {"url": "https://i.scdn.co/orta", "width": 300},
                        {"url": "https://i.scdn.co/buyuk", "width": 640},
                        {"url": "https://i.scdn.co/kucuk", "width": 64},
                    ],
                )],
                "next": None,
            })

    result = fetch_recently_played("token", None, _Http())
    assert result[0]["image_url"] == "https://i.scdn.co/buyuk"


def test_album_yoksa_image_url_none_olur():
    """Kapak gelmezse None — çökmez. UI o zaman deterministik gradyana düşer."""
    class _Http:
        def get(self, url, headers=None, params=None):
            return _Resp({
                "items": [_item("2026-08-05T10:00:00Z", "t1")],  # album YOK
                "next": None,
            })

    result = fetch_recently_played("token", None, _Http())
    assert result[0]["image_url"] is None


def test_bozuk_images_alani_cokmez():
    """`album.images` beklenmedik şekilde gelirse sessizce None döner.

    ⚠ `_item(images=None)` `album` anahtarını HİÇ eklemiyor (yukarıdaki test
    onu kapsıyor). Burada album'ü elle kuruyoruz ki `images` alanının kendisi
    bozuk olduğunda ne olduğunu ölçelim — ikisi FARKLI senaryo.
    """
    for bozuk in (None, [], [{"width": 640}], "metin", {"url": "x"}):
        payload = {
            "items": [{
                "played_at": "2026-08-05T10:00:00Z",
                "track": {
                    "id": "t1",
                    "name": "Track t1",
                    "artists": [{"name": "Artist"}],
                    "external_ids": {},
                    "duration_ms": 210000,
                    "album": {"images": bozuk},
                },
            }],
            "next": None,
        }

        class _Http:
            def get(self, url, headers=None, params=None, _p=payload):
                return _Resp(_p)

        result = fetch_recently_played("token", None, _Http())
        assert result[0]["image_url"] is None, f"bozuk girdi: {bozuk!r}"
