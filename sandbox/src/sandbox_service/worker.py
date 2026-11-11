"""One-shot runtime entry point; only runs inside the restricted container."""

import base64
import json
import os
import selectors
import signal
import subprocess
import sys
import time
from pathlib import Path

# Isolated Python mode excludes workspace and environment import paths.
sys.path.insert(0, "/opt")
from sandbox_service.files import MAX_BYTES, file_call  # noqa: E402


def execute(command: str, timeout: float = 10) -> dict:
    process = subprocess.Popen(
        ["/bin/sh", "-c", command],
        cwd="/workspace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    output = bytearray()
    deadline = time.monotonic() + timeout
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    timed_out = truncated = False
    try:
        while True:
            if time.monotonic() >= deadline:
                timed_out = True
                break
            events = selector.select(min(0.1, max(0, deadline - time.monotonic())))
            if events:
                data = os.read(process.stdout.fileno(), 4096)
                if not data:
                    try:
                        process.wait(timeout=max(0.01, deadline - time.monotonic()))
                    except subprocess.TimeoutExpired:
                        timed_out = True
                    break
                available = MAX_BYTES - len(output)
                output.extend(data[:available])
                if len(data) > available:
                    truncated = True
                    break
        return_code = process.poll()
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
        selector.close()
        process.stdout.close()
    return {
        "output": output.decode("utf-8", errors="replace"),
        "exit_code": return_code if return_code is not None else process.returncode,
        "timed_out": timed_out,
        "truncated": truncated,
    }


if __name__ == "__main__":
    request = json.loads(base64.b64decode("".join(sys.argv[1:])))
    try:
        result = (
            execute(**request["arguments"])
            if request["operation"] == "execute"
            else file_call(Path("/workspace"), request["operation"], request["arguments"])
        )
        print(json.dumps({"result": result}, ensure_ascii=False))
    except (OSError, ValueError, KeyError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
