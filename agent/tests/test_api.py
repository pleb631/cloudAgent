import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from cloudagent.api import RunLimits, create_app
from cloudagent.config import AgentConfig, AgentRegistry, Settings

KEY = "test-api-key-which-is-at-least-24-chars"
OTHER_KEY = "second-test-key-which-is-at-least-24-chars"


class FakeRuntime:
    def __init__(self):
        self.calls = []

    async def stream(self, messages, agent_id, tenant_id, session_id):
        self.calls.append((messages, agent_id, tenant_id, session_id))
        yield {"type": "token", "text": "你"}
        yield {"type": "token", "text": "好"}
        yield {"type": "usage", "prompt_tokens": 2, "completion_tokens": 3}


def app_with(runtime=None, **settings):
    return create_app(
        Settings(api_keys={KEY: "alice", OTHER_KEY: "bob"}, **settings),
        registry=AgentRegistry([AgentConfig(id="assistant", system_prompt="help")]),
        runtime=runtime or FakeRuntime(),
    )


@pytest.fixture
def client():
    with TestClient(app_with()) as client:
        yield client


def request_body(**kwargs):
    return {"model": "assistant", "messages": [{"role": "user", "content": "Hi"}], **kwargs}


def headers(key=KEY):
    return {"Authorization": f"Bearer {key}"}


def test_auth_and_models(client):
    assert client.get("/healthz").status_code == 200
    assert client.get("/v1/models").status_code == 401
    assert client.get("/v1/models", headers=headers()).json()["data"][0]["id"] == "assistant"
    assert client.get("/v1/models", headers=headers("bad")).status_code == 401


def test_nonstream_and_session_identity():
    runtime = FakeRuntime()
    with TestClient(app_with(runtime)) as client:
        response = client.post(
            "/v1/chat/completions",
            json=request_body(user="bob"),
            headers={**headers(), "X-Session-ID": "session1"},
        )
        assert response.status_code == 200
        assert response.headers["X-Session-ID"] == "session1"
        assert response.json()["choices"][0]["message"]["content"] == "你好"
        assert response.json()["usage"]["total_tokens"] == 5
        assert runtime.calls[0][2:] == ("alice", "session1")


def test_stream_protocol(client):
    response = client.post(
        "/v1/chat/completions",
        headers=headers(),
        json=request_body(stream=True, stream_options={"include_usage": True}),
    )
    assert response.status_code == 200
    lines = [line[6:] for line in response.text.splitlines() if line.startswith("data: ")]
    assert lines[-1] == "[DONE]"
    chunks = [json.loads(line) for line in lines[:-1]]
    assert chunks[0]["choices"][0]["delta"] == {"role": "assistant"}
    assert chunks[-2]["choices"][0]["finish_reason"] == "stop"
    assert chunks[-1]["choices"] == []
    assert chunks[-1]["usage"]["total_tokens"] == 5


def test_validation(client):
    assert (
        client.post(
            "/v1/chat/completions", headers=headers(), json=request_body(model="missing")
        ).status_code
        == 404
    )
    assert (
        client.post(
            "/v1/chat/completions", headers=headers(), json=request_body(tools=[])
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/v1/chat/completions",
            headers={**headers(), "X-Session-ID": "../oops"},
            json=request_body(),
        ).status_code
        == 422
    )


def test_body_limit():
    with TestClient(app_with(max_request_bytes=1024)) as client:
        response = client.post("/v1/chat/completions", headers=headers(), content=b"x" * 1025)
        assert response.status_code == 413


async def test_limits_release_and_tenant_partition():
    limits = RunLimits(total=2, per_tenant=1)
    release_a = await limits.acquire("alice", "same")
    release_b = await limits.acquire("bob", "same")
    with pytest.raises(Exception) as duplicate:
        await limits.acquire("alice", "same")
    assert duplicate.value.status_code == 409
    with pytest.raises(Exception) as limit:
        await limits.acquire("alice", "other")
    assert limit.value.status_code == 429
    await release_a()
    await release_a()  # idempotent cleanup
    await release_b()
    assert not limits.active


def test_errors_are_sanitized_and_slot_released():
    class FailingRuntime:
        async def stream(self, *args):
            yield {"type": "token", "text": "hello"}
            raise RuntimeError("secret-provider-key-do-not-leak")

    with TestClient(app_with(FailingRuntime())) as client:
        for stream in [False, True, False]:
            response = client.post(
                "/v1/chat/completions",
                headers={**headers(), "X-Session-ID": "same"},
                json=request_body(stream=stream),
            )
            assert response.status_code == (200 if stream else 502)
            assert "secret-provider" not in response.text
            if stream:
                assert '"code":"run_failed"' in response.text
                assert "[DONE]" in response.text


async def test_parallel_request_admission():
    started = asyncio.Event()
    finish = asyncio.Event()

    class BlockingRuntime:
        async def stream(self, *args):
            started.set()
            await finish.wait()
            yield {"type": "token", "text": "done"}

    app = app_with(BlockingRuntime(), max_runs_per_tenant=1)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        first = asyncio.create_task(
            client.post(
                "/v1/chat/completions",
                headers={**headers(), "X-Session-ID": "one"},
                json=request_body(),
            )
        )
        await started.wait()
        same = await client.post(
            "/v1/chat/completions",
            headers={**headers(), "X-Session-ID": "one"},
            json=request_body(),
        )
        other = await client.post(
            "/v1/chat/completions",
            headers={**headers(), "X-Session-ID": "two"},
            json=request_body(),
        )
        assert same.status_code == 409
        assert other.status_code == 429
        finish.set()
        assert (await first).status_code == 200


async def test_run_cancellation_releases_admission():
    started = asyncio.Event()

    class BlockingRuntime:
        async def stream(self, *args):
            started.set()
            await asyncio.Event().wait()
            yield {"type": "token", "text": "unreachable"}

    app = app_with(BlockingRuntime())
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        task = asyncio.create_task(
            client.post(
                "/v1/chat/completions",
                headers={**headers(), "X-Session-ID": "one"},
                json=request_body(),
            )
        )
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        app.state.runtime = FakeRuntime()
        response = await client.post(
            "/v1/chat/completions",
            headers={**headers(), "X-Session-ID": "one"},
            json=request_body(),
        )
        assert response.status_code == 200


async def test_stale_cleanup_cannot_release_a_new_run():
    limits = RunLimits(total=1, per_tenant=1)
    old_release = await limits.acquire("alice", "same")
    await old_release()
    new_release = await limits.acquire("alice", "same")
    await old_release()  # old StreamingResponse background task runs late
    assert ("alice", "same") in limits.active
    with pytest.raises(Exception) as duplicate:
        await limits.acquire("alice", "same")
    assert duplicate.value.status_code == 409
    await new_release()
    assert not limits.active
