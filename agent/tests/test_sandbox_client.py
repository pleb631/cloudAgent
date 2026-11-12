import asyncio
from types import SimpleNamespace as NS

import httpx
import pytest
from opensandbox.adapters.filesystem_adapter import FilesystemAdapter
from opensandbox.config import ConnectionConfig
from opensandbox.models.sandboxes import SandboxEndpoint

from cloudagent.sandbox_client import MAX_BYTES, OpenSandboxClient, SandboxError


class Files:
    def __init__(self):
        self.data = {}
        self.directories = {"/workspace"}
        self.stream_closed = False

    async def create_directories(self, entries):
        self.directories.update(e.path for e in entries)

    async def write_files(self, entries):
        self.data.update({e.path: e.data.encode() for e in entries})

    async def read_bytes_stream(self, path, **kwargs):
        # The SDK coroutine returns an async iterator; it is not itself one.
        async def chunks():
            try:
                for offset in range(0, len(self.data[path]), 8192):
                    yield self.data[path][offset : offset + 8192]
            finally:
                self.stream_closed = True

        return chunks()

    def info(self, path):
        return NS(
            path=path,
            entry_type="directory" if path in self.directories else "file",
            size=len(self.data.get(path, b"")),
        )

    async def get_file_info(self, paths):
        return {p: self.info(p) for p in paths}

    async def list_directory(self, entry):
        return [
            self.info(p)
            for p in sorted(self.directories | self.data.keys())
            if p != entry.path and p.rsplit("/", 1)[0] == entry.path
        ]


class Commands:
    def __init__(self):
        self.started = asyncio.Event()
        self.block = False
        self.interrupted = []
        self.active = 0
        self.peak = 0

    async def run(self, command, *, opts, handlers):
        assert opts.timeout.total_seconds() <= 30
        assert handlers.skip_accumulation
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await handlers.on_init(NS(id="execution-123"))
            self.started.set()
            if self.block:
                await asyncio.Event().wait()
            await asyncio.sleep(0.01)
            await handlers.on_stdout(NS(text="x" * (MAX_BYTES + 10)))
            await handlers.on_stderr(NS(text="stderr"))
            return NS(exit_code=0)
        finally:
            self.active -= 1

    async def interrupt(self, execution_id):
        self.interrupted.append(execution_id)


class FakeSandbox:
    def __init__(self):
        self.files, self.commands = Files(), Commands()
        self.killed = self.closed = self.renewed = 0

    async def renew(self, ttl):
        self.renewed += 1

    async def kill(self):
        self.killed += 1

    async def close(self):
        self.closed += 1


class Factory:
    def __init__(self):
        self.created, self.kwargs = [], []

    async def __call__(self, image, **kwargs):
        self.kwargs.append(kwargs)
        sandbox = FakeSandbox()
        self.created.append(sandbox)
        return sandbox


