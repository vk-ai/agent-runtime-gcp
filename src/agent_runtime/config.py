"""Settings from environment variables."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field


def _csv(v: str) -> list[str]:
    return [x.strip() for x in v.split(",") if x.strip()]


@dataclass
class Settings:
    persistence: str = "memory"  # memory | sqlite | postgres
    sqlite_path: str = "agent_runtime.sqlite"
    pg_dsn: str | None = None
    # Mock providers in failover order; AGENTRT_FAIL_PROVIDERS injects faults, e.g. "mock-primary=error".
    mock_providers: list[str] = field(default_factory=lambda: ["mock-primary", "mock-backup"])
    fail_providers: dict[str, str] = field(default_factory=dict)
    openai_base_url: str | None = None
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    stuck_after_s: float = 120.0
    recover_on_start: bool = True
    watchdog_interval_s: float = 15.0
    approver_token: str | None = None
    # Per-reviewer tokens {token: reviewer_id}. The decision's reviewer is taken from the token.
    reviewer_tokens: dict[str, str] = field(default_factory=dict)
    approval_ttl_s: float = 3600.0  # pending approvals expire after this (0 = never)
    forbid_self_approval: bool = False  # reject decisions where reviewer == the run's user_id
    crash_at: str | None = None  # demo only: os._exit() when this node starts

    @classmethod
    def from_env(cls) -> "Settings":
        e = os.environ
        fails = {}
        for item in _csv(e.get("AGENTRT_FAIL_PROVIDERS", "")):
            name, _, mode = item.partition("=")
            fails[name] = mode or "error"
        return cls(
            persistence=e.get("AGENTRT_PERSISTENCE", "memory"),
            sqlite_path=e.get("AGENTRT_SQLITE_PATH", "agent_runtime.sqlite"),
            pg_dsn=e.get("AGENTRT_PG_DSN"),
            mock_providers=_csv(e.get("AGENTRT_MOCK_PROVIDERS", "mock-primary,mock-backup")),
            fail_providers=fails,
            openai_base_url=e.get("OPENAI_BASE_URL"),
            openai_api_key=e.get("OPENAI_API_KEY", ""),
            openai_model=e.get("OPENAI_MODEL", "gpt-4o-mini"),
            stuck_after_s=float(e.get("AGENTRT_STUCK_AFTER_S", "120")),
            recover_on_start=e.get("AGENTRT_RECOVER_ON_START", "1") not in ("0", "false", "no"),
            watchdog_interval_s=float(e.get("AGENTRT_WATCHDOG_INTERVAL_S", "15")),
            approver_token=e.get("AGENTRT_APPROVER_TOKEN"),
            reviewer_tokens=json.loads(e.get("AGENTRT_REVIEWER_TOKENS") or "{}"),
            approval_ttl_s=float(e.get("AGENTRT_APPROVAL_TTL_S", "3600")),
            forbid_self_approval=e.get("AGENTRT_FORBID_SELF_APPROVAL", "0") in ("1", "true", "yes"),
            crash_at=e.get("AGENTRT_CRASH_AT"),
        )


def build_router(s: Settings):
    from .providers import FailoverRouter, MockProvider, OpenAICompatProvider

    providers = []
    if s.openai_base_url:
        providers.append(OpenAICompatProvider(base_url=s.openai_base_url, api_key=s.openai_api_key,
                                              model=s.openai_model))
    for i, name in enumerate(s.mock_providers):
        providers.append(MockProvider(name=name, model="mock-small" if i == 0 else "mock-large",
                                      fail_mode=s.fail_providers.get(name, "none")))
    return FailoverRouter(providers)
