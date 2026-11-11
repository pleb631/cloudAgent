import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessageChunk, ToolMessage
from langchain_core.outputs import ChatGenerationChunk
from pydantic import Field

from cloudagent.runtime import AgentRuntime, IterationLimitError, ProviderError, ToolExecutionError


def config(id="main", tools=(), subagents=(), skills=None, max_iterations=5):
    return SimpleNamespace(
        id=id,
        description="Test agent",
        system_prompt="Configured instructions",
        model="test",
        temperature=0,
        tools=list(tools),
        skills=skills or {},
        subagents=list(subagents),
        max_iterations=max_iterations,
    )


class Registry:
    def __init__(self, *configs):
        self.configs = {c.id: c for c in configs}

    def get(self, name):
        return self.configs[name]


class Model(BaseChatModel):
    responses: Any
    histories: list = Field(default_factory=list)
    tools: list = Field(default_factory=list)

    def __init__(self, responses):
        super().__init__(responses=iter(responses))

    @property
    def _llm_type(self):
        return "cloudagent-test"

    def bind_tools(self, tools, **kwargs):
        self.tools = tools
        return self.bind(tools=tools)

    def _generate(self, *args, **kwargs):
        raise NotImplementedError("Async tests only")

    async def _astream(self, messages, **kwargs):
        self.histories.append(list(messages))
        await asyncio.sleep(0)
        for chunk in next(self.responses):
            yield ChatGenerationChunk(message=chunk)


class Sandbox:
    def __init__(self):
        self.calls = []

    async def call(self, *args):
        self.calls.append(args)
        return {"content": "file contents"}


def tool(name, args):
    return [
        AIMessageChunk(
            content="",
            tool_call_chunks=[
                {"name": name, "args": __import__("json").dumps(args), "id": "call1", "index": 0}
            ],
        )
    ]


async def collect(runtime, agent="main", session="session"):
    return [
        event
        async for event in runtime.stream(
            [{"role": "user", "content": "hello"}], agent, "tenant", session
        )
    ]


async def test_tool_loop_tokens_usage_and_history():
    model = Model(
        [
            tool("read", {"path": "test.txt"}),
            [
                AIMessageChunk(content="Hello"),
                AIMessageChunk(
                    content=" world",
                    usage_metadata={"input_tokens": 10, "output_tokens": 2, "total_tokens": 12},
                ),
            ],
        ]
    )
    sandbox = Sandbox()
    runtime = AgentRuntime(Registry(config(tools=["read"])), sandbox, model_factory=lambda c: model)
    events = await collect(runtime)
    assert "".join(e["text"] for e in events if e["type"] == "token") == "Hello world"
    assert sandbox.calls == [("tenant", "session", "read", {"path": "test.txt"})]
    assert isinstance(model.histories[1][-1], ToolMessage)
    assert any(e.get("prompt_tokens") == 10 for e in events)


async def test_disallowed_tool_never_reaches_sandbox():
    sandbox = Sandbox()
    runtime = AgentRuntime(
        Registry(config()),
        sandbox,
        model_factory=lambda c: Model([tool("execute", {"command": "bad"})]),
    )
    with pytest.raises(ToolExecutionError, match="not allowed"):
        await collect(runtime)
    assert sandbox.calls == []


async def test_iteration_bound():
    model = Model([tool("todo", {}), tool("todo", {})])
    runtime = AgentRuntime(
        Registry(config(tools=["todo"], max_iterations=2)), Sandbox(), model_factory=lambda c: model
    )
    with pytest.raises(IterationLimitError):
        await collect(runtime)
    assert len(model.histories) == 2


async def test_child_task_result_and_private_todos():
    parent = Model(
        [
            tool("todo", {"items": ["parent"]}),
            tool("task", {"subagent_type": "child", "description": "work"}),
            tool("todo", {}),
            [AIMessageChunk(content="Done")],
        ]
    )
    child = Model([tool("todo", {}), [AIMessageChunk(content="Child answer")]])
    configs = Registry(
        config(tools=["todo", "task"], subagents=["child"]), config("child", tools=["todo"])
    )
    runtime = AgentRuntime(
        configs, Sandbox(), model_factory=lambda c: parent if c.id == "main" else child
    )
    events = await collect(runtime)
    assert child.histories[1][-1].content == '{"items": []}'
    assert parent.histories[3][-1].content == '{"items": ["parent"]}'
    assert "Child answer" in parent.histories[2][-1].content
    assert [e["text"] for e in events if e["type"] == "token"] == ["Done"]


