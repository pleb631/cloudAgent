"""Static deployment contract checks; these do not launch Docker containers."""

from pathlib import Path

import yaml

from cloudagent.config import Settings

ROOT = Path(__file__).resolve().parents[2]


def read_example(path):
    return {
        key: value
        for line in path.read_text().splitlines()
        if line and not line.startswith("#")
        for key, value in [line.split("=", 1)]
    }


def test_compose_exposes_all_runtime_settings():
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text())
    env = compose["services"]["agent"]["environment"]
    # These belong to the Compose topology, not to user configuration. The key
    # is intentionally remapped so both trusted services use the same secret.
    fixed = {"agents_dir", "opensandbox_domain", "opensandbox_protocol", "opensandbox_api_key"}
    for name in Settings.model_fields:
        if name not in fixed:
            key = "CLOUDAGENT_" + name.upper()
            assert key in env, f"Compose silently ignores {key}"
            assert env[key].startswith("${" + key + ":")
    assert env["CLOUDAGENT_AGENTS_DIR"] == "/app/agents"
    assert env["CLOUDAGENT_OPENSANDBOX_DOMAIN"] == "127.0.0.1:8090"
    assert "OPENSANDBOX_SERVER_API_KEY" in env["CLOUDAGENT_OPENSANDBOX_API_KEY"]


def test_examples_and_compose_match_capacity_timeout_defaults():
    env = yaml.safe_load((ROOT / "compose.yaml").read_text())["services"]["agent"]["environment"]
    root_example = read_example(ROOT / ".env.example")
    agent_example = read_example(ROOT / "agent/.env.example")
    for name, field in Settings.model_fields.items():
        if name.startswith("max_") or name.endswith("timeout") or name.startswith("sandbox_"):
            key = "CLOUDAGENT_" + name.upper()
            expected = str(field.default)
            assert root_example[key] == expected
            assert agent_example[key] == expected
            assert env[key] == "${" + key + ":-" + expected + "}"


def test_single_worker_and_control_plane_only_limits():
    services = yaml.safe_load((ROOT / "compose.yaml").read_text())["services"]
    command = services["agent"]["command"]
    assert command[command.index("--workers") + 1] == "1"
    for service in services.values():
        assert service["mem_limit"]
        assert service["cpus"]
        assert service["pids_limit"]
    assert "/var/run/docker.sock:/var/run/docker.sock" not in services["agent"]["volumes"]
