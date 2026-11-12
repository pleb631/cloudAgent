"""Resource bounds must cover actual provider calls across the whole run tree."""

import asyncio
import json
from collections import Counter
from contextlib import suppress
from typing import Any

import pytest
from langchain_core.messages import AIMessageChunk
from langchain_core.outputs import ChatGenerationChunk
from test_runtime import Model, Registry, Sandbox, config, tool

from cloudagent.runtime import AgentRuntime, ProviderError, ResourceLimitError
from cloudagent.runtime.engine import RunBudget


async def eventually(predicate):
    async with asyncio.timeout(5):
        while not predicate():  # noqa: ASYNC110 - observes runtime-owned counters, not a test event
            await asyncio.sleep(0.001)


async def collect(runtime, agent="main", tenant="tenant", session="session"):
    return [
        event
        async for event in runtime.stream(
            [{"role": "user", "content": "hello"}], agent, tenant, session
        )
    ]


def fanout(child, count=16):
    return [
        AIMessageChunk(
            content="",
            tool_call_chunks=[
                {
                    "name": "task",
                    "args": json.dumps({"subagent_type": child, "description": "work"}),
                    "id": f"call-{i}",
                    "index": i,
                }
                for i in range(count)
            ],
        )
    ]


class Probe:
    def __init__(self):
        self.active = 0
        self.peak = 0
        self.calls = Counter()
        self.by_label = Counter()
        self.peaks = Counter()
        self.gate = None
        self.block_label = None
        self.fail = False


class ProbedModel(Model):
    probe: Any = None
    label: str = ""

    def __init__(self, responses, probe, label):
        super().__init__(responses)
        self.probe, self.label = probe, label

    async def _astream(self, messages, **kwargs):
        p, label = self.probe, self.label
        p.active += 1
        p.by_label[label] += 1
        p.calls[label] += 1
        p.peak = max(p.peak, p.active)
        p.peaks[label] = max(p.peaks[label], p.by_label[label])
        try:
            if p.gate is not None and (p.block_label is None or p.block_label == label):
                await p.gate.wait()
            if p.fail:
                raise ValueError("private provider details")
            async for chunk in super()._astream(messages, **kwargs):
                yield chunk
        finally:
            p.active -= 1
            p.by_label[label] -= 1


def assert_idle(runtime):
    assert runtime.model_limits.active == 0
    assert runtime.model_limits.tenants == {}
    assert runtime.model_limits.waiters == set()


async def test_sixteen_by_sixteen_nested_fanout_is_provider_bounded():
    probe = Probe()
    probe.gate, probe.block_label = asyncio.Event(), "leaf"
    responses = {
        "main": [fanout("branch"), [AIMessageChunk(content="done")]],
        "branch": [fanout("leaf"), [AIMessageChunk(content="done")]],
        "leaf": [[AIMessageChunk(content="done")]],
    }
    runtime = AgentRuntime(
        Registry(
            config(tools=["task"], subagents=["branch"]),
            config("branch", tools=["task"], subagents=["leaf"]),
            config("leaf"),
        ),
        Sandbox(),
        model_factory=lambda c: ProbedModel(responses[c.id], probe, c.id),
        max_concurrent_model_calls=5,
        max_model_calls_per_tenant=3,
        max_model_calls_per_run=1000,
        max_subagent_invocations_per_run=1000,
    )
    task = asyncio.create_task(collect(runtime))
    try:
        await eventually(lambda: probe.by_label["leaf"] == 3)
        assert probe.peak <= 3
        probe.gate.set()
        await asyncio.wait_for(task, 30)
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
    assert probe.calls == {"main": 2, "branch": 32, "leaf": 256}
    assert probe.peak == 3
    assert_idle(runtime)


async def test_multitenant_roots_share_global_and_tenant_limits():
    probe = Probe()
    probe.gate = asyncio.Event()
    runtime = AgentRuntime(
        Registry(config("a"), config("b")),
        Sandbox(),
        model_factory=lambda c: ProbedModel([[AIMessageChunk(content="done")]], probe, c.id),
        max_concurrent_model_calls=3,
        max_model_calls_per_tenant=2,
        max_model_calls_per_run=1,
    )
    tasks = [
        asyncio.create_task(collect(runtime, tenant, tenant, str(i)))
        for tenant in ("a", "b")
        for i in range(4)
    ]
    try:
        await eventually(lambda: probe.active == 3 and len(runtime.model_limits.waiters) == 5)
        assert max(probe.by_label.values()) <= 2
        probe.gate.set()
        await asyncio.wait_for(asyncio.gather(*tasks), 10)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    # Each independent root has its own one-call cumulative budget.
    assert probe.calls == {"a": 4, "b": 4}
    assert probe.peak == 3
    assert max(probe.peaks.values()) <= 2
    assert_idle(runtime)


