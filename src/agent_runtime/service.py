"""RunManager: drives LangGraph runs and keeps the run registry + metrics in sync."""
from __future__ import annotations

import asyncio
import os
import time
import uuid
from dataclasses import asdict
from typing import Any

from langgraph.types import Command

from . import telemetry
from .graph import AgentState, build_graph
from .persistence import Persistence
from .providers import FailoverRouter
from .runs import RunRecord, RunRegistry


class RunConflict(Exception):
    """The requested transition is not valid for the run's current status."""


class RunNotFound(KeyError):
    pass


class RunManager:
    def __init__(self, persistence: Persistence, router: FailoverRouter, *, stuck_after_s: float = 120.0,
                 crash_at: str | None = None, fault_hook=None, clock=time.time):
        self.persistence = persistence
        self.router = router
        self.registry = RunRegistry(persistence.store, clock=clock)
        self.instance_id = f"{os.getpid()}-{uuid.uuid4().hex[:6]}"
        self.stuck_after_s = stuck_after_s
        self.crash_at = crash_at
        self.fault_hook = fault_hook
        self.clock = clock
        self._locks: dict[str, asyncio.Lock] = {}
        self.graph = build_graph(router, on_node=self._on_node).compile(
            checkpointer=persistence.checkpointer, store=persistence.store
        )

    # --- hooks -----------------------------------------------------------
    async def _on_node(self, name: str, state: AgentState) -> None:
        run_id = state.get("run_id")
        if run_id:
            await self.registry.heartbeat(run_id, name, self.instance_id)
        if self.crash_at and name == self.crash_at:  # demo: simulate a hard crash (OOM-kill / SIGKILL)
            os._exit(137)
        if self.fault_hook:
            r = self.fault_hook(name, state)
            if hasattr(r, "__await__"):
                await r

    # --- public API --------------------------------------------------------
    @staticmethod
    def _config(run_id: str) -> dict:
        return {"configurable": {"thread_id": run_id}}

    async def start(self, user_id: str, task: str, run_id: str | None = None) -> dict:
        run_id = run_id or uuid.uuid4().hex
        if await self.registry.get(run_id):
            raise RunConflict(f"run {run_id} already exists")
        await self.registry.put(RunRecord(run_id=run_id, user_id=user_id, task=task, owner=self.instance_id))
        telemetry.record_run_started(run_id)
        inp = {"run_id": run_id, "user_id": user_id, "task": task, "cost_usd": 0.0, "tokens": 0}
        return await self._drive(run_id, inp)

    async def decide(self, run_id: str, approved: bool, reviewer: str, comment: str = "") -> dict:
        rec = await self._get(run_id)
        if rec.status != "waiting_approval":
            raise RunConflict(f"run {run_id} is {rec.status}, not waiting_approval")
        await self.registry.update(run_id, status="running", heartbeat_at=self.clock(), owner=self.instance_id)
        cmd = Command(resume={"approved": approved, "reviewer": reviewer, "comment": comment})
        return await self._drive(run_id, cmd)

    async def resume(self, run_id: str) -> dict:
        """Continue a run from its last checkpoint (after a crash or a failed node)."""
        rec = await self._get(run_id)
        if rec.status not in ("running", "failed"):
            raise RunConflict(f"run {run_id} is {rec.status}; only running/failed runs can be resumed")
        snap = await self.graph.aget_state(self._config(run_id))
        if not snap.next:
            raise RunConflict(f"run {run_id} has nothing left to execute")
        await self.registry.update(run_id, status="running", error=None, attempts=rec.attempts + 1,
                                   heartbeat_at=self.clock(), owner=self.instance_id)
        telemetry.record_resume()
        return await self._drive(run_id, None)

    async def recover_orphans(self, older_than_s: float = 0.0) -> list[str]:
        """Resume runs left `running` by a process that died. Called on startup.
        With several replicas, use a heartbeat threshold > the node runtime so
        live runs on other replicas are not taken over."""
        resumed = []
        for rec in await self.registry.stale_running(older_than_s):
            if rec.owner == self.instance_id:
                continue
            try:
                await self.resume(rec.run_id)
                resumed.append(rec.run_id)
            except RunConflict:
                continue
        return resumed

    async def refresh_gauges(self) -> dict:
        stuck = await self.registry.stale_running(self.stuck_after_s)
        waiting = await self.registry.list("waiting_approval")
        telemetry.set_run_gauges(len(stuck), len(waiting))
        return {"stuck": [r.run_id for r in stuck], "waiting_approval": [r.run_id for r in waiting]}

    async def view(self, run_id: str) -> dict:
        rec = await self._get(run_id)
        snap = await self.graph.aget_state(self._config(run_id))
        pending = [i.value for t in snap.tasks for i in t.interrupts]
        vals = snap.values or {}
        return {
            **asdict(rec),
            "next": list(snap.next),
            "pending_approval": pending[0] if pending else None,
            "result": vals.get("result"),
            "plan": vals.get("plan"),
            "action": vals.get("action"),
            "approval": vals.get("approval"),
            "steps": vals.get("steps", []),
            "memories_used": vals.get("memories", []),
            "tokens": vals.get("tokens", 0),
        }

    async def memories(self, user_id: str) -> list[dict]:
        items = await self.persistence.store.asearch(("memories", user_id), limit=100)
        return [{"key": i.key, **i.value} for i in items]

    # --- internals ---------------------------------------------------------
    async def _get(self, run_id: str) -> RunRecord:
        rec = await self.registry.get(run_id)
        if rec is None:
            raise RunNotFound(run_id)
        return rec

    async def _drive(self, run_id: str, inp: Any) -> dict:
        lock = self._locks.setdefault(run_id, asyncio.Lock())
        if lock.locked():
            raise RunConflict(f"run {run_id} is already executing in this process")
        async with lock:
            with telemetry.span("agent.run.segment", run_id=run_id) as sp:
                try:
                    # durability="sync": each checkpoint is committed before the next node
                    # starts, so a hard kill can't lose the last finished step.
                    await self.graph.ainvoke(inp, self._config(run_id), durability="sync")
                except Exception as e:  # node failure, provider exhaustion, ...
                    rec = await self.registry.update(run_id, status="failed", error=f"{type(e).__name__}: {e}",
                                                     finished_at=self.clock())
                    sp.set_attribute("run.status", "failed")
                    snap = await self.graph.aget_state(self._config(run_id))
                    telemetry.record_run_finished("failed", (snap.values or {}).get("cost_usd", 0.0),
                                                  self.clock() - rec.created_at, rec.provider)
                    return await self.view(run_id)
                snap = await self.graph.aget_state(self._config(run_id))
                vals = snap.values or {}
                changes = {"cost_usd": vals.get("cost_usd", 0.0), "provider": vals.get("provider")}
                if snap.next:  # paused at the approval interrupt
                    status = "waiting_approval"
                else:
                    status = vals.get("outcome", "succeeded")
                    changes["finished_at"] = self.clock()
                rec = await self.registry.update(run_id, status=status, heartbeat_at=self.clock(), **changes)
                sp.set_attribute("run.status", status)
                if status != "waiting_approval":
                    telemetry.record_run_finished(status, rec.cost_usd, rec.finished_at - rec.created_at,
                                                  rec.provider)
                return await self.view(run_id)
