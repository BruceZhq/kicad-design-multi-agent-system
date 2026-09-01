"""Pure production/procurement BOM projection.

The production BOM answers "what is built?" from locked EDA assets.  The
procurement BOM answers "what can be bought, from which observed evidence?".
Keeping the projections independent prevents a stale or absent supplier quote
from being misreported as an electrical design failure.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from ratsnestpro.orchestration.component_preparation import (
    PreparedComponentManifest,
    PreparedComponentRecord,
)
from ratsnestpro.orchestration.pipeline_contracts import SelectionPlan

ProcurementRowStatus = Literal[
    "ready",
    "partial",
    "blocked",
    "not_applicable",
    "unavailable",
]

PRODUCTION_BOM_COLUMNS = (
    "Reference",
    "Value",
    "Symbol",
    "Footprint",
    "DNP",
    "ReleaseReady",
    "PreparedRecordId",
    "AssetLockDigest",
)

PROCUREMENT_BOM_COLUMNS = (
    "Reference",
    "Manufacturer",
    "MPN",
    "LCSC",
    "SupplierEvidenceIds",
    "ProcurementStatus",
    "ProcurementBlockers",
)


@dataclass(frozen=True, slots=True)
class ProductionBomRow:
    reference: str
    value: str
    symbol: str
    footprint: str
    dnp: bool
    release_ready: bool
    prepared_record_id: str
    asset_lock_digest: str

    def csv_row(self) -> tuple[str, ...]:
        return (
            self.reference,
            self.value,
            self.symbol,
            self.footprint,
            "yes" if self.dnp else "",
            "yes" if self.release_ready else "no",
            self.prepared_record_id,
            self.asset_lock_digest,
        )


@dataclass(frozen=True, slots=True)
class ProcurementBomRow:
    reference: str
    manufacturer: str
    mpn: str
    lcsc: str
    supplier_evidence_ids: tuple[str, ...]
    status: ProcurementRowStatus
    blockers: tuple[str, ...]

    def csv_row(self) -> tuple[str, ...]:
        return (
            self.reference,
            self.manufacturer,
            self.mpn,
            self.lcsc,
            ";".join(self.supplier_evidence_ids),
            self.status,
            ";".join(self.blockers),
        )


@dataclass(frozen=True, slots=True)
class BomSplit:
    production_rows: tuple[ProductionBomRow, ...]
    procurement_rows: tuple[ProcurementBomRow, ...]
    production_ready: bool
    procurement_ready: bool
    production_blockers: tuple[str, ...]
    procurement_blockers: tuple[str, ...]

    def production_csv_rows(self) -> tuple[tuple[str, ...], ...]:
        return (PRODUCTION_BOM_COLUMNS, *(row.csv_row() for row in self.production_rows))

    def procurement_csv_rows(self) -> tuple[tuple[str, ...], ...]:
        return (
            PROCUREMENT_BOM_COLUMNS,
            *(row.csv_row() for row in self.procurement_rows),
        )


def _append_unique(items: list[str], value: str) -> None:
    if value not in items:
        items.append(value)


def _record_production_blockers(
    *,
    ref: str,
    asset_lock_digest: str,
    record: PreparedComponentRecord | None,
) -> list[str]:
    blockers: list[str] = []
    if record is None:
        return [f"{ref}:prepared_component_record_unavailable"]
    if asset_lock_digest != record.asset_lock_digest:
        blockers.append(f"{ref}:prepared_asset_lock_mismatch")
    blockers.extend(f"{ref}:{item}" for item in record.electrical_blockers)
    if record.electrical_status != "ready" and not record.electrical_blockers:
        blockers.append(f"{ref}:electrical_preparation_blocked")
    blockers.extend(f"{ref}:{item}" for item in record.mechanical_blockers)
    if record.mechanical_status == "blocked" and not record.mechanical_blockers:
        blockers.append(f"{ref}:mechanical_preparation_blocked")
    return blockers


def _record_procurement(
    *,
    ref: str,
    prepared_record_id: str,
    record: PreparedComponentRecord | None,
) -> tuple[ProcurementRowStatus, list[str]]:
    if record is None:
        return "unavailable", [f"{ref}:prepared_component_record_unavailable"]
    status: ProcurementRowStatus = record.procurement_status
    blockers = [f"{ref}:{item}" for item in record.procurement_blockers]
    if prepared_record_id != record.prepared_record_id:
        status = "blocked"
        blockers.append(f"{ref}:prepared_component_record_mismatch")
    if status == "ready" and not record.supplier_evidence:
        status = "partial"
        blockers.append(f"{ref}:supplier_evidence_unavailable")
    if status in {"partial", "blocked"} and not blockers:
        blockers.append(f"{ref}:procurement_{status}")
    return status, blockers


def split_manufacturing_bom(
    selection: SelectionPlan,
    prepared_manifest: PreparedComponentManifest | None,
) -> BomSplit:
    """Build independent, deterministic production and procurement views.

    This function performs no filesystem or network I/O.  A caller may write
    the returned CSV rows atomically and copy the two readiness decisions into
    :class:`ManufactureResult`.
    """

    records = (
        {record.ref: record for record in prepared_manifest.records}
        if prepared_manifest is not None
        else {}
    )
    production_rows: list[ProductionBomRow] = []
    procurement_rows: list[ProcurementBomRow] = []
    production_blockers: list[str] = []
    procurement_blockers: list[str] = []

    manifest_integrity_blockers: list[str] = []
    if prepared_manifest is None:
        manifest_integrity_blockers.append("prepared_component_manifest_unavailable")
    else:
        if (
            selection.prepared_manifest_sha256
            and selection.prepared_manifest_sha256 != prepared_manifest.manifest_sha256
        ):
            _append_unique(
                procurement_blockers,
                "prepared_component_manifest_mismatch",
            )
        if (
            selection.requirement_sha256
            and selection.requirement_sha256 != prepared_manifest.requirement_sha256
        ):
            manifest_integrity_blockers.append("prepared_requirement_mismatch")

    for blocker in manifest_integrity_blockers:
        _append_unique(production_blockers, blocker)
        _append_unique(procurement_blockers, blocker)

    selected_refs = {part.ref for part in selection.parts}
    for extra_ref in records.keys() - selected_refs:
        blocker = f"{extra_ref}:prepared_record_not_selected"
        _append_unique(production_blockers, blocker)
        _append_unique(procurement_blockers, blocker)

    for part in selection.parts:
        record = records.get(part.ref)
        row_production_blockers: list[str] = []
        if part.dnp:
            row_production_blockers.append(f"{part.ref}:dnp")
        if part.unresolved:
            row_production_blockers.append(f"{part.ref}:unresolved")
        if not part.release_ready:
            row_production_blockers.append(f"{part.ref}:component_not_release_ready")
        if not part.symbol:
            row_production_blockers.append(f"{part.ref}:symbol_unavailable")
        if not part.footprint:
            row_production_blockers.append(f"{part.ref}:footprint_unavailable")
        row_production_blockers.extend(
            _record_production_blockers(
                ref=part.ref,
                asset_lock_digest=part.asset_lock_digest,
                record=record,
            )
        )
        for blocker in row_production_blockers:
            _append_unique(production_blockers, blocker)

        procurement_status, row_procurement_blockers = _record_procurement(
            ref=part.ref,
            prepared_record_id=part.prepared_record_id,
            record=record,
        )
        for blocker in row_procurement_blockers:
            _append_unique(procurement_blockers, blocker)

        production_rows.append(ProductionBomRow(
            reference=part.ref,
            value=part.value,
            symbol=part.symbol,
            footprint=part.footprint,
            dnp=part.dnp,
            release_ready=not row_production_blockers,
            prepared_record_id=part.prepared_record_id,
            asset_lock_digest=part.asset_lock_digest,
        ))
        procurement_rows.append(ProcurementBomRow(
            reference=part.ref,
            manufacturer=(record.manufacturer if record is not None else part.manufacturer),
            mpn=(record.mpn if record is not None else part.mpn),
            lcsc=(record.lcsc if record is not None else part.lcsc),
            supplier_evidence_ids=(
                tuple(record.supplier_evidence_ids) if record is not None else ()
            ),
            status=procurement_status,
            blockers=tuple(row_procurement_blockers),
        ))

    return BomSplit(
        production_rows=tuple(production_rows),
        procurement_rows=tuple(procurement_rows),
        production_ready=bool(production_rows) and not production_blockers,
        procurement_ready=bool(procurement_rows) and not procurement_blockers,
        production_blockers=tuple(production_blockers),
        procurement_blockers=tuple(procurement_blockers),
    )
