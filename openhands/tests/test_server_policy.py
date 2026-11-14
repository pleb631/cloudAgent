"""Contract tests intentionally import the real pinned SDK, without Docker/LLM."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

SHIM_PATH = Path(__file__).resolve().parents[1] / "server" / "server.py"
spec = importlib.util.spec_from_file_location("cloudagent_oh_server_policy", SHIM_PATH)
policy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(policy)


@pytest.fixture
def sdk(monkeypatch):
    from openhands.sdk.conversation.impl import local_conversation

    # Register original values with monkeypatch before the shim replaces them,
    # ensuring that unrelated tests see the untouched upstream implementation.
    for name in ("load_available_plugins", "register_file_agents"):
        monkeypatch.setattr(local_conversation, name, getattr(local_conversation, name))
    return local_conversation


def test_real_sdk_discovery_call_sites_are_inert(sdk, monkeypatch):
    policy.apply_policy()

    def forbidden(*args, **kwargs):
        raise AssertionError("Ambient discovery must not read files")

    monkeypatch.setattr(Path, "open", forbidden)
    monkeypatch.setattr(Path, "iterdir", forbidden)
    monkeypatch.setattr(Path, "glob", forbidden)
    monkeypatch.setattr(Path, "exists", forbidden)
    assert (
        sdk.load_available_plugins(work_dir="/untrusted", include_user=True, include_project=True)
        == {}
    )
    assert sdk.register_file_agents("/untrusted", cipher=None) == []
    conversation = SimpleNamespace(
        workspace=SimpleNamespace(working_dir="/untrusted"), _cipher=None
    )
    sdk.LocalConversation._register_file_based_agents(conversation)
    assert sdk.load_available_plugins is policy._no_plugins
    assert sdk.register_file_agents is policy._no_file_agents


@pytest.mark.parametrize(
    "distribution", ["openhands-sdk", "openhands-tools", "openhands-agent-server"]
)
def test_version_mismatch_fails_closed(sdk, monkeypatch, distribution):
    original = sdk.register_file_agents
    monkeypatch.setattr(
        policy, "version", lambda name: "1.63.0" if name == distribution else "1.62.0"
    )
    with pytest.raises(RuntimeError, match="exactly 1.62.0"):
        policy.apply_policy()
    assert sdk.register_file_agents is original


def test_native_entrypoint_runs_after_policy(sdk, monkeypatch):
    from openhands.agent_server import __main__ as native

    called = []

    def run():
        assert sdk.register_file_agents is policy._no_file_agents
        assert sdk.load_available_plugins is policy._no_plugins
        called.append(True)

    monkeypatch.setattr(native, "main", run)
    monkeypatch.setattr(policy.sys, "argv", [str(SHIM_PATH), "--port", "8000"])
    policy.main()
    assert called == [True]


def test_reload_cannot_bypass_policy(monkeypatch):
    monkeypatch.setattr(policy.sys, "argv", [str(SHIM_PATH), "--reload"])
    with pytest.raises(RuntimeError, match="Reload is disabled"):
        policy.main()


def test_image_builds_locked_policy_interpreter():
    dockerfile = (SHIM_PATH.parent / "Dockerfile").read_text()
    assert "UV_PROJECT_ENVIRONMENT=/opt/cloudagent/venv" in dockerfile
    assert "uv sync --frozen" in dockerfile
    assert '"/opt/cloudagent/venv/bin/python"' in dockerfile
    assert "/agent-server/.venv" not in dockerfile.split("ENTRYPOINT", 1)[1]
    assert "USER openhands" in dockerfile
