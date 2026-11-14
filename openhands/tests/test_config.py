import pytest

from cloudagent_oh.config import AgentConfig, AgentRegistry, Settings


def test_load_resolves_prompt_and_skills(tmp_path):
    (tmp_path / "prompt.md").write_text("be useful")
    (tmp_path / "skill.md").write_text("check sources")
    (tmp_path / "agent.yaml").write_text(
        "id: helper\nsystem_prompt_file: prompt.md\nskill_files:\n  research: skill.md\ntools: [file_editor, grep]\n"
    )
    agent = AgentRegistry.load(tmp_path).get("helper")
    assert agent.system_prompt == "be useful"
    assert agent.skills == {"research": "check sources"}


def test_external_config_paths_blocked(tmp_path):
    (tmp_path / "agent.yaml").write_text("id: helper\nsystem_prompt_file: ../secret\n")
    with pytest.raises(ValueError, match="outside"):
        AgentRegistry.load(tmp_path)


def test_keys_and_no_agents(tmp_path):
    with pytest.raises(ValueError, match="24"):
        Settings(api_keys={"short": "alice"})
    with pytest.raises(ValueError, match="No agent"):
        AgentRegistry.load(tmp_path)


def test_skill_names_cannot_inject_virtual_paths():
    with pytest.raises(ValueError):
        AgentConfig(id="a", system_prompt="hello", skills={"../../escape": "bad"})


def agent(id="root", **kwargs):
    return AgentConfig(id=id, system_prompt="help", **kwargs)


def test_native_tools_only():
    with pytest.raises(ValueError, match="Unknown tools"):
        agent(tools=["execute"])
    with pytest.raises(ValueError, match="Duplicate"):
        agent(tools=["terminal", "terminal"])


def test_leaf_subagents_and_subset():
    root = agent(tools=["task_tool_set", "grep"], subagents=["child"])
    AgentRegistry([root, agent("child", tools=["grep", "task_tracker"])])
    with pytest.raises(ValueError, match="subset"):
        AgentRegistry([root, agent("child", tools=["terminal"])])
    with pytest.raises(ValueError, match="leaf"):
        AgentRegistry([root, agent("child", tools=["task_tool_set"])])
    with pytest.raises(ValueError, match="require task_tool_set"):
        AgentRegistry([agent(subagents=["child"]), agent("child")])
    with pytest.raises(ValueError, match="Unknown subagent"):
        AgentRegistry([root])
    with pytest.raises(ValueError, match="leaf"):
        AgentRegistry([agent(tools=["task_tool_set"], subagents=["root"])])


def test_workspace_roots_are_operator_absolute_nonoverlapping_paths(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    assert Settings(workspace_roots={"a": a, "b": b}).workspace_roots["a"] == a
    for roots in (
        {"a": a, "b": a},
        {"a": tmp_path, "b": a},
        {"a": "relative"},
        {"a": a / "missing"},
    ):
        with pytest.raises(ValueError):
            Settings(workspace_roots=roots)
    link = tmp_path / "link"
    link.symlink_to(a, target_is_directory=True)
    with pytest.raises(ValueError, match="symlinks"):
        Settings(workspace_roots={"a": link})


@pytest.mark.parametrize(
    "name,value",
    [
        ("max_concurrent_runs", 0),
        ("max_runs_per_tenant", 0),
        ("sandbox_cpus", 0),
        ("sandbox_pids", 0),
        ("sandbox_memory", "64m"),
        ("max_output_bytes", 0),
    ],
)
def test_resource_limits_are_validated(name, value):
    with pytest.raises(ValueError):
        Settings(**{name: value})


def test_environment_is_separate(monkeypatch):
    monkeypatch.setenv("CLOUDAGENT_MAX_CONCURRENT_RUNS", "50")
    monkeypatch.setenv("CLOUDAGENT_OH_MAX_CONCURRENT_RUNS", "3")
    assert Settings(_env_file=None).max_concurrent_runs == 3


@pytest.mark.parametrize("overrides", [{"model": "other-model"}, {"temperature": 0.7}])
def test_subagent_model_configuration_must_match_parent(overrides):
    with pytest.raises(ValueError, match="inherit parent model and temperature"):
        AgentRegistry(
            [
                agent(tools=["task_tool_set"], subagents=["child"]),
                agent("child", **overrides),
            ]
        )
