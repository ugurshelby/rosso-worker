"""spotify_playlists — playlist listeleme + snapshot_id kontrolü + item pagination."""
from app.services.spotify_playlists import (
    fetch_user_playlists,
    fetch_playlist_snapshot_id,
    fetch_playlist_items,
)


class _Resp:
    def __init__(self, payload):
        self._payload = payload
    def raise_for_status(self): pass
    def json(self):
        return self._payload


def test_fetch_user_playlists_single_page():
    class _Http:
        def get(self, url, headers=None, params=None):
            return _Resp({
                "items": [{"id": "pl1", "name": "My Playlist", "snapshot_id": "snap1",
                           "tracks": {"total": 10}}],
                "next": None,
            })
    result = fetch_user_playlists("tok", _Http())
    assert len(result) == 1
    assert result[0]["snapshot_id"] == "snap1"


def test_fetch_user_playlists_reads_track_count_from_new_items_field():
    """Spotify API (2026 şeması) artık playlist.tracks yerine playlist.items
    kullanıyor — canlı doğrulandı, 2026-07-02. track_count buradan okunmalı."""
    class _Http:
        def get(self, url, headers=None, params=None):
            return _Resp({
                "items": [{"id": "pl2", "name": "New Schema Playlist", "snapshot_id": "snap3",
                           "items": {"href": "...", "total": 16}}],
                "next": None,
            })
    result = fetch_user_playlists("tok", _Http())
    assert result[0]["track_count"] == 16


def test_fetch_user_playlists_prefers_old_tracks_field_if_present():
    """Eski şema (tracks.total) hâlâ desteklenmeli — geriye dönük uyumluluk."""
    class _Http:
        def get(self, url, headers=None, params=None):
            return _Resp({
                "items": [{"id": "pl3", "name": "Old Schema Playlist", "snapshot_id": "snap4",
                           "tracks": {"total": 25}}],
                "next": None,
            })
    result = fetch_user_playlists("tok", _Http())
    assert result[0]["track_count"] == 25


def test_fetch_playlist_snapshot_id_cheap_call():
    class _Http:
        def get(self, url, headers=None, params=None):
            assert params.get("fields") == "snapshot_id"
            return _Resp({"snapshot_id": "snap2"})
    result = fetch_playlist_snapshot_id("tok", "pl1", _Http())
    assert result == "snap2"


def test_fetch_playlist_items_pagination():
    calls = []
    class _Http:
        def get(self, url, headers=None, params=None):
            calls.append(url)
            if len(calls) == 1:
                return _Resp({
                    "items": [{"track": {"id": f"t{i}", "name": "X", "artists": [],
                                          "external_ids": {}}, "added_at": "2026-01-01T00:00:00Z"}
                               for i in range(100)],
                    "next": "https://api.spotify.com/v1/playlists/pl1/items?offset=100",
                })
            return _Resp({"items": [], "next": None})
    result = fetch_playlist_items("tok", "pl1", _Http())
    assert len(result) == 100
    assert result[0]["position"] == 0
    assert result[99]["position"] == 99


def test_fetch_playlist_items_reads_new_item_key():
    """Spotify 2026 şeması: track objesi artık item['track'] değil item['item']
    altında (canlı doğrulandı, 2026-07-03 — P0 bug). Yeni anahtar okunmalı."""
    class _Http:
        def get(self, url, headers=None, params=None):
            return _Resp({
                "items": [{
                    "item": {"id": "trk1", "name": "New Schema Song",
                             "artists": [{"name": "Artist A"}],
                             "external_ids": {"isrc": "TR1234567890"}},
                    "added_at": "2026-07-01T00:00:00Z",
                }],
                "next": None,
            })
    result = fetch_playlist_items("tok", "pl1", _Http())
    assert len(result) == 1
    assert result[0]["spotify_id"] == "trk1"
    assert result[0]["title"] == "New Schema Song"
    assert result[0]["artists"] == ["Artist A"]
    assert result[0]["isrc"] == "TR1234567890"


