"""Real SDK + official HTTP agent-server, with a scripted local model provider.

No model credentials or Docker required. All tool writes target pytest tmp_path.
Run explicitly: pytest -m sdk tests/test_sdk_integration.py -v
"""

import contextlib
import importlib.util
import json
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.sdk


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class ScriptedProvider:
    def __init__(self, target, delegated=False, bind="127.0.0.1", gated=True, finish_tool=False):
        self.target = str(target)
        self.delegated = delegated
        self.gated = gated
        self.finish_tool = finish_tool
        self.requests = []
        self.first_token = threading.Event()
        self.release_final = threading.Event()
        self.finished = threading.Event()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if not request.get("stream"):
                    message = {"role": "assistant", "content": "Test title"}
                    finish = "stop"
                    if request.get("tools"):
                        outer.requests.append(request)
                        if len(outer.requests) == 2:
                            names = [t["function"]["name"] for t in request["tools"]]
                            name = next(n for n in names if "file_editor" in n)
                            args = {
                                "command": "str_replace",
                                "path": outer.target,
                                "old_str": "original",
                                "new_str": "edited",
                            }
                            message = {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "child-edit",
                                        "type": "function",
                                        "function": {"name": name, "arguments": json.dumps(args)},
                                    }
                                ],
                            }
                            finish = "tool_calls"
                        else:
                            message["content"] = "Leaf done"
                    body = json.dumps(
                        {
                            "id": "title",
                            "object": "chat.completion",
                            "created": 1,
                            "model": "gpt-4.1-mini",
                            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                        }
                    ).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                outer.requests.append(request)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()

                def send(delta, finish=None):
                    chunk = {
                        "id": "chatcmpl-scripted",
                        "object": "chat.completion.chunk",
                        "created": 1,
                        "model": "gpt-4.1-mini",
                        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
                    }
                    self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
                    self.wfile.flush()

                send({"role": "assistant"})
                step = len(outer.requests)
                if outer.delegated and step == 1:
                    names = [t["function"]["name"] for t in request["tools"]]
                    name = next(n for n in names if n == "task")
                    args = {
                        "prompt": "Edit the disposable file.",
                        "subagent_type": "integration-leaf",
                        "description": "Edit test file",
                    }
                    send(
                        {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call-task",
                                    "type": "function",
                                    "function": {"name": name, "arguments": json.dumps(args)},
                                }
                            ]
                        }
                    )
                    send({}, "tool_calls")
                elif step == (2 if outer.delegated else 1):
                    names = [t["function"]["name"] for t in request["tools"]]
                    name = next(n for n in names if "file_editor" in n)
                    args = {
                        "command": "str_replace",
                        "path": outer.target,
                        "old_str": "original",
                        "new_str": "edited",
                    }
                    send(
                        {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call-edit",
                                    "type": "function",
                                    "function": {"name": name, "arguments": json.dumps(args)},
                                }
                            ]
                        }
                    )
                    send({}, "tool_calls")
                elif outer.delegated and step == 3:
                    send({"content": "Leaf done"})
                    send({}, "stop")
                elif outer.finish_tool:
                    send(
                        {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call-finish",
                                    "type": "function",
                                    "function": {
                                        "name": "finish",
                                        "arguments": json.dumps(
                                            {"message": "Finished via native tool"}
                                        ),
                                    },
                                }
                            ]
                        }
                    )
                    send({}, "tool_calls")
                else:
                    send({"content": "first "})
                    outer.first_token.set()
                    # A callback must receive the token while the HTTP stream is
                    # unfinished. The test releases this gate after asserting it.
                    if outer.gated:
                        outer.release_final.wait(20)
                    send({"content": "second"})
                    send({}, "stop")
                    outer.finished.set()
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()

        self.server = ThreadingHTTPServer((bind, 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_args):
        self.release_final.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_port}/v1"


