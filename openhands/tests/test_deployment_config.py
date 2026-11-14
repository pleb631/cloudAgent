"""Regression checks for the branch's sole OpenHands deployment, without Docker."""

import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_default_compose_is_only_openhands():
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text())
    assert set(compose["services"]) == {"openhands-api", "openhands-server-image"}
    api = compose["services"]["openhands-api"]
    assert api["build"] == "./openhands"
    assert api["read_only"] is True
    assert api["cap_drop"] == ["ALL"]
    assert api["mem_limit"] == "2g"
    assert api["pids_limit"] == 256
    source_mount = next(v for v in api["volumes"] if isinstance(v, dict))
    assert source_mount["read_only"] is True
    assert source_mount["bind"]["create_host_path"] is False


def test_dependency_manifests_have_no_legacy_frameworks():
    manifest = tomllib.loads((ROOT / "openhands/pyproject.toml").read_text())
    deps = " ".join(manifest["project"]["dependencies"]).lower()
    assert "openhands-sdk==1.62.0" in deps
    assert "deepagents" not in deps
    assert "opensandbox" not in deps
    for directory in ("agent", "sandbox"):
        assert not (ROOT / directory / "pyproject.toml").exists()
        assert not (ROOT / directory / "Dockerfile").exists()
    assert not (ROOT / ".github/workflows/ci.yml").exists()


def test_root_env_example_supplies_required_deployment_values():
    root_example = (ROOT / ".env.example").read_text()
    child_example = (ROOT / "openhands/.env.example").read_text()

    def parse(s):
        return dict(
            line.split("=", 1) for line in s.splitlines() if line and not line.startswith("#")
        )

    values = parse(root_example)
    assert values == parse(child_example)
    for key in (
        "CLOUDAGENT_OH_API_KEYS",
        "CLOUDAGENT_OH_LLM_API_KEY",
        "CLOUDAGENT_OH_HOST_DATA_ROOT",
        "CLOUDAGENT_OH_WORKSPACE_ROOTS",
    ):
        assert values[key]
    assert not any(key.startswith("CLOUDAGENT_OPENSANDBOX") for key in values)
