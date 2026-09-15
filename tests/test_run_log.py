"""pipeline_runs kayıt servisi testleri."""
from app.services import run_log


class _FakeRPC:
    def execute(self):
        class _R:
            data = "00000000-0000-0000-0000-000000000000"
        return _R()


class _FakeClient:
    def __init__(self):
        self.calls = []
    def rpc(self, name, params):
        self.calls.append((name, params))
        return _FakeRPC()


def test_record_run_calls_rpc_with_all_fields():
    client = _FakeClient()
    run_log.record_run(
        client, "enrichment", job_id=None, outcome="blocked",
        stats={"remaining": 1800}, error=None,
    )
    assert len(client.calls) == 1
    name, params = client.calls[0]
    assert name == "record_run"
    assert params["p_run_type"] == "enrichment"
    assert params["p_outcome"] == "blocked"
    assert params["p_stats"] == {"remaining": 1800}


def test_record_run_swallows_errors():
    class _Boom:
        def rpc(self, *a, **k):
            raise RuntimeError("db down")
    # hata fırlatmamalı — loglama asla ana işi çökertmez
    run_log.record_run(_Boom(), "export", outcome="success")