@contextlib.contextmanager
def local_agent_server(tmp_path):
    if importlib.util.find_spec("openhands.agent_server") is None:
        pytest.skip("Install openhands-agent-server==1.62.0 to run official-server tests")
    port = free_port()
    key = "test-server-authentication-key-not-production"
    env = {
        **os.environ,
        "OH_SESSION_API_KEYS_0": key,
        "OPENHANDS_SUPPRESS_BANNER": "1",
        "HOME": str(tmp_path),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "LITELLM_LOCAL_MODEL_COST_MAP": "True",
    }
    log = (tmp_path / "server.log").open("w+")
    shim = Path(__file__).resolve().parents[1] / "server" / "server.py"
    command = (
        [sys.executable, str(shim)]
        if shim.is_file()
        else [sys.executable, "-m", "openhands.agent_server"]
    )
    process = subprocess.Popen(
        [*command, "--host", "127.0.0.1", "--port", str(port)],
        cwd=tmp_path,
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    host = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if process.poll() is not None:
                log.seek(0)
                pytest.fail("agent-server exited: " + log.read()[-6000:])
            try:
                if httpx.get(host + "/health", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.2)
        else:
            log.seek(0)
            pytest.fail("agent-server did not become healthy: " + log.read()[-6000:])
        yield host, key
    finally:
        import signal

        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
        log.close()


@pytest.mark.parametrize("delegated", [False, True], ids=["direct", "native-subagent"])
def test_remote_native_file_edit_skill_and_true_token_stream(tmp_path, delegated):
    from openhands.sdk import Conversation
    from openhands.sdk.workspace import RemoteWorkspace

    from cloudagent_oh.config import AgentConfig
    from cloudagent_oh.runtime import sdk_agent

    target = tmp_path / "existing.txt"
    target.write_text("original\n")
    config = AgentConfig(
        id="test",
        system_prompt="Only edit the specified disposable file.",
        tools=["file_editor"],
        skills={"test-skill": "SKILL_SENTINEL_4821"},
        max_iterations=4,
    ).model_dump()
    children = []
    if delegated:
        config["tools"].append("task_tool_set")
        config["subagents"] = ["integration-leaf"]
        children = [
            AgentConfig(
                id="integration-leaf",
                description="Edits test files",
                system_prompt="LEAF_SENTINEL_921. Edit only the disposable file.",
                tools=["file_editor"],
            ).model_dump()
        ]
    tokens = []
    token_received = threading.Event()
    errors = []
    with (
        ScriptedProvider(target, delegated) as provider,
        local_agent_server(tmp_path) as (host, key),
    ):
        agent = sdk_agent(
            config,
            children,
            {"llm_api_key": "local-fake-key", "llm_base_url": provider.url, "request_timeout": 30},
        )
        agent.llm.api_mode = "chat"  # Scripted provider implements Chat Completions only.
        workspace = RemoteWorkspace(host=host, api_key=key, working_dir=str(tmp_path))

        def on_token(chunk):
            for choice in chunk.choices or []:
                if choice.delta.content:
                    tokens.append(choice.delta.content)
                    if "first" in choice.delta.content:
                        token_received.set()

        conversation = Conversation(
            agent=agent,
            workspace=workspace,
            token_callbacks=[on_token],
            visualizer=None,
            max_iteration_per_run=4,
            delete_on_close=True,
        )
        try:
            conversation.send_message("Replace original with edited in the specified file.")

            def run():
                try:
                    conversation.run(timeout=40, poll_interval=0.1)
                except Exception as exc:
                    errors.append(exc)

            thread = threading.Thread(target=run, daemon=True)
            thread.start()
            try:
                assert provider.first_token.wait(25), f"No final response; errors={errors}"
                assert token_received.wait(5), "Remote callback buffered tokens until completion"
                assert not provider.finished.is_set(), (
                    "Provider completed before token was observed"
                )
                assert target.read_text() == "edited\n", json.dumps(provider.requests[-1])
            finally:
                provider.release_final.set()
                thread.join(45)
            assert not thread.is_alive()
            assert not errors, errors
            assert "".join(tokens).endswith("first second")
            assert len(provider.requests) == (4 if delegated else 2)
            if delegated:
                assert "LEAF_SENTINEL_921" in json.dumps(provider.requests[1]["messages"])
            assert "SKILL_SENTINEL_4821" in json.dumps(provider.requests[0]["messages"])
            assert any(m["role"] == "tool" for m in provider.requests[-1]["messages"])
        finally:
            conversation.close()
            workspace.reset_client()


@pytest.mark.docker
@pytest.mark.asyncio
@pytest.mark.skipif(
    os.getenv("RUN_OPENHANDS_DOCKER_TESTS") != "1",
    reason="Set RUN_OPENHANDS_DOCKER_TESTS=1 on a Linux Docker host",
)
async def test_docker_runtime_real_mount_usage_and_cleanup(tmp_path):
    """Full production runtime path, including spawned SDK worker and cleanup.

    Build the policy image first. The fake provider binds all host interfaces
    only for this opt-in test so the sandbox can reach the Docker bridge gateway.
    No real provider secrets are used.
    """
    import asyncio
    import uuid

    import docker

    from cloudagent_oh.config import AgentConfig, AgentRegistry, Settings
    from cloudagent_oh.runtime import LABEL, OpenHandsRuntime

    client = docker.from_env()
    client.ping()  # Explicit opt-in must fail, not silently skip, if Docker is broken.
    bridge = client.networks.get("bridge")
    gateway = bridge.attrs["IPAM"]["Config"][0]["Gateway"]
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o777)
    workspace.chmod(0o777)
    target = workspace / "existing.txt"
    target.write_text("original\n")
    target.chmod(0o666)
    deployment = "sdk-test-" + uuid.uuid4().hex[:12]
    config = AgentConfig(
        id="test",
        system_prompt="Edit only /workspace/existing.txt.",
        tools=["file_editor"],
        max_iterations=4,
    )
    with ScriptedProvider("/workspace/existing.txt", bind="0.0.0.0", gated=False) as provider:
        settings = Settings(
            api_keys={"fake-test-api-key-long-enough": "demo"},
            workspace_roots={"demo": workspace},
            llm_api_key="local-fake-key",
            llm_base_url=f"http://{gateway}:{provider.server.server_port}/v1",
            server_image=os.getenv(
                "OPENHANDS_TEST_SERVER_IMAGE", "cloudagent-openhands-server:1.62.0"
            ),
            deployment_id=deployment,
            state_dir=tmp_path / "state",
            run_timeout=120,
            request_timeout=30,
        )
        runtime = OpenHandsRuntime(settings, AgentRegistry([config]), docker_client=client)
        try:
            await runtime.start()
            async with asyncio.timeout(150):
                events = [
                    event
                    async for event in runtime.stream(
                        [
                            {
                                "role": "user",
                                "content": "Replace original with edited in existing.txt.",
                            }
                        ],
                        "test",
                        "demo",
                        "integration",
                    )
                ]
            assert target.read_text() == "edited\n"
            assert "".join(e["text"] for e in events if e["type"] == "token") == "first second"
            usage = [e for e in events if e["type"] == "usage"]
            assert len(usage) == 1
            assert isinstance(usage[0]["prompt_tokens"], int)
            assert isinstance(usage[0]["completion_tokens"], int)
            assert not runtime.runs
            assert not client.containers.list(all=True, filters={"label": f"{LABEL}={deployment}"})
        finally:
            await runtime.close()


