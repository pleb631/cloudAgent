"""Opt-in tests of the real official server, SDK, execd and egress containers."""

import asyncio
import json
import os
import shlex
import subprocess
import time
import uuid

import httpx
import pytest

from cloudagent.sandbox_client import MAX_BYTES, OpenSandboxClient, SandboxError

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_OPENSANDBOX_TESTS") != "1",
    reason="requires a disposable official OpenSandbox server and Docker host",
)


def inspect_container(name):
    return json.loads(subprocess.check_output(["docker", "inspect", name], text=True))[0]


async def test_real_sdk_files_isolation_execution_network_and_cleanup():
    domain = os.environ.get("CLOUDAGENT_OPENSANDBOX_DOMAIN", "127.0.0.1:8090")
    api_key = os.environ["CLOUDAGENT_OPENSANDBOX_API_KEY"]
    client = OpenSandboxClient(
        domain,
        api_key,
        max_sessions=2,
        ttl_seconds=300,
        image=os.environ.get("CLOUDAGENT_SANDBOX_IMAGE", "python:3.12-slim"),
    )
    session = uuid.uuid4().hex
    ids = []
    try:
        await client.call(
            "alice", session, "write", {"path": "notes/a.txt", "content": "hello world"}
        )
        assert (await client.call("alice", session, "read", {"path": "notes/a.txt"}))[
            "content"
        ] == "hello world"
        await client.call(
            "alice", session, "edit", {"path": "notes/a.txt", "old": "world", "new": "OpenSandbox"}
        )
        listing = await client.call("alice", session, "ls", {"path": "notes"})
        assert any(entry["path"].endswith("a.txt") for entry in listing["entries"])
        matches = await client.call(
            "alice", session, "grep", {"pattern": "OpenSandbox", "path": "."}
        )
        assert matches["matches"][0]["path"] == "notes/a.txt"
        with pytest.raises(SandboxError):
            await client.call("bob", session, "read", {"path": "notes/a.txt"})
        await client.call("bob", session, "write", {"path": "notes/a.txt", "content": "bob only"})
        assert (await client.call("alice", session, "read", {"path": "notes/a.txt"}))[
            "content"
        ] == "hello OpenSandbox"
        alice = client._sessions[("alice", session)].sandbox
        bob = client._sessions[("bob", session)].sandbox
        ids = [alice.id, bob.id]
        assert len(set(ids)) == 2

        async with httpx.AsyncClient(base_url="http://" + domain, trust_env=False) as http:
            for sandbox_id in ids:
                existing = await http.get(
                    "/v1/sandboxes/" + sandbox_id, headers={"OPEN-SANDBOX-API-KEY": api_key}
                )
                assert existing.status_code == 200

        result = await client.call("alice", session, "execute", {"command": "printf success"})
        assert result["stdout"].strip() == "success" and result["exit_code"] == 0
        failed = await client.call("alice", session, "execute", {"command": "exit 7"})
        assert failed["exit_code"] == 7 and failed["failed"]
        begin = time.monotonic()
        timed_out = await client.call(
            "alice", session, "execute", {"command": "sleep 10", "timeout": 0.2}
        )
        assert timed_out["failed"] and time.monotonic() - begin < 8
        large = await client.call(
            "alice", session, "execute", {"command": "python -c \"print('x'*100000)\""}
        )
        assert large["truncated"] and len(large["stdout"].encode()) <= MAX_BYTES
        await client.call(
            "alice",
            session,
            "execute",
            {"command": "python -c \"open('/workspace/big.txt','w').write('x'*100000)\""},
        )
        with pytest.raises(SandboxError, match="64 KiB"):
            await client.call("alice", session, "read", {"path": "big.txt"})
        with pytest.raises(SandboxError):
            await client.call("alice", session, "read", {"path": "../etc/passwd"})

        for code in [
            "import socket; socket.create_connection(('1.1.1.1',443),timeout=2)",
            "import socket; socket.setdefaulttimeout(2); socket.getaddrinfo('example.com',443)",
        ]:
            denied = await client.call(
                "alice",
                session,
                "execute",
                {"command": "python -c " + shlex.quote(code), "timeout": 5},
            )
            assert denied["failed"], (
                "Explicit deny-egress policy must reject DNS and direct IP traffic"
            )

        attrs = await asyncio.to_thread(inspect_container, "sandbox-" + alice.id)
        host = attrs["HostConfig"]
        assert host["Memory"] == 512 * 1024 * 1024
        assert host["NanoCpus"] == 1_000_000_000 or (
            host["CpuQuota"] > 0 and host["CpuQuota"] == host["CpuPeriod"]
        )
        assert host["PidsLimit"] == 256
        assert "ALL" in host["CapDrop"]
        assert any(option.startswith("no-new-privileges") for option in host["SecurityOpt"])
        assert not host["Privileged"]
        assert host["NetworkMode"].startswith("container:")
        assert not any(mount["Destination"].endswith("docker.sock") for mount in attrs["Mounts"])
        sidecar = await asyncio.to_thread(inspect_container, host["NetworkMode"].split(":", 1)[1])
        bindings = sidecar["HostConfig"]["PortBindings"]
        assert all(
            binding["HostIp"] == "127.0.0.1"
            for group in bindings.values()
            if group
            for binding in group
        )
    finally:
        await client.close()

    # The real lifecycle server must confirm deletion, not merely local client closure.
    async with httpx.AsyncClient(base_url="http://" + domain, trust_env=False) as http:
        for sandbox_id in ids:
            response = await http.get(
                "/v1/sandboxes/" + sandbox_id, headers={"OPEN-SANDBOX-API-KEY": api_key}
            )
            assert response.status_code == 404, response.status_code
