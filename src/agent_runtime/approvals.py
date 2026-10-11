"""Pending approval records: approve / reject / edit, bound to an arguments digest.

When a run pauses at ``human_approval`` the runtime writes a *pending record*
to the LangGraph store (``("approvals", run_id)``):

    {pending_id, args_digest, action, created_at, expires_at, status}

A decision is accepted only while that record is ``pending`` and unexpired.
If the client sends ``pending_id`` / ``args_digest`` (what the reviewer actually
looked at), they must match, so a stale screen can't approve different
arguments. A record is consumed by its first decision (single use). An *edit*
replaces the action's arguments (only existing keys, scalar values) and records
both the original and the approved digest. Every decision is appended to a
decision log (``("decisions",)``).

Single use is enforced by the run's status check plus the record's status,
under a per-process lock. It is not a cross-replica compare-and-swap.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from langgraph.store.base import BaseStore

DECISIONS = ("approve", "reject", "edit")
TERMINAL = ("approved", "rejected", "edited", "expired")
_SCALARS = (str, int, float, bool)


class DecisionError(Exception):
    status_code = 409


class StaleDecision(DecisionError):
    """pending_id or args_digest doesn't match the current pending record."""


class ApprovalExpired(DecisionError):
    status_code = 410


class InvalidEdit(DecisionError):
    status_code = 422


class SelfApprovalForbidden(DecisionError):
    status_code = 403


def args_digest(action: dict | None) -> str:
    """SHA-256 over the canonical JSON of what will execute: the action type and its arguments."""
    body = {"type": (action or {}).get("type"), "args": (action or {}).get("args") or {}}
    return "sha256:" + hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def validate_edit(original: dict, edited: Any) -> dict:
    if not isinstance(edited, dict) or not edited:
        raise InvalidEdit("edited_args must be a non-empty object")
    extra = set(edited) - set(original)
    if extra:
        raise InvalidEdit(f"edited_args may only change existing arguments; unknown: {sorted(extra)}")
    bad = [k for k, v in edited.items() if not isinstance(v, _SCALARS)]
    if bad:
        raise InvalidEdit(f"edited_args values must be strings, numbers or booleans: {sorted(bad)}")
    return {**original, **edited}


class ApprovalStore:
    def __init__(self, store: BaseStore, ttl_s: float, clock):
        self.store, self.ttl_s, self.clock = store, ttl_s, clock

    async def get(self, run_id: str) -> dict | None:
        item = await self.store.aget(("approvals", run_id), "pending")
        return item.value if item else None

    async def open(self, run_id: str, action: dict) -> dict:
        """Create the pending record for this pause (idempotent while one is pending)."""
        cur = await self.get(run_id)
        if cur and cur["status"] == "pending" and cur["args_digest"] == args_digest(action):
            return cur
        now = self.clock()
        rec = {"pending_id": uuid.uuid4().hex, "run_id": run_id, "action": action, "args_digest": args_digest(action),
               "created_at": now, "expires_at": now + self.ttl_s if self.ttl_s > 0 else None, "status": "pending"}
        await self.store.aput(("approvals", run_id), "pending", rec, index=False)
        return rec

    async def close(self, rec: dict, status: str, **extra) -> dict:
        rec = {**rec, "status": status, **extra}
        await self.store.aput(("approvals", rec["run_id"]), "pending", rec, index=False)
        return rec

    async def log(self, entry: dict) -> None:
        await self.store.aput(("decisions",), f"{entry['run_id']}:{entry['pending_id']}", entry, index=False)

    async def decisions(self, run_id: str | None = None, limit: int = 100) -> list[dict]:
        items = await self.store.asearch(("decisions",), filter={"run_id": run_id} if run_id else None, limit=1000)
        return sorted((i.value for i in items), key=lambda e: e["decided_at"], reverse=True)[:limit]
