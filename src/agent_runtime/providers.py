"""LLM providers and a failover router.

`MockProvider` is deterministic and offline (used by tests, CI and the demo).
`OpenAICompatProvider` talks to any OpenAI-compatible `/v1/chat/completions`
endpoint (OpenAI, vLLM, Ollama, the vk-ai inference stub, ...) and is only
used when `OPENAI_BASE_URL` / `OPENAI_API_KEY` are configured.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Protocol

import httpx

from . import telemetry

# USD per 1K tokens (input, output). Illustrative numbers, not a price list.
PRICES_PER_1K: dict[str, tuple[float, float]] = {
    "mock-small": (0.0005, 0.0015),
    "mock-large": (0.003, 0.015),
    "gpt-4o-mini": (0.00015, 0.0006),
}
DEFAULT_PRICE = (0.001, 0.002)


def cost_usd(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    pin, pout = PRICES_PER_1K.get(model, DEFAULT_PRICE)
    return round(prompt_tokens / 1000 * pin + completion_tokens / 1000 * pout, 8)


@dataclass
class Completion:
    text: str
    provider: str
    model: str
    prompt_tokens: int
    completion_tokens: int

    @property
    def cost_usd(self) -> float:
        return cost_usd(self.model, self.prompt_tokens, self.completion_tokens)


class ProviderError(Exception):
    """A provider call failed. `retryable` errors trigger failover."""

    def __init__(self, provider: str, reason: str, retryable: bool = True):
        super().__init__(f"{provider}: {reason}")
        self.provider = provider
        self.reason = reason
        self.retryable = retryable


class AllProvidersFailed(Exception):
    def __init__(self, errors: list[ProviderError]):
        super().__init__("all providers failed: " + "; ".join(str(e) for e in errors))
        self.errors = errors


class Provider(Protocol):
    name: str
    model: str

    async def complete(self, prompt: str) -> Completion: ...


def _tokens(text: str) -> int:
    # Rough whitespace token count; good enough for cost accounting in a demo.
    return max(1, len(text.split()))


@dataclass
class MockProvider:
    """Deterministic offline provider.

    fail_mode: "none" | "error" (HTTP 503-like) | "rate_limit" | "timeout" |
    "fatal" (non-retryable 400) | "flaky:N" (fail the first N calls, then succeed).
    """

    name: str = "mock-primary"
    model: str = "mock-small"
    fail_mode: str = "none"
    latency_s: float = 0.0
    calls: int = 0

    async def complete(self, prompt: str) -> Completion:
        self.calls += 1
        if self.latency_s:
            await asyncio.sleep(self.latency_s)
        mode = self.fail_mode
        if mode.startswith("flaky:"):
            mode = "error" if self.calls <= int(mode.split(":", 1)[1]) else "none"
        if mode == "error":
            raise ProviderError(self.name, "http_503")
        if mode == "rate_limit":
            raise ProviderError(self.name, "http_429")
        if mode == "timeout":
            raise ProviderError(self.name, "timeout")
        if mode == "fatal":
            raise ProviderError(self.name, "http_400", retryable=False)
        text = self._answer(prompt)
        return Completion(text, self.name, self.model, _tokens(prompt), _tokens(text))

    @staticmethod
    def _answer(prompt: str) -> str:
        task = prompt.rsplit("TASK:", 1)[-1].strip()
        first = task.splitlines()[0] if task else ""
        return f"[{first[:80]}] Plan: 1) gather context 2) draft result 3) verify."


@dataclass
class OpenAICompatProvider:
    base_url: str
    api_key: str = ""
    model: str = "gpt-4o-mini"
    name: str = "openai-compat"
    timeout_s: float = 20.0
    client: httpx.AsyncClient | None = None

    async def complete(self, prompt: str) -> Completion:
        url = self.base_url.rstrip("/") + "/chat/completions"
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        body = {"model": self.model, "messages": [{"role": "user", "content": prompt}]}
        client = self.client or httpx.AsyncClient(timeout=self.timeout_s)
        try:
            resp = await client.post(url, json=body, headers=headers)
        except httpx.TimeoutException as e:
            raise ProviderError(self.name, "timeout") from e
        except httpx.HTTPError as e:
            raise ProviderError(self.name, f"transport:{type(e).__name__}") from e
        finally:
            if self.client is None:
                await client.aclose()
        if resp.status_code == 429 or resp.status_code >= 500:
            raise ProviderError(self.name, f"http_{resp.status_code}")
        if resp.status_code >= 400:
            raise ProviderError(self.name, f"http_{resp.status_code}", retryable=False)
        data = resp.json()
        usage = data.get("usage") or {}
        text = data["choices"][0]["message"]["content"] or ""
        return Completion(
            text,
            self.name,
            data.get("model", self.model),
            int(usage.get("prompt_tokens", _tokens(prompt))),
            int(usage.get("completion_tokens", _tokens(text))),
        )


@dataclass
class _Breaker:
    failures: int = 0
    open_until: float = 0.0


@dataclass
class FailoverRouter:
    """Try providers in order; skip ones whose circuit breaker is open.

    A provider's breaker opens after `breaker_threshold` consecutive retryable
    failures and stays open for `breaker_cooldown_s`.
    """

    providers: list[Provider]
    breaker_threshold: int = 3
    breaker_cooldown_s: float = 30.0
    clock: callable = time.monotonic
    _breakers: dict[str, _Breaker] = field(default_factory=dict)

    def breaker_open(self, name: str) -> bool:
        b = self._breakers.get(name)
        return bool(b and b.open_until > self.clock())

    async def complete(self, prompt: str) -> Completion:
        errors: list[ProviderError] = []
        for idx, p in enumerate(self.providers):
            if self.breaker_open(p.name):
                errors.append(ProviderError(p.name, "circuit_open"))
                continue
            try:
                result = await p.complete(prompt)
            except ProviderError as e:
                errors.append(e)
                telemetry.record_provider_failure(p.name, e.reason)
                if not e.retryable:
                    raise AllProvidersFailed(errors) from e
                b = self._breakers.setdefault(p.name, _Breaker())
                b.failures += 1
                if b.failures >= self.breaker_threshold:
                    b.open_until = self.clock() + self.breaker_cooldown_s
                continue
            self._breakers.pop(p.name, None)
            if idx > 0:
                telemetry.record_failover(self.providers[0].name, p.name)
            telemetry.record_tokens(p.name, result.model, result.prompt_tokens, result.completion_tokens)
            return result
        raise AllProvidersFailed(errors)
