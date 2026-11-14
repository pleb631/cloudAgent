import asyncio
import queue
import threading
from types import SimpleNamespace

import pytest
from docker.errors import NotFound

from cloudagent_oh.config import AgentConfig, AgentRegistry, Settings
from cloudagent_oh.runtime import LABEL, OpenHandsRuntime, Run


class Container:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.labels = kwargs.get("labels", {})
        self.attrs = {"NetworkSettings": {"Ports": {"8000/tcp": [{"HostPort": "32100"}]}}}
        self.removed = 0
        self.fail = False

    def reload(self):
        pass

    def remove(self, **kwargs):
        if self.fail:
            raise OSError("unavailable")
        self.removed += 1


class Containers:
    def __init__(self):
        self.items = {}
        self.entered = threading.Event()
        self.block = None

    def run(self, *args, **kwargs):
        self.entered.set()
        if self.block:
            self.block.wait(5)
        c = Container(**kwargs)
        self.items[kwargs["name"]] = c
        return c

    def get(self, name):
        if name not in self.items:
            raise NotFound("missing")
        return self.items[name]

    def list(self, **kwargs):
        return [c for c in self.items.values() if not c.removed]


class FakeDocker:
    def __init__(self):
        self.containers = Containers()
        self.closed = False

    def close(self):
        self.closed = True


class FakeQueue(queue.Queue):
    def close(self):
        pass

    def cancel_join_thread(self):
        pass


class Process:
    def __init__(self, target, args, daemon):
        self.args = args
        self.alive = False
        self.pid = 123

    def start(self):
        self.alive = True
        self.args[-1].put({"type": "token", "text": "ok"})
        self.args[-1].put({"type": "done"})

    def is_alive(self):
        return self.alive

    def terminate(self):
        self.alive = False

    def kill(self):
        self.alive = False

    def join(self, timeout):
        pass

    def close(self):
        pass


def make(tmp_path, **extra):
    root = tmp_path / "alice"
    root.mkdir(exist_ok=True)
    settings = Settings(workspace_roots={"alice": root}, state_dir=tmp_path / "state", **extra)
    registry = AgentRegistry([AgentConfig(id="assistant", system_prompt="test")])
    runtime = OpenHandsRuntime(settings, registry, docker_client=FakeDocker())
    runtime.ctx = SimpleNamespace(Queue=FakeQueue, Process=Process)

    async def healthy(host):
        return None

    runtime._healthy = healthy
    return runtime


@pytest.mark.asyncio
async def test_bind_mount_resources_native_worker_and_persistent_root(tmp_path):
    runtime = make(tmp_path)
    source = tmp_path / "alice" / "existing.txt"
    source.write_text("unchanged")
    await runtime.start()
    events = [e async for e in runtime.stream([], "assistant", "alice", "session")]
    assert events == [{"type": "token", "text": "ok"}]
    c = next(iter(runtime.docker.containers.items.values()))
    assert c.kwargs["mounts"][0]["Source"] == str(source.parent)
    assert c.kwargs["mounts"][0]["Target"] == "/workspace"
    assert c.kwargs["mounts"][0]["ReadOnly"] is False
    assert c.kwargs["ports"] == {"8000/tcp": ("127.0.0.1", None)}
    assert c.kwargs["cap_drop"] == ["ALL"]
    assert c.kwargs["mem_limit"] == "1g"
    assert c.kwargs["nano_cpus"] == 1_000_000_000
    assert c.kwargs["pids_limit"] == 256
    assert len(c.kwargs["environment"]["OH_SESSION_API_KEYS_0"]) >= 32
    assert c.removed == 1
    assert not runtime.runs
    assert source.read_text() == "unchanged"
    await runtime.close()


@pytest.mark.asyncio
async def test_cancel_during_create_waits_for_ownership_then_removes(tmp_path):
    runtime = make(tmp_path)
    block = runtime.docker.containers.block = threading.Event()
    task = asyncio.create_task(anext(runtime.stream([], "assistant", "alice", "s")))
    await asyncio.to_thread(runtime.docker.containers.entered.wait, 1)
    task.cancel()
    await asyncio.sleep(0.03)
    assert not task.done()
    task.cancel()
    block.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert next(iter(runtime.docker.containers.items.values())).removed == 1
    assert not runtime.runs
    await runtime.close()


@pytest.mark.asyncio
async def test_failed_cleanup_retains_quota_until_retry(tmp_path):
    runtime = make(tmp_path, max_concurrent_runs=1)
    c = Container(labels={LABEL: runtime.settings.deployment_id})
    c.fail = True
    run = Run("failed", "alice", container=c)
    runtime.runs[run.name] = run
    assert not await runtime._dispose(run)
    assert run.name in runtime.runs
    with pytest.raises(RuntimeError, match="capacity"):
        await anext(runtime.stream([], "assistant", "alice", "another"))
    c.fail = False
    assert await runtime._dispose(run)
    assert not runtime.runs
    await runtime.close()


