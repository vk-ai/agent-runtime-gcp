"""Approve / reject / edit decisions bound to a single-use, expiring pending record."""
import pytest
from fastapi.testclient import TestClient

from agent_runtime.api import create_app
from agent_runtime.approvals import (ApprovalExpired, InvalidEdit, SelfApprovalForbidden, StaleDecision, args_digest,
                                     validate_edit)
from agent_runtime.config import Settings
from agent_runtime.persistence import open_persistence
from agent_runtime.service import RunConflict, RunManager

from conftest import make_router


class Clock:
    def __init__(self):
        self.t = 1_000.0

    def __call__(self):
        return self.t


@pytest.fixture
async def mgr_clock():
    clock = Clock()
    async with open_persistence("memory") as p:
        yield RunManager(p, make_router(), clock=clock, approval_ttl_s=60), clock


def test_digest_covers_type_and_args_only():
    a = {"type": "deploy", "description": "x", "args": {"target": "rev 42 to staging"}}
    assert args_digest(a) == args_digest({**a, "description": "different"})
    assert args_digest(a) != args_digest({**a, "args": {"target": "rev 42 to prod"}})
    assert args_digest(a) != args_digest({**a, "type": "delete"})
    assert args_digest(a).startswith("sha256:")


def test_validate_edit():
    assert validate_edit({"target": "a", "n": 1}, {"target": "b"}) == {"target": "b", "n": 1}
    for bad in ({}, None, {"new_key": "x"}, {"target": {"nested": 1}}):
        with pytest.raises(InvalidEdit):
            validate_edit({"target": "a"}, bad)


async def test_pending_record_and_bound_approve(mgr_clock):
    m, clock = mgr_clock
    v = await m.start("u1", "deploy revision 42 to staging", "p1")
    pd = v["pending_decision"]
    assert pd["args"] == {"target": "revision 42 to staging"} and pd["expires_at"] == 1060.0
    assert pd["args_digest"] == args_digest(v["action"])
    v = await m.decide("p1", reviewer="alice", decision="approve", pending_id=pd["pending_id"], args_digest=pd["args_digest"])
    assert v["status"] == "succeeded" and v["pending_decision"] is None
    assert v["approval"]["decision"] == "approve" and v["approval"]["approved_args_digest"] == pd["args_digest"]
    log = await m.decisions("p1")
    assert [(e["decision"], e["reviewer"]) for e in log] == [("approved", "alice")]
    with pytest.raises(RunConflict):  # single use
        await m.decide("p1", reviewer="alice", decision="approve", pending_id=pd["pending_id"])


async def test_edit_executes_edited_args_and_records_both_digests(mgr_clock):
    m, _ = mgr_clock
    v = await m.start("u1", "deploy revision 42 to prod", "p2")
    pd = v["pending_decision"]
    v = await m.decide("p2", reviewer="bob", decision="edit", pending_id=pd["pending_id"], args_digest=pd["args_digest"],
                       edited_args={"target": "revision 42 to staging"}, comment="prod is frozen")
    assert v["status"] == "succeeded"
    assert v["action"]["args"] == {"target": "revision 42 to staging"}
    assert v["action"]["original_args"] == {"target": "revision 42 to prod"}
    assert 'simulated deploy executed with {"target": "revision 42 to staging"}' in v["result"]
    ap = v["approval"]
    assert ap["decision"] == "edit" and ap["original_args_digest"] == pd["args_digest"]
    assert ap["approved_args_digest"] == args_digest(v["action"]) != pd["args_digest"]
    e = (await m.decisions("p2"))[0]
    assert e["decision"] == "edited" and e["edited_args"] == {"target": "revision 42 to staging"}


async def test_invalid_edit_leaves_run_pending(mgr_clock):
    m, _ = mgr_clock
    await m.start("u1", "refund order 42", "p3")
    with pytest.raises(InvalidEdit):
        await m.decide("p3", reviewer="bob", decision="edit", edited_args={"amount": 1_000_000})
    v = await m.view("p3")
    assert v["status"] == "waiting_approval" and v["pending_decision"] is not None


