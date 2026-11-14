import asyncio
import json
import tempfile
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from cloudagent_oh.api import RunLimits, create_app
from cloudagent_oh.config import AgentConfig, AgentRegistry, Settings

KEY = "test-api-key-which-is-at-least-24-chars"
OTHER_KEY = "second-test-key-which-is-at-least-24-chars"


class ClosableRuntime:
    async def close(self):
        pass


class FakeRuntime(ClosableRuntime):
    def __init__(self):
        self.calls = []

    async def stream(self, messages, agent_id, tenant_id, session_id):
        self.calls.append((messages, agent_id, tenant_id, session_id))
        yield {"type": "token", "text": "你"}
        yield {"type": "token", "text": "好"}
        yield {"type": "usage", "prompt_tokens": 2, "completion_tokens": 3}


_workspaces = tempfile.TemporaryDirectory()
for _name in ("alice", "bob"):
    (Path(_workspaces.name) / _name).mkdir()


def app_with(runtime=None, **settings):
    settings.setdefault(
        "workspace_roots", {name: Path(_workspaces.name) / name for name in ("alice", "bob")}
    )
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
    assert limit.value.status_code == 409
    await release_a()
    await release_a()  # idempotent cleanup
    await release_b()
    assert not limits.active


def test_errors_are_sanitized_and_slot_released():
    class FailingRuntime(ClosableRuntime):
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

    class BlockingRuntime(ClosableRuntime):
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
        assert other.status_code == 409
        finish.set()
        assert (await first).status_code == 200


async def test_run_cancellation_releases_admission():
    started = asyncio.Event()

    class BlockingRuntime(ClosableRuntime):
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
    assert ("alice", "alice") in limits.active
    with pytest.raises(Exception) as duplicate:
        await limits.acquire("alice", "same")
    assert duplicate.value.status_code == 409
    await new_release()
    assert not limits.active


class DisconnectRuntime(ClosableRuntime):
    def __init__(self):
        self.started = asyncio.Event()
        self.closed = asyncio.Event()

    async def stream(self, *args):
        try:
            self.started.set()
            yield {"type": "token", "text": "started"}
            await asyncio.Event().wait()
        finally:
            # Make cleanup asynchronous so cancellation shielding is exercised.
            await asyncio.sleep(0)
            self.closed.set()


def asgi_request(stream=True, spec="2.4"):
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": spec},
        "method": "POST",
        "scheme": "http",
        "path": "/v1/chat/completions",
        "raw_path": b"/v1/chat/completions",
        "query_string": b"",
        "headers": [
            (b"authorization", f"Bearer {KEY}".encode()),
            (b"content-type", b"application/json"),
            (b"x-session-id", b"same"),
        ],
        "server": ("test", 80),
        "client": ("client", 1),
        "http_version": "1.1",
    }


async def assert_slots_reusable(app):
    app.state.runtime = FakeRuntime()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        for session in ("same", "other"):
            response = await client.post(
                "/v1/chat/completions",
                headers={**headers(), "X-Session-ID": session},
                json=request_body(),
            )
            assert response.status_code == 200


