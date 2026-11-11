"""Validated, operator-controlled agent definitions. Reload by restarting the service."""

from pathlib import Path
from typing import Annotated

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Name = Annotated[str, Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")]
ALLOWED_TOOLS = {"execute", "read", "write", "edit", "ls", "grep", "todo", "load_skill", "task"}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CLOUDAGENT_", env_file=".env", extra="ignore")

    agents_dir: Path = Path("agents")
    api_keys: dict[str, Name] = Field(default_factory=dict, repr=False)
    llm_api_key: SecretStr = SecretStr("")
    llm_base_url: str = "https://api.openai.com/v1"
    sandbox_url: str = "http://127.0.0.1:8001"
    sandbox_token: SecretStr = SecretStr("")
    max_concurrent_runs: int = Field(default=20, ge=1, le=1000)
    max_runs_per_tenant: int = Field(default=4, ge=1, le=100)
    request_timeout: float = Field(default=120, ge=1, le=1800)
    run_timeout: float = Field(default=300, ge=1, le=3600)
    max_request_bytes: int = Field(default=1_048_576, ge=1024, le=10_485_760)

    @field_validator("api_keys")
    @classmethod
    def validate_keys(cls, value: dict[str, str]) -> dict[str, str]:
        if any(len(key) < 24 for key in value):
            raise ValueError("API keys must contain at least 24 characters")
        return value


class AgentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: Name
    description: str = Field(default="", max_length=2000)
    system_prompt: str = Field(min_length=1, max_length=100_000)
    model: str = Field(default="gpt-4.1-mini", min_length=1, max_length=256)
    temperature: float = Field(default=0, ge=0, le=2)
    tools: list[str] = Field(default_factory=list)
    skills: dict[str, str] = Field(default_factory=dict)
    subagents: list[Name] = Field(default_factory=list)
    max_iterations: int = Field(default=12, ge=1, le=50)

    @field_validator("tools")
    @classmethod
    def validate_tools(cls, value: list[str]) -> list[str]:
        unknown = set(value) - ALLOWED_TOOLS
        if unknown:
            raise ValueError(f"Unknown tools: {', '.join(sorted(unknown))}")
        if len(value) != len(set(value)):
            raise ValueError("Duplicate tools are not allowed")
        return value


def read_config_file(root: Path, name: str) -> str:
    path = (root / name).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError(f"Config resource is missing or outside agents_dir: {name}")
    if path.stat().st_size > 100_000:
        raise ValueError(f"Config resource exceeds 100 KB: {name}")
    return path.read_text(encoding="utf-8")


class AgentRegistry:
    def __init__(self, agents: list[AgentConfig]):
        self.agents = {agent.id: agent for agent in agents}
        if not agents:
            raise ValueError("No agent definitions found")
        if len(agents) != len(self.agents):
            raise ValueError("Duplicate agent IDs")
        for agent in agents:
            for child in agent.subagents:
                if child not in self.agents:
                    raise ValueError(f"Unknown subagent {child!r} in {agent.id!r}")

        # Graph cycles are rejected instead of relying only on runtime depth limits.
        def visit(name: str, ancestors: set[str]) -> None:
            if name in ancestors:
                raise ValueError(f"Subagent cycle involving {name!r}")
            for child in self.agents[name].subagents:
                visit(child, ancestors | {name})

        for name in self.agents:
            visit(name, set())

    @classmethod
    def load(cls, directory: Path) -> "AgentRegistry":
        root = directory.resolve()
        agents = []
        for path in sorted(root.glob("*.yaml")):
            raw = yaml.safe_load(read_config_file(root, path.name))
            if not isinstance(raw, dict):
                raise ValueError(f"Agent config must be a mapping: {path.name}")
            raw = dict(raw)
            if "system_prompt_file" in raw:
                if "system_prompt" in raw:
                    raise ValueError("Choose system_prompt or system_prompt_file, not both")
                raw["system_prompt"] = read_config_file(root, raw.pop("system_prompt_file"))
            skill_files = raw.pop("skill_files", {})
            if not isinstance(skill_files, dict):
                raise ValueError("skill_files must map names to relative paths")
            if raw.get("skills"):
                raise ValueError("Use skill_files in YAML; skills is an internal resolved field")
            raw["skills"] = {
                name: read_config_file(root, filename) for name, filename in skill_files.items()
            }
            agents.append(AgentConfig.model_validate(raw))
        return cls(agents)

    def get(self, agent_id: str) -> AgentConfig:
        return self.agents[agent_id]

    def list(self) -> list[AgentConfig]:
        return list(self.agents.values())
