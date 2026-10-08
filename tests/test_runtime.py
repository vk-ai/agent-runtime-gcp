import os
import uuid

import pytest

from agent_runtime.graph import RISKY_ACTIONS, detect_action
from agent_runtime.persistence import open_persistence
from agent_runtime.providers import FailoverRouter, MockProvider
from agent_runtime.service import RunConflict, RunManager, RunNotFound
from conftest import make_router


@pytest.mark.parametrize("task,expected", [
    ("deploy the new revision", "deploy"),
    ("Please DELETE the stale rows", "delete"),
    ("send an email to ops", "email"),
    ("refund order 42", "refund"),
    ("summarise the weekly report", None),
    ("the word deployment is fine", None),
])
def test_action_detection(task, expected):
    a = detect_action(task)
    assert (a["type"] if a else None) == expected
    assert set(RISKY_ACTIONS) >= {"deploy", "delete", "refund", "pay", "email", "send"}


async def test_plain_run_succeeds_and_records_cost(manager, otel):
    v = await manager.start("u1", "summarise the weekly report", "r1")
    assert v["status"] == "succeeded"
    assert v["steps"] == ["recall_memory", "plan", "propose_action", "save_memory", "finalize"]
    assert v["action"] is None and v["provider"] == "mock-primary" and v["cost_usd"] > 0
    assert v["next"] == [] and "weekly report" in v["result"]
    assert otel.total("agent_runs_started") == 1
    assert otel.total("agent_runs_finished", status="succeeded") == 1
    assert "agent.run.segment" in otel.span_names()
    assert "agent.node.plan" in otel.span_names()


async def test_approval_interrupt_then_approve_executes_once(manager, otel):
    v = await manager.start("u1", "deploy the new revision to staging", "r2")
    assert v["status"] == "waiting_approval"
    assert v["next"] == ["human_approval"]
    assert v["pending_approval"]["action"]["type"] == "deploy"
    g = await manager.refresh_gauges()
    assert g["waiting_approval"] == ["r2"]
    assert otel.total("agent_runs_finished") == 0  # not finished while paused

    v = await manager.decide("r2", True, "alice", "looks safe")
    assert v["status"] == "succeeded"
    assert v["approval"]["reviewer"] == "alice"
    assert v["steps"].count("execute_action") == 1
    assert "simulated deploy executed" in v["result"]
    assert otel.total("agent_runs_finished", status="succeeded") == 1


async def test_reject_skips_action(manager, otel):
    await manager.start("u1", "refund order 42", "r3")
    v = await manager.decide("r3", False, "bob", "not verified")
    assert v["status"] == "rejected" and v["approval"]["approved"] is False
    assert "execute_action" not in v["steps"]
    assert "rejected by bob" in v["result"]
    assert otel.total("agent_runs_finished", status="rejected") == 1


async def test_decision_on_wrong_state_conflicts(manager):
    await manager.start("u1", "summarise x", "r4")
    with pytest.raises(RunConflict):
        await manager.decide("r4", True, "alice")
    with pytest.raises(RunNotFound):
        await manager.decide("missing", True, "alice")


async def test_memory_persists_across_runs(manager):
    await manager.start("u1", "Please remember that I prefer bullet points.", "m1")
    v = await manager.start("u1", "write a status update", "m2")
    assert "I prefer bullet points" in v["memories_used"]
    mems = await manager.memories("u1")
    assert [m["text"] for m in mems] == ["I prefer bullet points"]
    # Same fact stored twice dedupes on a stable key.
    await manager.start("u1", "remember that I prefer bullet points", "m3")
    assert len(await manager.memories("u1")) == 1
    assert (await manager.start("u2", "write a status update", "m4"))["memories_used"] == []


async def test_failover_inside_graph(persistence, otel):
    mgr = RunManager(persistence, make_router(primary="error"))
    v = await mgr.start("u1", "summarise x", "f1")
    assert v["status"] == "succeeded" and v["provider"] == "mock-backup"
    assert otel.total("agent_provider_failovers", served_by="mock-backup") == 1


async def test_all_providers_down_marks_run_failed_and_resumable(persistence, otel):
    mgr = RunManager(persistence, make_router("error", "error"))
    v = await mgr.start("u1", "summarise x", "f2")
    assert v["status"] == "failed" and "AllProvidersFailed" in v["error"]
    assert v["next"] == ["plan"]  # checkpointed before the failing node
    assert otel.total("agent_runs_finished", status="failed") == 1
    # Providers recover; resuming continues from the plan node, not from scratch.
    # The router is bound when the graph is built, so a fresh manager (a redeploy) picks up the fix.
    mgr2 = RunManager(persistence, make_router())
    v = await mgr2.resume("f2")
    assert v["status"] == "succeeded"
    assert v["steps"] == ["recall_memory", "plan", "propose_action", "save_memory", "finalize"]
    assert otel.total("agent_runs_resumed") == 1


