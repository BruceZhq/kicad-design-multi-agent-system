from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from ratsnestpro.orchestration.component_preparation import (
    ComponentPreparationInput,
    ComponentPreparationService,
    build_supplier_evidence,
    validate_prepared_selection,
)
from ratsnestpro.orchestration.component_resolution import (
    ComponentResolution,
    ResolutionStatus,
)
from ratsnestpro.orchestration.design_closure import (
    ComponentClosureManifest,
    build_component_closure_manifest,
)
from ratsnestpro.orchestration.pipeline_contracts import SelectedPart, SelectionPlan


class _Resolver:
    def resolve(self, part: SelectedPart, **kwargs: object) -> ComponentResolution:
        requested = str(kwargs.get("trusted_requested_identity") or part.value)
        return ComponentResolution(
            ref=part.ref,
            status=ResolutionStatus.INSTALLED_EXACT,
            requested_identity=requested,
            symbol=part.symbol,
            footprint=part.footprint,
            release_ready=True,
            blocks_execution=False,
            reason_code="exact",
            detail="installed symbol and footprint verified",
            identity_mode=str(kwargs.get("trusted_identity_mode") or "capability_only"),
            identity_provenance=str(
                kwargs.get("trusted_identity_provenance") or "selection_proposal"
            ),
        )


class _NoCatalog:
    def available(self) -> bool:
        return False


def _selection(*, mpn: str = "", lcsc: str = "") -> SelectionPlan:
    return SelectionPlan(parts=[SelectedPart(
        ref="J1",
        symbol="Connector_Generic:Conn_01x02",
        value="5V input",
        footprint="Connector:PinHeader_1x02",
        requested_identity="two-pin input connector",
        identity_mode="capability_only",
        identity_provenance="user_requirement",
        mpn=mpn,
        lcsc=lcsc,
    )])


def _service(
    symbol_file: Path,
    footprint_file: Path | None,
) -> ComponentPreparationService:
    return ComponentPreparationService(
        resolution_service=_Resolver(),  # type: ignore[arg-type]
        part_selector=_NoCatalog(),  # type: ignore[arg-type]
        symbol_pins=lambda _lib_id: [
            {"number": "1", "name": "Pin_1"},
            {"number": "2", "name": "Pin_2"},
        ],
        footprint_pads=lambda _lib_id: [{"number": "1"}, {"number": "2"}],
        symbol_path=lambda _lib_id: symbol_file,
        footprint_path=lambda _lib_id: footprint_file,
    )


def test_preparation_locks_electrical_assets_without_conflating_procurement(
    tmp_path: Path,
) -> None:
    symbol_file = tmp_path / "Connector_Generic.kicad_sym"
    footprint_file = tmp_path / "PinHeader_1x02.kicad_mod"
    symbol_file.write_text("symbol", encoding="utf-8")
    footprint_file.write_text("footprint", encoding="utf-8")
    selection = _selection()

    result = _service(symbol_file, footprint_file).prepare(
        selection,
        "build a two-pin 5 V input",
        observed_at=datetime(2026, 9, 1, tzinfo=UTC),
    )

    record = result.manifest.records[0]
    assert result.manifest.electrical_status == "ready"
    assert result.manifest.procurement_status == "partial"
    assert result.manifest.mechanical_status == "not_applicable"
    assert record.consistency.model_dump() == {
        "identity": "verified",
        "mpn": "unverified",
        "package": "verified",
        "symbol_semantics": "verified",
        "pin_pad": "verified",
        "asset_provenance": "verified",
    }
    assert selection.parts[0].prepared_record_id == record.prepared_record_id
    assert selection.parts[0].asset_lock_digest == record.asset_lock_digest
    assert selection.prepared_manifest_sha256 == result.manifest.manifest_sha256
    assert selection.requirement_sha256 == result.manifest.requirement_sha256
    assert validate_prepared_selection(selection, result.manifest).valid is True

    closure = build_component_closure_manifest(
        selection,
        result.closure,
        observed_at=datetime(2026, 9, 1, tzinfo=UTC),
        symbol_pins=lambda _lib_id: [
            {"number": "1", "name": "Pin_1"},
            {"number": "2", "name": "Pin_2"},
        ],
        footprint_pads=lambda _lib_id: [{"number": "1"}, {"number": "2"}],
        symbol_path=lambda _lib_id: symbol_file,
        footprint_path=lambda _lib_id: footprint_file,
    )
    assert closure.release_ready is True
    assert closure.schema_version == "ratsnestpro.component-closure.v2"
    assert closure.prepared_manifest_sha256 == result.manifest.manifest_sha256
    assert closure.components[0].prepared_record_id == record.prepared_record_id
    assert closure.locked_bom_sha256 == closure.manifest_sha256

    symbol_file.write_text("symbol-changed-after-lock", encoding="utf-8")
    stale_lock = build_component_closure_manifest(
        selection,
        result.closure,
        symbol_pins=lambda _lib_id: [
            {"number": "1", "name": "Pin_1"},
            {"number": "2", "name": "Pin_2"},
        ],
        footprint_pads=lambda _lib_id: [{"number": "1"}, {"number": "2"}],
        symbol_path=lambda _lib_id: symbol_file,
        footprint_path=lambda _lib_id: footprint_file,
    )
    assert stale_lock.release_ready is False
    assert stale_lock.blockers == ["J1:prepared_asset_lock_mismatch"]

    selection.parts[0].mpn = "changed-after-preparation"
    validation = validate_prepared_selection(selection, result.manifest)
    assert validation.valid is False
    assert validation.blockers == ["J1:mpn_changed_after_preparation"]


