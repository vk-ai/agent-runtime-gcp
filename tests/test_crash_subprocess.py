"""A real process crash: the CLI is killed with os._exit(137) inside a node,
then a second process resumes the run from the SQLite checkpoint."""
import json
import os
import subprocess
import sys


def _cli(tmp_path, *args, **env):
    e = {**os.environ, "AGENTRT_PERSISTENCE": "sqlite", "AGENTRT_SQLITE_PATH": str(tmp_path / "rt.sqlite"), **env}
    return subprocess.run([sys.executable, "-m", "agent_runtime.cli", *args], env=e, capture_output=True,
                          text=True, timeout=60)


RESUME = """
import asyncio, json, sys
from agent_runtime.config import Settings, build_router
from agent_runtime.persistence import open_persistence
from agent_runtime.service import RunManager

async def main():
    s = Settings.from_env()
    async with open_persistence("sqlite", sqlite_path=s.sqlite_path) as p:
        m = RunManager(p, build_router(s))
        print(json.dumps({"recovered": await m.recover_orphans(0), "view": await m.view("crash-1")}, default=str))
asyncio.run(main())
"""


def test_killed_process_is_resumed_by_next_process(tmp_path):
    first = _cli(tmp_path, "run", "summarise the incident timeline", "--run-id", "crash-1",
                 AGENTRT_CRASH_AT="propose_action")
    assert first.returncode == 137, first.stderr
    e = {**os.environ, "AGENTRT_SQLITE_PATH": str(tmp_path / "rt.sqlite")}
    second = subprocess.run([sys.executable, "-c", RESUME], env=e, capture_output=True, text=True, timeout=60)
    assert second.returncode == 0, second.stderr
    out = json.loads(second.stdout)
    assert out["recovered"] == ["crash-1"]
    v = out["view"]
    assert v["status"] == "succeeded" and v["attempts"] == 2
    # recall_memory and plan ran in the first process and were NOT repeated.
    assert v["steps"] == ["recall_memory", "plan", "propose_action", "save_memory", "finalize"]