@pytest.mark.parametrize("failure_at", ["start", "role", "token"])
async def test_sse_send_failure_closes_iterators_with_retained_traceback(failure_at):
    runtime = DisconnectRuntime()
    app = app_with(runtime, max_concurrent_runs=1, max_runs_per_tenant=1)
    received = False
    bodies = 0

    async def receive():
        nonlocal received
        if not received:
            received = True
            return {"type": "http.request", "body": json.dumps(request_body(stream=True)).encode()}
        await asyncio.Event().wait()

    async def send(message):
        nonlocal bodies
        if message["type"] == "http.response.start" and failure_at == "start":
            raise OSError("disconnected before body")
        if message["type"] == "http.response.body":
            bodies += 1
            if bodies == {"role": 1, "token": 2}.get(failure_at):
                raise OSError("disconnected during body")

    async with app.router.lifespan_context(app):
        with pytest.raises(Exception) as retained:
            await app(asgi_request(), receive, send)
        # Retain the exception and its traceback throughout the assertions. GC
        # must not be responsible for releasing the iterator or admission slot.
        assert retained.value.__traceback__ is not None
        assert runtime.started.is_set() == (failure_at == "token")
        assert runtime.closed.is_set() == (failure_at == "token")
        await assert_slots_reusable(app)


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("external_cancel", [False, True])
async def test_disconnect_or_cancellation_closes_runtime_and_releases_slots(
    stream, external_cancel
):
    runtime = DisconnectRuntime()
    app = app_with(runtime, max_concurrent_runs=1, max_runs_per_tenant=1)
    received = False
    disconnect = asyncio.Event()

    async def receive():
        nonlocal received
        if not received:
            received = True
            return {
                "type": "http.request",
                "body": json.dumps(request_body(stream=stream)).encode(),
            }
        await disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        pass

    async with app.router.lifespan_context(app):
        task = asyncio.create_task(app(asgi_request(stream, "2.3"), receive, send))
        await asyncio.wait_for(runtime.started.wait(), 1)
        if external_cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 1)
        else:
            disconnect.set()
            await asyncio.wait_for(task, 1)
        assert runtime.closed.is_set()
        await assert_slots_reusable(app)


@pytest.mark.parametrize("stream", [False, True])
async def test_tcp_disconnect_cancels_runtime_promptly(stream):
    import socket

    import uvicorn

    runtime = DisconnectRuntime()
    app = app_with(runtime, run_timeout=60, max_concurrent_runs=1, max_runs_per_tenant=1)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="on"))
    server_task = asyncio.create_task(server.serve(sockets=[sock]))
    writer = None
    try:
        async with asyncio.timeout(5):
            while not server.started:
                if server_task.done():
                    await server_task
                await asyncio.sleep(0.01)
        _, writer = await asyncio.open_connection("127.0.0.1", port)
        body = json.dumps(request_body(stream=stream)).encode()
        writer.write(
            (
                "POST /v1/chat/completions HTTP/1.1\r\n"
                f"Host: localhost\r\nAuthorization: Bearer {KEY}\r\n"
                "X-Session-ID: same\r\nContent-Type: application/json\r\n"
                f"Content-Length: {len(body)}\r\n\r\n"
            ).encode()
            + body
        )
        await writer.drain()
        await asyncio.wait_for(runtime.started.wait(), 2)
        writer.close()
        await writer.wait_closed()
        await asyncio.wait_for(runtime.closed.wait(), 2)
        app.state.runtime = FakeRuntime()
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
            for session in ("same", "other"):
                response = await client.post(
                    "/v1/chat/completions",
                    headers={**headers(), "X-Session-ID": session},
                    json=request_body(),
                )
                assert response.status_code == 200
    finally:
        if writer is not None:
            writer.close()
            await writer.wait_closed()
        server.should_exit = True
        await asyncio.wait_for(server_task, 5)
        sock.close()


