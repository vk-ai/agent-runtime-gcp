from fastapi.testclient import TestClient

from agent_runtime.api import create_app
from agent_runtime.config import Settings


def client(**kw):
    s = Settings(persistence="memory", watchdog_interval_s=0.05, **kw)
    return TestClient(create_app(s))


def test_health_and_plain_run():
    with client() as c:
        assert c.get("/healthz").json() == {"ok": True, "persistence": "memory"}
        r = c.post("/runs", json={"user_id": "u1", "task": "summarise the logs", "run_id": "a1"})
        assert r.status_code == 201 and r.json()["status"] == "succeeded"
        assert c.get("/runs/a1").json()["result"].startswith("[summarise the logs]")
        assert [x["run_id"] for x in c.get("/runs", params={"status": "succeeded"}).json()] == ["a1"]


def test_approval_flow_over_http():
    with client() as c:
        r = c.post("/runs", json={"user_id": "u1", "task": "delete the temp bucket", "run_id": "a2"}).json()
        assert r["status"] == "waiting_approval" and r["pending_approval"]["action"]["type"] == "delete"
        assert c.get("/metrics/summary").json()["waiting_approval"] == ["a2"]
        r = c.post("/runs/a2/decision", json={"approved": True, "reviewer": "alice"})
        assert r.status_code == 200 and r.json()["status"] == "succeeded"
        again = c.post("/runs/a2/decision", json={"approved": True, "reviewer": "alice"})
        assert again.status_code == 409


def test_errors():
    with client() as c:
        assert c.get("/runs/nope").status_code == 404
        assert c.post("/runs/nope/resume").status_code == 404
        assert c.post("/runs/nope/decision", json={"approved": True, "reviewer": "x"}).status_code == 404
        c.post("/runs", json={"user_id": "u1", "task": "summarise", "run_id": "dup"})
        assert c.post("/runs", json={"user_id": "u1", "task": "summarise", "run_id": "dup"}).status_code == 409
        assert c.post("/runs/dup/resume").status_code == 409


def test_approver_token_required_when_configured():
    with client(approver_token="s3cret") as c:
        c.post("/runs", json={"user_id": "u1", "task": "pay invoice 7", "run_id": "t1"})
        body = {"approved": False, "reviewer": "eve"}
        assert c.post("/runs/t1/decision", json=body).status_code == 401
        r = c.post("/runs/t1/decision", json=body, headers={"X-Approver-Token": "s3cret"})
        assert r.status_code == 200 and r.json()["status"] == "rejected"


def test_memories_endpoint_and_failover_env():
    with client(fail_providers={"mock-primary": "timeout"}) as c:
        r = c.post("/runs", json={"user_id": "u9", "task": "remember that I am on call"}).json()
        assert r["provider"] == "mock-backup"
        assert [m["text"] for m in c.get("/users/u9/memories").json()] == ["I am on call"]