def test_supplier_and_optional_3d_have_separate_readiness(
    tmp_path: Path,
) -> None:
    timestamp = datetime(2026, 9, 1, tzinfo=UTC)
    symbol_file = tmp_path / "Connector_Generic.kicad_sym"
    footprint_file = tmp_path / "PinHeader_1x02.kicad_mod"
    model_file = tmp_path / "header.step"
    symbol_file.write_text("symbol", encoding="utf-8")
    footprint_file.write_text("footprint", encoding="utf-8")
    model_file.write_text("step-model", encoding="utf-8")
    supplier = build_supplier_evidence(
        supplier="JLCPCB",
        supplier_part_number="C123",
        mpn="HDR-2P",
        source_id="local_jlcpcb_cache",
        observed_at=timestamp,
        stock=120,
        unit_price=0.04,
    )

    result = _service(symbol_file, footprint_file).prepare(
        _selection(mpn="HDR-2P", lcsc="C123"),
        "build a mechanically constrained input",
        inputs={
            "J1": ComponentPreparationInput(
                manufacturer="Example",
                supplier_evidence=[supplier],
                model_3d_path=str(model_file),
                require_3d=True,
            )
        },
        observed_at=timestamp,
    )

    record = result.manifest.records[0]
    assert record.consistency.mpn == "verified"
    assert result.manifest.procurement_status == "ready"
    assert result.manifest.mechanical_status == "ready"
    assert record.manufacturer == "Example"
    assert {item.kind for item in record.assets} == {
        "symbol",
        "footprint",
        "model_3d",
    }


def test_missing_physical_asset_blocks_before_schematic(tmp_path: Path) -> None:
    symbol_file = tmp_path / "Connector_Generic.kicad_sym"
    symbol_file.write_text("symbol", encoding="utf-8")

    result = _service(symbol_file, None).prepare(
        _selection(),
        "build a two-pin input",
    )

    assert result.manifest.electrical_status == "blocked"
    assert result.manifest.design_ready is False
    assert result.manifest.electrical_blockers == [
        "J1:asset_provenance_consistency_failed"
    ]


def test_v1_component_closure_digest_remains_readable() -> None:
    timestamp = datetime(2026, 8, 28, tzinfo=UTC)
    component = {
        "ref": "J1",
        "requested_identity": "two-pin input connector",
        "identity_mode": "capability_only",
        "identity_provenance": "user_requirement",
        "resolution_status": "installed_exact",
        "symbol_lib_id": "Connector_Generic:Conn_01x02",
        "footprint_lib_id": "Connector:PinHeader_1x02",
        "symbol_pin_numbers": ["1", "2"],
        "footprint_pad_numbers": ["1", "2"],
        "pin_pad_bindings": [
            {"pin_number": "1", "pin_name": "Pin_1", "pad_number": "1"},
            {"pin_number": "2", "pin_name": "Pin_2", "pad_number": "2"},
        ],
        "evidence": [
            {
                "kind": "symbol",
                "lib_id": "Connector_Generic:Conn_01x02",
                "source_path": "C:/legacy/symbol.kicad_sym",
                "sha256": "a" * 64,
                "size_bytes": 1,
                "modified_ns": 1,
                "observed_at": timestamp.isoformat().replace("+00:00", "Z"),
            },
            {
                "kind": "footprint",
                "lib_id": "Connector:PinHeader_1x02",
                "source_path": "C:/legacy/footprint.kicad_mod",
                "sha256": "b" * 64,
                "size_bytes": 1,
                "modified_ns": 1,
                "observed_at": timestamp.isoformat().replace("+00:00", "Z"),
            },
        ],
        "release_ready": True,
        "blockers": [],
    }
    payload = {
        "schema_version": "ratsnestpro.component-closure.v1",
        "generated_at": timestamp.isoformat().replace("+00:00", "Z"),
        "components": [component],
        "release_ready": True,
        "blockers": [],
    }
    digest = hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()

    manifest = ComponentClosureManifest.model_validate({
        **payload,
        "manifest_sha256": digest,
    })

    assert manifest.schema_version == "ratsnestpro.component-closure.v1"
    assert manifest.locked_bom_sha256 == digest
