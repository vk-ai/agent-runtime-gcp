"""FastAPI surface for the runtime."""
from __future__ import annotations

import asyncio
import contextlib
from typing import Any, AsyncIterator, Literal

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel

from .config import Settings, build_router
from .persistence import open_persistence
from .approvals import DecisionError
from .service import RunConflict, RunManager, RunNotFound
from .telemetry import configure_from_env, shutdown as telemetry_shutdown


class StartRun(BaseModel):
    user_id: str
    task: str
    run_id: str | None = None


class Decision(BaseModel):
    """Legacy: ``{approved, reviewer}``. New: ``{decision: approve|reject|edit, pending_id, args_digest, edited_args}``."""
    approved: bool | None = None
    decision: Literal["approve", "reject", "edit"] | None = None
    reviewer: str | None = None  # taken from the per-reviewer token when AGENTRT_REVIEWER_TOKENS is set
    comment: str = ""
    pending_id: str | None = None
    args_digest: str | None = None
    edited_args: dict[str, Any] | None = None


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_from_env()
        async with open_persistence(settings.persistence, sqlite_path=settings.sqlite_path,
                                    pg_dsn=settings.pg_dsn) as p:
            app.state.manager = RunManager(p, build_router(settings), stuck_after_s=settings.stuck_after_s,
                                           crash_at=settings.crash_at, approval_ttl_s=settings.approval_ttl_s,
                                           forbid_self_approval=settings.forbid_self_approval)
            stop = asyncio.Event()

            async def watchdog():
                while not stop.is_set():
                    await app.state.manager.refresh_gauges()
                    try:
                        await asyncio.wait_for(stop.wait(), settings.watchdog_interval_s)
                    except asyncio.TimeoutError:
                        pass

            task = asyncio.create_task(watchdog())
            if settings.recover_on_start:
                await app.state.manager.recover_orphans(settings.stuck_after_s)
            yield
            stop.set()
            await task
        telemetry_shutdown()

    app = FastAPI(title="agent-runtime-gcp", version="0.1.0", lifespan=lifespan)

    def mgr() -> RunManager:
        return app.state.manager

    def guard(x_approver_token: str | None = Header(default=None)) -> str | None:
        """Returns the reviewer bound to a per-reviewer token, or None for the shared/no-token modes."""
        if x_approver_token and x_approver_token in settings.reviewer_tokens:
            return settings.reviewer_tokens[x_approver_token]
        if settings.approver_token and x_approver_token == settings.approver_token:
            return None
        if settings.approver_token or settings.reviewer_tokens:
            raise HTTPException(401, "invalid or missing X-Approver-Token")
        return None

    @app.get("/healthz")
    async def healthz():
        return {"ok": True, "persistence": settings.persistence}

    @app.post("/runs", status_code=201)
    async def start(body: StartRun):
        try:
            return await mgr().start(body.user_id, body.task, body.run_id)
        except RunConflict as e:
            raise HTTPException(409, str(e)) from e

    @app.get("/runs/{run_id}")
    async def get_run(run_id: str):
        try:
            return await mgr().view(run_id)
        except RunNotFound as e:
            raise HTTPException(404, str(e)) from e

    @app.post("/runs/{run_id}/decision")
    async def decide(run_id: str, body: Decision, token_reviewer: str | None = Depends(guard)):
        reviewer = body.reviewer
        if token_reviewer is not None:
            if reviewer and reviewer != token_reviewer:
                raise HTTPException(403, f"token belongs to {token_reviewer!r}, not {reviewer!r}")
            reviewer = token_reviewer
        if not reviewer:
            raise HTTPException(422, "reviewer is required (or use a per-reviewer token)")
        if body.approved is None and body.decision is None:
            raise HTTPException(422, "pass 'decision' (approve|reject|edit) or 'approved'")
        if body.decision == "edit" and not body.edited_args:
            raise HTTPException(422, "decision 'edit' needs edited_args")
        try:
            return await mgr().decide(run_id, body.approved, reviewer, body.comment, decision=body.decision,
                                      pending_id=body.pending_id, args_digest=body.args_digest,
                                      edited_args=body.edited_args)
        except DecisionError as e:
            raise HTTPException(e.status_code, str(e)) from e
        except (RunNotFound, RunConflict) as e:
            raise HTTPException(409 if isinstance(e, RunConflict) else 404, str(e)) from e

    @app.get("/runs/{run_id}/decisions")
    async def run_decisions(run_id: str):
        try:
            await mgr()._get(run_id)
        except RunNotFound as e:
            raise HTTPException(404, str(e)) from e
        return await mgr().decisions(run_id)

    @app.get("/decisions")
    async def all_decisions(limit: int = 100):
        return await mgr().decisions(None, max(1, min(limit, 500)))

    @app.post("/runs/{run_id}/resume")
    async def resume(run_id: str):
        try:
            return await mgr().resume(run_id)
        except (RunNotFound, RunConflict) as e:
            raise HTTPException(409 if isinstance(e, RunConflict) else 404, str(e)) from e

    @app.get("/runs")
    async def list_runs(status: str | None = None):
        return [r.__dict__ for r in await mgr().registry.list(status)]

    @app.get("/users/{user_id}/memories")
    async def memories(user_id: str):
        return await mgr().memories(user_id)

    @app.get("/metrics/summary")
    async def metrics_summary():
        return await mgr().refresh_gauges()

    return app

