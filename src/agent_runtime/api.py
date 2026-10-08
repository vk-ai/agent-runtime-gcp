"""FastAPI surface for the runtime."""
from __future__ import annotations

import asyncio
import contextlib
from typing import AsyncIterator

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel

from .config import Settings, build_router
from .persistence import open_persistence
from .service import RunConflict, RunManager, RunNotFound
from .telemetry import configure_from_env, shutdown as telemetry_shutdown


class StartRun(BaseModel):
    user_id: str
    task: str
    run_id: str | None = None


class Decision(BaseModel):
    approved: bool
    reviewer: str
    comment: str = ""


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_from_env()
        async with open_persistence(settings.persistence, sqlite_path=settings.sqlite_path,
                                    pg_dsn=settings.pg_dsn) as p:
            app.state.manager = RunManager(p, build_router(settings), stuck_after_s=settings.stuck_after_s,
                                           crash_at=settings.crash_at)
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

    def guard(x_approver_token: str | None = Header(default=None)) -> None:
        if settings.approver_token and x_approver_token != settings.approver_token:
            raise HTTPException(401, "invalid or missing X-Approver-Token")

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
    async def decide(run_id: str, body: Decision, _: None = Depends(guard)):
        try:
            return await mgr().decide(run_id, body.approved, body.reviewer, body.comment)
        except (RunNotFound, RunConflict) as e:
            raise HTTPException(409 if isinstance(e, RunConflict) else 404, str(e)) from e

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