def _run_local_sdk_worker(host, key, config, settings, workspace_path, output):
    """Adapt only the fixed container mount path for a host-local server test."""
    import openhands.sdk.workspace as workspace_module
    from openhands.sdk import Conversation  # noqa: F401

    from cloudagent_oh.runtime import sdk_worker

    original = workspace_module.RemoteWorkspace

    class TemporaryRemoteWorkspace(original):
        def __init__(self, **kwargs):
            kwargs["working_dir"] = workspace_path
            super().__init__(**kwargs)

    workspace_module.RemoteWorkspace = TemporaryRemoteWorkspace
    try:
        sdk_worker(
            host,
            key,
            config,
            [],
            settings,
            [{"role": "user", "content": "Edit the specified disposable file."}],
            output,
        )
    finally:
        workspace_module.RemoteWorkspace = original


@pytest.mark.parametrize("finish_tool", [False, True], ids=["text", "finish-tool"])
def test_sdk_worker_real_remote_tokens_usage_and_done(tmp_path, finish_tool):
    import multiprocessing
    import queue

    from cloudagent_oh.config import AgentConfig

    target = tmp_path / "existing.txt"
    target.write_text("original\n")
    config = AgentConfig(
        id="worker-test",
        system_prompt="Edit only the specified test file.",
        tools=["file_editor"],
        max_iterations=4,
    ).model_dump()
    context = multiprocessing.get_context("spawn")
    output = context.Queue(maxsize=32)
    events = []
    with (
        ScriptedProvider(target, gated=False, finish_tool=finish_tool) as provider,
        local_agent_server(tmp_path) as (host, key),
    ):
        settings = {
            "llm_api_key": "local-fake-key",
            "llm_base_url": provider.url,
            "request_timeout": 30,
            "run_timeout": 40,
            "max_output_bytes": 100_000,
        }
        process = context.Process(
            target=_run_local_sdk_worker, args=(host, key, config, settings, str(tmp_path), output)
        )
        process.start()
        try:
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                try:
                    event = output.get(timeout=1)
                except queue.Empty:
                    if not process.is_alive():
                        pytest.fail(
                            f"Worker exited without done: exit={process.exitcode}; events={events}"
                        )
                    continue
                events.append(event)
                assert event["type"] != "error", events
                if event["type"] == "done":
                    break
            else:
                pytest.fail(f"Worker timed out: {events}")
            process.join(15)
            assert process.exitcode == 0
            assert target.read_text() == "edited\n"
            expected = "Finished via native tool" if finish_tool else "first second"
            assert "".join(e["text"] for e in events if e["type"] == "token") == expected
            usage = [e for e in events if e["type"] == "usage"]
            assert len(usage) == 1
            assert isinstance(usage[0]["prompt_tokens"], int)
            assert isinstance(usage[0]["completion_tokens"], int)
            assert events[-1] == {"type": "done"}
        finally:
            if process.is_alive():
                process.terminate()
                process.join(5)
            if process.is_alive():
                process.kill()
                process.join(5)
            process.close()
            output.close()
            output.cancel_join_thread()
