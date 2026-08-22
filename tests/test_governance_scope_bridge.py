from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path

import pytest
from pydantic import SecretStr

from agents.ratsnestpro import ratsnestpro_agent, tools
from agents.ratsnestpro.ehe_memory import EheMemory
from agents.ratsnestpro.temporal import activities, client, step_runner
from core import settings
from service.governance_scope import (
    GOVERNANCE_SCOPE_ENV,
    TrustedGovernanceScope,
    issue_governance_scope_token,
    verify_governance_scope_token,
)

_SECRET = "governance-test-signing-secret-32-bytes-minimum"


def _scope() -> TrustedGovernanceScope:
    return TrustedGovernanceScope(
        tenant_scope="1" * 16,
        project_scope="2" * 16,
        run_scope="3" * 64,
        harness_version_id="harness-v1",
        harness_manifest_digest="4" * 64,
    )


def _governed_command() -> dict[str, object]:
    scope = _scope()
    requirement = "build a governed board"
    return {
        "workflow_id": "workflow-1",
        "requirement": requirement,
        "requirement_hash": hashlib.sha256(requirement.encode()).hexdigest(),
        "run_name": "run-1",
        "display_run_name": "Run 1",
        "execution_scope": "internal",
        "project_name": "board",
        "llm_mode": "required",
        **{key: value for key, value in scope.payload().items() if key != "v"},
        "governance_scope_token": issue_governance_scope_token(
            scope,
            secret=_SECRET,
        ),
    }


def test_governance_token_is_signed_and_tamper_fails_closed() -> None:
    token = issue_governance_scope_token(_scope(), secret=_SECRET)
    assert verify_governance_scope_token(token, secret=_SECRET) == _scope()
    with pytest.raises(ValueError, match="signature"):
        verify_governance_scope_token(token[:-1] + "A", secret=_SECRET)


def test_temporal_manifest_never_persists_governance_token(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("RATSNESTPRO_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setattr(
        settings,
        "RATSNEST_INTERNAL_SIGNING_SECRET",
        SecretStr(_SECRET),
    )
    command = _governed_command()

    path, manifest = activities._manifest(command)
    persisted = json.loads(path.read_text(encoding="utf-8"))

    assert manifest["tenant_scope"] == _scope().tenant_scope
    assert "governance_scope_token" not in manifest
    assert "governance_scope_token" not in persisted
    reload_command = {
        key: value
        for key, value in command.items()
        if key not in {"requirement", "run_name", "display_run_name"}
    }
    reload_command["manifest_path"] = str(path)
    _, restored = activities._manifest(reload_command)
    assert restored["run_scope"] == _scope().run_scope

    reload_command["governance_scope_token"] = str(
        reload_command["governance_scope_token"]
    )[:-1] + "A"
    _, ungoverned = activities._manifest(reload_command)
    assert "tenant_scope" not in ungoverned


def test_step_runner_installs_only_a_verified_scope(monkeypatch) -> None:
    monkeypatch.setattr(
        settings,
        "RATSNEST_INTERNAL_SIGNING_SECRET",
        SecretStr(_SECRET),
    )
    command = _governed_command()
    step_runner._install_governance_environment(command)
    assert os.environ[GOVERNANCE_SCOPE_ENV] == command["governance_scope_token"]
    assert os.environ["RATSNESTPRO_PROJECT_SCOPE"] == _scope().project_scope

    command["project_scope"] = "9" * 16
    step_runner._install_governance_environment(command)
    assert GOVERNANCE_SCOPE_ENV not in os.environ
    assert "RATSNESTPRO_PROJECT_SCOPE" not in os.environ


def test_langgraph_dispatch_carries_all_signed_governance_fields(
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    async def fake_dispatch(**kwargs):
        captured.update(kwargs)
        return {"mode": "temporal", "status": "started", "workflow_id": "wf"}

    monkeypatch.setattr(client, "dispatch_hardware_workflow", fake_dispatch)
    monkeypatch.setattr(client, "temporal_enabled", lambda: True)
    monkeypatch.setattr(
        ratsnestpro_agent,
        "_hardware_requirement",
        lambda _state: "build a board",
    )
    scope = _scope()
    token = issue_governance_scope_token(scope, secret=_SECRET)
    state = {
        "request_id": "request-1",
        "requirement": "build a board",
        "run_name": "run-1",
        "workspace_run_name": "workspace-run-1",
        "execution_scope": "internal",
        "project_name": "board",
        "hardware_attempts": [],
        "capability_profile": {},
        **{key: value for key, value in scope.payload().items() if key != "v"},
    }

    asyncio.run(
        ratsnestpro_agent.hardware_dispatch_phase(
            state,  # type: ignore[arg-type]
            {
                "configurable": {
                    "request_id": "request-1",
                    "governance_scope_token": token,
                }
            },  # type: ignore[arg-type]
        )
    )

    for field in (
        "tenant_scope",
        "project_scope",
        "run_scope",
        "harness_version_id",
        "harness_manifest_digest",
        "governance_scope_token",
    ):
        assert captured[field] == (
            token if field == "governance_scope_token" else state[field]
        )


def test_scoped_verified_experience_is_retrievable_without_leaking_authority(
    tmp_path: Path,
    monkeypatch,
) -> None:
    scope = _scope()
    token = issue_governance_scope_token(scope, secret=_SECRET)
    monkeypatch.setattr(
        settings,
        "RATSNEST_INTERNAL_SIGNING_SECRET",
        SecretStr(_SECRET),
    )
    monkeypatch.setattr(tools, "_workspace_root", lambda: tmp_path)
    monkeypatch.setattr(
        tools,
        "search_external_knowledge",
        lambda **_kwargs: {
            "status": "unavailable",
            "evidence_sufficient": False,
            "results": [],
        },
    )
    EheMemory(tmp_path / "ehe", governance_scope=scope).promote_verified_run(
        requirement="mcu controller",
        resolved_issues=[],
        selected_roles=["mcu"],
        human_amendment=False,
        independent_review_passed=True,
        release_ready_evidence=True,
    )

    payload = json.loads(tools.ratsnest_search_internal_knowledge(
        "mcu",
        tenant_scope=scope.tenant_scope,
        project_scope=scope.project_scope,
        run_scope=scope.run_scope,
        harness_version_id=scope.harness_version_id,
        harness_manifest_digest=scope.harness_manifest_digest,
        governance_scope_token=token,
    ))
    assert any(
        result.get("source") == "local_ehe_memory"
        for result in payload["results"]
    )
    public = ratsnestpro_agent._public_knowledge_arguments({
        "query": "mcu",
        "tenant_scope": scope.tenant_scope,
        "harness_version_id": scope.harness_version_id,
        "governance_scope_token": token,
    })
    assert public == {"query": "mcu"}