@pytest.mark.parametrize("stream", [False, True])
async def test_run_timeout_closes_runtime_and_releases_slots(stream):
    runtime = DisconnectRuntime()
    app = app_with(runtime, run_timeout=1, max_concurrent_runs=1, max_runs_per_tenant=1)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        response = await client.post(
            "/v1/chat/completions",
            headers={**headers(), "X-Session-ID": "same"},
            json=request_body(stream=stream),
        )
        assert response.status_code == (200 if stream else 504)
        if stream:
            assert '"code":"run_failed"' in response.text
            assert "[DONE]" in response.text
        assert runtime.closed.is_set()
        await assert_slots_reusable(app)


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("trigger", ["timeout_disconnect", "parent_cancel"])
@pytest.mark.parametrize("repeated_cancel", [False, True])
async def test_cleanup_survives_disconnect_and_repeated_cancellation(
    stream, trigger, repeated_cancel
):
    started = asyncio.Event()
    cleaning = asyncio.Event()
    finish_cleanup = asyncio.Event()
    closed = asyncio.Event()
    disconnected = asyncio.Event()

    class SlowCleanupRuntime(ClosableRuntime):
        async def stream(self, *args):
            try:
                started.set()
                yield {"type": "token", "text": "started"}
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await finish_cleanup.wait()
                closed.set()

    app = app_with(
        SlowCleanupRuntime(), run_timeout=1, max_concurrent_runs=1, max_runs_per_tenant=1
    )
    received = False

    async def receive():
        nonlocal received
        if not received:
            received = True
            return {
                "type": "http.request",
                "body": json.dumps(request_body(stream=stream)).encode(),
            }
        await disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        pass

    async with app.router.lifespan_context(app):
        task = asyncio.create_task(app(asgi_request(stream), receive, send))
        try:
            await asyncio.wait_for(started.wait(), 1)
            if trigger == "parent_cancel":
                task.cancel()
            await asyncio.wait_for(cleaning.wait(), 2)
            disconnected.set()
            # Give the response owner time to observe the disconnect and join
            # cleanup. Neither it nor repeated cancellation may interrupt finally.
            for _ in range(5):
                await asyncio.sleep(0)
            if repeated_cancel:
                for _ in range(3):
                    task.cancel()
                    await asyncio.sleep(0)
            assert not task.done()
            assert not closed.is_set()
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                for session, expected in (("same", 409), ("other", 409)):
                    response = await client.post(
                        "/v1/chat/completions",
                        headers={**headers(), "X-Session-ID": session},
                        json=request_body(),
                    )
                    assert response.status_code == expected
            finish_cleanup.set()
            if trigger == "parent_cancel" or repeated_cancel:
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 1)
            else:
                await asyncio.wait_for(task, 1)
            assert closed.is_set()
            await assert_slots_reusable(app)
        finally:
            finish_cleanup.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def test_runtime_closed_on_shutdown():
    class Runtime(FakeRuntime):
        closed = False

        async def close(self):
            self.closed = True

    runtime = Runtime()
    with TestClient(app_with(runtime)):
        assert not runtime.closed
    assert runtime.closed


def test_every_authenticated_tenant_requires_workspace():
    with pytest.raises(RuntimeError, match="workspace root"):
        with TestClient(app_with(workspace_roots={})):
            pass


@pytest.mark.parametrize("stream", [False, True])
def test_output_bytes_limit_closes_runtime(stream):
    class LargeRuntime(FakeRuntime):
        closed_stream = False

        async def stream(self, *args):
            try:
                yield {"type": "token", "text": "你" * 400}
            finally:
                self.closed_stream = True

    runtime = LargeRuntime()
    with TestClient(app_with(runtime, max_output_bytes=1024)) as client:
        response = client.post(
            "/v1/chat/completions", headers=headers(), json=request_body(stream=stream)
        )
        assert response.status_code == (200 if stream else 502)
        assert "你" not in response.text
        assert runtime.closed_stream


async def test_global_admission_limit_is_independent_of_tenant_lock():
    limits = RunLimits(total=1, per_tenant=1)
    release = await limits.acquire("alice", "one")
    with pytest.raises(Exception) as result:
        await limits.acquire("bob", "one")
    assert result.value.status_code == 429
    await release()


@pytest.mark.parametrize("startup_fails", [False, True])
def test_real_runtime_start_and_close_even_on_start_failure(monkeypatch, startup_fails):
    import sys
    import types

    instances = []

    class Runtime(FakeRuntime):
        def __init__(self, settings, registry):
            super().__init__()
            self.started = False
            self.closed = False
            instances.append(self)

        async def start(self):
            self.started = True
            if startup_fails:
                raise RuntimeError("startup failed")

        async def close(self):
            self.closed = True

    monkeypatch.setitem(
        sys.modules, "cloudagent_oh.runtime", types.SimpleNamespace(OpenHandsRuntime=Runtime)
    )
    app = create_app(
        Settings(
            api_keys={KEY: "alice"},
            llm_api_key="provider-key",
            workspace_roots={"alice": Path(_workspaces.name) / "alice"},
        ),
        registry=AgentRegistry([AgentConfig(id="assistant", system_prompt="help")]),
    )
    if startup_fails:
        with pytest.raises(RuntimeError, match="startup failed"):
            with TestClient(app):
                pass
    else:
        with TestClient(app):
            assert instances[0].started
            assert not instances[0].closed
    assert instances[0].started
    assert instances[0].closed
