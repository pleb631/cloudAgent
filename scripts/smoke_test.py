"""Real HTTP end-to-end smoke test: API -> fake OpenAI server -> local Sandbox Manager.

Run `make install` first, then `python scripts/smoke_test.py`. No provider credentials,
Docker, or internet required. This validates wiring, not production Docker isolation.
"""

import json
import os
import socket
import subprocess
import tempfile
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
    with urllib.request.urlopen(req, timeout=15) as response:
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


def main():
    manager_port, api_port = free_port(), free_port()
    provider = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    thread = threading.Thread(target=provider.serve_forever, daemon=True)
    thread.start()
    processes = []
    with tempfile.TemporaryDirectory(prefix="cloudagent-smoke-") as directory:
        try:

            def launch(service, module, port, variables):
                process = subprocess.Popen(
                    [
                        str(ROOT / service / ".venv/bin/python"),
                        "-m",
                        "uvicorn",
                        module,
                        "--host",
                        "127.0.0.1",
                        "--port",
                        str(port),
                        "--log-level",
                        "warning",
                    ],
                    cwd=ROOT / service,
                    env={**os.environ, **variables},
                )
                processes.append(process)
                return process

            manager = launch(
                "sandbox",
                "sandbox_service.manager:app",
                manager_port,
                {
                    "SANDBOX_TOKEN": TEST_TOKEN,
                    "SANDBOX_BACKEND": "local",
                    "SANDBOX_LOCAL_ROOT": directory,
                },
            )
            ready(f"http://127.0.0.1:{manager_port}/health", manager)
            api = launch(
                "agent",
                "cloudagent.api:app",
                api_port,
                {
                    "CLOUDAGENT_API_KEYS": json.dumps({TEST_TOKEN: "smoke-user"}),
                    "CLOUDAGENT_LLM_API_KEY": "test-provider-key",
                    "CLOUDAGENT_LLM_BASE_URL": f"http://127.0.0.1:{provider.server_port}/v1",
                    "CLOUDAGENT_SANDBOX_URL": f"http://127.0.0.1:{manager_port}",
                    "CLOUDAGENT_SANDBOX_TOKEN": TEST_TOKEN,
                    "CLOUDAGENT_AGENTS_DIR": str(ROOT / "agent/agents"),
                },
            )
            ready(f"http://127.0.0.1:{api_port}/healthz", api)
            payload = {
                "model": "assistant",
                "messages": [{"role": "user", "content": "Write and read a file"}],
            }
            result = json.loads(
                request(f"http://127.0.0.1:{api_port}/v1/chat/completions", payload)
            )
            assert (
                result["choices"][0]["message"]["content"] == "smoke test complete"
            ), result
            assert result["usage"]["total_tokens"] == 6, result
            streamed = request(
                f"http://127.0.0.1:{api_port}/v1/chat/completions",
                {**payload, "stream": True},
            )
            assert "smoke test complete" in streamed and "[DONE]" in streamed, streamed
            print(
                "PASS: real HTTP, LangGraph + OpenAI SDK, sandbox write/read, nonstream + SSE"
            )
        finally:
            for process in reversed(processes):
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            provider.shutdown()
            provider.server_close()


if __name__ == "__main__":
    main()
