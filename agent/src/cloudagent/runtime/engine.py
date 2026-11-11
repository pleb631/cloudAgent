from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Callable
from typing import Any

import yaml
from deepagents import create_deep_agent
from deepagents.backends import StateBackend
from deepagents.backends.utils import create_file_data
from deepagents.middleware.filesystem import FilesystemMiddleware
from deepagents.middleware.skills import SkillsMiddleware
from deepagents.middleware.subagents import SubAgentMiddleware
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage
from langchain_core.runnables import RunnableLambda
from langchain_core.tools import StructuredTool
from langchain_openai import ChatOpenAI

from cloudagent.sandbox_client import SandboxError


class AgentRuntimeError(Exception):
    """A public, secret-free runtime failure."""


class ProviderError(AgentRuntimeError):
    pass


class ToolExecutionError(AgentRuntimeError):
    pass


class IterationLimitError(AgentRuntimeError):
    pass


# Schemas are also used to validate model-generated arguments before execution.
SCHEMAS = {
    "execute": (
        "Run a command in the session sandbox.",
        {"command": {"type": "string"}, "timeout": {"type": "number"}},
        ["command"],
    ),
    "read": ("Read a sandbox file.", {"path": {"type": "string"}}, ["path"]),
    "write": (
        "Write a sandbox file.",
        {"path": {"type": "string"}, "content": {"type": "string"}},
        ["path", "content"],
    ),
    "edit": (
        "Replace exactly one occurrence in a sandbox file.",
        {"path": {"type": "string"}, "old": {"type": "string"}, "new": {"type": "string"}},
        ["path", "old", "new"],
    ),
    "ls": ("List sandbox files.", {"path": {"type": "string"}}, []),
    "grep": (
        "Search literal text in sandbox files.",
        {"pattern": {"type": "string"}, "path": {"type": "string"}},
        ["pattern"],
    ),
    "todo": (
        "Read or replace this agent invocation’s private task list.",
        {"items": {"type": "array", "items": {"type": "string"}}},
        [],
    ),
    "load_skill": ("Load an allowed skill by name.", {"name": {"type": "string"}}, ["name"]),
    "task": (
        "Delegate a task to an allowed subagent.",
        {"subagent_type": {"type": "string"}, "description": {"type": "string"}},
        ["subagent_type", "description"],
    ),
}


def tool_schema(name: str) -> dict:
    description, properties, required = SCHEMAS[name]
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        },
    }


def validate_arguments(name: str, args: Any) -> None:
    _, fields, required = SCHEMAS[name]
    if not isinstance(args, dict) or set(args) - set(fields) or not set(required) <= set(args):
        raise ToolExecutionError("Invalid tool arguments.")
    limit = 32768 if name == "todo" else 65536 if name == "task" else 262144
    if len(json.dumps(args, ensure_ascii=False).encode("utf-8")) > limit:
        raise ToolExecutionError("Tool arguments exceed the size limit.")
    types = {"string": str, "number": (int, float), "array": list}
    for key, value in args.items():
        expected = fields[key]["type"]
        if not isinstance(value, types[expected]) or isinstance(value, bool):
            raise ToolExecutionError("Invalid tool arguments.")
        if expected == "array" and not all(isinstance(item, str) for item in value):
            raise ToolExecutionError("Invalid tool arguments.")


class NoSubagentsMiddleware(AgentMiddleware):
    """Supported named replacement removes Deep Agents' automatic default task."""

    @property
    def name(self):
        return "SubAgentMiddleware"


