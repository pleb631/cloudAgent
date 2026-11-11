"""Opt-in integration tests against a disposable Docker daemon."""

import os
import shlex
import uuid

import pytest

from sandbox_service.backend import DockerSandbox, workspace_key
from sandbox_service.models import Call

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_DOCKER_TESTS") != "1",
    reason="requires opt-in Docker daemon and runtime image",
)


@pytest.mark.docker
def test_real_runtime_persistence_isolation_and_limits():
    backend = DockerSandbox(os.environ.get("SANDBOX_IMAGE", "cloudagent-sandbox-runtime:local"))
    tenant, session = "test-" + uuid.uuid4().hex, uuid.uuid4().hex

    def call(operation, arguments, owner=tenant):
        return backend.call(
            Call(tenant_id=owner, session_id=session, operation=operation, arguments=arguments)
        )

    try:
        call("write", {"path": "hello.txt", "content": "persisted"})
        malicious = "from pathlib import Path; Path('/workspace/pwned').write_text('bad')"
        call("write", {"path": "sandbox_service/__init__.py", "content": malicious})
        call("write", {"path": "sitecustomize.py", "content": malicious})
        assert call("read", {"path": "hello.txt"})["content"] == "persisted"
        with pytest.raises(ValueError):
            call("read", {"path": "pwned"})
        with pytest.raises(ValueError):
            call("read", {"path": "hello.txt"}, owner=tenant + "-other")
        assert call("execute", {"command": "id -u"})["output"].strip() == "10001"
        command = (
            "from pathlib import Path; "
            "s=dict(line.split(':',1) for line in Path('/proc/self/status').read_text().splitlines()); "
            "assert int(s['CapEff'].strip(),16)==0; assert s['NoNewPrivs'].strip()=='1'; "
            "mounts=Path('/proc/mounts').read_text().splitlines(); "
            "assert any(x.split()[1]=='/' and 'ro' in x.split()[3].split(',') for x in mounts); "
            "assert not Path('/var/run/docker.sock').exists(); print('restricted')"
        )
        constraints = call("execute", {"command": "python -I -c " + shlex.quote(command)})
        assert constraints["exit_code"] == 0, constraints
        assert constraints["output"].strip() == "restricted"

        result = call(
            "execute",
            {
                "command": "python -c \"import socket; s=socket.socket(); s.settimeout(1); s.connect(('1.1.1.1',443))\""
            },
        )
        assert result["exit_code"] != 0
        assert call("execute", {"command": "sleep 10", "timeout": 0.2})["timed_out"]
        assert call("execute", {"command": "python -c \"print('x'*100000)\""})["truncated"]
        call("execute", {"command": "ln -s /etc/passwd escaped"})
        with pytest.raises(ValueError):
            call("read", {"path": "escaped"})
    finally:
        for owner in (tenant, tenant + "-other"):
            backend.client.volumes.get("cloudagent-" + workspace_key(owner, session)).remove()
        backend.close()
