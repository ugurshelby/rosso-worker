"""mood_weekly_sync_runner — senkron açık kullanıcıların Spotify playlist'i
taze pakete göre güncelleniyor mu, hata izolasyonu çalışıyor mu.

⚠ Migration 0271'den beri `mood_pkg.payload` doğrudan `spotify_id` taşıyor
(önceden ayrı bir `tracks` sorgusuyla track_id → spotify_id çevrimi
yapılıyordu). Runner artık payload'daki sırayı olduğu gibi kullanıyor —
payload zaten skora göre sıralı geliyor (`with ordinality`), ayrıca bir
sıra yeniden kurma adımına gerek yok.
"""
from unittest.mock import patch

from app.pipeline import mood_weekly_sync_runner as runner


class _FakeClient:
    """candidates: get_mood_weekly_sync_candidates() satırları.
    pkg_payload: {(user_id, mood_key): [track dict, ...]} — spotify_id dahil.
    """

    def __init__(self, candidates, pkg_payload=None):
        self._candidates = candidates
        self._pkg_payload = pkg_payload or {}

    def rpc(self, fn, _params):
        assert fn == "get_mood_weekly_sync_candidates"
        client = self

        class _E:
            def execute(self):
                class _R:
                    data = client._candidates
                return _R()
        return _E()

    def table(self, name):
        if name == "mood_pkg":
            return _MoodPkgTable(self)
        raise AssertionError(f"beklenmeyen tablo: {name}")


class _MoodPkgTable:
    def __init__(self, client):
        self._client = client
        self._uid = None
        self._mood = None

    def select(self, *_a, **_k):
        return self

    def eq(self, col, val):
        if col == "user_id":
            self._uid = val
        elif col == "mood_key":
            self._mood = val
        return self

    def maybe_single(self):
        return self

    def execute(self):
        payload = self._client._pkg_payload.get((self._uid, self._mood), [])
        class _R:
            data = {"payload": payload}
        return _R()


class _FakeHttp:
    def __init__(self, status_code=200):
        self._status = status_code
        self.calls: list[dict] = []

    def put(self, url, json=None, headers=None):
        self.calls.append({"url": url, "json": json, "headers": headers})

        class _R:
            status_code = self._status
        r = _R()
        r.status_code = self._status
        return r


def _patched_token(token="tok-123"):
    return patch(
        "app.pipeline.mood_weekly_sync_runner.get_valid_spotify_token",
        return_value=token,
    )


def test_aday_yoksa_empty():
    client = _FakeClient(candidates=[])
    result = runner.run_mood_weekly_sync(client, "key", _FakeHttp())
    assert result["outcome"] == "empty"
    assert result["synced"] == 0


def test_basarili_senkron_replace_cagrisi_atar():
    client = _FakeClient(
        candidates=[{"user_id": "u1", "mood_key": "gece3", "exported_playlist_id": "pl1"}],
        pkg_payload={("u1", "gece3"): [
            {"track_id": "t1", "spotify_id": "sp1"},
            {"track_id": "t2", "spotify_id": "sp2"},
        ]},
    )
    http = _FakeHttp(status_code=200)
    with _patched_token():
        result = runner.run_mood_weekly_sync(client, "key", http)

    assert result["outcome"] == "success"
    assert result["synced"] == 1
    assert len(http.calls) == 1
    assert http.calls[0]["url"] == "https://api.spotify.com/v1/playlists/pl1/tracks"
    # Sıra korunmalı: payload sp1,sp2 sırasında geldi, URI listesi de öyle olmalı.
    assert http.calls[0]["json"]["uris"] == ["spotify:track:sp1", "spotify:track:sp2"]


def test_spotify_id_eksik_track_atlanir_sira_korunur():
    """⚠ Payload'da spotify_id eksik olan şarkılar atlanır, kalanların
    sırası korunur (payload zaten skora göre sıralı geliyor)."""
    client = _FakeClient(
        candidates=[{"user_id": "u1", "mood_key": "gece3", "exported_playlist_id": "pl1"}],
        pkg_payload={("u1", "gece3"): [
            {"track_id": "t3", "spotify_id": "sp3"},
            {"track_id": "t1", "spotify_id": None},
            {"track_id": "t2", "spotify_id": "sp2"},
        ]},
    )
    http = _FakeHttp(status_code=200)
    with _patched_token():
        result = runner.run_mood_weekly_sync(client, "key", http)

    assert result["synced"] == 1
    assert http.calls[0]["json"]["uris"] == [
        "spotify:track:sp3", "spotify:track:sp2",
    ]


