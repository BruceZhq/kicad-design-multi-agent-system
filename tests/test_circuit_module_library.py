from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from agents.ratsnestpro.ehe_memory import EheMemory
from agents.ratsnestpro.tools import (
    _checkpoint_artifact_digest,
    _checkpoint_content_digest,
    _safe_build_circuit_module_candidates,
    _verified_experience_text,
    load_reviewed_circuit_module_source,
)
from ratsnestpro.knowledge.circuit_modules import (
    CircuitModuleCandidate,
    build_circuit_module_candidates,
    circuit_module_search_text,
    validate_circuit_module_candidates,
)
from ratsnestpro.orchestration.pipeline_contracts import (
    LogicalPin,
    ManufactureResult,
    NetIntent,
    NetlistIntent,
    SelectedPart,
    SelectionPlan,
    TopologyBlock,
    TopologyPlan,
)
from ratsnestpro.orchestration.release_invariants import ReleaseIdentity
from service.governance_scope import TrustedGovernanceScope

_INTEGRITY_SECRET = "module-review-secret-" + "a" * 32


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def _part(ref: str, role: str) -> SelectedPart:
    return SelectedPart(
        ref=ref,
        role=role,
        value=f"VALUE-{ref}",
        mpn=f"MPN-{ref}",
        symbol="Device:R",
        footprint="Resistor_SMD:R_0603_1608Metric",
        release_ready=True,
        prepared_record_id="1" * 64,
        asset_lock_digest="2" * 64,
    )


def _identity(project: str = "verified-board") -> ReleaseIdentity:
    return ReleaseIdentity(
        project_name=project,
        requirement_source_digest="3" * 64,
        pcb_relpath=f"{project}.kicad_pcb",
        pcb_sha256="4" * 64,
    )


def test_extracts_only_explicit_release_ready_functional_blocks() -> None:
    modules = build_circuit_module_candidates(
        topology=TopologyPlan(
            blocks=[
                TopologyBlock(
                    name="power-entry",
                    kind="power_entry",
                    implementation_refs=["J1", "R1"],
                )
            ],
            rails=["VCC"],
        ),
        selection=SelectionPlan(parts=[_part("J1", "power_input"), _part("R1", "pull")]),
        netlist=NetlistIntent(
            nets=[
                NetIntent(
                    name="VCC",
                    kind="power",
                    pins=[
                        LogicalPin(ref="J1", pin="1"),
                        LogicalPin(ref="R1", pin="1"),
                        LogicalPin(ref="U1", pin="VCC"),
                    ],
                )
            ]
        ),
        release_identity=_identity(),
    )

    assert len(modules) == 1
    module = CircuitModuleCandidate.model_validate(modules[0])
    assert {component.ref for component in module.components} == {"J1", "R1"}
    assert module.nets[0].boundary is True
    assert {pin.ref for pin in module.nets[0].pins} == {"J1", "R1"}


def test_reviewer_boundary_rejects_stale_release_identity() -> None:
    modules = build_circuit_module_candidates(
        topology=TopologyPlan(
            blocks=[TopologyBlock(name="pull", kind="bias", implementation_refs=["R1"])],
            rails=["VCC"],
        ),
        selection=SelectionPlan(parts=[_part("R1", "pull")]),
        netlist=NetlistIntent(),
        release_identity=_identity(),
    )

    with pytest.raises(ValueError, match="release identity is stale"):
        validate_circuit_module_candidates(
            modules,
            release_identity=_identity("different-board").model_dump(mode="json"),
        )


def test_only_reviewer_promoted_modules_are_cross_run_searchable(tmp_path: Path) -> None:
    identity = _identity()
    topology = TopologyPlan(
        blocks=[
            TopologyBlock(
                name="power-entry",
                kind="power_entry",
                implementation_refs=["J1"],
            )
        ],
        rails=["VCC"],
    )
    selection = SelectionPlan(parts=[_part("J1", "power_input")])
    netlist = NetlistIntent()
    modules = build_circuit_module_candidates(
        topology=topology,
        selection=selection,
        netlist=netlist,
        release_identity=identity,
    )
    memory = EheMemory(
        tmp_path,
        governance_scope=TrustedGovernanceScope(
            tenant_scope="1" * 16,
            project_scope="2" * 16,
            run_scope="3" * 64,
            harness_version_id="harness-v1",
            harness_manifest_digest="4" * 64,
        ),
        integrity_secret=_INTEGRITY_SECRET,
    )

    memory.promote_verified_run(
        requirement="verified power entry",
        resolved_issues=[],
        selected_roles=["power_input"],
        human_amendment=False,
        independent_review_passed=True,
        release_ready_evidence=True,
        circuit_modules=modules,
        release_identity=identity.model_dump(mode="json"),
        topology=topology,
        selection=selection,
        netlist=netlist,
    )

    found = memory.search_verified_modules("power entry MPN-J1")
    assert len(found) == 1
    persisted = json.loads(next(memory.modules_dir.glob("*.json")).read_text("utf-8"))
    assert persisted["evidence"]["independent_review"]["passed"] is True
    assert persisted["review_receipt"]["algorithm"] == "hmac-sha256"
    assert "verified-board" not in json.dumps(persisted)