def client(**kwargs):
    factory = Factory()
    return OpenSandboxClient("localhost:8090", "server-secret", factory=factory, **kwargs), factory


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [b"hello", b"", b"x" * MAX_BYTES, b"x" * (MAX_BYTES + 1)])
async def test_read_real_sdk_stream_contract_and_cleanup(payload):
    responses = []

    class PayloadStream(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            for offset in range(0, len(payload), 8192):
                yield payload[offset : offset + 8192]

        async def aclose(self):
            self.closed = True

    stream = PayloadStream()

    def download(request):
        assert request.url.params["path"] == "/workspace/a.txt"
        response = httpx.Response(200, stream=stream)
        responses.append(response)
        return response

    files = FilesystemAdapter(
        ConnectionConfig(transport=httpx.MockTransport(download)),
        SandboxEndpoint(endpoint="localhost:44772"),
    )
    c, _ = client()
    try:
        if len(payload) > MAX_BYTES:
            with pytest.raises(SandboxError, match="64 KiB"):
                await c._read(NS(files=files), "/workspace/a.txt")
        else:
            assert await c._read(NS(files=files), "/workspace/a.txt") == payload.decode()
        assert len(responses) == 1 and responses[0].is_closed and stream.closed
    finally:
        await files._httpx_client.aclose()
        await c.close()


@pytest.mark.asyncio
async def test_isolation_reuse_configuration_and_cleanup():
    c, f = client()
    await c.call("alice", "same", "write", {"path": "x", "content": "alice"})
    await c.call("bob", "same", "write", {"path": "x", "content": "bob"})
    assert (await c.call("alice", "same", "read", {"path": "x"}))["content"] == "alice"
    assert len(f.created) == 2
    tokens = []
    for kw in f.kwargs:
        assert kw["resource"] == {"cpu": "1", "memory": "512Mi"}
        assert kw["network_policy"].default_action == "deny"
        assert kw["network_policy"].egress == []
        assert kw["connection_config"].use_server_proxy
        token = kw["env"]["EXECD_ACCESS_TOKEN"]
        assert token != "server-secret"
        assert kw["connection_config"].headers["X-EXECD-ACCESS-TOKEN"] == token
        tokens.append(token)
    assert len(set(tokens)) == 2
    await c.close()
    assert all(s.killed == s.closed == 1 for s in f.created)
    with pytest.raises(SandboxError, match="closed"):
        await c.call("alice", "same", "ls", {})


@pytest.mark.asyncio
async def test_admission_and_expiry():
    c, f = client(max_sessions=2, max_sessions_per_tenant=1)
    await c.call("a", "1", "ls", {})
    with pytest.raises(SandboxError, match="capacity"):
        await c.call("a", "2", "ls", {})
    await c.call("b", "1", "ls", {})
    with pytest.raises(SandboxError, match="capacity"):
        await c.call("c", "1", "ls", {})
    c._sessions[("a", "1")].expires = 0
    await c.call("a", "2", "ls", {})
    await c.close()
    assert f.created[0].killed == f.created[0].closed == 1


@pytest.mark.asyncio
async def test_files_edit_literal_grep_and_bounds():
    c, f = client()
    await c.call("a", "1", "write", {"path": "a.txt", "content": "hello .*\nworld"})
    await c.call("a", "1", "edit", {"path": "a.txt", "old": "world", "new": "earth"})
    result = await c.call("a", "1", "grep", {"pattern": ".*"})
    assert result["matches"] == [{"path": "a.txt", "line": 1, "text": "hello .*"}]
    assert (await c.call("a", "1", "ls", {}))["entries"][0]["path"] == "a.txt"
    with pytest.raises(SandboxError, match="exactly one"):
        await c.call("a", "1", "edit", {"path": "a.txt", "old": "l", "new": "x"})
    with pytest.raises(SandboxError, match="oversized"):
        await c.call("a", "1", "write", {"path": "large", "content": "x" * (MAX_BYTES + 1)})
    f.created[0].files.data["/workspace/large"] = b"x" * (MAX_BYTES + 1)
    with pytest.raises(SandboxError, match="64 KiB"):
        await c.call("a", "1", "read", {"path": "large"})
    assert f.created[0].files.stream_closed
    await c.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["../x", "/etc/passwd", "a/../../x", "a\\b", "a\x00b"])
async def test_path_rejection(path):
    c, f = client()
    with pytest.raises(SandboxError, match="path|Path"):
        await c.call("a", "1", "read", {"path": path})
    assert not f.created
    await c.close()


@pytest.mark.asyncio
async def test_execute_bounded_serialized_and_cancellation_interrupt():
    c, f = client()
    results = await asyncio.gather(
        *(c.call("a", "1", "execute", {"command": "echo hi"}) for _ in range(3))
    )
    assert len(f.created) == 1 and f.created[0].commands.peak == 1
    assert all(len(r["stdout"]) == MAX_BYTES and r["truncated"] for r in results)
    command = f.created[0].commands
    command.block = True
    command.started.clear()
    task = asyncio.create_task(c.call("a", "1", "execute", {"command": "sleep 99"}))
    await command.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert command.interrupted == ["execution-123"]
    await c.close()


@pytest.mark.asyncio
async def test_sdk_errors_redacted_and_failed_setup_cleaned():
    c, f = client()
    await c.call("a", "1", "ls", {})

    async def fail(*args, **kwargs):
        raise RuntimeError("server-secret EXECD-SECRET raw internal URL")

    f.created[0].renew = fail
    with pytest.raises(SandboxError) as error:
        await c.call("a", "1", "ls", {})
    assert "secret" not in str(error.value).lower()
    original_kill = f.created[0].kill
    f.created[0].kill = fail
    with pytest.raises(SandboxError, match="cleanup incomplete"):
        await c.close()
    assert f.created[0].closed == 0
    f.created[0].kill = original_kill
    await c.close()

    async def bad_factory(*args, **kwargs):
        sandbox = await f(*args, **kwargs)
        sandbox.files.create_directories = fail
        return sandbox

    broken = OpenSandboxClient("localhost:8090", "server-secret", factory=bad_factory)
    with pytest.raises(SandboxError):
        await broken.call("a", "1", "ls", {})
    assert f.created[-1].killed == f.created[-1].closed == 1