def test_token_yoksa_atlanir_hata_degil():
    client = _FakeClient(
        candidates=[{"user_id": "u1", "mood_key": "gece3", "exported_playlist_id": "pl1"}],
        pkg_payload={("u1", "gece3"): [{"track_id": "t1", "spotify_id": "sp1"}]},
    )
    with _patched_token(token=None):
        result = runner.run_mood_weekly_sync(client, "key", _FakeHttp())

    # synced=0, errors=0 → outcome 'empty' (hiçbir şey senkron edilmedi,
    # 'success' yanıltıcı olurdu — diğer paket runner'larıyla aynı sözleşme).
    assert result["outcome"] == "empty"
    assert result["synced"] == 0
    assert result["skipped"] == 1
    assert result["errors"] == 0


def test_spotify_hatasi_izole_diger_kullaniciyi_etkilemez():
    client = _FakeClient(
        candidates=[
            {"user_id": "u1", "mood_key": "gece3", "exported_playlist_id": "pl1"},
            {"user_id": "u2", "mood_key": "odak", "exported_playlist_id": "pl2"},
        ],
        pkg_payload={
            ("u1", "gece3"): [{"track_id": "t1", "spotify_id": "sp1"}],
            ("u2", "odak"): [{"track_id": "t2", "spotify_id": "sp2"}],
        },
    )

    real_put = _FakeHttp.put

    def flaky_put(self, url, json=None, headers=None):
        if "pl1" in url:
            class _R:
                status_code = 500
            r = _R()
            r.status_code = 500
            self.calls.append({"url": url})
            return r
        return real_put(self, url, json=json, headers=headers)

    http = _FakeHttp(status_code=200)
    with patch.object(_FakeHttp, "put", flaky_put), _patched_token():
        result = runner.run_mood_weekly_sync(client, "key", http)

    assert result["outcome"] == "partial"
    assert result["synced"] == 1
    assert result["errors"] == 1


def test_payloadda_spotify_id_yoksa_atlanir():
    client = _FakeClient(
        candidates=[{"user_id": "u1", "mood_key": "gece3", "exported_playlist_id": "pl1"}],
        pkg_payload={("u1", "gece3"): []},
    )
    with _patched_token():
        result = runner.run_mood_weekly_sync(client, "key", _FakeHttp())

    assert result["outcome"] == "empty"
    assert result["synced"] == 0
    assert result["skipped"] == 1


def test_100_ustu_track_kirpilmaz_atlanir():
    """⚠ 2026-08-26 düzeltmesi: MOOD_TRACK_LIMIT bugün 50 ama ileride
    büyürse, Spotify'ın tek istekte kabul ettiği 100 URI sınırını aşan
    bir payload SESSİZCE kırpılıp eksik yazılmamalı — aday tümüyle
    atlanmalı (kullanıcı hiç senkronlanmamış görür, eksik playlist DEĞİL).
    """
    fazla_payload = [
        {"track_id": f"t{i}", "spotify_id": f"sp{i}"} for i in range(101)
    ]
    client = _FakeClient(
        candidates=[{"user_id": "u1", "mood_key": "gece3", "exported_playlist_id": "pl1"}],
        pkg_payload={("u1", "gece3"): fazla_payload},
    )
    http = _FakeHttp(status_code=200)
    with _patched_token():
        result = runner.run_mood_weekly_sync(client, "key", http)

    assert result["outcome"] == "empty"
    assert result["synced"] == 0
    assert result["skipped"] == 1
    assert len(http.calls) == 0  # Spotify'a HİÇ istek atılmadı — kırpılmış veri yazılmadı


def test_tam_100_track_sinirda_gecerli():
    """Sınır dahil (100) — hâlâ tek istekte geçerli, atlanmamalı."""
    tam_payload = [
        {"track_id": f"t{i}", "spotify_id": f"sp{i}"} for i in range(100)
    ]
    client = _FakeClient(
        candidates=[{"user_id": "u1", "mood_key": "gece3", "exported_playlist_id": "pl1"}],
        pkg_payload={("u1", "gece3"): tam_payload},
    )
    http = _FakeHttp(status_code=200)
    with _patched_token():
        result = runner.run_mood_weekly_sync(client, "key", http)

    assert result["outcome"] == "success"
    assert result["synced"] == 1
    assert len(http.calls[0]["json"]["uris"]) == 100
