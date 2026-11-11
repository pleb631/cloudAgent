"""Exercise the real OpenAI SDK + LangChain streaming adapter without network or secrets."""

import json

import httpx
from langchain_openai import ChatOpenAI

from cloudagent.config import AgentConfig, AgentRegistry
from cloudagent.runtime import AgentRuntime


def sse_chunk(delta, *, finish=None, usage=None):
    value = {
        "id": "chatcmpl-test",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "test-model",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    if usage:
        value["usage"] = usage
    return "data: " + json.dumps(value) + "\n\n"


async def test_real_provider_adapter_stream_and_tool_round_trip():
    calls = []
    sandbox_calls = []

    async def provider(request):
        body = json.loads(request.content)
        calls.append(body)
        assert request.url.path == "/v1/chat/completions"
        assert body["stream"] is True
        if len(calls) == 1:
            payload = sse_chunk(
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call-write",
                            "type": "function",
                            "function": {
                                "name": "write",
                                "arguments": '{"path":"hello.txt","content":"你好"}',
                            },
                        }
                    ],
                }
            )
            payload += sse_chunk(
                {},
                finish="tool_calls",
                usage={"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14},
            )
        else:
            assert body["messages"][-1]["role"] == "tool"
            assert body["messages"][-1]["tool_call_id"] == "call-write"
            payload = sse_chunk({"role": "assistant", "content": "已写入"})
            payload += sse_chunk({"content": "文件"})
            payload += sse_chunk(
                {},
                finish="stop",
                usage={"prompt_tokens": 15, "completion_tokens": 2, "total_tokens": 17},
            )
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=payload + "data: [DONE]\n\n"
        )

    class Sandbox:
        async def call(self, tenant, session, operation, arguments):
            sandbox_calls.append((tenant, session, operation, arguments))
            return {"path": "hello.txt", "bytes": 6}

    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http_client:

        def model_factory(config):
            return ChatOpenAI(
                model=config.model,
                api_key="test-not-a-secret",
                base_url="https://provider.test/v1",
                http_async_client=http_client,
                stream_usage=True,
            )

        config = AgentConfig(
            id="assistant", model="test-model", system_prompt="help", tools=["write"]
        )
        runtime = AgentRuntime(AgentRegistry([config]), Sandbox(), model_factory=model_factory)
        events = [
            event
            async for event in runtime.stream(
                [{"role": "user", "content": "Write hello"}], "assistant", "alice", "session"
            )
        ]
    assert "".join(event["text"] for event in events if event["type"] == "token") == "已写入文件"
    assert len(calls) == 2
    assert calls[0]["tools"][0]["function"]["name"] == "write"
    assert sandbox_calls == [
        ("alice", "session", "write", {"path": "hello.txt", "content": "你好"})
    ]
    assert sum(event["prompt_tokens"] for event in events if event["type"] == "usage") == 25
