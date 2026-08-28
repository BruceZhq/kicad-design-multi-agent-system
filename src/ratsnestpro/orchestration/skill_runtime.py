"""Discover and load governed runtime skills from source trees or container mounts."""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

_SKILL_ROOT_ENV = "RATSNESTPRO_AGENT_SKILLS_DIR"
_SKILL_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_STEP_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
_CAPABILITY_NAME = re.compile(r"^[a-z][a-z0-9_.-]*$")
_FRONTMATTER_FIELDS = frozenset({
    "name",
    "description",
    "mode",
    "applies_to_steps",
    "allowed_capabilities",
    "required_gates",
    "write_scope",
})


class SkillMode(StrEnum):
    EXECUTE = "execute"
    REFLECT = "reflect"


class SkillDefinitionError(ValueError):
    pass


@dataclass(frozen=True)
class RuntimeSkill:
    name: str
    description: str
    mode: SkillMode
    applies_to_steps: tuple[str, ...]
    allowed_capabilities: frozenset[str]
    required_gates: tuple[str, ...]
    write_scope: tuple[str, ...]
    instructions: str
    source_path: Path
    digest: str

    def applies_to(self, step: str | object) -> bool:
        value = _step_value(step)
        return value in self.applies_to_steps or "*" in self.applies_to_steps

    def allows(self, capability: str) -> bool:
        return capability in self.allowed_capabilities