async def test_child_cannot_expand_tools():
    parent = Model([tool("task", {"subagent_type": "child", "description": "work"})])
    child = Model([tool("execute", {"command": "bad"})])
    sandbox = Sandbox()
    runtime = AgentRuntime(
        Registry(config(tools=["task"], subagents=["child"]), config("child", tools=["execute"])),
        sandbox,
        model_factory=lambda c: parent if c.id == "main" else child,
    )
    with pytest.raises(ToolExecutionError, match="not allowed"):
        await collect(runtime)
    assert sandbox.calls == []


async def test_depth_bound():
    runtime = AgentRuntime(
        Registry(config(tools=["task"], subagents=["main"])),
        Sandbox(),
        model_factory=lambda c: Model(
            [tool("task", {"subagent_type": "main", "description": "again"})]
        ),
        max_subagent_depth=1,
    )
    with pytest.raises(ToolExecutionError, match="depth"):
        await collect(runtime)


async def test_concurrent_invocations_are_isolated():
    models = []

    def factory(c):
        model = Model([tool("todo", {}), [AIMessageChunk(content="Done")]])
        models.append(model)
        return model

    runtime = AgentRuntime(Registry(config(tools=["todo"])), Sandbox(), model_factory=factory)
    await asyncio.gather(collect(runtime, session="a"), collect(runtime, session="b"))
    assert len(models) == 2
    assert all(m.histories[1][-1].content == '{"items": []}' for m in models)
    assert models[0].histories[0][0] is not models[1].histories[0][0]


async def test_provider_errors_are_sanitized():
    def broken(c):
        raise ValueError("secret-api-key")

    runtime = AgentRuntime(Registry(config()), Sandbox(), model_factory=broken)
    with pytest.raises(ProviderError) as error:
        await collect(runtime)
    assert "secret" not in str(error.value)


async def test_generator_close_cancels_provider():
    cancelled = asyncio.Event()

    class BlockingModel(Model):
        async def _astream(self, messages, **kwargs):
            try:
                yield ChatGenerationChunk(message=AIMessageChunk(content="start"))
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    runtime = AgentRuntime(Registry(config()), Sandbox(), model_factory=lambda c: BlockingModel([]))
    stream = runtime.stream([{"role": "user", "content": "go"}], "main", "t", "s")
    assert (await anext(stream))["text"] == "start"
    await stream.aclose()
    await asyncio.wait_for(cancelled.wait(), 1)


async def test_oversized_task_prompt_rejected():
    runtime = AgentRuntime(
        Registry(config(tools=["task"], subagents=["main"])),
        Sandbox(),
        model_factory=lambda c: Model(
            [tool("task", {"subagent_type": "main", "description": "x" * 65536})]
        ),
    )
    with pytest.raises(ToolExecutionError, match="size limit"):
        await collect(runtime)


async def test_sandbox_operational_error_is_recoverable():
    from cloudagent.sandbox_client import SandboxError

    class UnavailableSandbox:
        async def call(self, *args):
            raise SandboxError("private transport detail")

    model = Model([tool("read", {"path": "missing"}), [AIMessageChunk(content="Could not read")]])
    runtime = AgentRuntime(
        Registry(config(tools=["read"])), UnavailableSandbox(), model_factory=lambda c: model
    )
    await collect(runtime)
    assert "Sandbox operation failed" in model.histories[1][-1].content
    assert "private" not in model.histories[1][-1].content


async def test_skill_allowlist_and_loading():
    model = Model([tool("load_skill", {"name": "research"}), [AIMessageChunk(content="Done")]])
    runtime = AgentRuntime(
        Registry(config(tools=["load_skill"], skills={"research": "Skill instructions"})),
        Sandbox(),
        model_factory=lambda c: model,
    )
    await collect(runtime)
    assert "Skill instructions" in model.histories[1][-1].content
    denied = AgentRuntime(
        Registry(config(tools=["load_skill"])),
        Sandbox(),
        model_factory=lambda c: Model([tool("load_skill", {"name": "research"})]),
    )
    with pytest.raises(ToolExecutionError, match="Skill is not allowed"):
        await collect(denied)