@pytest.mark.parametrize("kind", ["model", "child"])
async def test_nested_siblings_share_cumulative_budget(kind):
    probe = Probe()
    probe.gate, probe.block_label = asyncio.Event(), "leaf"
    created = Counter()

    def factory(c):
        created[c.id] += 1
        responses = (
            [fanout("branch" if c.id == "main" else "leaf", 2), [AIMessageChunk(content="done")]]
            if c.id != "leaf"
            else [[AIMessageChunk(content="done")]]
        )
        return ProbedModel(responses, probe, c.id)

    runtime = AgentRuntime(
        Registry(
            config(tools=["task"], subagents=["branch"]),
            config("branch", tools=["task"], subagents=["leaf"]),
            config("leaf"),
        ),
        Sandbox(),
        model_factory=factory,
        max_model_calls_per_run=4 if kind == "model" else 100,
        max_subagent_invocations_per_run=3 if kind == "child" else 100,
    )
    with pytest.raises(ResourceLimitError, match="model call" if kind == "model" else "subagent"):
        await asyncio.wait_for(collect(runtime), 10)
    assert sum(probe.calls.values()) <= (4 if kind == "model" else 8)
    if kind == "child":
        assert created == {"main": 1, "branch": 2, "leaf": 1}
    assert_idle(runtime)


async def test_capacity_one_releases_parent_slot_before_recursion():
    probe = Probe()
    runtime = AgentRuntime(
        Registry(config(tools=["task"], subagents=["child"]), config("child")),
        Sandbox(),
        model_factory=lambda c: ProbedModel(
            [
                tool("task", {"subagent_type": "child", "description": "work"}),
                [AIMessageChunk(content="done")],
            ]
            if c.id == "main"
            else [[AIMessageChunk(content="child")]],
            probe,
            c.id,
        ),
        max_concurrent_model_calls=1,
        max_model_calls_per_tenant=1,
    )
    await asyncio.wait_for(collect(runtime), 5)
    assert probe.calls == {"main": 2, "child": 1}
    assert probe.peak == 1
    assert_idle(runtime)


async def test_zero_child_budget_never_invokes_child():
    made = []

    def factory(c):
        made.append(c.id)
        return Model([tool("task", {"subagent_type": "child", "description": "work"})])

    runtime = AgentRuntime(
        Registry(config(tools=["task"], subagents=["child"]), config("child")),
        Sandbox(),
        model_factory=factory,
        max_subagent_invocations_per_run=0,
    )
    with pytest.raises(ResourceLimitError, match="subagent"):
        await asyncio.wait_for(collect(runtime), 5)
    assert made == ["main"]
    assert_idle(runtime)