@pytest.mark.asyncio
async def test_parallel_creation_does_not_block_existing_tenant():
    c, f = client()
    await c.call("existing", "1", "ls", {})
    entered = 0
    both_entered, release = asyncio.Event(), asyncio.Event()

    async def slow_factory(*args, **kwargs):
        nonlocal entered
        entered += 1
        if entered == 2:
            both_entered.set()
        await release.wait()
        return await f(*args, **kwargs)

    c.factory = slow_factory
    tasks = [asyncio.create_task(c.call(tenant, "1", "ls", {})) for tenant in ("a", "b")]
    await asyncio.wait_for(both_entered.wait(), 1)
    assert entered == 2
    await asyncio.wait_for(c.call("existing", "1", "ls", {}), 1)
    release.set()
    await asyncio.gather(*tasks)
    await c.close()


@pytest.mark.asyncio
async def test_pending_creation_reserves_capacity_and_close_cleans_cancelled_request():
    c, f = client(max_sessions=1)
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow_factory(*args, **kwargs):
        entered.set()
        await release.wait()
        return await f(*args, **kwargs)

    c.factory = slow_factory
    first = asyncio.create_task(c.call("a", "1", "ls", {}))
    await entered.wait()
    with pytest.raises(SandboxError, match="capacity"):
        await c.call("b", "1", "ls", {})
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    closing = asyncio.create_task(c.close())
    await asyncio.sleep(0)
    release.set()
    await closing
    assert len(f.created) == 1
    assert f.created[0].killed == f.created[0].closed == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation,args",
    [
        ("write", {"path": "x", "content": "x" * (MAX_BYTES + 1)}),
        ("execute", {"command": "echo x", "timeout": 31}),
        ("edit", {"path": "x", "old": "", "new": "y"}),
        ("grep", {"pattern": ""}),
    ],
)
async def test_invalid_arguments_do_not_allocate(operation, args):
    c, f = client()
    with pytest.raises(SandboxError):
        await c.call("a", "1", operation, args)
    assert not f.created
    await c.close()


@pytest.mark.asyncio
async def test_execution_error_is_safe_and_not_success():
    c, f = client()
    await c.call("a", "1", "ls", {})

    async def failed(*args, **kwargs):
        return NS(exit_code=None, error=NS(value="server-secret"))

    f.created[0].commands.run = failed
    result = await c.call("a", "1", "execute", {"command": "false"})
    assert result["failed"]
    assert result["error"] == "Command failed or did not complete"
    assert "server-secret" not in str(result)
    await c.close()


