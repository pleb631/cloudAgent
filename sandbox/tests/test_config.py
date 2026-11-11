"""Validate our deployment against the pinned official server schema."""

from pathlib import Path

from opensandbox_server.config import load_config


def test_deployment_config(monkeypatch):
    monkeypatch.setenv("OPENSANDBOX_SERVER_API_KEY", "test-key-not-a-real-secret")
    config = load_config(Path(__file__).parents[1] / "config.toml")
    assert config.server.host == "127.0.0.1"
    assert config.server.api_key == "test-key-not-a-real-secret"
    assert config.runtime.type == "docker"
    assert config.runtime.execd_image == "opensandbox/execd:release-1.2.0"
    assert config.docker.network_mode == "bridge"
    assert config.docker.publish_host == "127.0.0.1"
    assert config.docker.drop_capabilities == ["ALL"]
    assert config.docker.no_new_privileges
    assert config.docker.pids_limit == 256
    assert config.docker.sandbox_binds == []
    assert config.storage.allowed_host_paths
    assert config.egress.mode == "dns+nft"
    assert config.egress.disable_ipv6
    assert config.egress.image == "opensandbox/egress:release-1.2.0"
