"""Real HTTP end-to-end smoke test: API -> fake OpenAI server -> official SDK adapter.

Run `make install` first, then `python scripts/smoke_test.py`. Default mode uses an in-memory fake SDK sandbox and requires no Docker or provider key.
Set RUN_OPENSANDBOX_TESTS=1 with a running official server for real sandbox verification.
"""

import json
import os
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEST_TOKEN = "local-smoke-only-not-a-real-secret-123456789"


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Provider(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        last = body["messages"][-1]
        if last["role"] != "tool":
            delta = {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "write1",
                        "type": "function",
                        "function": {
                            "name": "write",
                            "arguments": json.dumps(
                                {"path": "smoke.txt", "content": "你好 cloudAgent"}
                            ),
                        },
                    }
                ]
            }
            finish = "tool_calls"
        elif last["tool_call_id"] == "write1":
            assert "error" not in json.loads(last["content"]), last
            delta = {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "read1",
                        "type": "function",
                        "function": {
                            "name": "read",
                            "arguments": '{"path":"smoke.txt"}',
                        },
                    }
                ]
            }
            finish = "tool_calls"
        else:
            assert json.loads(last["content"])["content"] == "你好 cloudAgent", last
            delta, finish = {"content": "smoke test complete"}, "stop"
        chunk = {
            "id": "chatcmpl-smoke",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "test",
            "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
        }
        final = {
            **chunk,
            "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
        data = (
            "data: "
            + json.dumps(chunk)
            + "\n\ndata: "
            + json.dumps(final)
            + "\n\ndata: [DONE]\n\n"
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def request(url, body=None):
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {TEST_TOKEN}",
            "Content-Type": "application/json",
            "X-Session-ID": "smoke-session",
        },
    )
    with urllib.request.urlopen(req, timeout=120) as response:
        return response.read().decode()


def ready(url, process):
    for _ in range(100):
        if process.poll() is not None:
            raise RuntimeError("Service exited during startup")
        try:
            request(url)
            return
        except (OSError, urllib.error.URLError):
            time.sleep(0.1)
    raise RuntimeError(f"Service did not become ready: {url}")


class FakeFiles:
    def __init__(self):
        self.files = {}

    async def create_directories(self, entries):
        pass

    async def write_files(self, entries):
        for entry in entries:
            self.files[entry.path] = (
                entry.data.encode() if isinstance(entry.data, str) else entry.data
            )

    async def read_bytes_stream(self, path, *, chunk_size=8192, limit=None):
        data = self.files[path][:limit]

        async def chunks():
            for offset in range(0, len(data), chunk_size):
                yield data[offset : offset + chunk_size]

        return chunks()


class FakeSandbox:
    """Explicit smoke-only SDK double. No production fallback exists."""

    def __init__(self):
        self.files = FakeFiles()
        self.id = "fake-smoke-sandbox"

    @classmethod
    async def create(cls, *args, **kwargs):
        return cls()

    async def renew(self, ttl):
        pass

    async def kill(self):
        pass

    async def close(self):
        pass


def build_app():
    # Imported only by the child interpreter in agent/.venv.
    from cloudagent.api import create_app
    from cloudagent.config import Settings
    from cloudagent.sandbox_client import OpenSandboxClient

    settings = Settings()
    if os.environ.get("RUN_OPENSANDBOX_TESTS") == "1":
        return create_app(settings)
    client = OpenSandboxClient(
        settings.opensandbox_domain,
        settings.opensandbox_api_key.get_secret_value(),
        factory=FakeSandbox.create,
    )
    return create_app(settings, sandbox=client)


def main():
    api_port = free_port()
    provider = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    thread = threading.Thread(target=provider.serve_forever, daemon=True)
    thread.start()
    process = None
    try:
        code = (
            "import sys; sys.path.insert(0, " + repr(str(ROOT / "scripts")) + "); "
            "import uvicorn; from smoke_test import build_app; "
            "uvicorn.run(build_app(), host='127.0.0.1', port="
            + str(api_port)
            + ", log_level='warning')"
        )
        environment = {
            **os.environ,
            "CLOUDAGENT_API_KEYS": json.dumps({TEST_TOKEN: "smoke-user"}),
            "CLOUDAGENT_LLM_API_KEY": "test-provider-key",
            "CLOUDAGENT_LLM_BASE_URL": f"http://127.0.0.1:{provider.server_port}/v1",
            "CLOUDAGENT_OPENSANDBOX_API_KEY": os.environ.get(
                "CLOUDAGENT_OPENSANDBOX_API_KEY", TEST_TOKEN
            ),
            "CLOUDAGENT_AGENTS_DIR": str(ROOT / "agent/agents"),
        }
        process = subprocess.Popen(
            [str(ROOT / "agent/.venv/bin/python"), "-c", code],
            cwd=ROOT / "agent",
            env=environment,
        )
        ready(f"http://127.0.0.1:{api_port}/healthz", process)
        payload = {
            "model": "assistant",
            "messages": [{"role": "user", "content": "Write and read a file"}],
        }
        result = json.loads(
            request(f"http://127.0.0.1:{api_port}/v1/chat/completions", payload)
        )
        assert result["choices"][0]["message"]["content"] == "smoke test complete", (
            result
        )
        assert result["usage"]["total_tokens"] == 6, result
        streamed = request(
            f"http://127.0.0.1:{api_port}/v1/chat/completions",
            {**payload, "stream": True},
        )
        assert "smoke test complete" in streamed and "[DONE]" in streamed, streamed
        mode = (
            "real OpenSandbox"
            if os.environ.get("RUN_OPENSANDBOX_TESTS") == "1"
            else "mock OpenSandbox SDK"
        )
        print(
            f"PASS: real HTTP, official Deep Agents, {mode}, write/read, nonstream + SSE"
        )
    finally:
        if process:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        provider.shutdown()
        provider.server_close()


if __name__ == "__main__":
    main()