async def test_cancel_queued_and_active_roots_releases_slots_and_waiters():
    probe = Probe()
    probe.gate = asyncio.Event()
    runtime = AgentRuntime(
        Registry(config()),
        Sandbox(),
        model_factory=lambda c: ProbedModel([[AIMessageChunk(content="done")]], probe, c.id),
        max_concurrent_model_calls=1,
        max_model_calls_per_tenant=1,
    )
    active = asyncio.create_task(collect(runtime, session="active"))
    queued = survivor = None
    try:
        await eventually(lambda: probe.active == 1)
        queued = asyncio.create_task(collect(runtime, session="queued"))
        await eventually(lambda: len(runtime.model_limits.waiters) == 1)
        queued.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(queued, 5)
        assert not runtime.model_limits.waiters
        assert probe.calls["main"] == 1
        survivor = asyncio.create_task(collect(runtime, session="survivor"))
        await eventually(lambda: len(runtime.model_limits.waiters) == 1)
        active.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(active, 5)
        await eventually(lambda: probe.calls["main"] == 2)
        probe.gate.set()
        await asyncio.wait_for(survivor, 5)
    finally:
        tasks = [task for task in (active, queued, survivor) if task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    assert probe.active == 0
    assert_idle(runtime)


async def test_provider_error_releases_capacity_for_next_root():
    probe = Probe()
    probe.fail = True
    runtime = AgentRuntime(
        Registry(config()),
        Sandbox(),
        model_factory=lambda c: ProbedModel([[AIMessageChunk(content="done")]], probe, c.id),
        max_concurrent_model_calls=1,
        max_model_calls_per_tenant=1,
    )
    with pytest.raises(ProviderError, match="Model request failed"):
        await asyncio.wait_for(collect(runtime), 5)
    assert_idle(runtime)
    probe.fail = False
    await asyncio.wait_for(collect(runtime), 5)
    assert probe.calls["main"] == 2
    assert_idle(runtime)


@pytest.mark.parametrize(
    "claim,field", [("claim_model", "model_calls_left"), ("claim_child", "children_left")]
)
async def test_failed_and_cancelled_work_does_not_refund_budget(claim, field):
    budget = RunBudget(2, 2)
    getattr(budget, claim)()
    # A claim is spent even when the admitted work fails.
    with pytest.raises(ValueError):
        raise ValueError("work failed")
    claimed = asyncio.Event()

    async def work():
        getattr(budget, claim)()
        claimed.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(work())
    await asyncio.wait_for(claimed.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert getattr(budget, field) == 0
    with pytest.raises(ResourceLimitError):
        getattr(budget, claim)()


async def test_slow_consumer_close_cancels_provider_with_full_event_queue():
    sent = asyncio.Event()
    closed = asyncio.Event()

    class FloodModel(Model):
        async def _astream(self, messages, **kwargs):
            try:
                for _ in range(128):
                    yield ChatGenerationChunk(message=AIMessageChunk(content="token"))
                    await asyncio.sleep(0)
                sent.set()
                await asyncio.Event().wait()
            finally:
                closed.set()

    runtime = AgentRuntime(
        Registry(config()),
        Sandbox(),
        model_factory=lambda c: FloodModel([]),
        max_concurrent_model_calls=1,
        max_model_calls_per_tenant=1,
    )
    stream = runtime.stream([{"role": "user", "content": "go"}], "main", "tenant", "session")
    try:
        assert (await asyncio.wait_for(anext(stream), 5))["type"] == "token"
        # Stop consuming. More than queue.maxsize tokens now fill the public
        # queue while the graph and provider remain active behind it.
        await asyncio.wait_for(sent.wait(), 5)
        assert runtime.model_limits.active == 1
    finally:
        await asyncio.wait_for(stream.aclose(), 5)
    await asyncio.wait_for(closed.wait(), 5)
    assert_idle(runtime)


async def test_budget_failure_cancels_and_joins_sibling_sandbox_operation():
    started, closed = asyncio.Event(), asyncio.Event()

    class BlockingSandbox:
        async def call(self, *args):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                # Command interruption/HTTP client cleanup may itself suspend.
                await asyncio.sleep(0.01)
                closed.set()

    calls = [
        ("execute", {"command": "long operation"}),
        ("task", {"subagent_type": "child", "description": "work"}),
    ]
    response = [
        AIMessageChunk(
            content="",
            tool_call_chunks=[
                {"name": name, "args": json.dumps(args), "id": str(i), "index": i}
                for i, (name, args) in enumerate(calls)
            ],
        )
    ]
    runtime = AgentRuntime(
        Registry(config(tools=["execute", "task"], subagents=["child"]), config("child")),
        BlockingSandbox(),
        model_factory=lambda c: Model([response]),
        max_subagent_invocations_per_run=0,
    )
    with pytest.raises(ResourceLimitError, match="subagent"):
        await asyncio.wait_for(collect(runtime), 5)
    assert started.is_set()
    assert closed.is_set()
    assert_idle(runtime)


async def test_owned_tool_cleanup_survives_repeated_cancellation_and_stops_new_work():
    budget = RunBudget(4, 4)
    started, cleaning, finish_cleanup, closed = (asyncio.Event() for _ in range(4))

    async def operation():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await finish_cleanup.wait()
            closed.set()

    run = asyncio.create_task(budget.run_owned(operation))
    await started.wait()
    stop = asyncio.create_task(budget.stop())
    await cleaning.wait()
    stop.cancel()
    run.cancel()
    # Cleanup is still tracked and new operations cannot escape terminal state.
    with pytest.raises(asyncio.CancelledError):
        await budget.run_owned(operation)
    assert budget.operation_tasks
    assert not closed.is_set()
    finish_cleanup.set()
    await asyncio.wait_for(asyncio.gather(stop, run, return_exceptions=True), 2)
    assert closed.is_set()
    assert not budget.operation_tasks
