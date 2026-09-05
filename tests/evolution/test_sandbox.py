from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import evolution.sandbox as sandbox_module
from evolution.contracts import EvolutionCandidate, HarnessManifest
from evolution.kubernetes_sandbox import KubernetesSandboxExecutor, _eval_containers
from evolution.kubernetes_sandbox_runner import _remove_private_candidate_inputs
from evolution.optimizer import (
    CandidateFilePatch,
    PatchBundle,
    PatchChange,
    PatchPlan,
    PatchProposal,
    load_governance_policy,
)
from evolution.sandbox import (
    CandidateEvalReport,
    EvalCommand,
    _materialize_files,
    _sanitized_eval_environment,
    governed_eval_commands,
    materialize_and_evaluate_candidate,
)
from evolution.temporal.contracts import FIXED_EVAL_IDS

ROOT = Path(__file__).resolve().parents[2]


def test_eval_environment_imports_only_from_candidate_worktree(tmp_path: Path) -> None:
    worktree = tmp_path / "candidate"
    worktree.mkdir()
    environment = _sanitized_eval_environment(worktree)

    assert environment["PYTHONPATH"] == str(worktree / "src")
    assert environment["PYTHONNOUSERSITE"] == "1"
    assert "RATSNEST_INTERNAL_SIGNING_SECRET" not in environment


def test_governed_suite_commands_are_fixed_and_digest_bound() -> None:
    class Suite:
        def __init__(self, eval_id: str, manifest_ref: str, suite_digest: str) -> None:
            self.eval_id = eval_id
            self.manifest_ref = manifest_ref
            self.suite_digest = suite_digest

    suites = [
        Suite("optimization-suite", "evals/regression/optimization.v1.json", "1" * 64),
        Suite("holdout-suite", "evals/sealed/regression/holdout.v1.json", "2" * 64),
        Suite("adversarial-suite", "evals/sealed/regression/adversarial.v1.json", "3" * 64),
    ]

    commands = governed_eval_commands(suites)

    assert tuple(commands) == FIXED_EVAL_IDS
    assert commands["holdout-suite"].argv[-2:] == (
        "--expected-suite-digest",
        "2" * 64,
    )
    assert "evals/sealed/regression/holdout.v1.json" in commands["holdout-suite"].argv


def test_kubernetes_sealed_suites_use_trusted_root_and_public_suite_uses_checkout() -> None:
    class Suite:
        def __init__(
            self,
            eval_id: str,
            manifest_ref: str,
            suite_digest: str,
            *,
            sealed: bool,
        ) -> None:
            self.eval_id = eval_id
            self.manifest_ref = manifest_ref
            self.suite_digest = suite_digest
            self.sealed = sealed

    class TrialInput:
        patch_bundle = type("Bundle", (), {"files": []})()
        evaluation_suites = [
            Suite(
                "optimization-suite",
                "evals/regression/optimization.v1.json",
                "1" * 64,
                sealed=False,
            ),
            Suite(
                "holdout-suite",
                "evals/sealed/regression/holdout.v1.json",
                "2" * 64,
                sealed=True,
            ),
        ]

    class Request:
        trial_input = TrialInput()

    commands = {item[0]: item[1] for item in _eval_containers(Request())}

    assert commands["optimization-suite"][5:9] == (
        "--root",
        "/workspace/repo",
        "--suite-root",
        "/workspace/repo",
    )
    assert commands["holdout-suite"][5:9] == (
        "--root",
        "/workspace/repo",
        "--suite-root",
        "/opt/ratsnest-evolution-evaluator",
    )
    assert commands["optimization-suite"][4].startswith("/opt/ratsnest-evolution-evaluator/")