@pytest.mark.asyncio
async def test_transport_caps_stream_before_json_buffering_and_ignores_false_length():
    import httpx

    from cloudagent.sandbox_client import BoundedSandboxTransport

    class HugeStream(httpx.AsyncByteStream):
        def __init__(self):
            self.chunks_read = 0
            self.closed = False

        async def __aiter__(self):
            for _ in range(1000):
                self.chunks_read += 1
                yield b"x" * 1024

        async def aclose(self):
            self.closed = True

    for headers in ({}, {"Content-Length": "1"}):
        stream = HugeStream()

        async def handle(request, headers=headers, stream=stream):
            assert request.headers["Accept-Encoding"] == "identity"
            return httpx.Response(200, headers=headers, stream=stream)

        transport = BoundedSandboxTransport(httpx.MockTransport(handle), limit=4096)
        async with httpx.AsyncClient(transport=transport) as http:
            with pytest.raises(SandboxError, match="size limit"):
                await http.get("http://sandbox/files")
        assert stream.chunks_read == 5
        assert stream.closed and transport.closed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    [
        {"Content-Length": "10000000"},
        {"Content-Encoding": "gzip"},
        {"Content-Encoding": "br"},
        {"Content-Encoding": "identity, gzip"},
    ],
)
async def test_transport_rejects_compression_and_large_length_before_read(headers):
    import httpx

    from cloudagent.sandbox_client import BoundedSandboxTransport

    class UnreadableStream(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            raise AssertionError("Unsafe response must not be consumed")
            yield b""

        async def aclose(self):
            self.closed = True

    stream = UnreadableStream()
    transport = BoundedSandboxTransport(
        httpx.MockTransport(lambda request: httpx.Response(200, headers=headers, stream=stream))
    )
    async with httpx.AsyncClient(transport=transport) as http:
        with pytest.raises(SandboxError):
            await http.get("http://sandbox/files")
    assert stream.closed


@pytest.mark.asyncio
async def test_transport_preserves_normal_json_and_closes_per_sandbox():
    import httpx
    from opensandbox.config import ConnectionConfig

    from cloudagent.sandbox_client import BoundedSandboxTransport

    transport = BoundedSandboxTransport(
        httpx.MockTransport(lambda request: httpx.Response(200, json={"files": []}))
    )
    config = ConnectionConfig(domain="sandbox", transport=transport)
    async with httpx.AsyncClient(transport=config.transport) as http:
        response = await http.get("http://sandbox/files")
        assert response.json() == {"files": []}
    assert transport.closed
    c, f = client()
    await c.call("a", "1", "ls", {})
    await c.call("b", "1", "ls", {})
    transports = [kw["connection_config"].transport for kw in f.kwargs]
    assert transports[0] is not transports[1]
    await c.close()
    assert all(t.closed for t in transports)


@pytest.mark.asyncio
async def test_transport_closed_when_sdk_create_fails():
    captured = []

    async def fail(*args, **kwargs):
        captured.append(kwargs["connection_config"].transport)
        raise RuntimeError("secret")

    c = OpenSandboxClient("localhost:8090", "server-secret", factory=fail)
    with pytest.raises(SandboxError):
        await c.call("a", "1", "ls", {})
    assert captured[0].closed
    assert not c._pending
    await c.close()


@pytest.mark.asyncio
async def test_cancelled_and_concurrent_close_preserve_cleanup():
    c, f = client(cleanup_concurrency=1, max_sessions_per_tenant=3)
    for i in range(3):
        await c.call("a", str(i), "ls", {})
    entered, release = asyncio.Event(), asyncio.Event()
    original = f.created[0].kill

    async def blocked_kill():
        entered.set()
        await release.wait()
        await original()

    f.created[0].kill = blocked_kill
    first = asyncio.create_task(c.close())
    await entered.wait()
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert len(c._retiring) == 3
    with pytest.raises(SandboxError, match="closed"):
        await c.call("new", "1", "ls", {})
    other = asyncio.create_task(c.close())
    release.set()
    await asyncio.gather(other, c.close())
    await c.close()
    assert all(s.killed == s.closed == 1 for s in f.created)
    assert not c._retiring


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["kill", "close", "transport"])
async def test_failed_cleanup_is_retryable_without_repeating_successful_phases(phase):
    c, f = client()
    await c.call("a", "1", "ls", {})
    entry = c._sessions[("a", "1")]
    target = entry.transport if phase == "transport" else entry.sandbox
    name = "aclose" if phase == "transport" else phase
    original = getattr(target, name)

    async def fail():
        raise RuntimeError("secret")

    setattr(target, name, fail)
    with pytest.raises(SandboxError, match="cleanup incomplete"):
        await c.close()
    assert c._retiring[("a", "1")] is entry
    if phase == "kill":
        assert f.created[0].closed == 0 and not entry.transport.closed
    setattr(target, name, original)
    await c.close()
    await c.close()
    assert f.created[0].killed == f.created[0].closed == 1
    assert entry.transport.closed


@pytest.mark.asyncio
async def test_shutdown_budget_bounds_many_hung_cleanup_tasks_and_concurrency():
    c, f = client(
        max_sessions=20,
        max_sessions_per_tenant=20,
        shutdown_timeout=0.03,
        cleanup_timeout=0.02,
        cleanup_concurrency=2,
    )
    for i in range(12):
        await c.call("a", str(i), "ls", {})
    active = peak = 0
    release = asyncio.Event()

    async def stubborn_kill():
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
        finally:
            active -= 1

    for sandbox in f.created:
        sandbox.kill = stubborn_kill
    start = asyncio.get_running_loop().time()
    with pytest.raises(SandboxError, match="cleanup incomplete"):
        await c.close()
    assert asyncio.get_running_loop().time() - start < 0.2
    assert len(c._retiring) == 12 and peak == 2
    release.set()
    await c.close()
    assert not c._retiring
    assert all(s.closed == 1 for s in f.created)


