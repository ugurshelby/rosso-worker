"""FAZ 6 — Spotify kabul kuyruğunun doğrulama ucu (mark_access).

Buradaki asıl güvence: admin panelinde görünen durum GERÇEĞİ söylesin.
Admin Spotify Dashboard'a eklediğini sanıp "Eklendi" demiş olabilir; Spotify
hâlâ 403 veriyorsa panel bunu itiraf etmeli.
"""
from app.services.spotify_allowlist import mark_access


class _Client:
    def __init__(self, current_status=None):
        self._current = current_status
        self.upserts: list[dict] = []

    def table(self, name):
        assert name == "spotify_allowlist_requests"
        client = self

        class _Q:
            def select(self, *a, **k): return self
            def eq(self, *a, **k): return self
            def limit(self, *a, **k): return self
            def upsert(self, payload, **k):
                client.upserts.append(payload)
                return self
            def execute(self):
                if client.upserts:
                    return type("R", (), {"data": []})()
                data = [{"status": client._current}] if client._current else []
                return type("R", (), {"data": data})()
        return _Q()


def test_403_kaydi_pendinge_ceker():
    """Admin 'eklendi' demiş ama Spotify 403 veriyor → durum pending'e döner.
    Panel 'Aktif' yalanını sürdürmez."""
    client = _Client(current_status="approved")
    mark_access(client, "u1", granted=False)
    assert client.upserts == [{"user_id": "u1", "status": "pending"}]


def test_basarili_senkron_activee_cikarir():
    client = _Client(current_status="approved")
    mark_access(client, "u1", granted=True)
    assert client.upserts[0]["status"] == "active"
    assert "activated_at" in client.upserts[0]


def test_zaten_dogru_durumda_yazma_yapilmaz():
    """Gereksiz yazım yok — her cron turunda DB'yi dövmeyelim."""
    client = _Client(current_status="active")
    mark_access(client, "u1", granted=True)
    assert client.upserts == []

    client = _Client(current_status="pending")
    mark_access(client, "u1", granted=False)
    assert client.upserts == []


def test_rejected_karari_ezilmez():
    """Admin bilinçli olarak reddetmiş — cron bunu diriltmez."""
    client = _Client(current_status="rejected")
    mark_access(client, "u1", granted=True)
    assert client.upserts == []


def test_kayit_yoksa_olusturulur():
    client = _Client(current_status=None)
    mark_access(client, "u1", granted=True)
    assert client.upserts[0]["status"] == "active"


def test_db_hatasi_senkronu_coktermez():
    """Yan-defter yazılamazsa ana senkron devam etmeli."""
    class _Broken:
        def table(self, name):
            raise RuntimeError("db down")

    mark_access(_Broken(), "u1", granted=True)  # patlamamalı
