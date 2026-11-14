"""Validated, operator-controlled agent definitions. Reload by restarting the service."""

from pathlib import Path
from typing import Annotated

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Name = Annotated[str, Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")]
ALLOWED_TOOLS = {"terminal", "file_editor", "glob", "grep", "task_tracker", "task_tool_set"}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CLOUDAGENT_OH_", env_file=".env", extra="ignore")

    agents_dir: Path = Path("agents")
    api_keys: dict[str, Name] = Field(default_factory=dict, repr=False)
    workspace_roots: dict[Name, Path] = Field(default_factory=dict)
    llm_api_key: SecretStr = SecretStr("")
    llm_base_url: str = "https://api.openai.com/v1"
    server_image: str = Field(
        default="cloudagent-openhands-server:1.62.0", min_length=1, max_length=256
    )
    max_concurrent_runs: int = Field(default=4, ge=1, le=1000)
    max_runs_per_tenant: int = Field(default=1, ge=1, le=100)
    run_timeout: float = Field(default=300, ge=1, le=3600)
    request_timeout: float = Field(default=60, ge=1, le=1800)
    max_request_bytes: int = Field(default=1_048_576, ge=1024, le=10_485_760)
    max_output_bytes: int = Field(default=1_048_576, ge=1024, le=10_485_760)
    sandbox_memory: str = Field(default="1g", pattern=r"^[1-9][0-9]*[mg]$")
    sandbox_cpus: float = Field(default=1, ge=0.1, le=8)
    sandbox_pids: int = Field(default=256, ge=32, le=4096)
    deployment_id: Name = "cloudagent-oh"
    state_dir: Path = Path("/tmp/cloudagent-oh-state")

    @field_validator("sandbox_memory")
    @classmethod
    def validate_memory(cls, value: str) -> str:
        mib = int(value[:-1]) * (1024 if value.endswith("g") else 1)
        if not 128 <= mib <= 8192:
            raise ValueError("Sandbox memory must be between 128m and 8g")
        return value

    @field_validator("workspace_roots")
    @classmethod
    def validate_roots(cls, value: dict[str, Path]) -> dict[str, Path]:
        roots = {}
        for tenant, path in value.items():
            if not path.is_absolute() or not path.is_dir():
                raise ValueError("Workspace roots must be absolute existing directories")
            if any(part.is_symlink() for part in (path, *path.parents)):
                raise ValueError("Workspace roots cannot contain symlinks")
            resolved = path.resolve()
            if any(
                resolved.is_relative_to(other) or other.is_relative_to(resolved)
                for other in roots.values()
            ):
                raise ValueError("Tenant workspace roots must be distinct and nonoverlapping")
            roots[tenant] = resolved
        return roots

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
    skills: dict[Name, str] = Field(default_factory=dict)
    subagents: list[Name] = Field(default_factory=list)
    max_budget: float | None = Field(default=None, gt=0)
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
    if not isinstance(name, str) or Path(name).is_absolute():
        raise ValueError("Config resources must use relative paths inside agents_dir")
    root = root.resolve()
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

        for agent in agents:
            if agent.subagents and "task_tool_set" not in agent.tools:
                raise ValueError("Agents with subagents require task_tool_set")
            for name in agent.subagents:
                child = self.agents[name]
                if child.model != agent.model or child.temperature != agent.temperature:
                    raise ValueError("Subagents must inherit parent model and temperature")
                if child.subagents or "task_tool_set" in child.tools:
                    raise ValueError("Subagents must be leaf agents without task_tool_set")
                if set(child.tools) - set(agent.tools) - {"task_tracker"}:
                    raise ValueError("Subagent tools must be a subset of parent tools")

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
