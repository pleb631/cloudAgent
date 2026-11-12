"""Compaction is model work too: it must not bypass shared resource limits."""

import asyncio

import pytest
from langchain_core.messages import AIMessageChunk
from test_runtime import Model, Registry, Sandbox, config

from cloudagent.runtime import AgentRuntime, ResourceLimitError


def long_history():
    return [
        {"role": "user" if i % 2 == 0 else "assistant", "content": "old " * 1000} for i in range(6)
    ] + [{"role": "user", "content": "now"}]


def summary_model():
    model = Model(
        [
            [
                AIMessageChunk(
                    content="PRIVATE SUMMARY",
                    usage_metadata={"input_tokens": 10, "output_tokens": 2, "total_tokens": 12},
                )
            ],
            [AIMessageChunk(content="Answer")],
        ]
    )
    model.profile = {"max_input_tokens": 1000}
    return model


async def test_automatic_summary_uses_call_budget_without_exposing_summary_tokens():
    model = summary_model()
    runtime = AgentRuntime(
        Registry(config()),
        Sandbox(),
        model_factory=lambda c: model,
        max_model_calls_per_run=2,
        max_concurrent_model_calls=1,
        max_model_calls_per_tenant=1,
    )
    async with asyncio.timeout(2):
        events = [event async for event in runtime.stream(long_history(), "main", "t", "s")]
    assert len(model.histories) == 2
    assert [event["text"] for event in events if event["type"] == "token"] == ["Answer"]
    assert sum(event.get("prompt_tokens", 0) for event in events) == 10
    assert sum(event.get("completion_tokens", 0) for event in events) == 2
    assert runtime.model_limits.active == 0


async def test_summary_consumes_root_budget_before_normal_call():
    model = summary_model()
    runtime = AgentRuntime(
        Registry(config()), Sandbox(), model_factory=lambda c: model, max_model_calls_per_run=1
    )
    with pytest.raises(ResourceLimitError, match="model call budget"):
        async with asyncio.timeout(2):
            _ = [event async for event in runtime.stream(long_history(), "main", "t", "s")]
    assert len(model.histories) == 1
    assert "Context Extraction Assistant" in model.histories[0][0].content
    assert runtime.model_limits.active == 0


async def test_summary_waits_for_shared_tenant_admission_and_can_be_cancelled():
    model = summary_model()
    runtime = AgentRuntime(
        Registry(config()),
        Sandbox(),
        model_factory=lambda c: model,
        max_concurrent_model_calls=2,
        max_model_calls_per_tenant=1,
    )

    async def consume():
        return [event async for event in runtime.stream(long_history(), "main", "t", "s")]

    async with runtime.model_limits.slot("t"):
        run = asyncio.create_task(consume())
        async with asyncio.timeout(2):
            while not runtime.model_limits.waiters:  # noqa: ASYNC110 - inspect admission state
                await asyncio.sleep(0)
        assert not model.histories
        run.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(run, 2)
        assert runtime.model_limits.active == 1
        assert not runtime.model_limits.waiters
    assert runtime.model_limits.active == 0
    assert not runtime.model_limits.tenants


@pytest.mark.parametrize("summarizing", [False, True])
async def test_model_timeout_then_disconnect_does_not_interrupt_provider_cleanup(summarizing):
    cleaning, finish_cleanup, closed = (asyncio.Event() for _ in range(3))

    class SlowCleanup(Model):
        async def _astream(self, messages, **kwargs):
            try:
                await asyncio.Event().wait()
                yield  # pragma: no cover - unreachable, declares async generator
            finally:
                cleaning.set()
                await finish_cleanup.wait()
                closed.set()

    model = SlowCleanup([])
    if summarizing:
        model.profile = {"max_input_tokens": 1000}
    runtime = AgentRuntime(
        Registry(config()), Sandbox(), model_factory=lambda c: model, request_timeout=0.03
    )

    async def consume():
        messages = long_history() if summarizing else [{"role": "user", "content": "go"}]
        return [event async for event in runtime.stream(messages, "main", "t", "s")]

    run = asyncio.create_task(consume())
    await asyncio.wait_for(cleaning.wait(), 2)
    run.cancel()
    await asyncio.sleep(0.02)
    assert not closed.is_set()
    assert not run.done()
    assert runtime.model_limits.active == 1
    finish_cleanup.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(run, 2)
    assert closed.is_set()
    assert runtime.model_limits.active == 0
    assert not runtime.model_limits.tenants
