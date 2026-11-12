import pytest

from cloudagent.config import AgentConfig, AgentRegistry, Settings


def test_load_resolves_prompt_and_skills(tmp_path):
    (tmp_path / "prompt.md").write_text("be useful")
    (tmp_path / "skill.md").write_text("check sources")
    (tmp_path / "agent.yaml").write_text(
        "id: helper\nsystem_prompt_file: prompt.md\nskill_files:\n  research: skill.md\ntools: [read, load_skill]\n"
    )
    agent = AgentRegistry.load(tmp_path).get("helper")
    assert agent.system_prompt == "be useful"
    assert agent.skills == {"research": "check sources"}


def test_external_config_paths_blocked(tmp_path):
    (tmp_path / "agent.yaml").write_text("id: helper\nsystem_prompt_file: ../secret\n")
    with pytest.raises(ValueError, match="outside"):
        AgentRegistry.load(tmp_path)


def test_unknown_tools_and_cycles():
    with pytest.raises(ValueError, match="Unknown tools"):
        AgentConfig(id="a", system_prompt="hello", tools=["unrestricted"])
    with pytest.raises(ValueError, match="cycle"):
        AgentRegistry([AgentConfig(id="a", system_prompt="hello", subagents=["a"])])
    with pytest.raises(ValueError, match="Unknown subagent"):
        AgentRegistry([AgentConfig(id="a", system_prompt="hello", subagents=["b"])])


def test_keys_and_no_agents(tmp_path):
    with pytest.raises(ValueError, match="24"):
        Settings(api_keys={"short": "alice"})
    with pytest.raises(ValueError, match="No agent"):
        AgentRegistry.load(tmp_path)


def test_skill_names_cannot_inject_virtual_paths():
    with pytest.raises(ValueError):
        AgentConfig(id="a", system_prompt="hello", skills={"../../escape": "bad"})


@pytest.mark.parametrize(
    "name,value",
    [
        ("max_concurrent_model_calls", 0),
        ("max_model_calls_per_tenant", 0),
        ("max_model_calls_per_run", 0),
        ("max_subagent_invocations_per_run", -1),
        ("max_subagent_invocations_per_run", 10001),
    ],
)
def test_model_resource_limits_are_validated(name, value):
    with pytest.raises(ValueError):
        Settings(**{name: value})


def test_model_resource_limits_load_from_environment(monkeypatch):
    monkeypatch.setenv("CLOUDAGENT_MAX_CONCURRENT_MODEL_CALLS", "3")
    monkeypatch.setenv("CLOUDAGENT_MAX_MODEL_CALLS_PER_TENANT", "2")
    monkeypatch.setenv("CLOUDAGENT_MAX_MODEL_CALLS_PER_RUN", "12")
    monkeypatch.setenv("CLOUDAGENT_MAX_SUBAGENT_INVOCATIONS_PER_RUN", "0")
    settings = Settings(_env_file=None)
    assert settings.max_concurrent_model_calls == 3
    assert settings.max_model_calls_per_tenant == 2
    assert settings.max_model_calls_per_run == 12
    assert settings.max_subagent_invocations_per_run == 0
