"""Run registry kept in the LangGraph long-term store (namespace ("runs",)).

Using the same store as memory means run metadata lives in Postgres alongside
the checkpoints, so a fresh process can find runs that a crashed one left
behind.
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field

from langgraph.store.base import BaseStore

NS = ("runs",)
ACTIVE = ("running",)
TERMINAL = ("succeeded", "failed", "rejected")


@dataclass
class RunRecord:
    run_id: str
    user_id: str
    task: str
    status: str = "running"  # running | waiting_approval | succeeded | failed | rejected
    owner: str = ""
    created_at: float = field(default_factory=time.time)
    heartbeat_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    current_node: str | None = None
    attempts: int = 1
    cost_usd: float = 0.0
    provider: str | None = None
    error: str | None = None


class RunRegistry:
    def __init__(self, store: BaseStore, clock=time.time):
        self.store = store
        self.clock = clock

    async def put(self, rec: RunRecord) -> RunRecord:
        await self.store.aput(NS, rec.run_id, asdict(rec), index=False)
        return rec

    async def get(self, run_id: str) -> RunRecord | None:
        item = await self.store.aget(NS, run_id)
        return RunRecord(**item.value) if item else None

    async def update(self, run_id: str, **changes) -> RunRecord:
        rec = await self.get(run_id)
        if rec is None:
            raise KeyError(run_id)
        for k, v in changes.items():
            setattr(rec, k, v)
        return await self.put(rec)

    async def heartbeat(self, run_id: str, node: str, owner: str) -> None:
        await self.update(run_id, heartbeat_at=self.clock(), current_node=node, owner=owner)

    async def list(self, status: str | None = None, limit: int = 500) -> list[RunRecord]:
        flt = {"status": status} if status else None
        items = await self.store.asearch(NS, filter=flt, limit=limit)
        return sorted((RunRecord(**i.value) for i in items), key=lambda r: r.created_at)

    async def stale_running(self, older_than_s: float) -> list[RunRecord]:
        """Runs still marked `running` whose last heartbeat is older than the
        threshold: either stuck (hung node) or orphaned by a crashed process."""
        cutoff = self.clock() - older_than_s
        return [r for r in await self.list("running") if r.heartbeat_at < cutoff]
