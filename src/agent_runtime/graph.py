"""The agent graph, built with the real `langgraph` package.

    START -> recall_memory -> plan -> propose_action --(no action)--> save_memory -> finalize -> END
                                           |
                                           +--(risky action)--> human_approval --approved--> execute_action -> save_memory
                                                                     |
                                                                     +--rejected--> save_memory

`human_approval` calls `langgraph.types.interrupt`, so the run pauses with its
state checkpointed and is resumed later with `Command(resume={...})`.
Every node is checkpointed after it finishes, so a crash mid-run resumes from
the last completed node (see `service.RunManager.resume`).
"""
from __future__ import annotations

import json
import operator
import re
import uuid
from typing import Annotated, Any, Awaitable, Callable, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.store.base import BaseStore
from langgraph.types import interrupt

from . import telemetry
from .providers import FailoverRouter

RISKY_ACTIONS = ("deploy", "delete", "refund", "pay", "email", "send")
_REMEMBER = re.compile(r"remember(?: that)?[:\s]+(.+?)(?:\.|$)", re.IGNORECASE)


class AgentState(TypedDict, total=False):
    run_id: str
    user_id: str
    task: str
    memories: list[str]
    plan: str
    provider: str
    model: str
    cost_usd: Annotated[float, operator.add]
    tokens: Annotated[int, operator.add]
    action: dict | None
    approval: dict | None
    action_result: str | None
    result: str
    outcome: str
    steps: Annotated[list[str], operator.add]


NodeHook = Callable[[str, AgentState], Awaitable[None] | None]


def detect_action(task: str) -> dict | None:
    lowered = task.lower()
    for kw in RISKY_ACTIONS:
        if re.search(rf"\b{kw}\b", lowered):
            m = re.search(rf"\b{kw}\b\s*(.*)", task.strip(), re.IGNORECASE | re.DOTALL)
            target = (m.group(1) if m else "").strip().rstrip(".")[:200]
            return {"type": kw, "description": task.strip()[:200], "args": {"target": target}}
    return None


def build_graph(router: FailoverRouter, on_node: NodeHook | None = None) -> StateGraph:
    """Return an uncompiled StateGraph. `on_node(name, state)` runs before each
    node (used for heartbeats and fault injection)."""

    async def _hook(name: str, state: AgentState) -> None:
        if on_node is not None:
            r = on_node(name, state)
            if hasattr(r, "__await__"):
                await r

    def node(name: str):
        def deco(fn):
            async def wrapped(state: AgentState, *, store: BaseStore):
                with telemetry.span(f"agent.node.{name}", run_id=state.get("run_id")):
                    await _hook(name, state)
                    out = await fn(state, store)
                    out = dict(out or {})
                    out["steps"] = [name]
                    return out

            wrapped.__name__ = name
            return wrapped

        return deco

    @node("recall_memory")
    async def recall_memory(state: AgentState, store: BaseStore):
        items = await store.asearch(("memories", state["user_id"]), limit=20)
        return {"memories": [i.value["text"] for i in items]}

    @node("plan")
    async def plan(state: AgentState, store: BaseStore):
        mem = "\n".join(f"- {m}" for m in state.get("memories") or []) or "- (none)"
        prompt = f"You are a careful agent.\nKnown facts about the user:\n{mem}\nTASK: {state['task']}"
        c = await router.complete(prompt)
        return {
            "plan": c.text,
            "provider": c.provider,
            "model": c.model,
            "cost_usd": c.cost_usd,
            "tokens": c.prompt_tokens + c.completion_tokens,
        }

    @node("propose_action")
    async def propose_action(state: AgentState, store: BaseStore):
        return {"action": detect_action(state["task"])}

    @node("human_approval")
    async def human_approval(state: AgentState, store: BaseStore):
        decision: Any = interrupt(
            {"run_id": state.get("run_id"), "action": state["action"], "plan": state.get("plan")}
        )
        if not isinstance(decision, dict) or "approved" not in decision:
            raise ValueError("resume value must be a dict with an 'approved' key")
        approved = bool(decision["approved"])
        out: dict[str, Any] = {
            "approval": {
                "approved": approved,
                "reviewer": str(decision.get("reviewer", "unknown")),
                "comment": str(decision.get("comment", "")),
                "decision": str(decision.get("decision") or ("approve" if approved else "reject")),
            }
        }
        for k in ("pending_id", "original_args_digest", "approved_args_digest"):
            if decision.get(k):
                out["approval"][k] = decision[k]
        if approved and isinstance(decision.get("edited_args"), dict):
            a = dict(state["action"])
            out["action"] = {**a, "args": dict(decision["edited_args"]), "original_args": a.get("args") or {}}
        return out

    @node("execute_action")
    async def execute_action(state: AgentState, store: BaseStore):
        # Idempotency record: if a crash happens after the side effect but
        # before the checkpoint, the resumed node sees the record and does not
        # repeat the side effect.
        ns = ("actions", state["run_id"])
        done = await store.aget(ns, "executed")
        if done is not None:
            return {"action_result": done.value["result"] + " (deduplicated on resume)"}
        a = state["action"]
        args = a.get("args") or {}
        result = f"simulated {a['type']} executed" + (f" with {json.dumps(args, sort_keys=True)}" if args else "")
        await store.aput(ns, "executed", {"result": result})
        await _hook("execute_action:after_side_effect", state)  # crash window used by tests/demo
        return {"action_result": result}

    @node("save_memory")
    async def save_memory(state: AgentState, store: BaseStore):
        for fact in _REMEMBER.findall(state["task"]):
            key = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{state['user_id']}:{fact.strip().lower()}"))
            await store.aput(("memories", state["user_id"]), key, {"text": fact.strip(), "run_id": state["run_id"]})
        return {}

    @node("finalize")
    async def finalize(state: AgentState, store: BaseStore):
        approval = state.get("approval")
        if state.get("action") and approval and not approval["approved"]:
            outcome = "rejected"
            result = f"Action '{state['action']['type']}' rejected by {approval['reviewer']}. Plan only: {state['plan']}"
        elif state.get("action_result"):
            outcome = "succeeded"
            result = f"{state['plan']} Action: {state['action_result']}."
        else:
            outcome = "succeeded"
            result = state["plan"]
        return {"result": result, "outcome": outcome}

    def after_propose(state: AgentState) -> str:
        return "human_approval" if state.get("action") else "save_memory"

    def after_approval(state: AgentState) -> str:
        return "execute_action" if state["approval"]["approved"] else "save_memory"

    g = StateGraph(AgentState)
    for fn in (recall_memory, plan, propose_action, human_approval, execute_action, save_memory, finalize):
        g.add_node(fn.__name__, fn)
    g.add_edge(START, "recall_memory")
    g.add_edge("recall_memory", "plan")
    g.add_edge("plan", "propose_action")
    g.add_conditional_edges("propose_action", after_propose, ["human_approval", "save_memory"])
    g.add_conditional_edges("human_approval", after_approval, ["execute_action", "save_memory"])
    g.add_edge("execute_action", "save_memory")
    g.add_edge("save_memory", "finalize")
    g.add_edge("finalize", END)
    return g
