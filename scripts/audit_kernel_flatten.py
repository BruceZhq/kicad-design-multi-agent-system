"""Create deterministic evidence for the flattened Python Agent kernel.

This script performs static inspection only.  It does not import the runtime,
contact external services, or mutate anything except the requested JSON file.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
KERNEL = SRC / "ratsnestpro"
_LEGACY_ARCHIVE_NAME = "RatsNest" + "Pro-main"
OLD_PATH = re.compile(
    rf"{_LEGACY_ARCHIVE_NAME}|src[/\\]RatsNestPro|"
    rf"{_LEGACY_ARCHIVE_NAME}[/\\]{_LEGACY_ARCHIVE_NAME}"
)
RUNTIME_REFERENCE = re.compile(
    r"PYTHONPATH|pip install|--editable|src[/\\]ratsnestpro|/app/ratsnestpro"
)
TEXT_FILES = (
    ROOT / "pyproject.toml",
    ROOT / ".env.example",
    ROOT / ".gitignore",
    ROOT / ".dockerignore",
    ROOT / "compose.yaml",
    ROOT / "langgraph.json",
    ROOT / "README.md",
)
KEY_PUBLIC_MODULES = (
    KERNEL / "__init__.py",
    KERNEL / "agents" / "__init__.py",
    KERNEL / "domain" / "__init__.py",
    KERNEL / "eda" / "__init__.py",
    KERNEL / "knowledge" / "__init__.py",
    KERNEL / "orchestration" / "__init__.py",
    KERNEL / "parts" / "__init__.py",
    KERNEL / "verification" / "__init__.py",
    SRC / "agents" / "agents.py",
    SRC / "agents" / "ratsnestpro" / "tools.py",
    SRC / "agents" / "ratsnestpro" / "temporal" / "contracts.py",
)
IGNORED_AUDIT_DIRECTORIES = {
    ".git",
    ".mypy_cache",
    ".next",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "node_modules",
    "target",
}


def relative(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def is_auditable_directory(path: Path) -> bool:
    try:
        relative_parts = path.relative_to(ROOT).parts
    except ValueError:
        return False
    return not any(part in IGNORED_AUDIT_DIRECTORIES for part in relative_parts)


def is_empty_directory(path: Path) -> bool:
    if not is_auditable_directory(path):
        return False
    try:
        return next(path.iterdir(), None) is None
    except OSError:
        # Local caches and tool-owned directories can be unreadable on Windows.
        # They are not source-layout evidence and must not make the audit crash.
        return False


def module_name(path: Path) -> str:
    parts = list(path.relative_to(SRC).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def public_symbols(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if not any(isinstance(target, ast.Name) and target.id == "__all__" for target in targets):
            continue
        try:
            return sorted(str(item) for item in ast.literal_eval(node.value))
        except (TypeError, ValueError):
            break
    return sorted(
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        and not node.name.startswith("_")
    )


def literal_assignment(path: Path, name: str) -> object | None:
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if not any(isinstance(target, ast.Name) and target.id == name for target in targets):
            continue
        try:
            return ast.literal_eval(node.value)
        except (TypeError, ValueError):
            return None
    return None


def scan_text_references() -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    candidates = list(TEXT_FILES)
    for directory in (ROOT / "docker", ROOT / "scripts", ROOT / "docs"):
        if directory.is_dir():
            candidates.extend(path for path in directory.rglob("*") if path.is_file())
    old_references: list[dict[str, object]] = []
    runtime_references: list[dict[str, object]] = []
    for path in sorted(set(candidates)):
        if path.resolve() == Path(__file__).resolve() or "refactor-evidence" in path.parts:
            continue
        if path.stat().st_size > 5_000_000:
            continue
        try:
            lines = path.read_text(encoding="utf-8-sig").splitlines()
        except UnicodeDecodeError:
            continue
        for line_number, line in enumerate(lines, start=1):
            evidence = {
                "file": relative(path),
                "line": line_number,
                "text": line.strip(),
            }
            if OLD_PATH.search(line):
                old_references.append(evidence)
            if RUNTIME_REFERENCE.search(line):
                runtime_references.append(evidence)
    return old_references, runtime_references


def build_snapshot(snapshot: str) -> dict[str, object]:
    modules = sorted(KERNEL.rglob("*.py"))
    resources = sorted(
        path
        for path in KERNEL.rglob("*")
        if path.is_file() and path.suffix not in {".py", ".pyc", ".pyo"}
    )
    resource_hash_lines = [f"{relative(path)}:{file_hash(path)}" for path in resources]
    old_references, runtime_references = scan_text_references()
    env_keys = []
    for line in (ROOT / ".env.example").read_text(encoding="utf-8-sig").splitlines():
        match = re.match(r"^([A-Z][A-Z0-9_]*)=", line)
        if match:
            env_keys.append(match.group(1))
    graph_config = json.loads((ROOT / "langgraph.json").read_text(encoding="utf-8-sig"))
    temporal_contracts = SRC / "agents" / "ratsnestpro" / "temporal" / "contracts.py"
    steps = literal_assignment(temporal_contracts, "CANONICAL_STEPS") or []
    agent_graph = (SRC / "agents" / "ratsnestpro" / "ratsnestpro_agent.py").read_text(
        encoding="utf-8-sig"
    )
    role_ids = sorted(
        set(re.findall(r'"((?:supervisor|sub-agent)-ratsnest-[a-z-]+)"', agent_graph))
    )
    cache_directories = sorted(
        relative(path) for path in ROOT.rglob("__pycache__") if path.is_dir()
    )
    empty_directories = sorted(
        relative(path)
        for path in ROOT.rglob("*")
        if path.is_dir() and is_empty_directory(path)
    )
    tool_path = SRC / "agents" / "ratsnestpro" / "tools.py"
    return {
        "schema_version": "ratsnestpro.kernel-flatten-audit.v1",
        "snapshot": snapshot,
        "root_layout": sorted(path.name for path in SRC.iterdir()),
        "path_checks": {
            "nested_source_exists": (SRC / _LEGACY_ARCHIVE_NAME).exists(),
            "flat_package_exists": KERNEL.is_dir(),
            "old_reference_count": len(old_references),
            "old_references": old_references,
        },
        "source": {
            "module_count": len(modules),
            "modules": [
                {"name": module_name(path), "path": relative(path), "sha256": file_hash(path)}
                for path in modules
            ],
            "resource_count": len(resources),
            "resources": [
                {"path": relative(path), "size": path.stat().st_size, "sha256": file_hash(path)}
                for path in resources
            ],
            "package_data_tree_sha256": hashlib.sha256(
                "\n".join(resource_hash_lines).encode("utf-8")
            ).hexdigest(),
        },
        "key_public_symbols": {
            module_name(path): public_symbols(path) for path in KEY_PUBLIC_MODULES if path.exists()
        },
        "registries": {
            "langgraph": graph_config.get("graphs", {}),
            "tool_functions": [
                name for name in public_symbols(tool_path) if name.startswith("ratsnest_")
            ],
            "temporal_step_count": len(steps),
            "temporal_steps": list(steps),
            "role_ids": role_ids,
        },
        "env_keys": sorted(set(env_keys)),
        "runtime_references": runtime_references,
        "cleanup_candidates": {
            "cache_directories": cache_directories,
            "cache_directory_count": len(cache_directories),
            "compiled_python_file_count": sum(
                1
                for path in ROOT.rglob("*")
                if path.is_file() and path.suffix in {".pyc", ".pyo"}
            ),
            "empty_directories": empty_directories,
        },
        "entrypoints": {
            "service": "src/run_service.py -> service:app",
            "langgraph": graph_config.get("graphs", {}),
            "temporal_worker": "agents.ratsnestpro.temporal.worker",
            "evolution_worker": "evolution.temporal.worker",
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("snapshot", choices=("before", "after"))
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    output = args.output if args.output.is_absolute() else ROOT / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(build_snapshot(args.snapshot), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
