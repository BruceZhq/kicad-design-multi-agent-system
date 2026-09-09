"""Portable, bounded project snapshots; no remote path or credential authority."""
import base64
import hashlib
import json
from pathlib import PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ProjectFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str
    data: str = Field(max_length=32_000_000)

    @field_validator("path")
    @classmethod
    def safe_path(cls, value):
        p = PurePosixPath(value)
        if (not value or p.is_absolute() or ".." in p.parts or "\\" in value or ":" in value
                or (value not in {"fp-lib-table", "sym-lib-table", "programs/repair_generator.py"} and p.suffix not in {".json", ".pdf", ".md", ".txt", ".csv", ".net", ".dsn", ".ses", ".kicad_pcb", ".kicad_sch", ".kicad_pro", ".kicad_dru", ".kicad_sym", ".kicad_mod"})
                or len(p.parts) > 12):
            raise ValueError("unsafe project file")
        return value

    @field_validator("data")
    @classmethod
    def valid_data(cls, value):
        base64.b64decode(value, validate=True)
        return value


class RepairTaskInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["ratsnest.external-repair.v2"] = "ratsnest.external-repair.v2"
    base_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    scope: str = Field(pattern=r"^[a-f0-9]{64}$")
    allowance: str = Field(pattern=r"^[a-f0-9]{64}$")
    model: str = Field(min_length=1, max_length=120)
    reasoning_effort: str = Field(pattern=r"^(none|minimal|low|medium|high|xhigh|max|ultra)$")
    requirement: str = Field(max_length=1_000_000)
    project_name: str = Field(max_length=120)
    artifacts: dict
    files: list[ProjectFile] = Field(max_length=512)
    dossier: dict = Field(default_factory=dict)
    joint: bool = False
    fanout_approval: dict = Field(default_factory=dict)

    def digest(self):
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()


def portable(value, source, target):
    """Rebase only snapshot artifact paths, including embedded manifest JSON."""
    return json.loads(json.dumps(value, ensure_ascii=False).replace(source, target))


def reject_host_paths(value):
    """Artifacts cannot point the independent service at its host filesystem."""
    if isinstance(value, dict):
        for item in value.values():
            reject_host_paths(item)
    elif isinstance(value, list):
        for item in value:
            reject_host_paths(item)
    elif isinstance(value, str):
        if (value.startswith(("/", "\\", "file:")) or ".." in PurePosixPath(value).parts
                or (value.startswith("@project") and value != "@project" and not value.startswith("@project/"))
                or (len(value) > 2 and value[1:3] in {":/", ":\\"})):
            raise ValueError("nonportable absolute artifact path")
        if value.startswith("{"):
            try:
                nested = json.loads(value)
            except ValueError:
                return
            reject_host_paths(nested)
