from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from agents.ratsnestpro import ratsnestpro_agent
from agents.ratsnestpro.temporal import activities
from core import settings
from ratsnestpro.orchestration import pipeline as pipeline_module
from ratsnestpro.orchestration.component_resolution import (
    ComponentResolutionService,
    GroundedReplacement,
    LibraryClosureResult,
    build_user_replacement_approval,
)
from ratsnestpro.orchestration.pipeline import PipelineContext, PipelineState
from ratsnestpro.orchestration.pipeline_contracts import SelectedPart, SelectionPlan

_SECRET = "replacement-approval-test-secret-32-bytes-long"


def _replacement(*, ref: str = "U1", revision: int = 3) -> GroundedReplacement:
    evidence_ids = ["installed-assets:abc123"]
    approval = build_user_replacement_approval(
        decision_id="hitl-decision-1",
        target_ref=ref,
        requested_identity="OLD123",
        candidate_symbol="Interface:NEW123",
        candidate_value="NEW123",
        candidate_footprint="Package:SOIC-8",
        evidence_ids=evidence_ids,
        revision=revision,
        secret=_SECRET,
    )
    return GroundedReplacement(
        symbol="Interface:NEW123",
        value="NEW123",
        footprint="Package:SOIC-8",
        identity_relation="equivalent_validated",
        evidence_ids=evidence_ids,
        evidence_covers={"capability", "electrical", "package", "pin_topology"},
        user_approval=approval,
    )


def test_only_valid_internal_receipt_enters_agent_state(monkeypatch: pytest.MonkeyPatch) -> None:
    replacement = _replacement()
    monkeypatch.setattr(
        settings,
        "RATSNEST_INTERNAL_SIGNING_SECRET",
        SecretStr(_SECRET),
    )
    config = {
        "configurable": {
            "approved_component_replacements": {
                "U1": replacement.model_dump(mode="json")
            }
        }
    }

    trusted = ratsnestpro_agent._trusted_component_replacement_state(
        {}, config, preserve_state=False
    )
    assert GroundedReplacement.model_validate(trusted["U1"]) == replacement

    tampered = replacement.model_dump(mode="json")
    tampered["value"] = "ATTACKER_VALUE"
    config["configurable"]["approved_component_replacements"] = {"U1": tampered}
    assert ratsnestpro_agent._trusted_component_replacement_state(
        {}, config, preserve_state=False
    ) == {}


def test_user_receipt_can_amend_fixed_identity_only_with_full_equivalence_evidence() -> None:
    replacement = _replacement()
    assert ComponentResolutionService._replacement_allowed(
        replacement,
        target_ref="U1",
        requested_identity="OLD123",
        fixed_identity=True,
        allow_equivalent=False,
        revision=3,
        approval_secret=_SECRET,
    )

    incomplete = replacement.model_copy(update={"evidence_covers": {"package"}})
    assert not ComponentResolutionService._replacement_allowed(
        incomplete,
        target_ref="U1",
        requested_identity="OLD123",
        fixed_identity=True,
        allow_equivalent=False,
        revision=3,
        approval_secret=_SECRET,
    )


def test_signed_replacement_reaches_component_preparation(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    replacement = _replacement()
    selection = SelectionPlan(parts=[SelectedPart(
        ref="U1",
        symbol="Interface:OLD123",
        value="OLD123",
        footprint="Package:SOIC-8",
        role="transceiver",
        requested_identity="OLD123",
    )])
    state = PipelineState(requirement_text="Use OLD123", revision=3)
    captured: dict[str, object] = {}

    class _Manifest:
        def model_dump_json(self, *, indent: int) -> str:
            return json.dumps({"indent": indent})

    class _PreparationService:
        def __init__(self, **kwargs: object) -> None:
            captured["secret"] = kwargs.get("replacement_approval_secret")

        def prepare(self, plan, requirement, *, inputs, mutate_selection):
            captured["directive"] = inputs["U1"]
            return SimpleNamespace(
                selection=plan,
                closure=LibraryClosureResult(resolutions=[]),
                manifest=_Manifest(),
            )

    monkeypatch.setattr(
        pipeline_module, "ComponentPreparationService", _PreparationService
    )
    monkeypatch.setattr(
        pipeline_module, "_trusted_package_evidence", lambda *args, **kwargs: []
    )
    pipeline_module._prepare_and_persist_components(
        selection,
        state,
        PipelineContext(
            out_dir=str(tmp_path),
            approved_component_replacements={"U1": replacement},
            internal_signing_secret=_SECRET,
        ),
        preserve_requested_identities=True,
    )

    directive = captured["directive"]
    assert directive.replacement == replacement
    assert directive.workflow_revision == 3
    assert captured["secret"] == _SECRET


def test_temporal_manifest_revalidates_signed_replacement(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    replacement = _replacement()
    monkeypatch.setenv("RATSNESTPRO_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setattr(
        settings,
        "RATSNEST_INTERNAL_SIGNING_SECRET",
        SecretStr(_SECRET),
    )
    requirement = "Use OLD123"
    command = {
        "workflow_id": "workflow-replacement-1",
        "requirement": requirement,
        "requirement_hash": hashlib.sha256(requirement.encode()).hexdigest(),
        "run_name": "replacement-run",
        "project_name": "board",
        "approved_component_replacements": {
            "U1": replacement.model_dump(mode="json")
        },
    }
    path, manifest = activities._manifest(command)
    assert manifest["approved_component_replacements"]["U1"]["value"] == "NEW123"

    persisted = json.loads(path.read_text(encoding="utf-8"))
    persisted["approved_component_replacements"]["U1"]["value"] = "TAMPERED"
    path.write_text(json.dumps(persisted), encoding="utf-8")
    with pytest.raises(ValueError, match="approval is invalid"):
        activities._manifest({
            "manifest_path": str(path),
            "workflow_id": command["workflow_id"],
            "requirement_hash": command["requirement_hash"],
        })
