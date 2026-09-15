"""account_purge_runner — soft-delete süresi dolan hesapların otomatik silinmesi.

Kritik davranışlar (admin purge route ile aynı kapılar):
  - Yalnız deleted_at DOLU + 30 gün geçmiş hesaplar aday (filtre doğru mu).
  - Aday yoksa 'empty' (normal durum — hiçbir şey silinmez).
  - Storage temizliği CASCADE silmeyi ENGELLEMEZ (storage hatası yutulur).
  - Bir kullanıcının silinmesi hata verse bile diğerleri denenebilir ('partial').
"""
from app.pipeline import account_purge_runner as runner


class _StorageBucket:
    def __init__(self, files_by_user, fail_remove=False):
        self._files = files_by_user      # {user_id: [{"name": ...}, ...]}
        self.fail_remove = fail_remove
        self.removed = []                # temizlenen path listeleri
    def list(self, user_id):
        return self._files.get(user_id, [])
    def remove(self, paths):
        if self.fail_remove:
            raise RuntimeError("storage down")
        self.removed.append(paths)
        return {}


class _Storage:
    def __init__(self, bucket):
        self._bucket = bucket
    def from_(self, name):
        return self._bucket


class _AuthAdmin:
    def __init__(self, fail_for=None):
        self.fail_for = fail_for or set()   # bu user_id'lerde delete patlar
        self.deleted = []
    def delete_user(self, user_id):
        if user_id in self.fail_for:
            raise RuntimeError("delete failed")
        self.deleted.append(user_id)
        return {}


class _Auth:
    def __init__(self, admin):
        self.admin = admin


class _Query:
    """social_profiles select zinciri — filtreleri kaydeder, adayları döndürür."""
    def __init__(self, rows, recorder):
        self._rows = rows
        self._rec = recorder
    def select(self, *a, **k):
        return self
    @property
    def not_(self):
        self._rec["not_"] = True
        return self
    def is_(self, col, val):
        self._rec["is_"] = (col, val)
        return self
    def lt(self, col, val):
        self._rec["lt"] = (col, val)
        return self
    def order(self, *a, **k):
        return self
    def limit(self, n):
        self._rec["limit"] = n
        return self
    def execute(self):
        return type("R", (), {"data": self._rows})()


class _Client:
    def __init__(self, rows, *, files=None, fail_delete=None, fail_remove=False, query_raises=False):
        self._rows = rows
        self.query_rec = {}
        self._query_raises = query_raises
        self.auth = _Auth(_AuthAdmin(fail_for=fail_delete))
        self._bucket = _StorageBucket(files or {}, fail_remove=fail_remove)
        self.storage = _Storage(self._bucket)
    def table(self, name):
        assert name == "social_profiles"
        if self._query_raises:
            class _Boom:
                def select(self, *a, **k): return self
                @property
                def not_(self): return self
                def is_(self, *a, **k): return self
                def lt(self, *a, **k): return self
                def order(self, *a, **k): return self
                def limit(self, *a, **k): return self
                def execute(self): raise RuntimeError("db down")
            return _Boom()
        return _Query(self._rows, self.query_rec)


def _row(user_id, days_ago):
    from datetime import datetime, timedelta, timezone
    ts = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    return {"user_id": user_id, "deleted_at": ts}


def test_aday_yoksa_empty():
    client = _Client([])
    result = runner.run_account_purge(client)
    assert result["outcome"] == "empty"
    assert result["purged"] == 0
    # Filtre gerçekten uygulandı mı: deleted_at not null + lt cutoff
    assert client.query_rec.get("is_") == ("deleted_at", "null")
    assert client.query_rec.get("lt", (None,))[0] == "deleted_at"


def test_suresi_dolan_hesap_silinir():
    client = _Client([_row("u1", 40), _row("u2", 35)])
    result = runner.run_account_purge(client)
    assert result["outcome"] == "success"
    assert result["purged"] == 2
    assert result["failed"] == 0
    assert set(client.auth.admin.deleted) == {"u1", "u2"}


def test_storage_dosyalari_temizlenir():
    client = _Client(
        [_row("u1", 40)],
        files={"u1": [{"name": "avatar.jpg"}, {"name": "old.png"}]},
    )
    result = runner.run_account_purge(client)
    assert result["purged"] == 1
    assert result["storage_files"] == 2
    assert client._bucket.removed == [["u1/avatar.jpg", "u1/old.png"]]


def test_storage_hatasi_silmeyi_engellemez():
    """Storage remove patlarsa bile auth.users silinmeli (yetim dosya < silinememe)."""
    client = _Client(
        [_row("u1", 40)],
        files={"u1": [{"name": "avatar.jpg"}]},
        fail_remove=True,
    )
    result = runner.run_account_purge(client)
    assert result["purged"] == 1                 # silme yine oldu
    assert result["storage_files"] == 0          # storage temizlenemedi ama yutuldu
    assert client.auth.admin.deleted == ["u1"]


def test_bir_silme_patlarsa_digerleri_denenir():
    """Bir kullanıcının silmesi hata verse de diğeri silinir → 'partial'."""
    client = _Client([_row("u1", 40), _row("u2", 40)], fail_delete={"u1"})
    result = runner.run_account_purge(client)
    assert result["outcome"] == "partial"
    assert result["purged"] == 1
    assert result["failed"] == 1
    assert client.auth.admin.deleted == ["u2"]   # u1 patladı, u2 silindi


def test_aday_sorgusu_patlarsa_error():
    client = _Client([], query_raises=True)
    result = runner.run_account_purge(client)
    assert result["outcome"] == "error"
    assert result["purged"] == 0