def test_materializer_scrubs_sealed_sources_and_git_objects(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    (worktree / "evals" / "sealed").mkdir(parents=True)
    (worktree / "evals" / "sealed" / "holdout.json").write_text("secret")
    (worktree / "evals" / "regression").mkdir(parents=True)
    (worktree / "evals" / "regression" / "optimization.json").write_text("public")
    (worktree / ".git" / "objects").mkdir(parents=True)
    (worktree / ".git" / "objects" / "sealed-blob").write_text("secret")

    _remove_private_candidate_inputs(worktree)

    assert not (worktree / "evals" / "sealed").exists()
    assert not (worktree / ".git").exists()
    assert (worktree / "evals" / "regression" / "optimization.json").is_file()


def test_kubernetes_job_marks_workspace_separation_as_non_authoritative() -> None:
    executor = object.__new__(KubernetesSandboxExecutor)
    executor.image = "registry.invalid/evaluator@sha256:" + "8" * 64
    executor.mirror_claim = "trusted-mirror"
    executor.namespace = "sandbox"

    job = executor._job("trial", {"test": "sealed"}, ())

    annotations = job["metadata"]["annotations"]
    assert annotations["ratsnest.io/sealed-evaluation-mode"] == (
        "workspace-separated-non-blind"
    )
    assert annotations["ratsnest.io/promotion-authoritative"] == "false"


def _git(repository: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repository), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _fixture(tmp_path: Path) -> dict[str, object]:
    repository = tmp_path / "repository"
    source = repository / "src" / "agents" / "ratsnestpro" / "intent_router.py"
    source.parent.mkdir(parents=True)
    source.write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    _git(repository, "config", "user.email", "evolution-tests@example.invalid")
    _git(repository, "config", "user.name", "Evolution Tests")
    _git(repository, "add", ".")
    _git(repository, "commit", "-qm", "base")
    commit = _git(repository, "rev-parse", "HEAD")
    manifest_seed = HarnessManifest(
        source_commit=commit,
        source_tree_digest="1" * 64,
        dirty=False,
        bundle_digest="2" * 64,
        contract_digest="3" * 64,
        policy_digest="4" * 64,
        runtime_image_digest="sha256:" + "8" * 64,
        toolchain_digest="9" * 64,
        manifest_digest="5" * 64,
    )
    manifest = manifest_seed.model_copy(
        update={"manifest_digest": manifest_seed.calculated_manifest_digest()}
    )
    candidate = EvolutionCandidate(
        candidate_id="6" * 64,
        base_harness_version_id="harness-test",
        base_manifest_digest=manifest.manifest_digest,
        failure_signature="generic-test-signature",
        step="routing",
        profile_references=["sipi-channel-pdn-eval@1.0"],
        observation_ids=["7" * 64],
        occurrence_count=2,
        project_count=2,
        status="eligible",
    )
    policy = load_governance_policy(ROOT / "config" / "harness" / "invariants.v1.json")
    preserved = [item.id for item in policy.invariants if not item.mutable_by_evolution]
    plan = PatchPlan(
        candidate_id=candidate.candidate_id,
        base_commit=commit,
        summary="Replace one generic routing constant.",
        changes=[
            PatchChange(
                operation="modify",
                path="src/agents/ratsnestpro/intent_router.py",
                rationale="Exercise governed whole-file replacement.",
                estimated_added_lines=1,
            )
        ],
        preserved_invariants=preserved,
    )
    content = "VALUE = 2\n"
    bundle = PatchBundle(
        candidate_id=candidate.candidate_id,
        base_commit=commit,
        files=[
            CandidateFilePatch(
                operation="replace",
                path="src/agents/ratsnestpro/intent_router.py",
                expected_old_sha256=hashlib.sha256(b"VALUE = 1\n").hexdigest(),
                content_sha256=hashlib.sha256(content.encode()).hexdigest(),
                content=content,
            )
        ],
    )
    return {
        "repository": repository,
        "sandbox": tmp_path / "sandbox",
        "manifest": manifest,
        "candidate": candidate,
        "policy": policy,
        "plan": plan,
        "bundle": bundle,
    }


def _run(fixture: dict[str, object], command: EvalCommand):
    return materialize_and_evaluate_candidate(
        candidate=fixture["candidate"],
        harness_manifest=fixture["manifest"],
        plan=fixture["plan"],
        bundle=fixture["bundle"],
        policy=fixture["policy"],
        repository_root=fixture["repository"],
        sandbox_root=fixture["sandbox"],
        eval_ids=[command.eval_id],
        eval_registry={command.eval_id: command},
    )


def test_patch_contract_rejects_path_traversal() -> None:
    content = "unsafe\n"
    with pytest.raises(ValueError, match="unsafe segment"):
        CandidateFilePatch(
            operation="create",
            path="../escape.py",
            content_sha256=hashlib.sha256(content.encode()).hexdigest(),
            content=content,
        )


def test_sealed_eval_path_is_rejected_before_worktree_creation(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    fixture["plan"] = fixture["plan"].model_copy(
        update={
            "changes": [
                PatchChange(
                    operation="create",
                    path="evals/sealed/attack.py",
                    rationale="Must be rejected.",
                )
            ]
        }
    )
    content = "ATTACK = True\n"
    fixture["bundle"] = PatchBundle(
        candidate_id=fixture["candidate"].candidate_id,
        base_commit=fixture["manifest"].source_commit,
        files=[
            CandidateFilePatch(
                operation="create",
                path="evals/sealed/attack.py",
                content_sha256=hashlib.sha256(content.encode()).hexdigest(),
                content=content,
            )
        ],
    )
    report = _run(
        fixture,
        EvalCommand("compile", (sys.executable, "-m", "compileall", "-q", "."), 10),
    )
    assert report.verdict == "policy_rejected", (report.error, report.cleanup_succeeded)
    assert not report.worktree_created


def test_materializer_rejects_symlink_component(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worktree = tmp_path / "worktree"
    link = worktree / "src" / "agents" / "ratsnestpro" / "link"
    outside = tmp_path / "outside"
    link.parent.mkdir(parents=True)
    outside.mkdir()
    try:
        os.symlink(outside, link, target_is_directory=True)
    except OSError:
        link.mkdir()
        original_is_link = sandbox_module._is_link
        monkeypatch.setattr(
            sandbox_module,
            "_is_link",
            lambda path: path == link or original_is_link(path),
        )
    content = "VALUE = 1\n"
    bundle = PatchBundle(
        candidate_id="8" * 64,
        base_commit="9" * 40,
        files=[
            CandidateFilePatch(
                operation="create",
                path="src/agents/ratsnestpro/link/escape.py",
                content_sha256=hashlib.sha256(content.encode()).hexdigest(),
                content=content,
            )
        ],
    )
    with pytest.raises(ValueError, match="symlink or junction"):
        _materialize_files(worktree, bundle, max_added_lines=500)
    assert not (outside / "escape.py").exists()


def test_stale_hash_is_rejected_and_worktree_is_cleaned(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    patch = fixture["bundle"].files[0].model_copy(
        update={"expected_old_sha256": "0" * 64}
    )
    fixture["bundle"] = fixture["bundle"].model_copy(update={"files": [patch]})
    report = _run(
        fixture,
        EvalCommand("compile", (sys.executable, "-m", "compileall", "-q", "."), 10),
    )
    assert report.verdict == "policy_rejected", (report.error, report.cleanup_succeeded)
    assert report.worktree_created
    assert report.cleanup_succeeded
    assert not any(fixture["sandbox"].iterdir())


def test_failing_fixed_command_is_reported_and_cleaned(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    command = EvalCommand(
        "known-failure",
        (sys.executable, "-m", "py_compile", "missing-eval-file.py"),
        10,
    )
    report = _run(fixture, command)
    assert report.verdict == "failed", (report.error, report.cleanup_succeeded)
    assert report.command_results[0].exit_code != 0
    assert not report.command_results[0].passed
    assert report.cleanup_succeeded
    assert not any(fixture["sandbox"].iterdir())


def test_passing_fixed_command_materializes_then_cleans_worktree(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    target = "src/agents/ratsnestpro/intent_router.py"
    command = EvalCommand("compile", (sys.executable, "-m", "py_compile", target), 10)
    report = _run(fixture, command)
    assert report.verdict == "passed", (report.error, report.cleanup_succeeded)
    assert report.materialized_files == [target]
    assert report.cleanup_succeeded
    assert not any(fixture["sandbox"].iterdir())
    assert str(fixture["sandbox"]) not in _git(fixture["repository"], "worktree", "list")


def test_temporal_activity_ignores_caller_supplied_candidate_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from evolution.temporal import activities

    monkeypatch.setenv("RATSNEST_EVOLUTION_SANDBOX_MODE", "local_process")
    monkeypatch.setenv("RATSNEST_EVOLUTION_ALLOW_LOCAL_SANDBOX", "true")
    monkeypatch.setenv("RATSNEST_EVOLUTION_EXECUTOR_IMAGE_DIGEST", "sha256:" + "8" * 64)
    monkeypatch.setenv("RATSNEST_EVOLUTION_TOOLCHAIN_DIGEST", "9" * 64)

    fixture = _fixture(tmp_path)
    generated = CandidateEvalReport(
        candidate_id=fixture["candidate"].candidate_id,
        base_commit=fixture["plan"].base_commit,
        patch_digest="a" * 64,
        verdict="failed",
        worktree_created=True,
        cleanup_succeeded=True,
    )
    monkeypatch.setattr(
        activities,
        "_configured_paths",
        lambda: (fixture["repository"], fixture["sandbox"]),
    )
    monkeypatch.setattr(activities, "load_governance_policy", lambda _: fixture["policy"])
    monkeypatch.setattr(
        activities,
        "materialize_and_evaluate_candidate",
        lambda **_: generated,
    )
    command = {
        "candidate": fixture["candidate"].model_dump(mode="json", by_alias=True),
        "harness_manifest": fixture["manifest"].model_dump(mode="json", by_alias=True),
        "patch_plan": fixture["plan"].model_dump(mode="json", by_alias=True),
        "patch_bundle": fixture["bundle"].model_dump(mode="json", by_alias=True),
        "evaluation_suites": [
            {
                "eval_id": "optimization-suite",
                "manifest_ref": "evals/regression/optimization.v1.json",
                "suite_digest": "1" * 64,
            },
            {
                "eval_id": "holdout-suite",
                "manifest_ref": "evals/sealed/regression/holdout.v1.json",
                "suite_digest": "2" * 64,
            },
            {
                "eval_id": "adversarial-suite",
                "manifest_ref": "evals/sealed/regression/adversarial.v1.json",
                "suite_digest": "3" * 64,
            },
        ],
        "candidate_report": {"verdict": "passed", "forged": True},
    }
    report = asyncio.run(activities.evaluate_candidate_activity(command))
    assert report["verdict"] == "failed"
    assert report["patchDigest"] == "a" * 64


def test_versioned_candidate_contract_tracks_python_models() -> None:
    schema = json.loads(
        (ROOT / "contracts" / "evolution" / "v1" / "candidate-patch.schema.json").read_text(
            encoding="utf-8"
        )
    )
    assert set(schema["$defs"]["PatchProposal"]["properties"]) == set(
        PatchProposal.model_json_schema(by_alias=True)["properties"]
    )
    assert set(schema["$defs"]["CandidateEvalReport"]["properties"]) == set(
        CandidateEvalReport.model_json_schema(by_alias=True)["properties"]
    )
