import asyncio
from types import SimpleNamespace as NS

import pytest

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
        try:
            for offset in range(0, len(self.data[path]), 8192):
                yield self.data[path][offset : offset + 8192]
        finally:
            self.stream_closed = True

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
    f.created[0].kill = fail
    await c.close()
    assert f.created[0].closed == 1

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
