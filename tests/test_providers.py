import httpx
import pytest

from agent_runtime.providers import (
    AllProvidersFailed, FailoverRouter, MockProvider, OpenAICompatProvider, ProviderError, cost_usd,
)


async def test_mock_is_deterministic():
    p = MockProvider()
    a = await p.complete("TASK: summarise the report")
    b = await p.complete("TASK: summarise the report")
    assert a.text == b.text and "summarise the report" in a.text
    assert a.prompt_tokens == 4 and a.cost_usd > 0


@pytest.mark.parametrize("mode,reason,retryable", [
    ("error", "http_503", True), ("rate_limit", "http_429", True),
    ("timeout", "timeout", True), ("fatal", "http_400", False),
])
async def test_mock_fail_modes(mode, reason, retryable):
    with pytest.raises(ProviderError) as ei:
        await MockProvider(fail_mode=mode).complete("TASK: x")
    assert ei.value.reason == reason and ei.value.retryable is retryable


async def test_flaky_recovers_after_n_calls():
    p = MockProvider(fail_mode="flaky:2")
    for _ in range(2):
        with pytest.raises(ProviderError):
            await p.complete("TASK: x")
    assert (await p.complete("TASK: x")).provider == p.name


def test_cost_table():
    assert cost_usd("mock-small", 1000, 1000) == pytest.approx(0.002)
    assert cost_usd("unknown-model", 1000, 0) == pytest.approx(0.001)


async def test_failover_uses_backup_and_counts(otel):
    r = FailoverRouter([MockProvider("a", fail_mode="error"), MockProvider("b", model="mock-large")])
    c = await r.complete("TASK: x")
    assert c.provider == "b"
    assert otel.total("agent_provider_failovers", served_by="b") == 1
    assert otel.total("agent_provider_failures", provider="a", reason="http_503") == 1
    assert otel.total("agent_llm_tokens", provider="b", kind="prompt") == 2


async def test_non_retryable_error_does_not_fail_over():
    backup = MockProvider("b")
    r = FailoverRouter([MockProvider("a", fail_mode="fatal"), backup])
    with pytest.raises(AllProvidersFailed):
        await r.complete("TASK: x")
    assert backup.calls == 0


async def test_all_fail_raises_with_every_error():
    r = FailoverRouter([MockProvider("a", fail_mode="error"), MockProvider("b", fail_mode="timeout")])
    with pytest.raises(AllProvidersFailed) as ei:
        await r.complete("TASK: x")
    assert [e.reason for e in ei.value.errors] == ["http_503", "timeout"]


async def test_circuit_breaker_opens_and_half_opens():
    now = [0.0]
    primary = MockProvider("a", fail_mode="error")
    r = FailoverRouter([primary, MockProvider("b")], breaker_threshold=2, breaker_cooldown_s=10,
                       clock=lambda: now[0])
    await r.complete("TASK: x")
    await r.complete("TASK: x")
    assert r.breaker_open("a")
    await r.complete("TASK: x")
    assert primary.calls == 2  # skipped while open
    now[0] = 11
    primary.fail_mode = "none"
    assert (await r.complete("TASK: x")).provider == "a"
    assert not r.breaker_open("a")


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_openai_compat_success_parses_usage():
    def handler(req: httpx.Request):
        assert req.url.path == "/v1/chat/completions"
        assert req.headers["authorization"] == "Bearer k"
        return httpx.Response(200, json={
            "model": "gpt-4o-mini",
            "choices": [{"message": {"role": "assistant", "content": "hi"}}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 3},
        })
    p = OpenAICompatProvider("http://x/v1", api_key="k", client=_client(handler))
    c = await p.complete("hello")
    assert (c.text, c.prompt_tokens, c.completion_tokens) == ("hi", 11, 3)


@pytest.mark.parametrize("status,retryable", [(429, True), (502, True), (400, False), (401, False)])
async def test_openai_compat_status_mapping(status, retryable):
    p = OpenAICompatProvider("http://x/v1", client=_client(lambda r: httpx.Response(status, json={})))
    with pytest.raises(ProviderError) as ei:
        await p.complete("hello")
    assert ei.value.reason == f"http_{status}" and ei.value.retryable is retryable


async def test_openai_compat_timeout_is_retryable():
    def handler(req):
        raise httpx.ReadTimeout("slow", request=req)
    p = OpenAICompatProvider("http://x/v1", client=_client(handler))
    with pytest.raises(ProviderError) as ei:
        await p.complete("hello")
    assert ei.value.reason == "timeout" and ei.value.retryable
