from __future__ import annotations

import pytest
from pydantic import ValidationError

from ratsnestpro.orchestration.component_preparation import (
    PreparedComponentManifest,
    PreparedComponentRecord,
    SupplierEvidence,
)
from ratsnestpro.orchestration.manufacturing_bom import split_manufacturing_bom
from ratsnestpro.orchestration.pipeline_contracts import (
    ManufactureResult,
    SelectedPart,
    SelectionPlan,
)

_RECORD_ID = "a" * 64
_ASSET_LOCK = "b" * 64
_MANIFEST_ID = "c" * 64
_REQUIREMENT_ID = "d" * 64
_SUPPLIER_EVIDENCE_ID = "e" * 64


def _selection(*, asset_lock: str = _ASSET_LOCK) -> SelectionPlan:
    return SelectionPlan(
        parts=[SelectedPart(
            ref="U1",
            symbol="Regulator_Linear:AP2112K-3.3",
            value="AP2112K-3.3",
            footprint="Package_TO_SOT_SMD:SOT-23-5",
            manufacturer="Diodes Incorporated",
            mpn="AP2112K-3.3TRG1",
            release_ready=True,
            prepared_record_id=_RECORD_ID,
            asset_lock_digest=asset_lock,
        )],
        prepared_manifest_sha256=_MANIFEST_ID,
        requirement_sha256=_REQUIREMENT_ID,
    )


def _manifest(
    *,
    procurement_status: str,
    supplier_evidence: bool,
    record_id: str = _RECORD_ID,
    manifest_id: str = _MANIFEST_ID,
) -> PreparedComponentManifest:
    evidence = []
    if supplier_evidence:
        evidence = [SupplierEvidence.model_construct(
            evidence_id=_SUPPLIER_EVIDENCE_ID,
        )]
    record = PreparedComponentRecord.model_construct(
        ref="U1",
        manufacturer="Diodes Incorporated",
        mpn="AP2112K-3.3TRG1",
        lcsc="C51118",
        prepared_record_id=record_id,
        asset_lock_digest=_ASSET_LOCK,
        electrical_status="ready",
        electrical_blockers=[],
        mechanical_status="not_applicable",
        mechanical_blockers=[],
        procurement_status=procurement_status,
        procurement_blockers=(
            [] if procurement_status == "ready" else ["supplier_evidence_unavailable"]
        ),
        supplier_evidence=evidence,
    )
    return PreparedComponentManifest.model_construct(
        records=[record],
        manifest_sha256=manifest_id,
        requirement_sha256=_REQUIREMENT_ID,
    )


def test_missing_supplier_evidence_does_not_fail_production_bom() -> None:
    split = split_manufacturing_bom(
        _selection(),
        _manifest(procurement_status="partial", supplier_evidence=False),
    )

    assert split.production_ready is True
    assert split.production_blockers == ()
    assert split.production_rows[0].release_ready is True
    assert split.procurement_ready is False
    assert split.procurement_rows[0].status == "partial"
    assert split.procurement_blockers == ("U1:supplier_evidence_unavailable",)


def test_supplier_evidence_makes_procurement_view_ready() -> None:
    split = split_manufacturing_bom(
        _selection(),
        _manifest(procurement_status="ready", supplier_evidence=True),
    )

    assert split.production_ready is True
    assert split.procurement_ready is True
    assert split.procurement_rows[0].supplier_evidence_ids == (
        _SUPPLIER_EVIDENCE_ID,
    )
    assert split.production_csv_rows()[0] == (
        "Reference",
        "Value",
        "Symbol",
        "Footprint",
        "DNP",
        "ReleaseReady",
        "PreparedRecordId",
        "AssetLockDigest",
    )
    assert "Footprint" not in split.procurement_csv_rows()[0]


def test_asset_lock_mismatch_only_blocks_production_view() -> None:
    split = split_manufacturing_bom(
        _selection(asset_lock="f" * 64),
        _manifest(procurement_status="ready", supplier_evidence=True),
    )

    assert split.production_ready is False
    assert split.production_blockers == ("U1:prepared_asset_lock_mismatch",)
    assert split.procurement_ready is True
    assert split.procurement_blockers == ()


def test_procurement_receipt_refresh_does_not_invalidate_locked_production_assets() -> None:
    split = split_manufacturing_bom(
        _selection(),
        _manifest(
            procurement_status="ready",
            supplier_evidence=True,
            record_id="f" * 64,
            manifest_id="0" * 64,
        ),
    )

    assert split.production_ready is True
    assert split.production_blockers == ()
    assert split.procurement_ready is False
    assert "prepared_component_manifest_mismatch" in split.procurement_blockers
    assert "U1:prepared_component_record_mismatch" in split.procurement_blockers


def test_legacy_bom_path_aliases_production_bom_without_claiming_readiness() -> None:
    result = ManufactureResult(bom_path="run/controller_bom.csv")

    assert result.bom_path == "run/controller_bom.csv"
    assert result.production_bom_path == "run/controller_bom.csv"
    assert result.production_bom_ready is False
    assert result.procurement_bom_ready is False


def test_explicit_production_path_backfills_legacy_bom_path() -> None:
    result = ManufactureResult(
        production_bom_path="run/controller_production_bom.csv",
        production_bom_ready=True,
    )

    assert result.bom_path == "run/controller_production_bom.csv"


def test_bom_contract_rejects_contradictory_paths_and_ready_without_artifact() -> None:
    with pytest.raises(ValidationError, match="must identify the same file"):
        ManufactureResult(
            bom_path="run/legacy.csv",
            production_bom_path="run/production.csv",
        )
    with pytest.raises(ValidationError, match="requires procurement_bom_path"):
        ManufactureResult(procurement_bom_ready=True)
