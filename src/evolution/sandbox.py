"""Isolated whole-file materialization and fixed-command candidate evaluation."""

from __future__ import annotations

import difflib
import hashlib
import os
import secrets
import shutil
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import Field

from evolution.contracts import EvolutionCandidate, EvolutionModel, HarnessManifest
from evolution.optimizer import (
    GovernancePolicy,
    PatchBundle,
    PatchPlan,
    build_worktree_plan,
    validate_patch_bundle,
    validate_patch_plan,
)

_MAX_OUTPUT_BYTES = 32 * 1024
_MAX_ERROR_CHARS = 2_000


@dataclass(frozen=True, slots=True)
class EvalCommand:
    """A trusted argv registered by code, never supplied by a candidate."""

    eval_id: str
    argv: tuple[str, ...]
    timeout_seconds: int


DEFAULT_EVAL_COMMANDS: Mapping[str, EvalCommand] = {
    "python-compile": EvalCommand(
        "python-compile",
        (
            sys.executable,
            "-m",
            "compileall",
            "-q",
            "src/evolution",
            "src/agents/ratsnestpro",
        ),
        60,
    ),
    "evolution-core": EvalCommand(
        "evolution-core",
        (
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "pytest_asyncio.plugin",
            "--confcutdir=tests/evolution",
            "tests/evolution/test_evolution_core.py",
            "tests/evolution/test_sandbox.py",
        ),
        180,
    ),
}


class EvalCommandResult(EvolutionModel):
    eval_id: str = Field(min_length=1, max_length=80)
    argv: list[str] = Field(min_length=1, max_length=16)
    exit_code: int | None
    duration_ms: int = Field(ge=0)
    timed_out: bool
    output_limit_exceeded: bool
    output: str = Field(max_length=_MAX_OUTPUT_BYTES)
    passed: bool


class CandidateEvalReport(EvolutionModel):
    schema_version: Literal["1.0"] = "1.0"
    candidate_id: str = Field(min_length=1, max_length=128)
    base_commit: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    patch_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    verdict: Literal["passed", "failed", "policy_rejected", "error"]
    worktree_created: bool
    materialized_files: list[str] = Field(default_factory=list, max_length=8)
    command_results: list[EvalCommandResult] = Field(default_factory=list, max_length=8)
    error: str | None = Field(default=None, max_length=_MAX_ERROR_CHARS)
    cleanup_succeeded: bool
    executor_mode: Literal["local_process", "kubernetes_job"] = "local_process"
    automatic_merge: Literal[False] = False
    automatic_push: Literal[False] = False
    automatic_deploy: Literal[False] = False


