"""Immutable materializer used before untrusted candidate evaluation containers."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from evolution.optimizer import load_governance_policy
from evolution.sandbox import validate_and_materialize_candidate
from evolution.temporal.trial_contracts import trial_request_from_command

_MAX_INPUT_BYTES = 768 * 1024
_POLICY_PATH = Path("config/harness/invariants.v1.json")
_POLICY_REJECTED_EXIT = 10
_ENVIRONMENT_ERROR_EXIT = 20


def _read_command(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ValueError("sandbox input must be a regular file")
    raw = path.read_bytes()
    if not raw or len(raw) > _MAX_INPUT_BYTES:
        raise ValueError("sandbox input size is outside the bounded limit")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("sandbox input must be a JSON object")
    return value


def _run_git(argv: list[str], *, cwd: Path | None = None) -> str:
    completed = subprocess.run(
        argv,
        cwd=cwd,
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        timeout=60,
        env={
            "HOME": "/tmp/evolution-home",
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
        },
    )
    return completed.stdout.strip()


def _independent_checkout(repository: Path, worktree: Path, base_commit: str) -> None:
    source = repository.resolve(strict=True)
    destination = worktree.absolute()
    if destination.exists() or destination.is_symlink():
        raise ValueError("sandbox worktree must not already exist")
    destination.parent.mkdir(parents=True, exist_ok=True)
    _run_git(
        [
            "git",
            "-c",
            "protocol.file.allow=always",
            "clone",
            "--no-local",
            "--no-checkout",
            str(source),
            str(destination),
        ]
    )
    actual = _run_git(
        ["git", "rev-parse", "--verify", f"{base_commit}^{{commit}}"],
        cwd=destination,
    )
    if actual.casefold() != base_commit.casefold():
        raise ValueError("base commit is not an exact object in the staged mirror")
    _run_git(
        [
            "git",
            "-c",
            "core.autocrlf=false",
            "checkout",
            "--detach",
            "--force",
            base_commit,
        ],
        cwd=destination,
    )


def materialize(input_path: Path, repository: Path, worktree: Path) -> None:
    command = _read_command(input_path)
    request = trial_request_from_command(command)
    trial_input = request.trial_input
    _independent_checkout(repository, worktree, trial_input.patch_plan.base_commit)

    policy_path = worktree / _POLICY_PATH
    if policy_path.is_symlink() or not policy_path.is_file():
        raise ValueError("pinned governance policy is not a regular file")
    if hashlib.sha256(policy_path.read_bytes()).hexdigest() != trial_input.harness_manifest.policy_digest:
        raise ValueError("pinned governance policy digest does not match the manifest")
    policy = load_governance_policy(policy_path)
    validate_and_materialize_candidate(
        candidate=trial_input.candidate,
        harness_manifest=trial_input.harness_manifest,
        plan=trial_input.patch_plan,
        bundle=trial_input.patch_bundle,
        policy=policy,
        worktree=worktree,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--worktree", type=Path, required=True)
    args = parser.parse_args()
    try:
        materialize(args.input, args.repository, args.worktree)
    except (ValueError, json.JSONDecodeError) as exc:
        print(f"materialization rejected: {type(exc).__name__}", file=sys.stderr)
        return _POLICY_REJECTED_EXIT
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"materialization failed: {type(exc).__name__}", file=sys.stderr)
        return _ENVIRONMENT_ERROR_EXIT
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