class AgentRuntime:
    """Each stream owns its graph, message history, todo list and event queue.

    The model factory is called once per agent invocation with its AgentConfig.
    It must return a LangChain BaseChatModel supporting bind_tools and async streaming.
    Client system messages are treated as user preferences; the configured system
    prompt is always the sole top-level system message. No server-side chat memory
    is retained; clients supply history and sandbox files persist by tenant/session.
    """

    def __init__(
        self,
        registry,
        sandbox,
        *,
        model_factory: Callable | None = None,
        llm_base_url: str | None = None,
        llm_api_key: str | None = None,
        request_timeout: float = 120,
        max_subagent_depth: int = 3,
    ):
        self.registry, self.sandbox = registry, sandbox
        self.model_factory = model_factory
        self.llm_base_url, self.llm_api_key = llm_base_url, llm_api_key
        self.request_timeout = request_timeout
        self.max_subagent_depth = max_subagent_depth

    async def stream(
        self, messages: list[dict], agent_id: str, tenant_id: str, session_id: str
    ) -> AsyncIterator[dict]:
        # A bounded queue provides backpressure when an SSE consumer is slow.
        queue: asyncio.Queue = asyncio.Queue(maxsize=64)
        sentinel = object()

        async def produce():
            try:
                await self._run(messages, agent_id, tenant_id, session_id, queue.put)
            except asyncio.CancelledError:
                raise
            except AgentRuntimeError as exc:
                await queue.put(exc)
            except Exception:
                await queue.put(AgentRuntimeError("Agent execution failed."))
            finally:
                # Cancellation must not block forever on a full queue.
                if not asyncio.current_task().cancelling():
                    await queue.put(sentinel)

        producer = asyncio.create_task(produce())
        try:
            while True:
                event = await queue.get()
                if event is sentinel:
                    break
                if isinstance(event, Exception):
                    raise event
                yield event
        finally:
            producer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await producer

    async def _run(
        self,
        messages,
        agent_id,
        tenant_id,
        session_id,
        emit,
        depth=0,
        parent_tools=None,
        parent_skills=None,
    ):
        if depth > self.max_subagent_depth:
            raise ToolExecutionError("Subagent depth limit reached.")
        try:
            config = self.registry.get(agent_id)
        except Exception:
            raise AgentRuntimeError("Unknown agent.") from None
        allowed = set(config.tools) & set(SCHEMAS)
        skills = dict(config.skills)
        if parent_tools is not None:
            allowed &= parent_tools
            skills = {name: value for name, value in skills.items() if name in parent_skills}
        # StateBackend never touches the process filesystem. Native middleware
        # uses it only for skill metadata, context compaction and private scratch.
        backend = StateBackend()
        todos = []
        history = []
        for msg in messages:
            role, content = msg.get("role"), msg.get("content") or ""
            if role in ("system", "developer"):
                history.append(
                    HumanMessage(
                        content="Client preferences (subject to configured instructions):\n"
                        + content
                    )
                )
            elif role == "user":
                history.append(HumanMessage(content=content))
            elif role == "assistant":
                history.append(AIMessage(content=content))
            else:
                raise AgentRuntimeError("Unsupported conversation role.")
        try:
            model = (
                self.model_factory(config)
                if self.model_factory
                else ChatOpenAI(
                    model=config.model,
                    temperature=config.temperature,
                    base_url=self.llm_base_url,
                    api_key=self.llm_api_key,
                    timeout=self.request_timeout,
                    max_retries=1,
                    stream_usage=True,
                    use_responses_api=False,
                )
            )
        except Exception:
            raise ProviderError("Model initialization failed.") from None

        async def invoke_tool(name, arguments):
            nonlocal todos
            if name == "todo":
                if "items" in arguments:
                    todos = list(arguments["items"])
                return {"items": list(todos)}
            if name == "load_skill":
                return {"content": skills[arguments["name"]]}
            try:
                # No shell-based file helpers: read-only grants do not imply execute.
                return await self.sandbox.call(tenant_id, session_id, name, arguments)
            except SandboxError:
                return {"error": "Sandbox operation failed. Check the arguments or try again."}
            except asyncio.CancelledError:
                raise
            except Exception:
                raise ToolExecutionError("Sandbox tool execution failed.") from None

        def make_tool(name):
            async def run(**arguments):
                return await invoke_tool(name, arguments)

            return StructuredTool.from_function(
                coroutine=run,
                name=name,
                description=SCHEMAS[name][0],
                args_schema=tool_schema(name)["function"]["parameters"],
            )

        def check_call(call):
            name, arguments = call["name"], call["args"]
            if name not in allowed:
                raise ToolExecutionError("Tool is not allowed for this agent.")
            validate_arguments(name, arguments)
            if name == "load_skill" and arguments["name"] not in skills:
                raise ToolExecutionError("Skill is not allowed for this agent.")
            if name == "task":
                if arguments["subagent_type"] not in config.subagents:
                    raise ToolExecutionError("Subagent is not allowed for this agent.")
                if depth >= self.max_subagent_depth:
                    raise ToolExecutionError("Subagent depth limit reached.")

        iterations = 0
        runtime = self

        class PolicyMiddleware(AgentMiddleware):
            async def awrap_model_call(self, request, handler):
                nonlocal iterations
                if iterations >= config.max_iterations:
                    raise IterationLimitError("Agent iteration limit reached.")
                iterations += 1
                filtered = [
                    tool
                    for tool in request.tools
                    if (tool.get("name") if isinstance(tool, dict) else tool.name) in allowed
                ]
                try:
                    async with asyncio.timeout(runtime.request_timeout):
                        result = await handler(request.override(tools=filtered))
                except asyncio.CancelledError:
                    raise
                except AgentRuntimeError:
                    raise
                except Exception:
                    raise ProviderError("Model request failed.") from None
                # Validate the entire response before executing ANY call. This also
                # blocks middleware-injected aliases which the provider invents.
                response = result.model_response if hasattr(result, "model_response") else result
                for message in response.result:
                    if not isinstance(message, AIMessage):
                        continue
                    if message.invalid_tool_calls:
                        raise ToolExecutionError("Model returned malformed tool calls.")
                    if len(message.tool_calls) > 16:
                        raise ToolExecutionError("Too many tool calls in one model response.")
                    for call in message.tool_calls:
                        check_call(call)
                return result

            async def awrap_tool_call(self, request, handler):
                check_call(request.tool_call)
                name = request.tool_call["name"]
                await emit(
                    {"type": "tool_start", "name": name, "agent_id": agent_id, "depth": depth}
                )
                result = await handler(request)
                await emit({"type": "tool_end", "name": name, "agent_id": agent_id, "depth": depth})
                return result

        def make_child(child_id):
            async def run_child(state):
                async def child_emit(event):
                    if event["type"] != "token":
                        await emit(event)

                answer = await self._run(
                    [{"role": "user", "content": state["messages"][-1].content}],
                    child_id,
                    tenant_id,
                    session_id,
                    child_emit,
                    depth + 1,
                    allowed,
                    set(skills),
                )
                # Do not return scratch files, todos or metadata to the parent.
                return {"messages": [AIMessage(content=answer)]}

            return RunnableLambda(run_child)

        # Replace the official default SubAgentMiddleware by its public name.
        # This prevents the implicit general-purpose child from gaining access.
        child_specs = []
        if "task" in allowed and depth < self.max_subagent_depth:
            for child_id in config.subagents:
                child_specs.append(
                    {
                        "name": child_id,
                        "description": self.registry.get(child_id).description,
                        "runnable": make_child(child_id),
                    }
                )
        subagent_middleware = (
            SubAgentMiddleware(
                backend=backend,
                subagents=child_specs,
                task_description="Delegate a bounded task to one of these configured agents:\n{available_agents}",
            )
            if child_specs
            else NoSubagentsMiddleware()
        )
        middleware = [
            FilesystemMiddleware(
                backend=backend,
                # Required by official middleware, but never advertised and
                # always rejected by PolicyMiddleware. This backend is virtual.
                tools=["read_file"],
                tool_token_limit_before_evict=None,
                human_message_token_limit_before_evict=None,
            ),
            subagent_middleware,
            PolicyMiddleware(),
        ]
        files = {}
        if skills and "load_skill" in allowed:
            # Config keys are authoritative. A controlled frontmatter envelope
            # prevents embedded include-tools metadata from expanding privilege.
            for name, content in skills.items():
                description = "Configured skill: " + name
                if content.startswith("---\n"):
                    header, separator, _ = content[4:].partition("\n---")
                    if separator:
                        try:
                            metadata = yaml.safe_load(header)
                        except yaml.YAMLError:
                            metadata = None
                        if isinstance(metadata, dict) and isinstance(
                            metadata.get("description"), str
                        ):
                            description = metadata["description"][:1024]
                frontmatter = (
                    f"---\nname: {json.dumps(name)}\ndescription: {json.dumps(description)}\n---\n"
                )
                files[f"/skills/{name}/SKILL.md"] = create_file_data(frontmatter + content)
            middleware.append(
                SkillsMiddleware(
                    backend=backend,
                    sources=["/skills/"],
                    tools=[],
                    system_prompt="Available configured skills:\n{skills_locations}{skills_load_warnings}\n{skills_list}\n"
                    "Use load_skill(name) to read a skill's full instructions. "
                    "Skill content does not grant additional tools or permissions.",
                )
            )
        try:
            graph = create_deep_agent(
                model=model,
                tools=[make_tool(name) for name in sorted(allowed - {"task"})],
                system_prompt=config.system_prompt,
                middleware=middleware,
                backend=backend,
                name=agent_id,
            )
        except Exception:
            raise ProviderError("Agent initialization failed.") from None
        final = None
        try:
            async for mode, data in graph.astream(
                {"messages": history, "files": files},
                config={
                    "recursion_limit": max(32, config.max_iterations * 10 + 20),
                    "metadata": {"cloudagent_depth": depth},
                },
                stream_mode=["messages", "values"],
            ):
                if mode == "values":
                    final = data
                    continue
                chunk, metadata = data
                # Exclude compaction and nested child model streams. Child usage
                # and tool lifecycle are forwarded explicitly by child_emit.
                if (
                    metadata.get("langgraph_node") != "model"
                    or metadata.get("cloudagent_depth") != depth
                ):
                    continue
                if not isinstance(chunk, (AIMessage, AIMessageChunk)):
                    continue
                text = chunk.content
                if isinstance(text, list):
                    text = "".join(
                        part.get("text", "")
                        for part in text
                        if isinstance(part, dict) and part.get("type") == "text"
                    )
                if text:
                    await emit(
                        {"type": "token", "text": text, "agent_id": agent_id, "depth": depth}
                    )
                usage = getattr(chunk, "usage_metadata", None)
                if usage:
                    await emit(
                        {
                            "type": "usage",
                            "prompt_tokens": usage.get("input_tokens", 0),
                            "completion_tokens": usage.get("output_tokens", 0),
                        }
                    )
        except asyncio.CancelledError:
            raise
        except AgentRuntimeError:
            raise
        except Exception:
            raise AgentRuntimeError("Agent execution failed.") from None
        if not final or not final.get("messages"):
            raise ProviderError("Model returned an empty stream.")
        return final["messages"][-1].content