def test_fetch_playlist_items_falls_back_to_old_track_key():
    """Eski şema (item['track']) hâlâ desteklenmeli — geriye dönük uyumluluk."""
    class _Http:
        def get(self, url, headers=None, params=None):
            return _Resp({
                "items": [{
                    "track": {"id": "trk2", "name": "Old Schema Song",
                              "artists": [{"name": "Artist B"}], "external_ids": {}},
                    "added_at": "2026-01-01T00:00:00Z",
                }],
                "next": None,
            })
    result = fetch_playlist_items("tok", "pl1", _Http())
    assert len(result) == 1
    assert result[0]["spotify_id"] == "trk2"
    assert result[0]["title"] == "Old Schema Song"


def test_fetch_playlist_items_skips_local_and_null_id():
    """is_local=True veya id yok olan item'lar (çöp kaynağı) atlanır."""
    class _Http:
        def get(self, url, headers=None, params=None):
            return _Resp({
                "items": [
                    {"is_local": True, "item": {"id": None, "name": "", "artists": []},
                     "added_at": "2026-07-01T00:00:00Z"},
                    {"item": None, "added_at": "2026-07-01T00:00:00Z"},
                    {"item": {"id": "good1", "name": "Real Song",
                              "artists": [{"name": "A"}], "external_ids": {}},
                     "added_at": "2026-07-01T00:00:00Z"},
                ],
                "next": None,
            })
    result = fetch_playlist_items("tok", "pl1", _Http())
    assert len(result) == 1
    assert result[0]["spotify_id"] == "good1"
    assert result[0]["position"] == 0


# ── FAZ SBA-3: kapak + açıklama (2026-07-21) ────────────────────────────────
# Bu iki alan Spotify'ın AYNI yanıtında geliyordu ama okunmadan atılıyordu:
# 105/105 playlist'te cover_url ve description NULL'dı. Ek istek maliyeti yok.

def test_fetch_user_playlists_kapak_ve_aciklama_okunur():
    class _Http:
        def get(self, url, headers=None, params=None):
            return _Resp({
                "items": [{
                    "id": "pl1", "name": "Kapaklı", "snapshot_id": "s1",
                    "tracks": {"total": 3},
                    "description": "Gece sürüşü",
                    "images": [
                        {"url": "https://i/kucuk.jpg", "width": 60, "height": 60},
                        {"url": "https://i/buyuk.jpg", "width": 640, "height": 640},
                    ],
                }],
                "next": None,
            })
    result = fetch_user_playlists("tok", _Http())
    # Sıralamaya değil width'e göre seçilmeli — büyük görsel ikinci sırada.
    assert result[0]["cover_url"] == "https://i/buyuk.jpg"
    assert result[0]["description"] == "Gece sürüşü"


def test_fetch_user_playlists_kapak_yoksa_none():
    """images boş/eksik olabilir (yeni playlist, mozaik üretilmemiş)."""
    class _Http:
        def get(self, url, headers=None, params=None):
            return _Resp({
                "items": [{"id": "pl2", "name": "Kapaksız", "snapshot_id": "s2",
                           "tracks": {"total": 0}, "images": []}],
                "next": None,
            })
    result = fetch_user_playlists("tok", _Http())
    assert result[0]["cover_url"] is None
    assert result[0]["description"] is None


def test_fetch_user_playlists_bos_aciklama_none_olur():
    """Spotify boş açıklamayı "" döner; DB'de NULL olmalı ki 'açıklama yok'
    ile 'boş açıklama' karışmasın."""
    class _Http:
        def get(self, url, headers=None, params=None):
            return _Resp({
                "items": [{"id": "pl3", "name": "Bos", "snapshot_id": "s3",
                           "tracks": {"total": 1}, "description": "   "}],
                "next": None,
            })
    result = fetch_user_playlists("tok", _Http())
    assert result[0]["description"] is None


def test_fetch_user_playlists_width_none_ise_kirilmaz():
    """Playlist mozaik kapaklarında width None gelebilir — max() patlamamalı."""
    class _Http:
        def get(self, url, headers=None, params=None):
            return _Resp({
                "items": [{"id": "pl4", "name": "Mozaik", "snapshot_id": "s4",
                           "tracks": {"total": 5},
                           "images": [{"url": "https://i/mozaik.jpg",
                                       "width": None, "height": None}]}],
                "next": None,
            })
    result = fetch_user_playlists("tok", _Http())
    assert result[0]["cover_url"] == "https://i/mozaik.jpg"
