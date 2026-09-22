"""0335 (2026-09-22, ÖLÇÜLDÜ): ISRC birleştirmesi "B aslında A'dır" notunu
tutmuyordu; recently-played ISRC vermediği için B her gelişinde yeni şarkı
olarak açılıyordu. `find_by_spotify_id` artık `track_spotify_alias`'a bakar."""
from app.matching.repo import SupabaseTrackRepo


class _Q:
    def __init__(self, db, table):
        self._db, self._table, self._filters = db, table, {}

    def select(self, *_a, **_k):
        return self

    def eq(self, col, val):
        self._filters[col] = val
        return self

    def limit(self, _n):
        return self

    def execute(self):
        self._db.calls.append(self._table)
        rows = [r for r in self._db.data.get(self._table, [])
                if all(r.get(c) == v for c, v in self._filters.items())]
        return type("R", (), {"data": rows})()


class _DB:
    def __init__(self, data):
        self.data, self.calls = data, []

    def table(self, name):
        return _Q(self, name)


_A = {"id": "A_uuid", "spotify_id": "A", "title": "Centuries", "artists": ["Fall Out Boy"], "duration_ms": 1}


def test_alias_li_eski_id_kanonik_sarkiya_cozulur():
    db = _DB({"tracks": [_A], "track_spotify_alias": [{"spotify_id": "B", "track_id": "A_uuid"}]})
    hit = SupabaseTrackRepo(db).find_by_spotify_id("B")
    assert hit is not None and hit["id"] == "A_uuid"


def test_birebir_eslesme_alias_a_hic_bakmaz():
    db = _DB({"tracks": [_A], "track_spotify_alias": [{"spotify_id": "A", "track_id": "baska"}]})
    hit = SupabaseTrackRepo(db).find_by_spotify_id("A")
    assert hit["id"] == "A_uuid"
    assert "track_spotify_alias" not in db.calls


def test_hicbir_yerde_yoksa_none_ve_onbellege_alinir():
    db = _DB({"tracks": [_A], "track_spotify_alias": []})
    repo = SupabaseTrackRepo(db)
    assert repo.find_by_spotify_id("Z") is None
    n = len(db.calls)
    assert repo.find_by_spotify_id("Z") is None
    assert len(db.calls) == n   # ikinci çağrı DB'ye gitmez
