from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from sandbox_service.backend import DockerSandbox, LocalSandbox, workspace_key
from sandbox_service.manager import Settings, create_app
from sandbox_service.models import Call


def req(operation, arguments, tenant="a", session="s"):
    return Call(tenant_id=tenant, session_id=session, operation=operation, arguments=arguments)


def test_local_isolation_and_edit(tmp_path):
    backend = LocalSandbox(tmp_path)
    backend.call(req("write", {"path": "dir/a", "content": "hello"}))
    backend.call(req("edit", {"path": "dir/a", "old": "hello", "new": "bye"}))
    assert backend.call(req("read", {"path": "dir/a"}))["content"] == "bye"
    with pytest.raises(ValueError):
        backend.call(req("read", {"path": "dir/a"}, tenant="b"))
    with pytest.raises(ValueError):
        backend.call(req("execute", {"command": "id"}))
    assert workspace_key("ab", "c") != workspace_key("a", "bc")


@pytest.mark.parametrize("path", ["../secret", "/etc/passwd", "a/../../secret"])
def test_traversal(tmp_path, path):
    with pytest.raises(ValueError):
        LocalSandbox(tmp_path).call(req("read", {"path": path}))


def test_symlink(tmp_path):
    backend = LocalSandbox(tmp_path)
    backend.call(req("write", {"path": "a", "content": "ok"}))
    root = tmp_path / workspace_key("a", "s")
    (root / "link").symlink_to(tmp_path)
    with pytest.raises(ValueError):
        backend.call(req("write", {"path": "link/escape", "content": "bad"}))


def test_auth_and_validation(tmp_path):
    settings = Settings(token="t" * 32, backend="local", local_root=tmp_path)
    with TestClient(create_app(settings)) as client:
        payload = req("ls", {}).model_dump()
        assert client.post("/v1/call", json=payload).status_code == 401
        headers = {"Authorization": "Bearer " + "t" * 32}
        assert client.post("/v1/call", headers=headers, json=payload).status_code == 200
        payload["operation"] = "execute"
        payload["arguments"] = {"command": "sleep 100", "timeout": 100}
        assert client.post("/v1/call", headers=headers, json=payload).status_code == 422
        assert client.post("/v1/call", headers=headers, content="x" * 262145).status_code == 413


def test_docker_limits_cleanup_and_timeout():
    client = MagicMock()
    container = client.containers.create.return_value
    container.logs.return_value = b'{"result":{"content":"ok"}}'
    backend = DockerSandbox("runtime", client)
    assert backend.call(req("read", {"path": "a"})) == {"content": "ok"}
    kwargs = client.containers.create.call_args.kwargs
    assert kwargs["network_disabled"] and kwargs["read_only"]
    assert kwargs["user"] == "10001:10001"
    assert kwargs["cap_drop"] == ["ALL"]
    assert kwargs["pids_limit"] == 64
    assert list(kwargs["volumes"])[0].startswith("cloudagent-")
    container.remove.assert_called_with(force=True)
    container.wait.side_effect = TimeoutError("timeout")
    with pytest.raises(TimeoutError):
        backend.call(req("execute", {"command": "sleep 99", "timeout": 0.1}))
    assert container.remove.call_count == 2


def test_runtime_uses_isolated_python_and_chunked_arguments():
    import base64
    import json

    client = MagicMock()
    client.containers.create.return_value.logs.return_value = b'{"result":{}}'
    backend = DockerSandbox("runtime", client)
    request = req("edit", {"path": "a", "old": "x" * 65536, "new": "y" * 65536})
    backend.call(request)
    command = client.containers.create.call_args.args[1]
    assert command[:3] == ["python", "-I", "/opt/sandbox_service/worker.py"]
    assert all(len(chunk) <= 32768 for chunk in command[3:])
    assert json.loads(base64.b64decode("".join(command[3:])))["arguments"] == request.arguments


def test_docker_start_failure_removes_created_container():
    client = MagicMock()
    container = client.containers.create.return_value
    container.start.side_effect = RuntimeError("start failed")
    with pytest.raises(RuntimeError, match="start failed"):
        DockerSandbox("runtime", client).call(req("ls", {}))
    container.remove.assert_called_once_with(force=True)
    client.containers.run.assert_not_called()
    client.images.pull.assert_not_called()


def test_manager_rejects_saturated_capacity(tmp_path):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    entered = threading.Event()
    release = threading.Event()
    backend = MagicMock()

    def blocking_call(request):
        entered.set()
        assert release.wait(timeout=5)
        return {"entries": []}

    backend.call.side_effect = blocking_call
    settings = Settings(token="t" * 32, backend="local", local_root=tmp_path, max_concurrent=1)
    with TestClient(create_app(settings, backend)) as client, ThreadPoolExecutor() as pool:
        headers = {"Authorization": "Bearer " + "t" * 32}
        payload = req("ls", {}).model_dump()
        first = pool.submit(client.post, "/v1/call", headers=headers, json=payload)
        try:
            assert entered.wait(timeout=5)
            response = client.post("/v1/call", headers=headers, json=payload)
            assert response.status_code == 429
            assert backend.call.call_count == 1
        finally:
            release.set()
        assert first.result(timeout=5).status_code == 200
        assert client.post("/v1/call", headers=headers, json=payload).status_code == 200