@pytest.mark.asyncio
async def test_failed_expiry_does_not_release_quota():
    c, f = client(max_sessions=1)
    await c.call("a", "1", "ls", {})
    c._sessions[("a", "1")].expires = 0
    original = f.created[0].kill

    async def fail():
        raise RuntimeError("secret")

    f.created[0].kill = fail
    with pytest.raises(SandboxError, match="capacity"):
        await c.call("b", "1", "ls", {})
    assert len(f.created) == 1 and ("a", "1") in c._retiring
    f.created[0].kill = original
    await c.call("b", "1", "ls", {})
    await c.close()


@pytest.mark.asyncio
async def test_close_during_creation_is_bounded_and_keeps_returned_id():
    c, f = client(shutdown_timeout=0.02)
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow_factory(*args, **kwargs):
        entered.set()
        await release.wait()
        return await f(*args, **kwargs)

    c.factory = slow_factory
    request = asyncio.create_task(c.call("a", "1", "ls", {}))
    await entered.wait()
    with pytest.raises(SandboxError, match="cleanup incomplete"):
        await c.close()
    assert c._pending and not f.created
    release.set()
    with pytest.raises(SandboxError, match="closed"):
        await request
    await c.close()
    assert f.created[0].killed == f.created[0].closed == 1


@pytest.mark.asyncio
async def test_transport_close_failure_does_not_mark_closed():
    from cloudagent.sandbox_client import BoundedSandboxTransport

    class FlakyTransport(httpx.AsyncBaseTransport):
        calls = 0

        async def aclose(self):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("retry")

    inner = FlakyTransport()
    transport = BoundedSandboxTransport(inner)
    with pytest.raises(RuntimeError):
        await transport.aclose()
    assert not transport.closed
    await transport.aclose()
    await transport.aclose()
    assert transport.closed and inner.calls == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["kill", "close", "transport"])
async def test_cleanup_phase_timeout_preserves_retry(phase):
    c, f = client(cleanup_timeout=0.01)
    await c.call("a", "1", "ls", {})
    entry = c._sessions[("a", "1")]
    target = entry.transport if phase == "transport" else entry.sandbox
    name = "aclose" if phase == "transport" else phase
    original = getattr(target, name)

    async def hang():
        await asyncio.Event().wait()

    setattr(target, name, hang)
    with pytest.raises(SandboxError, match="cleanup incomplete"):
        await c.close()
    assert c._retiring[("a", "1")] is entry
    setattr(target, name, original)
    await c.close()
    assert f.created[0].killed == f.created[0].closed == 1


@pytest.mark.asyncio
async def test_failed_setup_and_failed_kill_keep_capacity_reserved():
    c, f = client(max_sessions=1)

    async def fail(*args, **kwargs):
        raise RuntimeError("secret")

    async def factory(*args, **kwargs):
        sandbox = await f(*args, **kwargs)
        sandbox.files.create_directories = fail
        sandbox.kill = fail
        return sandbox

    c.factory = factory
    with pytest.raises(SandboxError):
        await c.call("a", "1", "ls", {})
    assert not c._pending and ("a", "1") in c._retiring
    with pytest.raises(SandboxError, match="capacity"):
        await c.call("b", "1", "ls", {})
    f.created[0].kill = FakeSandbox.kill.__get__(f.created[0])
    await c.close()


@pytest.mark.asyncio
async def test_close_waits_for_active_operation_without_early_disposal():
    c, f = client(shutdown_timeout=0.01)
    await c.call("a", "1", "ls", {})
    entry = c._sessions[("a", "1")]
    await entry.lock.acquire()
    request = asyncio.create_task(c.call("a", "1", "ls", {}))
    await asyncio.sleep(0)
    with pytest.raises(SandboxError, match="cleanup incomplete"):
        await c.close()
    assert f.created[0].killed == 0
    entry.lock.release()
    with pytest.raises(SandboxError, match="closed"):
        await request
    await c.close()
    assert f.created[0].killed == f.created[0].closed == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [404, 403, 500])
async def test_kill_retry_accepts_only_remote_not_found(status):
    from opensandbox.exceptions import SandboxApiException

    c, f = client()
    await c.call("a", "1", "ls", {})
    original = f.created[0].kill

    async def missing():
        raise SandboxApiException(status_code=status)

    f.created[0].kill = missing
    if status == 404:
        await c.close()
        assert not c._retiring and f.created[0].closed == 1
    else:
        with pytest.raises(SandboxError, match="cleanup incomplete"):
            await c.close()
        assert c._retiring and f.created[0].closed == 0
        f.created[0].kill = original
        await c.close()