@pytest.mark.parametrize(
    "name,args",
    [
        ("read_file", {"file_path": "/etc/passwd"}),
        ("write_file", {"file_path": "/tmp/x", "content": "x"}),
        ("edit_file", {"file_path": "/tmp/x", "old_string": "a", "new_string": "b"}),
        ("glob", {"pattern": "*"}),
        ("write_todos", {"todos": []}),
        ("execute", {"command": "id"}),
    ],
)
async def test_official_builtin_aliases_cannot_bypass_read_only_policy(name, args):
    model = Model([tool(name, args)])
    sandbox = Sandbox()
    runtime = AgentRuntime(Registry(config(tools=["read"])), sandbox, model_factory=lambda c: model)
    with pytest.raises(ToolExecutionError, match="not allowed"):
        await collect(runtime)
    assert {t.name for t in model.tools} == {"read"}
    assert sandbox.calls == []


async def test_implicit_general_purpose_subagent_is_unavailable():
    model = Model([tool("task", {"subagent_type": "general-purpose", "description": "work"})])
    runtime = AgentRuntime(
        Registry(config(tools=["task"], subagents=["child"]), config("child")),
        Sandbox(),
        model_factory=lambda c: model,
    )
    with pytest.raises(ToolExecutionError, match="Subagent is not allowed"):
        await collect(runtime)
    task_tool = next(t for t in model.tools if t.name == "task")
    assert "general-purpose" not in task_tool.description
    assert "child" in task_tool.description


async def test_native_skill_discovery_uses_virtual_backend_and_progressive_disclosure():
    model = Model([tool("load_skill", {"name": "research"}), [AIMessageChunk(content="Done")]])
    runtime = AgentRuntime(
        Registry(config(tools=["load_skill"], skills={"research": "PRIVATE FULL SKILL TEXT"})),
        Sandbox(),
        model_factory=lambda c: model,
    )
    await collect(runtime)
    initial_prompt = str(model.histories[0][0].content)
    assert "/skills/research/SKILL.md" in initial_prompt
    assert "load_skill(name)" in initial_prompt
    assert "PRIVATE FULL SKILL TEXT" not in initial_prompt
    assert "PRIVATE FULL SKILL TEXT" in model.histories[1][-1].content
    assert {t.name for t in model.tools} == {"load_skill"}


async def test_native_child_keeps_same_sandbox_scope_and_separate_skill_overlay():
    parent = Model(
        [
            tool("task", {"subagent_type": "child", "description": "read the file"}),
            [AIMessageChunk(content="Done")],
        ]
    )
    child = Model(
        [
            tool("read", {"path": "report.txt"}),
            [
                AIMessageChunk(
                    content="Child result",
                    usage_metadata={"input_tokens": 5, "output_tokens": 2, "total_tokens": 7},
                )
            ],
        ]
    )
    sandbox = Sandbox()
    runtime = AgentRuntime(
        Registry(
            config(
                tools=["task", "read", "load_skill"],
                subagents=["child"],
                skills={"common": "parent shared"},
            ),
            config(
                "child",
                tools=["read", "load_skill"],
                skills={"common": "child shared", "secret": "SECRET SKILL"},
            ),
        ),
        sandbox,
        model_factory=lambda c: parent if c.id == "main" else child,
    )
    events = await collect(runtime)
    assert sandbox.calls == [("tenant", "session", "read", {"path": "report.txt"})]
    assert "/skills/common/SKILL.md" in str(child.histories[0][0].content)
    assert "/skills/secret/SKILL.md" not in str(child.histories[0][0].content)
    assert [e["text"] for e in events if e["type"] == "token"] == ["Done"]
    assert sum(e.get("prompt_tokens", 0) for e in events) == 5


async def test_native_skills_preserve_description_without_granting_embedded_tools():
    model = Model([[AIMessageChunk(content="Done")]])
    content = "---\nname: wrong-name\ndescription: Search academic literature\ninclude-tools: [execute]\n---\nInstructions"
    runtime = AgentRuntime(
        Registry(config(tools=["load_skill"], skills={"research": content})),
        Sandbox(),
        model_factory=lambda c: model,
    )
    await collect(runtime)
    prompt = str(model.histories[0][0].content)
    assert "Search academic literature" in prompt
    assert "wrong-name" not in prompt
    assert {t.name for t in model.tools} == {"load_skill"}
