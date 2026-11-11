from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Callable
from typing import Any, TypedDict

from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
    message_chunk_to_message,
)
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph

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
        {"agent_id": {"type": "string"}, "prompt": {"type": "string"}},
        ["agent_id", "prompt"],
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


class State(TypedDict):
    messages: list
    iterations: int


class AgentRuntime:
    """Each stream owns its graph, message history, todo list and event queue.

    The model factory is called once per agent invocation with its AgentConfig.
    It must return a LangChain-compatible model supporting bind_tools and astream.
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
        todos = []
        prompt = config.system_prompt
        if skills:
            prompt += "\nAvailable skills (use load_skill): " + ", ".join(sorted(skills))
        if "task" in allowed:
            prompt += "\nAllowed subagents: " + ", ".join(config.subagents)
        history = [SystemMessage(content=prompt)]
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
                )
            )
            if allowed:
                model = model.bind_tools([tool_schema(name) for name in sorted(allowed)])
        except Exception:
            raise ProviderError("Model initialization failed.") from None

        async def model_node(state):
            if state["iterations"] >= config.max_iterations:
                raise IterationLimitError("Agent iteration limit reached.")
            combined = None
            try:
                async with asyncio.timeout(self.request_timeout):
                    async for chunk in model.astream(state["messages"]):
                        combined = chunk if combined is None else combined + chunk
                        text = chunk.content
                        if isinstance(text, list):
                            text = "".join(
                                part.get("text", "")
                                for part in text
                                if isinstance(part, dict) and part.get("type") == "text"
                            )
                        if text:
                            await emit(
                                {
                                    "type": "token",
                                    "text": text,
                                    "agent_id": agent_id,
                                    "depth": depth,
                                }
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
            except Exception:
                raise ProviderError("Model request failed.") from None
            if combined is None:
                raise ProviderError("Model returned an empty stream.")
            response = message_chunk_to_message(combined)
            if len(response.tool_calls) > 16:
                raise ToolExecutionError("Too many tool calls in one model response.")
            if getattr(response, "invalid_tool_calls", None):
                raise ToolExecutionError("Model returned malformed tool calls.")
            return {
                "messages": [*state["messages"], response],
                "iterations": state["iterations"] + 1,
            }

        async def tool_node(state):
            nonlocal todos
            results = []
            for call in state["messages"][-1].tool_calls:
                name, args = call["name"], call["args"]
                if name not in allowed:
                    raise ToolExecutionError("Tool is not allowed for this agent.")
                validate_arguments(name, args)
                await emit(
                    {"type": "tool_start", "name": name, "agent_id": agent_id, "depth": depth}
                )
                try:
                    if name == "todo":
                        if "items" in args:
                            todos = list(args["items"])
                        result = {"items": list(todos)}
                    elif name == "load_skill":
                        if args["name"] not in skills:
                            raise ToolExecutionError("Skill is not allowed for this agent.")
                        result = {"content": skills[args["name"]]}
                    elif name == "task":
                        if args["agent_id"] not in config.subagents:
                            raise ToolExecutionError("Subagent is not allowed for this agent.")

                        # Child tokens are internal: only the final child answer is
                        # returned to the parent, never mixed into its public reply.
                        async def child_emit(event):
                            if event["type"] != "token":
                                await emit(event)

                        answer = await self._run(
                            [{"role": "user", "content": args["prompt"]}],
                            args["agent_id"],
                            tenant_id,
                            session_id,
                            child_emit,
                            depth + 1,
                            allowed,
                            set(skills),
                        )
                        result = {"answer": answer}
                    else:
                        result = await self.sandbox.call(tenant_id, session_id, name, args)
                except asyncio.CancelledError:
                    raise
                except SandboxError:
                    # Operational failures are recoverable but provider-controlled
                    # error bodies, paths and transport credentials are not echoed.
                    result = {
                        "error": "Sandbox operation failed. Check the arguments or try again."
                    }
                except AgentRuntimeError:
                    raise
                except Exception:
                    raise ToolExecutionError("Sandbox tool execution failed.") from None
                await emit({"type": "tool_end", "name": name, "agent_id": agent_id, "depth": depth})
                results.append(
                    ToolMessage(
                        content=json.dumps(result, ensure_ascii=False), tool_call_id=call["id"]
                    )
                )
            return {"messages": [*state["messages"], *results]}

        graph = StateGraph(State)
        graph.add_node("model", model_node)
        graph.add_node("tools", tool_node)
        graph.add_edge(START, "model")
        graph.add_conditional_edges(
            "model", lambda state: "tools" if state["messages"][-1].tool_calls else END
        )
        graph.add_edge("tools", "model")
        final = await graph.compile().ainvoke(
            {"messages": history, "iterations": 0},
            config={"recursion_limit": max(4, config.max_iterations * 2 + 2)},
        )
        return final["messages"][-1].content