@dataclass(frozen=True)
class SkillCatalog:
    roots: tuple[Path, ...]
    skills: tuple[RuntimeSkill, ...]

    @property
    def digest(self) -> str:
        payload = "\n".join(
            f"{skill.name}:{skill.digest}" for skill in sorted(self.skills, key=lambda item: item.name)
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def get(self, name: str) -> RuntimeSkill:
        for skill in self.skills:
            if skill.name == name:
                return skill
        raise KeyError(f"runtime skill {name!r} is not loaded")

    def select(self, step: str | object, *, mode: SkillMode | str = SkillMode.EXECUTE) -> tuple[RuntimeSkill, ...]:
        step_value = _step_value(step)
        mode_value = SkillMode(mode)
        matches = [
            skill
            for skill in self.skills
            if skill.mode == mode_value and skill.applies_to(step_value)
        ]
        return tuple(
            sorted(
                matches,
                key=lambda skill: (step_value not in skill.applies_to_steps, skill.name),
            )
        )

    def select_one(
        self,
        step: str | object,
        *,
        mode: SkillMode | str = SkillMode.EXECUTE,
    ) -> RuntimeSkill:
        matches = self.select(step, mode=mode)
        if not matches:
            raise KeyError(f"no {SkillMode(mode).value} runtime skill applies to {_step_value(step)!r}")
        return matches[0]

    def capabilities(
        self,
        step: str | object,
        *,
        mode: SkillMode | str = SkillMode.EXECUTE,
    ) -> frozenset[str]:
        return frozenset(
            capability
            for skill in self.select(step, mode=mode)
            for capability in skill.allowed_capabilities
        )


def discover_skill_roots() -> tuple[Path, ...]:
    candidates: list[Path] = []
    configured = os.environ.get(_SKILL_ROOT_ENV, "")
    candidates.extend(Path(item).expanduser() for item in configured.split(os.pathsep) if item)
    module_path = Path(__file__).resolve()
    candidates.append(module_path.parent / "skills")
    candidates.append(Path.cwd() / "config" / "agent-skills")
    candidates.extend(parent / "config" / "agent-skills" for parent in module_path.parents)
    return _deduplicate_paths(candidates)


def load_skill(path: str | os.PathLike[str]) -> RuntimeSkill:
    source_path = Path(path).expanduser().resolve()
    if source_path.is_dir():
        source_path = source_path / "SKILL.md"
    try:
        raw = source_path.read_bytes()
    except OSError as exc:
        raise SkillDefinitionError(f"cannot read runtime skill {source_path}: {exc}") from exc
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise SkillDefinitionError(f"runtime skill must be UTF-8: {source_path}") from exc
    metadata, instructions = _parse_skill_document(text, source_path)
    unknown = set(metadata).difference(_FRONTMATTER_FIELDS)
    if unknown:
        raise SkillDefinitionError(
            f"unsupported frontmatter fields in {source_path}: {', '.join(sorted(unknown))}"
        )
    name = _required_string(metadata, "name", source_path)
    if not _SKILL_NAME.fullmatch(name):
        raise SkillDefinitionError(f"invalid runtime skill name {name!r} in {source_path}")
    if source_path.parent.name != name:
        raise SkillDefinitionError(
            f"runtime skill name {name!r} must match directory {source_path.parent.name!r}"
        )
    description = _required_string(metadata, "description", source_path)
    try:
        mode = SkillMode(str(metadata.get("mode", SkillMode.EXECUTE.value)))
    except ValueError as exc:
        raise SkillDefinitionError(f"invalid runtime skill mode in {source_path}") from exc
    applies_to_steps = _required_string_tuple(metadata, "applies_to_steps", source_path)
    invalid_steps = [
        step for step in applies_to_steps if step != "*" and not _STEP_NAME.fullmatch(step)
    ]
    if invalid_steps:
        raise SkillDefinitionError(
            f"invalid applies_to_steps in {source_path}: {', '.join(invalid_steps)}"
        )
    allowed_capabilities = frozenset(
        _required_string_tuple(metadata, "allowed_capabilities", source_path)
    )
    _validate_names(allowed_capabilities, "allowed_capabilities", source_path)
    required_gates = _optional_string_tuple(metadata, "required_gates", source_path)
    _validate_names(required_gates, "required_gates", source_path)
    write_scope = _optional_string_tuple(metadata, "write_scope", source_path)
    _validate_names(write_scope, "write_scope", source_path)
    if not instructions.strip():
        raise SkillDefinitionError(f"runtime skill instructions are empty: {source_path}")
    canonical = text.replace("\r\n", "\n").replace("\r", "\n")
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return RuntimeSkill(
        name=name,
        description=description,
        mode=mode,
        applies_to_steps=applies_to_steps,
        allowed_capabilities=allowed_capabilities,
        required_gates=required_gates,
        write_scope=write_scope,
        instructions=instructions.strip(),
        source_path=source_path,
        digest=digest,
    )


def load_skill_catalog(
    roots: str | os.PathLike[str] | Iterable[str | os.PathLike[str]] | None = None,
) -> SkillCatalog:
    candidates = _coerce_roots(roots) if roots is not None else discover_skill_roots()
    existing = tuple(path for path in candidates if path.is_dir())
    if not existing:
        searched = ", ".join(str(path) for path in candidates) or "<none>"
        raise FileNotFoundError(
            f"no runtime skill root exists; searched {searched}. "
            f"Set {_SKILL_ROOT_ENV} for a mounted container path."
        )
    loaded: dict[str, RuntimeSkill] = {}
    for root in existing:
        documents = [root / "SKILL.md"] if (root / "SKILL.md").is_file() else sorted(root.glob("*/SKILL.md"))
        for document in documents:
            skill = load_skill(document)
            loaded.setdefault(skill.name, skill)
    if not loaded:
        raise SkillDefinitionError(
            "runtime skill roots contain no <skill>/SKILL.md documents: "
            + ", ".join(str(path) for path in existing)
        )
    return SkillCatalog(
        roots=existing,
        skills=tuple(sorted(loaded.values(), key=lambda skill: skill.name)),
    )


def select_skill(
    step: str | object,
    *,
    mode: SkillMode | str = SkillMode.EXECUTE,
    roots: str | os.PathLike[str] | Iterable[str | os.PathLike[str]] | None = None,
) -> RuntimeSkill:
    return load_skill_catalog(roots).select_one(step, mode=mode)


def skill_digest(
    name: str,
    *,
    roots: str | os.PathLike[str] | Iterable[str | os.PathLike[str]] | None = None,
) -> str:
    return load_skill_catalog(roots).get(name).digest


def allowed_capabilities(
    step: str | object,
    *,
    mode: SkillMode | str = SkillMode.EXECUTE,
    roots: str | os.PathLike[str] | Iterable[str | os.PathLike[str]] | None = None,
) -> frozenset[str]:
    return load_skill_catalog(roots).capabilities(step, mode=mode)


def _parse_skill_document(text: str, path: Path) -> tuple[dict[str, object], str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise SkillDefinitionError(f"runtime skill has no YAML frontmatter: {path}")
    try:
        end = next(index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---")
    except StopIteration as exc:
        raise SkillDefinitionError(f"runtime skill frontmatter is not closed: {path}") from exc
    metadata: dict[str, object] = {}
    for line in lines[1:end]:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if line[:1].isspace():
            raise SkillDefinitionError(
                f"runtime skill frontmatter uses unsupported nested YAML in {path}: {line!r}"
            )
        key, separator, raw_value = line.partition(":")
        if not separator or not key.strip() or not raw_value.strip():
            raise SkillDefinitionError(f"invalid runtime skill frontmatter line in {path}: {line!r}")
        key = key.strip()
        if key in metadata:
            raise SkillDefinitionError(f"duplicate runtime skill field {key!r} in {path}")
        value = raw_value.strip()
        try:
            metadata[key] = json.loads(value)
        except json.JSONDecodeError:
            metadata[key] = value
    return metadata, "\n".join(lines[end + 1 :])


def _required_string(metadata: dict[str, object], key: str, path: Path) -> str:
    value = metadata.get(key)
    if not isinstance(value, str) or not value.strip():
        raise SkillDefinitionError(f"runtime skill field {key!r} must be a non-empty string: {path}")
    return value.strip()


def _required_string_tuple(
    metadata: dict[str, object],
    key: str,
    path: Path,
) -> tuple[str, ...]:
    values = _optional_string_tuple(metadata, key, path)
    if not values:
        raise SkillDefinitionError(f"runtime skill field {key!r} must not be empty: {path}")
    return values


def _optional_string_tuple(
    metadata: dict[str, object],
    key: str,
    path: Path,
) -> tuple[str, ...]:
    value = metadata.get(key, [])
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise SkillDefinitionError(f"runtime skill field {key!r} must be a JSON string list: {path}")
    return tuple(dict.fromkeys(item.strip() for item in value))


def _validate_names(values: Iterable[str], field: str, path: Path) -> None:
    invalid = [value for value in values if not _CAPABILITY_NAME.fullmatch(value)]
    if invalid:
        raise SkillDefinitionError(f"invalid {field} in {path}: {', '.join(invalid)}")


def _step_value(step: str | object) -> str:
    value = getattr(step, "value", step)
    if not isinstance(value, str) or not value.strip():
        raise ValueError("pipeline step must be a non-empty string or enum with a string value")
    return value.strip()


def _coerce_roots(
    roots: str | os.PathLike[str] | Iterable[str | os.PathLike[str]],
) -> tuple[Path, ...]:
    if isinstance(roots, (str, os.PathLike)):
        values: Iterable[str | os.PathLike[str]] = (roots,)
    else:
        values = roots
    return _deduplicate_paths(Path(value).expanduser() for value in values)


def _deduplicate_paths(paths: Iterable[Path]) -> tuple[Path, ...]:
    unique: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        resolved = path.resolve()
        key = os.path.normcase(str(resolved))
        if key not in seen:
            seen.add(key)
            unique.append(resolved)
    return tuple(unique)


__all__ = [
    "RuntimeSkill",
    "SkillCatalog",
    "SkillDefinitionError",
    "SkillMode",
    "allowed_capabilities",
    "discover_skill_roots",
    "load_skill",
    "load_skill_catalog",
    "select_skill",
    "skill_digest",
]