async def test_stale_pending_id_or_digest_is_refused(mgr_clock):
    m, _ = mgr_clock
    v = await m.start("u1", "delete the temp bucket", "p4")
    with pytest.raises(StaleDecision):
        await m.decide("p4", reviewer="a", decision="approve", pending_id="not-it")
    with pytest.raises(StaleDecision):
        await m.decide("p4", reviewer="a", decision="approve", args_digest="sha256:" + "0" * 64)
    assert (await m.view("p4"))["status"] == "waiting_approval"
    assert (await m.decide("p4", reviewer="a", decision="reject"))["status"] == "rejected"


async def test_expired_pending_rejects_run(mgr_clock):
    m, clock = mgr_clock
    await m.start("u1", "pay invoice 7", "p5")
    clock.t += 61
    with pytest.raises(ApprovalExpired):
        await m.decide("p5", True, "alice")
    v = await m.view("p5")
    assert v["status"] == "rejected" and v["approval"]["decision"] == "expired"
    assert "execute_action" not in v["steps"]
    assert (await m.decisions("p5"))[0]["decision"] == "expired"


async def test_self_approval_can_be_forbidden():
    async with open_persistence("memory") as p:
        m = RunManager(p, make_router(), forbid_self_approval=True)
        await m.start("mallory", "send the payroll file", "p6")
        with pytest.raises(SelfApprovalForbidden):
            await m.decide("p6", True, "mallory")
        assert (await m.decide("p6", True, "alice"))["status"] == "succeeded"


def _client(**kw):
    return TestClient(create_app(Settings(persistence="memory", watchdog_interval_s=0.05, **kw)))


def test_http_edit_with_per_reviewer_tokens_and_decision_log():
    with _client(reviewer_tokens={"tok-alice": "alice", "tok-bob": "bob"}) as c:
        v = c.post("/runs", json={"user_id": "u1", "task": "deploy build 7 to prod", "run_id": "h1"}).json()
        pd = v["pending_decision"]
        body = {"decision": "edit", "pending_id": pd["pending_id"], "args_digest": pd["args_digest"],
                "edited_args": {"target": "build 7 to staging"}}
        assert c.post("/runs/h1/decision", json=body).status_code == 401
        assert c.post("/runs/h1/decision", json={**body, "reviewer": "bob"},
                      headers={"X-Approver-Token": "tok-alice"}).status_code == 403  # token is alice's
        assert c.post("/runs/h1/decision", json={**body, "edited_args": {"evil": 1}},
                      headers={"X-Approver-Token": "tok-alice"}).status_code == 422
        r = c.post("/runs/h1/decision", json=body, headers={"X-Approver-Token": "tok-alice"})
        assert r.status_code == 200 and r.json()["approval"]["reviewer"] == "alice"
        assert c.post("/runs/h1/decision", json=body, headers={"X-Approver-Token": "tok-bob"}).status_code == 409
        log = c.get("/runs/h1/decisions").json()
        assert [(e["decision"], e["reviewer"]) for e in log] == [("edited", "alice")]
        assert c.get("/decisions").json()[0]["run_id"] == "h1"
        assert c.get("/runs/nope/decisions").status_code == 404


def test_http_stale_digest_409_and_expiry_410():
    with _client(approval_ttl_s=0.01) as c:
        c.post("/runs", json={"user_id": "u1", "task": "refund order 9", "run_id": "h2"})
        import time
        time.sleep(0.05)
        r = c.post("/runs/h2/decision", json={"approved": True, "reviewer": "alice"})
        assert r.status_code == 410 and c.get("/runs/h2").json()["status"] == "rejected"
    with _client() as c:
        c.post("/runs", json={"user_id": "u1", "task": "refund order 9", "run_id": "h3"})
        r = c.post("/runs/h3/decision", json={"decision": "approve", "reviewer": "a", "args_digest": "sha256:x"})
        assert r.status_code == 409
        assert c.post("/runs/h3/decision", json={"reviewer": "a"}).status_code == 422
        assert c.post("/runs/h3/decision", json={"decision": "edit", "reviewer": "a"}).status_code == 422


def test_legacy_shared_token_body_still_works():
    with _client(approver_token="s3cret", reviewer_tokens={"tok-alice": "alice"}) as c:
        c.post("/runs", json={"user_id": "u1", "task": "pay invoice 7", "run_id": "h4"})
        r = c.post("/runs/h4/decision", json={"approved": True, "reviewer": "ops"}, headers={"X-Approver-Token": "s3cret"})
        assert r.status_code == 200 and r.json()["status"] == "succeeded"
