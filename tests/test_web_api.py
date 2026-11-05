import time

import pytest
from fastapi.testclient import TestClient

import web_api


@pytest.fixture
def client():
    return TestClient(web_api.app)


def _fake_agent_client(monkeypatch):
    # Stand-in for daemon AgentClient: every RPC succeeds with empty payload.
    class FakeAgentClient:
        def __init__(self, *a, **k):
            pass

        async def call(self, method, params):
            return {"jobs": [{"id": "j1", "expression": "5 9 * * *", "enabled": True}]}

    import agent_runtime.sdk.client as sdk

    monkeypatch.setattr(sdk, "AgentClient", FakeAgentClient)
    # CI runners have no module key (env/file absent) — stub it so authed_call
    # does not raise before reaching the fake client.
    import douyin_tools.rpc_auth as rpc_auth

    monkeypatch.setattr(rpc_auth, "_module_key", lambda: "ci-test-key")
    rpc_auth.reset_token_cache()


def test_health_ok(client, monkeypatch):
    monkeypatch.setattr(web_api, "mlx_health", lambda: {"ok": True, "reason": "ok", "http": 200, "model_count": 3})
    _fake_agent_client(monkeypatch)
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["mlx"]["ok"] is True
    assert body["daemon"]["ok"] is True
    assert "pending" in body["queue"]


def test_health_daemon_down(client, monkeypatch):
    monkeypatch.setattr(web_api, "mlx_health", lambda: {"ok": False, "reason": "x", "http": 0, "model_count": 0})

    import agent_runtime.sdk.client as sdk

    class DownClient:
        def __init__(self, *a, **k):
            pass

        async def call(self, method, params):
            raise OSError("socket missing")

    monkeypatch.setattr(sdk, "AgentClient", DownClient)
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["daemon"]["ok"] is False


def test_queue_counts(client, monkeypatch, tmp_path):
    import douyin_tools.queue_ops as qops

    monkeypatch.setattr(qops, "QUEUE_DIR", str(tmp_path))
    qops._get_store().enqueue({"id": "it1", "topic": "t1"})
    r = client.get("/api/queue")
    assert r.status_code == 200
    body = r.json()
    assert body["counts"]["pending"] == 1
    assert body["pending"][0]["id"] == "it1"


def test_graphs_lists_templates(client):
    r = client.get("/api/graphs")
    assert r.status_code == 200
    graphs = r.json()["graphs"]
    assert any(g["file"] == "douyin_auto_publish.json" for g in graphs)
    assert all(g["nodes"] > 0 for g in graphs)


def test_run_unknown_graph_404(client, monkeypatch):
    monkeypatch.setattr(web_api, "_mlx_api_key", lambda: "")
    r = client.post("/api/run", json={"graph": "nope.json", "approve": False})
    assert r.status_code == 404


def test_run_requires_bearer_when_key_configured(client, monkeypatch):
    monkeypatch.setattr(web_api, "_mlx_api_key", lambda: "secret")

    async def fake_run_once(graph_path, approve=True, extra_vars=None):
        return 1, ""

    import run_graph

    monkeypatch.setattr(run_graph, "run_once", fake_run_once)
    r = client.post("/api/run", json={"graph": "douyin_auto_publish.json"})
    assert r.status_code == 401
    r = client.post("/api/run", json={"graph": "douyin_auto_publish.json"},
                    headers={"Authorization": "Bearer secret"})
    assert r.status_code == 200
    run_id = r.json()["run_id"]
    # Background task uses real run_once (daemon absent) -> mock it out entirely.
    assert web_api._runs[run_id]["graph"] == "douyin_auto_publish.json"


def test_run_completes_with_mocked_runner(client, monkeypatch):
    monkeypatch.setattr(web_api, "_mlx_api_key", lambda: "")

    async def fake_run_once(graph_path, approve=True, extra_vars=None):
        return 0, '{"status":"archived"}'

    import run_graph

    monkeypatch.setattr(run_graph, "run_once", fake_run_once)
    r = client.post("/api/run", json={"graph": "douyin_auto_publish.json", "approve": True})
    assert r.status_code == 200
    run_id = r.json()["run_id"]
    for _ in range(50):
        if web_api._runs[run_id]["status"] not in ("queued", "running"):
            break
        time.sleep(0.05)
    entry = web_api._runs[run_id]
    assert entry["status"] == "completed"
    assert entry["rc"] == 0
    assert "archived" in entry["result"]
    r = client.get(f"/api/runs/{run_id}")
    assert r.status_code == 200
    assert r.json()["status"] == "completed"


def test_scheduler_endpoint(client, monkeypatch):
    _fake_agent_client(monkeypatch)
    r = client.get("/api/scheduler")
    assert r.status_code == 200
    jobs = r.json()["jobs"]
    assert len(jobs) == 1
    assert jobs[0]["expression"] == "5 9 * * *"


def test_scheduler_daemon_unreachable_503(client, monkeypatch):
    import agent_runtime.sdk.client as sdk

    class DownClient:
        def __init__(self, *a, **k):
            pass

        async def call(self, method, params):
            raise OSError("down")

    monkeypatch.setattr(sdk, "AgentClient", DownClient)
    r = client.get("/api/scheduler")
    assert r.status_code == 503