@pytest.mark.asyncio
async def test_concurrent_cleanup_is_idempotent(tmp_path):
    runtime = make(tmp_path)
    c = Container(labels={LABEL: runtime.settings.deployment_id})
    run = Run("one", "alice", container=c)
    runtime.runs[run.name] = run
    assert all(await asyncio.gather(*(runtime._dispose(run) for _ in range(8))))
    assert c.removed == 1
    await runtime.close()


@pytest.mark.asyncio
async def test_other_tenant_not_selected_by_session_or_message(tmp_path):
    runtime = make(tmp_path)
    with pytest.raises(KeyError):
        await anext(runtime.stream([{"content": "/alice"}], "assistant", "bob", "alice"))
    assert not runtime.docker.containers.items
    await runtime.close()


@pytest.mark.asyncio
async def test_symlink_root_replacement_fails_closed(tmp_path):
    runtime = make(tmp_path)
    root = tmp_path / "alice"
    root.rmdir()
    target = tmp_path / "other"
    target.mkdir()
    root.symlink_to(target, target_is_directory=True)
    with pytest.raises(RuntimeError, match="symlink"):
        await anext(runtime.stream([], "assistant", "alice", "s"))
    assert not runtime.docker.containers.items
    await runtime.close()


@pytest.mark.asyncio
async def test_single_controller_instance_lock(tmp_path):
    runtime = make(tmp_path)
    with pytest.raises(RuntimeError, match="one OpenHands controller"):
        make(tmp_path)
    await runtime.close()
    other = make(tmp_path)
    await other.close()


@pytest.mark.asyncio
async def test_start_removes_stale_owned_containers(tmp_path):
    runtime = make(tmp_path)
    stale = Container(labels={LABEL: runtime.settings.deployment_id})
    runtime.docker.containers.items["orphan"] = stale
    await runtime.start()
    assert stale.removed == 1
    await runtime.close()


@pytest.mark.asyncio
async def test_process_start_failure_still_removes_container(tmp_path):
    runtime = make(tmp_path)

    class BrokenProcess(Process):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.pid = None

        def start(self):
            raise OSError("process limit")

        def join(self, timeout):
            raise AssertionError("unstarted process")

    runtime.ctx.Process = BrokenProcess
    with pytest.raises(OSError, match="process limit"):
        await anext(runtime.stream([], "assistant", "alice", "s"))
    assert next(iter(runtime.docker.containers.items.values())).removed == 1
    assert not runtime.runs
    await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_close", [False, True])
async def test_close_waits_for_pending_create_and_never_starts_worker(tmp_path, cancel_close):
    runtime = make(tmp_path)
    block = runtime.docker.containers.block = threading.Event()
    worker_started = []

    class ForbiddenProcess(Process):
        def start(self):
            worker_started.append(True)
            raise AssertionError("Shutdown must prevent worker startup")

    runtime.ctx.Process = ForbiddenProcess
    stream = runtime.stream([], "assistant", "alice", "s")
    request = asyncio.create_task(anext(stream))
    assert await asyncio.to_thread(runtime.docker.containers.entered.wait, 1)
    closing = asyncio.create_task(runtime.close())
    await asyncio.sleep(0.03)
    assert not closing.done()
    assert runtime.runs
    assert not runtime.docker.closed
    if cancel_close:
        closing.cancel()
        await asyncio.sleep(0.01)
        closing.cancel()
    block.set()
    with pytest.raises(RuntimeError, match="shutting down"):
        await request
    if cancel_close:
        with pytest.raises(asyncio.CancelledError):
            await closing
    else:
        await closing
    await runtime.close()
    assert not worker_started
    assert not runtime.runs
    assert runtime.docker.closed
    assert next(iter(runtime.docker.containers.items.values())).removed == 1


@pytest.mark.asyncio
async def test_close_during_health_wait_prevents_worker_start(tmp_path):
    runtime = make(tmp_path)
    entered, resume = asyncio.Event(), asyncio.Event()

    async def healthy(host):
        entered.set()
        await resume.wait()

    runtime._healthy = healthy
    stream = runtime.stream([], "assistant", "alice", "s")
    request = asyncio.create_task(anext(stream))
    await entered.wait()
    await runtime.close()
    resume.set()
    with pytest.raises(RuntimeError, match="shutting down"):
        await request
    assert not runtime.runs
    assert next(iter(runtime.docker.containers.items.values())).removed == 1