def patch_digest(bundle: PatchBundle) -> str:
    payload = bundle.model_dump_json(by_alias=True, exclude_none=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def materialize_and_evaluate_candidate(
    *,
    candidate: EvolutionCandidate,
    harness_manifest: HarnessManifest,
    plan: PatchPlan,
    bundle: PatchBundle,
    policy: GovernancePolicy,
    repository_root: Path,
    sandbox_root: Path,
    eval_ids: Sequence[str],
    eval_registry: Mapping[str, EvalCommand] = DEFAULT_EVAL_COMMANDS,
) -> CandidateEvalReport:
    """Evaluate a candidate in a detached worktree and always remove that worktree."""

    digest = patch_digest(bundle)
    created = False
    cleanup_succeeded = True
    materialized: list[str] = []
    results: list[EvalCommandResult] = []
    verdict: Literal["passed", "failed", "policy_rejected", "error"] = "error"
    error: str | None = None
    directory: Path | None = None
    trial_root: Path | None = None

    try:
        selected = _select_eval_commands(eval_ids, eval_registry)
        if harness_manifest.calculated_manifest_digest() != harness_manifest.manifest_digest:
            raise ValueError("base harness manifest digest is invalid")
        validate_patch_plan(
            plan,
            candidate=candidate,
            harness_manifest=harness_manifest,
            policy=policy,
        )
        validate_patch_bundle(bundle, plan=plan, policy=policy)
        repository = repository_root.resolve(strict=True)
        sandbox = _validated_sandbox_root(repository, sandbox_root)
        trial_root = sandbox / f"t-{secrets.token_hex(4)}"
        trial_root.mkdir()
        worktree = build_worktree_plan(
            plan,
            repository_root=repository,
            worktree_root=trial_root,
        )
        directory = Path(worktree.directory)
        create_result = _run_argv(
            eval_id="git-worktree-add",
            argv=worktree.create_command,
            cwd=repository,
            log_directory=sandbox,
            timeout_seconds=30,
            max_output_bytes=16 * 1024,
        )
        if not create_result.passed:
            raise RuntimeError(f"detached worktree creation failed: {create_result.output}")
        created = True
        materialized = validate_and_materialize_candidate(
            candidate=candidate,
            harness_manifest=harness_manifest,
            plan=plan,
            bundle=bundle,
            policy=policy,
            worktree=directory,
            max_added_lines=policy.change_policy.max_added_lines,
        )
        for command in selected:
            result = _run_argv(
                eval_id=command.eval_id,
                argv=command.argv,
                cwd=directory,
                log_directory=sandbox,
                timeout_seconds=command.timeout_seconds,
                max_output_bytes=_MAX_OUTPUT_BYTES,
                environment=_sanitized_eval_environment(directory),
            )
            results.append(result)
            if not result.passed:
                break
        verdict = "passed" if len(results) == len(selected) and all(r.passed for r in results) else "failed"
    except ValueError as exc:
        verdict = "policy_rejected"
        error = _bounded_error(exc)
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        verdict = "error"
        error = _bounded_error(exc)
    finally:
        if directory is not None:
            cleanup_succeeded = _cleanup_worktree(repository_root, sandbox_root, directory, created)
        if trial_root is not None and trial_root.exists():
            try:
                trial_root.rmdir()
            except OSError:
                cleanup_succeeded = False

    if not cleanup_succeeded and verdict == "passed":
        verdict = "error"
        error = "worktree cleanup did not complete"

    return CandidateEvalReport(
        candidate_id=candidate.candidate_id,
        base_commit=plan.base_commit,
        patch_digest=digest,
        verdict=verdict,
        worktree_created=created,
        materialized_files=materialized,
        command_results=results,
        error=error,
        cleanup_succeeded=cleanup_succeeded,
    )


def validate_and_materialize_candidate(
    *,
    candidate: EvolutionCandidate,
    harness_manifest: HarnessManifest,
    plan: PatchPlan,
    bundle: PatchBundle,
    policy: GovernancePolicy,
    worktree: Path,
    max_added_lines: int | None = None,
) -> list[str]:
    """Validate trusted contracts and write whole files into an isolated checkout.

    This boundary deliberately does not execute candidate code.  It is shared by
    the local developer evaluator and the immutable Kubernetes materializer.
    """

    if harness_manifest.calculated_manifest_digest() != harness_manifest.manifest_digest:
        raise ValueError("base harness manifest digest is invalid")
    validate_patch_plan(
        plan,
        candidate=candidate,
        harness_manifest=harness_manifest,
        policy=policy,
    )
    validate_patch_bundle(bundle, plan=plan, policy=policy)
    return _materialize_files(
        worktree,
        bundle,
        max_added_lines=(
            policy.change_policy.max_added_lines
            if max_added_lines is None
            else max_added_lines
        ),
    )


def _select_eval_commands(
    eval_ids: Sequence[str], registry: Mapping[str, EvalCommand]
) -> list[EvalCommand]:
    if not eval_ids or len(eval_ids) > 8 or len(eval_ids) != len(set(eval_ids)):
        raise ValueError("eval IDs must be a non-empty unique bounded list")
    try:
        return [registry[item] for item in eval_ids]
    except KeyError as exc:
        raise ValueError(f"eval ID is not registered: {exc.args[0]}") from exc


def _validated_sandbox_root(repository: Path, sandbox_root: Path) -> Path:
    raw_sandbox = Path(os.path.abspath(sandbox_root))
    sandbox = raw_sandbox.resolve()
    if repository.drive.casefold() != sandbox.drive.casefold():
        raise ValueError("evolution sandbox must use the repository drive")
    if sandbox == repository or sandbox in repository.parents or repository in sandbox.parents:
        raise ValueError("evolution sandbox must be outside the repository")
    current = Path(raw_sandbox.anchor)
    for part in raw_sandbox.parts[1:]:
        current /= part
        if _is_link(current):
            raise ValueError("evolution sandbox path cannot cross a symlink or junction")
    sandbox.mkdir(parents=True, exist_ok=True)
    return sandbox


def _materialize_files(
    worktree: Path, bundle: PatchBundle, *, max_added_lines: int
) -> list[str]:
    root = worktree.resolve(strict=True)
    prepared: list[tuple[Path, bytes]] = []
    added_lines = 0
    for patch in bundle.files:
        relative = PurePosixPath(patch.path)
        destination = root.joinpath(*relative.parts)
        _assert_safe_destination(root, destination)
        if patch.operation == "create":
            if destination.exists() or destination.is_symlink():
                raise ValueError(f"create target already exists: {patch.path}")
        else:
            if not destination.is_file() or destination.is_symlink():
                raise ValueError(f"replace target is not a regular file: {patch.path}")
            if _sha256_file(destination) != patch.expected_old_sha256:
                raise ValueError(f"stale expectedOldSha256 for: {patch.path}")
        content = patch.content.encode("utf-8")
        old_content = destination.read_bytes().decode("utf-8") if destination.exists() else ""
        added_lines += _count_added_lines(old_content, patch.content)
        if added_lines > max_added_lines:
            raise ValueError("materialized patch exceeds maxAddedLines")
        prepared.append((destination, content))

    for destination, content in prepared:
        destination.parent.mkdir(parents=True, exist_ok=True)
        _assert_safe_destination(root, destination)
        temporary = destination.parent / f".{destination.name}.evolution-{secrets.token_hex(6)}.tmp"
        try:
            with temporary.open("xb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
        finally:
            if temporary.exists():
                temporary.unlink()
    return [item.path for item in bundle.files]


def _count_added_lines(old: str, new: str) -> int:
    matcher = difflib.SequenceMatcher(a=old.splitlines(), b=new.splitlines(), autojunk=False)
    return sum(
        new_end - new_start
        for operation, _, _, new_start, new_end in matcher.get_opcodes()
        if operation in {"insert", "replace"}
    )


def _assert_safe_destination(root: Path, destination: Path) -> None:
    relative = destination.relative_to(root)
    current = root
    for part in relative.parts:
        current /= part
        if _is_link(current):
            raise ValueError(
                f"patch path crosses a symlink or junction: {current.relative_to(root)}"
            )
    try:
        destination.resolve(strict=False).relative_to(root)
    except ValueError as exc:
        raise ValueError("patch path escaped the detached worktree") from exc


def _is_link(path: Path) -> bool:
    is_junction = getattr(os.path, "isjunction", lambda _: False)
    return path.is_symlink() or bool(is_junction(path))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run_argv(
    *,
    eval_id: str,
    argv: Sequence[str],
    cwd: Path,
    log_directory: Path,
    timeout_seconds: int,
    max_output_bytes: int,
    environment: Mapping[str, str] | None = None,
) -> EvalCommandResult:
    if not argv or timeout_seconds <= 0:
        raise ValueError("registered command is invalid")
    log_path = log_directory / f"command-{secrets.token_hex(8)}.log"
    started = time.monotonic()
    timed_out = False
    output_limit_exceeded = False
    exit_code: int | None = None
    try:
        with log_path.open("xb") as log:
            process = subprocess.Popen(
                list(argv),
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                shell=False,
                env=dict(environment) if environment is not None else None,
            )
            deadline = started + timeout_seconds
            while process.poll() is None:
                if time.monotonic() >= deadline:
                    timed_out = True
                    process.kill()
                    break
                log.flush()
                if log_path.stat().st_size > max_output_bytes:
                    output_limit_exceeded = True
                    process.kill()
                    break
                time.sleep(0.025)
            exit_code = process.wait(timeout=5)
        raw = log_path.read_bytes()
        if len(raw) > max_output_bytes:
            output_limit_exceeded = True
        output = raw[:max_output_bytes].decode("utf-8", errors="replace")
    finally:
        if log_path.exists():
            log_path.unlink()
    duration_ms = max(0, int((time.monotonic() - started) * 1_000))
    passed = exit_code == 0 and not timed_out and not output_limit_exceeded
    return EvalCommandResult(
        eval_id=eval_id,
        argv=list(argv),
        exit_code=exit_code,
        duration_ms=duration_ms,
        timed_out=timed_out,
        output_limit_exceeded=output_limit_exceeded,
        output=output,
        passed=passed,
    )


def _cleanup_worktree(
    repository_root: Path,
    sandbox_root: Path,
    directory: Path,
    registered: bool,
) -> bool:
    repository = repository_root.resolve()
    sandbox = sandbox_root.resolve()
    try:
        directory.resolve(strict=False).relative_to(sandbox)
    except ValueError:
        return False
    succeeded = True
    if registered:
        try:
            result = _run_argv(
                eval_id="git-worktree-remove",
                argv=(
                    "git",
                    "-C",
                    str(repository),
                    "worktree",
                    "remove",
                    "--force",
                    str(directory),
                ),
                cwd=repository,
                log_directory=sandbox,
                timeout_seconds=30,
                max_output_bytes=16 * 1024,
            )
            succeeded = result.passed
        except (OSError, subprocess.SubprocessError):
            succeeded = False
    if directory.exists() or directory.is_symlink():
        try:
            if _is_link(directory):
                directory.unlink()
            else:
                shutil.rmtree(directory)
        except OSError:
            succeeded = False
    return succeeded and not directory.exists()


def _bounded_error(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:_MAX_ERROR_CHARS]


def _sanitized_eval_environment(worktree: Path) -> dict[str, str]:
    temporary = worktree / ".evolution-tmp"
    temporary.mkdir(exist_ok=True)
    allowed = {
        key: value
        for key, value in os.environ.items()
        if key.casefold()
        in {
            "comspec",
            "lang",
            "lc_all",
            "path",
            "pathext",
            "systemroot",
            "windir",
        }
    }
    allowed.update(
        {
            "HOME": str(temporary),
            "TEMP": str(temporary),
            "TMP": str(temporary),
            # The root project intentionally uses a src layout without an
            # editable package install.  Pin imports to the detached
            # worktree so candidate checks cannot accidentally import the
            # stable worker's /app sources.
            "PYTHONPATH": str(worktree / "src"),
            "PYTHONHASHSEED": "0",
            "PYTHONNOUSERSITE": "1",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "HTTP_PROXY": "http://127.0.0.1:9",
            "HTTPS_PROXY": "http://127.0.0.1:9",
            "ALL_PROXY": "http://127.0.0.1:9",
            "NO_PROXY": "",
        }
    )
    return allowed
