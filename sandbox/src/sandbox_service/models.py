from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Call(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tenant_id: str = Field(min_length=1, max_length=128)
    session_id: str = Field(min_length=1, max_length=128)
    operation: Literal["execute", "read", "write", "edit", "ls", "grep"]
    arguments: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_arguments(self):
        required = {
            "execute": {"command"},
            "read": {"path"},
            "write": {"path", "content"},
            "edit": {"path", "old", "new"},
            "ls": set(),
            "grep": {"pattern"},
        }
        allowed = {
            "execute": {"command", "timeout"},
            "read": {"path"},
            "write": {"path", "content"},
            "edit": {"path", "old", "new"},
            "ls": {"path"},
            "grep": {"pattern", "path"},
        }
        if (
            not required[self.operation] <= self.arguments.keys()
            or self.arguments.keys() - allowed[self.operation]
        ):
            raise ValueError("missing or unknown arguments")
        for key, value in self.arguments.items():
            if key == "timeout":
                if (
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not 0 < value <= 30
                ):
                    raise ValueError("timeout must be greater than 0 and at most 30 seconds")
            elif not isinstance(value, str) or len(value.encode()) > 65536:
                raise ValueError("arguments must be strings of at most 64 KiB")
        if self.operation == "edit" and not self.arguments["old"]:
            raise ValueError("old must not be empty")
        return self
