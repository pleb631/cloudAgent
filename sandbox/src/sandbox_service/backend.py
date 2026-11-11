import base64
import hashlib
import json
import threading
from pathlib import Path

from .files import file_call
from .models import Call


def workspace_key(tenant_id: str, session_id: str) -> str:
    return hashlib.sha256(json.dumps([tenant_id, session_id]).encode()).hexdigest()


class LocalSandbox:
    """Development file-only backend. Never executes user commands."""

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()

    def call(self, request: Call) -> dict:
        if request.operation == "execute":
            raise ValueError("execute is disabled in the local development backend")
        with self.lock:
            root = self.root / workspace_key(request.tenant_id, request.session_id)
            if root.is_symlink():
                raise ValueError("workspace must not be a symlink")
            root.mkdir(exist_ok=True)
            return file_call(root, request.operation, request.arguments)

    def close(self):
        pass


class DockerSandbox:
    """Docker is defense in depth, not a hardened hostile-code boundary."""

    def __init__(self, image: str, client=None):
        if client is None:
            import docker

            client = docker.from_env(timeout=40)
        self.client = client
        self.image = image
        # Fixed striped locks prevent unbounded per-session lock growth.
        self.locks = [threading.Lock() for _ in range(64)]

    def call(self, request: Call) -> dict:
        key = workspace_key(request.tenant_id, request.session_id)
        with self.locks[int(key[:8], 16) % len(self.locks)]:
            name = "cloudagent-" + key
            self.client.volumes.create(
                name=name, labels={"cloudagent.workspace": "true", "cloudagent.owner": key}
            )
            payload = base64.b64encode(
                json.dumps(
                    {"operation": request.operation, "arguments": request.arguments}
                ).encode()
            ).decode()
            container = self.client.containers.create(
                self.image,
                [
                    "python",
                    "-I",
                    "/opt/sandbox_service/worker.py",
                    *[payload[i : i + 32768] for i in range(0, len(payload), 32768)],
                ],
                user="10001:10001",
                working_dir="/workspace",
                network_disabled=True,
                cap_drop=["ALL"],
                security_opt=["no-new-privileges:true"],
                read_only=True,
                pids_limit=64,
                mem_limit="256m",
                memswap_limit="256m",
                nano_cpus=1_000_000_000,
                init=True,
                volumes={name: {"bind": "/workspace", "mode": "rw"}},
                tmpfs={"/tmp": "rw,noexec,nosuid,size=32m,uid=10001,gid=10001"},
                log_config={"type": "json-file", "config": {"max-size": "1m", "max-file": "1"}},
                labels={"cloudagent.ephemeral": "true"},
            )
            try:
                container.start()
                container.wait(timeout=float(request.arguments.get("timeout", 10)) + 5)
                raw = container.logs(stdout=True, stderr=False)
                if len(raw) > 512_000:
                    raise ValueError("runtime response exceeds limit")
                response = json.loads(raw)
                if "error" in response:
                    raise ValueError(response["error"])
                return response["result"]
            finally:
                # Force removal kills remaining descendants even on wait timeout.
                container.remove(force=True)

    def close(self):
        self.client.close()
