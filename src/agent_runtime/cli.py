"""`agent-runtime` CLI: serve the API, or run one task headlessly (the demo)."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

from .config import Settings, build_router
from .persistence import open_persistence
from .service import RunManager
from .telemetry import configure_from_env


def _serve(args) -> None:
    import uvicorn

    uvicorn.run("agent_runtime.api:create_app", factory=True, host=args.host, port=args.port)


async def _run(args) -> int:
    configure_from_env()
    s = Settings.from_env()
    s.persistence = args.backend or s.persistence
    async with open_persistence(s.persistence, sqlite_path=s.sqlite_path, pg_dsn=s.pg_dsn) as p:
        mgr = RunManager(p, build_router(s), stuck_after_s=s.stuck_after_s, crash_at=s.crash_at)
        view = await mgr.start(args.user, args.task, args.run_id)
        print(json.dumps(view, indent=2, default=str))
        if view["status"] == "waiting_approval":
            print("\n# paused for human approval; resume with the API:", file=sys.stderr)
            print(f"# curl -X POST localhost:8000/runs/{view['run_id']}/decision "
                  f"-d '{{\"approved\":true,\"reviewer\":\"you\"}}'", file=sys.stderr)
        return 0 if view["status"] in ("succeeded", "waiting_approval") else 1


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="agent-runtime")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="run the FastAPI server")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    s.set_defaults(func=lambda a: _serve(a) or 0)
    r = sub.add_parser("run", help="run one task headlessly")
    r.add_argument("task")
    r.add_argument("--user", default="demo-user")
    r.add_argument("--backend", choices=["memory", "sqlite", "postgres"])
    r.add_argument("--run-id")
    r.set_defaults(func=lambda a: asyncio.run(_run(a)))
    args = p.parse_args(argv)
    sys.exit(args.func(args) or 0)


if __name__ == "__main__":  # pragma: no cover
    main()
