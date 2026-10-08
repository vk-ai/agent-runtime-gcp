"""End-to-end demo: failover, approval interrupt, memory, a real process crash
+ resume, and the OpenTelemetry metrics it produced. Offline; uses SQLite.

    python demo/run_demo.py
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile

from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from agent_runtime import telemetry
from agent_runtime.config import Settings, build_router
from agent_runtime.persistence import open_persistence
from agent_runtime.service import RunManager


def show(title, v):
    print(f"\n== {title}")
    print(f"   status={v['status']} provider={v['provider']} cost_usd={v['cost_usd']:.6f} attempts={v['attempts']}")
    print(f"   steps={v['steps']}")
    if v.get("pending_approval"):
        print(f"   pending_approval={v['pending_approval']['action']}")
    if v.get("memories_used"):
        print(f"   memories_used={v['memories_used']}")
    if v.get("result"):
        print(f"   result={v['result'][:110]}")


async def main() -> None:
    reader = InMemoryMetricReader()
    telemetry.configure(MeterProvider(metric_readers=[reader]))
    db = os.path.join(tempfile.mkdtemp(), "demo.sqlite")
    s = Settings(persistence="sqlite", sqlite_path=db, fail_providers={"mock-primary": "rate_limit"})

    async with open_persistence("sqlite", sqlite_path=db) as p:
        mgr = RunManager(p, build_router(s))
        show("1. primary provider returns 429 -> failover to backup",
             await mgr.start("vinay", "Summarise yesterday's error logs. Remember that I prefer bullet points.",
                             "run-1"))
        show("2. risky action pauses at the human-approval interrupt",
             await mgr.start("vinay", "Deploy revision 42 to staging", "run-2"))
        show("3. approve via the same API the HTTP endpoint uses",
             await mgr.decide("run-2", True, "reviewer@example.com", "staging only"))
        show("4. long-term memory recalled in a later run", await mgr.start("vinay", "Draft the weekly update", "run-3"))

    print("\n== 5. hard crash: a separate process is killed (os._exit 137) inside `propose_action`")
    env = {**os.environ, "AGENTRT_PERSISTENCE": "sqlite", "AGENTRT_SQLITE_PATH": db, "AGENTRT_CRASH_AT": "propose_action"}
    r = subprocess.run([sys.executable, "-m", "agent_runtime.cli", "run", "Summarise the incident", "--run-id",
                        "run-4", "--user", "vinay"], env=env, capture_output=True, text=True)
    print(f"   child exit code: {r.returncode}")
    async with open_persistence("sqlite", sqlite_path=db) as p:
        mgr = RunManager(p, build_router(Settings()))
        print(f"   new process recovered: {await mgr.recover_orphans(0)}")
        show("   run-4 after recovery (recall_memory/plan were not re-run)", await mgr.view("run-4"))
        await mgr.refresh_gauges()

    print("\n== 6. OpenTelemetry metrics recorded in this process")
    for rm in reader.get_metrics_data().resource_metrics:
        for sm in rm.scope_metrics:
            for m in sm.metrics:
                for dp in m.data.data_points:
                    val = getattr(dp, "value", None)
                    if val is None:
                        val = f"count={dp.count} sum={dp.sum:.6f}"
                    attrs = ",".join(f"{k}={v}" for k, v in sorted(dp.attributes.items()))
                    print(f"   {m.name}{{{attrs}}} {val}")


if __name__ == "__main__":
    asyncio.run(main())
