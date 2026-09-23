"""export cron stale-job recovery testleri."""
from app.cron.export import recover_stale_jobs


class _Q:
    """update().eq().lt().execute() zinciri — filtreleri ve payload'ı yakalar."""
    def __init__(self, sink):
        self._sink = sink
        self._payload = None
        self._filters = {}
    def update(self, payload):
        self._payload = payload
        return self
    def eq(self, col, val):
        self._filters[("eq", col)] = val
        return self
    def lt(self, col, val):
        self._filters[("lt", col)] = val
        return self
    def execute(self):
        self._sink.append({"payload": self._payload, "filters": self._filters})
        class _R:
            data = []
        return _R()


class _Client:
    def __init__(self):
        self.calls = []
    def table(self, name):
        assert name == "export_jobs"
        return _Q(self.calls)


def test_recover_stale_jobs_queued_yapar():
    client = _Client()
    recover_stale_jobs(client, stale_minutes=15)

    assert len(client.calls) == 1
    call = client.calls[0]
    # processing → queued
    assert call["payload"]["status"] == "queued"
    assert call["filters"][("eq", "status")] == "processing"
    # started_at < (now - 15dk) filtresi var
    assert ("lt", "started_at") in call["filters"]


def test_recover_stale_jobs_eq_filtre_processing():
    """Yalnızca processing durumundaki işlere dokunur (taze/queued'a değil)."""
    client = _Client()
    recover_stale_jobs(client)
    assert client.calls[0]["filters"][("eq", "status")] == "processing"


# ─── main(): başarı yolunda karne + tip yazımı (2026-07-19, FAZ EXPORT-V2) ────

class _Chain:
    """select/update zincirlerinin ikisini de karşılayan fake sorgu nesnesi."""
    def __init__(self, sink):
        self._sink = sink
        self._payload = None
    def select(self, *a, **k): return self
    def order(self, *a, **k): return self
    def limit(self, *a, **k): return self
    def eq(self, *a, **k): return self
    def lt(self, *a, **k): return self
    def update(self, payload):
        self._payload = payload
        return self
    def execute(self):
        if self._payload is not None:
            self._sink.append(self._payload)
            # PostgREST güncellenen satırları döner; atomik sahiplenme buna bakar.
            return type("R", (), {"data": [{"id": "j1"}]})()
        return type("R", (), {"data": [
            {"id": "j1", "user_id": "u1", "file_path": "p", "status": "queued"},
        ]})()


class _CronClient:
    def __init__(self):
        self.updates = []
    def table(self, name):
        assert name == "export_jobs"
        return _Chain(self.updates)


def test_main_yan_veri_isinde_karne_ve_tip_yazar(monkeypatch):
    """account_data job'unda export_type + matched/skipped yazılır; events=0
    olduğu için total_events yan-veri yazım sayısını alır (UI '0 olay' demesin);
    track üretilmediği için genre_pending False kalır."""
    import app.config as cfg
    import app.db as db
    import app.pipeline.export_runner as er
    from app.services import run_log

    client = _CronClient()
    monkeypatch.setattr(db, "get_client", lambda: client)
    monkeypatch.setattr(cfg, "get_settings", lambda: object())
    monkeypatch.setattr(er, "run_one_export", lambda c, s, j: {
        "outcome": "success", "tracks": 0, "events": 0, "podcasts": 0,
        "elapsed_ms": 5, "counts": {"signals_upserted": 3},
        "zip_type": "account_data", "matched_events": 3, "skipped_events": 1,
    })
    monkeypatch.setattr(run_log, "record_run", lambda *a, **k: None)

    from app.cron.export import main
    assert main() == 0

    done = client.updates[-1]
    assert done["status"] == "completed"
    assert done["export_type"] == "account_data"
    assert done["matched_events"] == 3
    assert done["skipped_events"] == 1
    assert done["total_events"] == 3
    assert done["genre_pending"] is False


# ─── Atomik sahiplenme (2026-09-23): iki çalışma aynı işi almasın ────────────

class _KaybedenChain(_Chain):
    """Sahiplenme güncellemesi 0 satır döner — işi başka çalışma aldı."""
    def execute(self):
        if self._payload is not None:
            self._sink.append(self._payload)
            return type("R", (), {"data": []})()
        return super().execute()


class _KaybedenClient(_CronClient):
    def table(self, name):
        assert name == "export_jobs"
        return _KaybedenChain(self.updates)


def test_sahiplenmeyi_kaybeden_calisma_isi_islemez(monkeypatch):
    import app.pipeline.export_runner as er
    from app.cron.export import process_next_export_job

    cagrildi = []
    monkeypatch.setattr(er, "run_one_export", lambda *a, **k: cagrildi.append(1))

    sonuc = process_next_export_job(_KaybedenClient(), object())
    assert sonuc["outcome"] == "empty"
    assert cagrildi == []