def test_reviewer_rebuild_rejects_self_hashed_module_not_in_reviewed_source() -> None:
    identity = _identity()
    topology = TopologyPlan(
        blocks=[TopologyBlock(name="pull", kind="bias", implementation_refs=["R1"])],
        rails=["VCC"],
    )
    selection = SelectionPlan(parts=[_part("R1", "pull")])
    netlist = NetlistIntent()
    module = build_circuit_module_candidates(
        topology=topology,
        selection=selection,
        netlist=netlist,
        release_identity=identity,
    )[0]
    forged = json.loads(json.dumps(module))
    forged["components"][0]["mpn"] = "FORGED-MPN"
    forged["module_digest"] = _digest(
        {key: value for key, value in forged.items() if key != "module_digest"}
    )

    assert validate_circuit_module_candidates(
        [forged],
        release_identity=identity.model_dump(mode="json"),
    )
    with pytest.raises(ValueError, match="reviewed pipeline source"):
        validate_circuit_module_candidates(
            [forged],
            release_identity=identity.model_dump(mode="json"),
            topology=topology,
            selection=selection,
            netlist=netlist,
        )


def test_signed_module_record_fails_closed_after_tampering(tmp_path: Path) -> None:
    identity = _identity()
    topology = TopologyPlan(
        blocks=[TopologyBlock(name="pull", kind="bias", implementation_refs=["R1"])],
        rails=["VCC"],
    )
    selection = SelectionPlan(parts=[_part("R1", "pull")])
    netlist = NetlistIntent()
    modules = build_circuit_module_candidates(
        topology=topology,
        selection=selection,
        netlist=netlist,
        release_identity=identity,
    )
    scope = TrustedGovernanceScope(
        tenant_scope="1" * 16,
        project_scope="2" * 16,
        run_scope="3" * 64,
        harness_version_id="harness-v1",
        harness_manifest_digest="4" * 64,
    )
    memory = EheMemory(
        tmp_path,
        governance_scope=scope,
        integrity_secret=_INTEGRITY_SECRET,
    )
    with pytest.raises(ValueError, match="reviewed pipeline source"):
        memory.promote_verified_run(
            requirement="unbound pull network",
            resolved_issues=[],
            selected_roles=["pull"],
            human_amendment=False,
            independent_review_passed=True,
            release_ready_evidence=True,
            circuit_modules=modules,
            release_identity=identity.model_dump(mode="json"),
        )
    memory.promote_verified_run(
        requirement="verified pull network",
        resolved_issues=[],
        selected_roles=["pull"],
        human_amendment=False,
        independent_review_passed=True,
        release_ready_evidence=True,
        circuit_modules=modules,
        release_identity=identity.model_dump(mode="json"),
        topology=topology,
        selection=selection,
        netlist=netlist,
    )
    assert memory.search_verified_modules("pull MPN-R1")
    assert EheMemory(tmp_path, governance_scope=scope).search_verified_modules(
        "pull MPN-R1"
    ) == []
    assert EheMemory(
        tmp_path,
        governance_scope=scope,
        integrity_secret="wrong-integrity-secret-" + "b" * 32,
    ).search_verified_modules("pull MPN-R1") == []

    record_path = next(memory.modules_dir.glob("*.json"))
    record = json.loads(record_path.read_text("utf-8"))
    record["feature_fingerprints"].append("0" * 64)
    record_path.write_text(json.dumps(record), encoding="utf-8")
    assert memory.search_verified_modules("pull MPN-R1") == []


def test_module_candidate_extraction_failure_is_a_warning() -> None:
    identity = _identity()
    topology = TopologyPlan(
        blocks=[TopologyBlock(name="wide", kind="logic", implementation_refs=["U1"])],
        rails=["VCC"],
    )
    selection = SelectionPlan(parts=[_part("U1", "logic")])
    netlist = NetlistIntent(
        nets=[
            NetIntent(
                name=f"N{index}",
                pins=[LogicalPin(ref="U1", pin=str(index))],
            )
            for index in range(501)
        ]
    )

    modules, warning = _safe_build_circuit_module_candidates(
        topology=topology,
        selection=selection,
        netlist=netlist,
        release_identity=identity,
    )

    assert modules == []
    assert "candidate extraction failed" in warning