async def test_crash_recovery_resumes_from_last_checkpoint(persistence, otel):
    """Process A dies mid-run (simulated by raising inside a node, which is what
    an unhandled error looks like). Process B, sharing the same store and
    checkpointer, recovers it on startup."""
    def boom(name, state):
        if name == "plan":
            raise RuntimeError("worker killed")
    a = RunManager(persistence, make_router(), fault_hook=boom)
    v = await a.start("u1", "summarise the outage", "c1")
    assert v["status"] == "failed" and v["next"] == ["plan"]

    b = RunManager(persistence, make_router())
    recovered = await b.recover_orphans(older_than_s=0)
    assert recovered == []  # failed runs are not auto-resumed (a human/API resumes those)

    # A hard crash leaves the run as `running` with a stale heartbeat.
    await b.registry.update("c1", status="running", heartbeat_at=0, owner="dead-process")
    recovered = await b.recover_orphans(older_than_s=0)
    assert recovered == ["c1"]
    assert (await b.view("c1"))["status"] == "succeeded"


async def test_stuck_gauge(persistence, otel):
    now = [1000.0]
    mgr = RunManager(persistence, make_router(), stuck_after_s=30, clock=lambda: now[0])
    await mgr.start("u1", "summarise x", "s1")  # finished: not stuck
    await mgr.registry.update("s1", status="running", heartbeat_at=now[0])
    now[0] += 31
    g = await mgr.refresh_gauges()
    assert g["stuck"] == ["s1"]
    assert otel.total("agent_runs_stuck") == 1


async def test_action_is_not_executed_twice_on_resume(persistence):
    """Crash after the side effect but before execute_action's checkpoint:
    the resumed node re-runs, finds the idempotency record, and skips it."""
    def boom(name, state):
        if name == "execute_action:after_side_effect":
            raise RuntimeError("died after side effect")
    a = RunManager(persistence, make_router(), fault_hook=boom)
    v = await a.start("u1", "deploy service", "d1")
    assert v["status"] == "waiting_approval"
    v = await a.decide("d1", True, "alice")
    assert v["status"] == "failed" and v["next"] == ["execute_action"]
    b = RunManager(persistence, make_router())
    v = await b.resume("d1")
    assert v["status"] == "succeeded"
    assert "deduplicated on resume" in v["result"]
    assert v["steps"].count("execute_action") == 1


async def test_sqlite_backend_survives_new_process(tmp_path, otel):
    db = str(tmp_path / "rt.sqlite")
    async with open_persistence("sqlite", sqlite_path=db) as p:
        await RunManager(p, make_router()).start("u1", "remember that my team is platform", "q1")
    async with open_persistence("sqlite", sqlite_path=db) as p:
        v = await RunManager(p, make_router()).start("u1", "who am I", "q2")
    assert "my team is platform" in v["memories_used"]


@pytest.mark.postgres
async def test_postgres_checkpoint_and_memory(otel):
    dsn = os.environ["AGENTRT_PG_DSN"]
    sfx = uuid.uuid4().hex[:8]
    user, r1, r2, r3 = f"u-{sfx}", f"pg1-{sfx}", f"pg2-{sfx}", f"pg3-{sfx}"
    async with open_persistence("postgres", pg_dsn=dsn) as p:
        a = RunManager(p, make_router())
        v = await a.start(user, "deploy the canary", r1)
        assert v["status"] == "waiting_approval"
    # A brand-new pool (a restarted process) sees the same checkpoint.
    async with open_persistence("postgres", pg_dsn=dsn) as p:
        b = RunManager(p, make_router())
        v = await b.view(r1)
        assert v["status"] == "waiting_approval" and v["next"] == ["human_approval"]
        v = await b.decide(r1, True, "carol")
        assert v["status"] == "succeeded"
        await b.start(user, "remember that deploys need carol", r2)
    async with open_persistence("postgres", pg_dsn=dsn) as p:
        v = await RunManager(p, make_router()).start(user, "status please", r3)
    assert "deploys need carol" in v["memories_used"]


def test_router_builder_uses_env(monkeypatch):
    from agent_runtime.config import Settings
    monkeypatch.setenv("AGENTRT_FAIL_PROVIDERS", "mock-primary=rate_limit")
    s = Settings.from_env()
    assert s.fail_providers == {"mock-primary": "rate_limit"}
    r = __import__("agent_runtime.config", fromlist=["build_router"]).build_router(s)
    assert isinstance(r, FailoverRouter) and r.providers[0].fail_mode == "rate_limit"
    assert all(isinstance(p, MockProvider) for p in r.providers)
