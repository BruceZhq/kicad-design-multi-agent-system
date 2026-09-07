"""Serializable contracts shared by the controller and isolated CAD executor."""

from __future__ import annotations

import base64
from pathlib import PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrongRepairOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    model: str = Field(min_length=1, max_length=120)
    reasoning_effort: str = Field(default="high", max_length=16)


class RepairLimits(BaseModel):
    """Server policy, not values the model or browser may increase."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    max_turns: int = Field(default=10, ge=1, le=20)
    max_script_seconds: int = Field(default=90, ge=1, le=120)
    max_total_seconds: int = Field(default=600, ge=30, le=1800)
    max_llm_tokens: int = Field(default=60_000, ge=1000, le=200_000)
    stagnation_threshold: int = Field(default=2, ge=1, le=6)
    max_sessions_per_run: int = Field(default=2, ge=1, le=4)


class SandboxFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1, max_length=240)
    data: str = Field(max_length=24_000_000)

    @field_validator("path")
    @classmethod
    def safe_path(cls, value: str) -> str:
        path = PurePosixPath(value)
        if (
            path.is_absolute()
            or ".." in path.parts
            or "\\" in value
            or ":" in value
            or any(part.startswith(".") for part in path.parts)
            or path.suffix
            not in {".kicad_pcb", ".kicad_pro", ".kicad_dru", ".kicad_mod", ".kicad_sym", ".json"}
        ):
            raise ValueError("only relative engineering file paths are accepted")
        return value

    @field_validator("data")
    @classmethod
    def valid_base64(cls, value: str) -> str:
        base64.b64decode(value, validate=True)
        return value


class SandboxRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    files: list[SandboxFile] = Field(min_length=1, max_length=100)
    script: str = Field(min_length=1, max_length=64_000)
    pcb_name: str = Field(min_length=1, max_length=160)
    timeout_seconds: int = Field(default=90, ge=1, le=120)

    @model_validator(mode="after")
    def bounded_files(self):
        names = [f.path for f in self.files]
        if len(set(names)) != len(names) or sum(len(f.data) for f in self.files) > 32_000_000:
            raise ValueError("duplicate paths or input archive too large")
        if (
            self.pcb_name not in names
            or not self.pcb_name.endswith(".kicad_pcb")
            or "/" in self.pcb_name
        ):
            raise ValueError("pcb_name must identify a supplied root-level PCB")
        return self


class SandboxResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["completed", "failed", "timeout"]
    pcb_data: str | None = None
    output: str = Field(default="", max_length=16000)
    exit_code: int | None = None


class RepairProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["execute_python", "stop"]
    rationale: str = Field(min_length=1, max_length=2000)
    script: str = Field(default="", max_length=64000)

    @model_validator(mode="after")
    def needs_script(self):
        if self.action == "execute_python" and not self.script.strip():
            raise ValueError("execute_python requires a real script")
        return self