def test_circuit_module_search_text_is_always_valid_bounded_json() -> None:
    identity = _identity()
    parts = [_part(f"R{index}", "filter") for index in range(1, 101)]
    module = build_circuit_module_candidates(
        topology=TopologyPlan(
            blocks=[
                TopologyBlock(
                    name="large-filter",
                    kind="filter",
                    implementation_refs=[part.ref for part in parts],
                )
            ],
            rails=["VCC"],
        ),
        selection=SelectionPlan(parts=parts),
        netlist=NetlistIntent(),
        release_identity=identity,
    )[0]

    text = circuit_module_search_text([module])

    assert len(text) <= 16_000
    assert isinstance(json.loads(text), list)
    experience_text = _verified_experience_text(
        {
            "selected_roles": ["filter"] * 2_000,
            "resolved_issues": [{"detail": "x" * 500}] * 100,
            "evidence": {"passed": True},
        }
    )
    assert len(experience_text) <= 8_000
    assert isinstance(json.loads(experience_text), dict)


def test_review_source_reloads_bound_checkpoint_and_physical_pcb(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = tmp_path / "runs" / "reviewed"
    run_dir.mkdir(parents=True)
    pcb_path = run_dir / "verified-board.kicad_pcb"
    pcb_path.write_text("(kicad_pcb (version 20240108))", encoding="utf-8")
    identity = ReleaseIdentity(
        project_name="verified-board",
        requirement_source_digest="3" * 64,
        pcb_relpath=pcb_path.name,
        pcb_sha256=hashlib.sha256(pcb_path.read_bytes()).hexdigest(),
    )
    topology = TopologyPlan(
        blocks=[TopologyBlock(name="pull", kind="bias", implementation_refs=["R1"])],
        rails=["VCC"],
    )
    selection = SelectionPlan(parts=[_part("R1", "pull")])
    netlist = NetlistIntent()
    artifacts = {
        "topology": topology.model_dump(mode="json"),
        "selection": selection.model_dump(mode="json"),
        "schematic_connections": netlist.model_dump(mode="json"),
        "manufacture": ManufactureResult(
            release_identity=identity
        ).model_dump(mode="json"),
    }
    payload = {
        "schema_version": 7,
        "project_name": identity.project_name,
        "intermediate_artifacts": artifacts,
    }
    payload["checkpoint_receipt"] = {
        "schema_version": 1,
        "state_sha256": _checkpoint_content_digest(payload),
        "artifact_manifest_digest": _checkpoint_artifact_digest(payload),
    }
    (run_dir / "pipeline_state.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )
    monkeypatch.setenv("RATSNESTPRO_WORKSPACE_ROOT", str(tmp_path))

    source = load_reviewed_circuit_module_source(
        str(run_dir),
        identity.model_dump(mode="json"),
    )
    assert source["selection"] == selection

    pcb_path.write_text("tampered", encoding="utf-8")
    with pytest.raises(ValueError, match="PCB digest is stale"):
        load_reviewed_circuit_module_source(
            str(run_dir),
            identity.model_dump(mode="json"),
        )


def test_legacy_checkpoint_candidate_promotes_experience_without_modules(
    tmp_path: Path,
) -> None:
    from agents.ratsnestpro.ratsnestpro_agent import (
        _reviewer_module_promotion_source,
    )

    identity = _identity().model_dump(mode="json")
    candidate = {
        "eligible": True,
        "resolved_issues": [],
        "selected_roles": ["pull"],
        "human_amendment": False,
    }
    modules, source, warning = _reviewer_module_promotion_source(
        candidate=candidate,
        hardware_release_identity=identity,
        project_path=str(tmp_path),
    )
    assert modules == []
    assert source == {}
    assert "legacy promotion candidate" in warning

    memory = EheMemory(
        tmp_path,
        governance_scope=TrustedGovernanceScope(
            tenant_scope="1" * 16,
            project_scope="2" * 16,
            run_scope="3" * 64,
            harness_version_id="harness-v1",
            harness_manifest_digest="4" * 64,
        ),
    )
    promoted = memory.promote_verified_run(
        requirement="legacy verified run",
        resolved_issues=[],
        selected_roles=["pull"],
        human_amendment=False,
        independent_review_passed=True,
        release_ready_evidence=True,
        circuit_modules=modules,
        release_identity=identity,
        **source,
    )
    assert promoted.is_file()
    assert list(memory.modules_dir.glob("*.json")) == []
