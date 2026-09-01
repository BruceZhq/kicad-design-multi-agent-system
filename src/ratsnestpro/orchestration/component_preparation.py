"""Prepare immutable component assets before schematic generation.

This module is intentionally a thin orchestration boundary.  It delegates
identity/library decisions to :class:`ComponentResolutionService`, observes the
installed KiCad files through the existing EDA adapters, and optionally records
grounded supplier rows from :class:`PartSelector`.  It does not search the web,
parse datasheets, or generate library files itself.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, model_validator

from ratsnestpro.domain.contracts import ContractModel
from ratsnestpro.eda import footprints, symbols
from ratsnestpro.orchestration.component_resolution import (
    ComponentResolution,
    ComponentResolutionService,
    GroundedReplacement,
    LibraryClosureResult,
    SymbolOnlyPlaceholderSpec,
)
from ratsnestpro.orchestration.pipeline_contracts import SelectedPart, SelectionPlan
from ratsnestpro.parts.selector import PartCandidate, PartSelector

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
VerificationStatus = Literal["verified", "unverified", "failed", "not_applicable"]
ReadinessStatus = Literal["ready", "partial", "blocked", "not_applicable"]


def _canonical_digest(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def requirement_digest(requirement: object) -> str:
    """Return a stable digest for raw text or an existing contract object."""

    if isinstance(requirement, str):
        payload: object = {"raw_text": requirement}
    elif hasattr(requirement, "model_dump"):
        payload = requirement.model_dump(mode="json")  # type: ignore[union-attr]
    elif isinstance(requirement, Mapping | list | tuple):
        payload = requirement
    else:
        raise TypeError("requirement must be text, a mapping/sequence, or a contract")
    return _canonical_digest(payload)


class PreparedAssetEvidence(ContractModel):
    """One content-addressed local asset used by the selected component."""

    kind: Literal["symbol", "footprint", "model_3d"]
    asset_id: str = Field(min_length=1, max_length=2_000)
    source_path: str = Field(min_length=1, max_length=2_000)
    sha256: str = Field(pattern=_SHA256_PATTERN)
    size_bytes: int = Field(ge=0)


class SupplierEvidence(ContractModel):
    """A time-scoped supplier fact; never an electrical identity claim."""

    supplier: str = Field(min_length=1, max_length=120)
    supplier_part_number: str = Field(default="", max_length=120)
    mpn: str = Field(default="", max_length=160)
    source_id: str = Field(min_length=1, max_length=500)
    observed_at: datetime
    stock: int | None = Field(default=None, ge=0)
    unit_price: float | None = Field(default=None, ge=0)
    evidence_id: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def _content_addressed(self) -> SupplierEvidence:
        expected = _canonical_digest(
            self.model_dump(mode="json", exclude={"evidence_id"})
        )
        if self.evidence_id != expected:
            raise ValueError("supplier evidence digest is invalid")
        return self


def build_supplier_evidence(
    *,
    supplier: str,
    source_id: str,
    observed_at: datetime,
    supplier_part_number: str = "",
    mpn: str = "",
    stock: int | None = None,
    unit_price: float | None = None,
) -> SupplierEvidence:
    payload = {
        "supplier": supplier,
        "supplier_part_number": supplier_part_number,
        "mpn": mpn,
        "source_id": source_id,
        "observed_at": observed_at.isoformat().replace("+00:00", "Z"),
        "stock": stock,
        "unit_price": unit_price,
    }
    return SupplierEvidence.model_validate({
        **payload,
        "evidence_id": _canonical_digest(payload),
    })


class ComponentConsistencyStatus(ContractModel):
    """The six independent consistency decisions required before locking BOM."""

    identity: VerificationStatus
    mpn: VerificationStatus
    package: VerificationStatus
    symbol_semantics: VerificationStatus
    pin_pad: VerificationStatus
    asset_provenance: VerificationStatus


class ComponentPreparationInput(ContractModel):
    """Trusted evidence already collected by Architect/Parts for one reference."""

    trusted_requested_identity: str = Field(default="", max_length=200)
    trusted_identity_mode: Literal[
        "",
        "fixed_exact",
        "family_variant",
        "capability_only",
    ] = ""
    trusted_identity_provenance: str = Field(default="", max_length=240)
    fixed_identity: bool = False
    allow_equivalent: bool = False
    allow_unverified_placeholder: bool = False
    pin_evidence: SymbolOnlyPlaceholderSpec | None = None
    replacement: GroundedReplacement | None = None
    manufacturer: str = Field(default="", max_length=160)
    supplier_evidence: list[SupplierEvidence] = Field(default_factory=list, max_length=32)
    technical_evidence_ids: list[str] = Field(default_factory=list, max_length=64)
    model_3d_path: str = Field(default="", max_length=2_000)
    require_3d: bool = False


class PreparedComponentRecord(ContractModel):
    """Immutable upstream record consumed by Selection and the locked BOM."""

    schema_version: Literal["ratsnestpro.prepared-component.v1"] = (
        "ratsnestpro.prepared-component.v1"
    )
    requirement_sha256: str = Field(pattern=_SHA256_PATTERN)
    ref: str = Field(min_length=1, max_length=32)
    requested_identity: str = Field(min_length=1, max_length=200)
    identity_mode: str = Field(min_length=1, max_length=32)
    identity_provenance: str = Field(min_length=1, max_length=240)
    resolution_status: str = Field(min_length=1, max_length=64)
    resolution_reason: str = Field(min_length=1, max_length=160)
    value: str = Field(min_length=1, max_length=200)
    role: str = Field(default="", max_length=120)
    manufacturer: str = Field(default="", max_length=160)
    mpn: str = Field(default="", max_length=160)
    lcsc: str = Field(default="", max_length=40)
    symbol_lib_id: str = Field(min_length=1, max_length=200)
    footprint_lib_id: str = Field(default="", max_length=240)
    assets: list[PreparedAssetEvidence] = Field(default_factory=list, max_length=3)
    supplier_evidence: list[SupplierEvidence] = Field(default_factory=list, max_length=32)
    technical_evidence_ids: list[str] = Field(default_factory=list, max_length=64)
    consistency: ComponentConsistencyStatus
    electrical_status: Literal["ready", "blocked"]
    procurement_status: ReadinessStatus
    mechanical_status: ReadinessStatus
    electrical_blockers: list[str] = Field(default_factory=list, max_length=32)
    procurement_blockers: list[str] = Field(default_factory=list, max_length=32)
    mechanical_blockers: list[str] = Field(default_factory=list, max_length=32)
    asset_lock_digest: str = Field(pattern=_SHA256_PATTERN)
    prepared_record_id: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def _content_addressed_and_consistent(self) -> PreparedComponentRecord:
        asset_kinds = [asset.kind for asset in self.assets]
        if len(asset_kinds) != len(set(asset_kinds)):
            raise ValueError("prepared component assets must have unique kinds")
        asset_payload = {
            "symbol_lib_id": self.symbol_lib_id,
            "footprint_lib_id": self.footprint_lib_id,
            "assets": [
                {
                    "kind": asset.kind,
                    "asset_id": asset.asset_id,
                    "sha256": asset.sha256,
                    "size_bytes": asset.size_bytes,
                }
                for asset in self.assets
            ],
        }
        if self.asset_lock_digest != _canonical_digest(asset_payload):
            raise ValueError("prepared component asset lock digest is invalid")
        record_payload = self.model_dump(mode="json", exclude={"prepared_record_id"})
        if self.prepared_record_id != _canonical_digest(record_payload):
            raise ValueError("prepared component record digest is invalid")
        if self.electrical_status == "ready" and self.electrical_blockers:
            raise ValueError("electrical-ready record cannot have electrical blockers")
        if self.electrical_status == "blocked" and not self.electrical_blockers:
            raise ValueError("electrical-blocked record requires electrical blockers")
        if self.procurement_status in {"ready", "not_applicable"}:
            if self.procurement_blockers:
                raise ValueError("procurement-ready record cannot have blockers")
        elif not self.procurement_blockers:
            raise ValueError("partial/blocked procurement record requires blockers")
        if self.mechanical_status in {"ready", "not_applicable"}:
            if self.mechanical_blockers:
                raise ValueError("mechanically ready record cannot have blockers")
        elif not self.mechanical_blockers:
            raise ValueError("partial/blocked mechanical record requires blockers")
        return self

    @property
    def supplier_evidence_ids(self) -> list[str]:
        return [item.evidence_id for item in self.supplier_evidence]


class PreparedComponentManifest(ContractModel):
    """Requirement-bound, content-addressed preparation receipt for one BOM."""

    schema_version: Literal["ratsnestpro.prepared-components.v1"] = (
        "ratsnestpro.prepared-components.v1"
    )
    requirement_sha256: str = Field(pattern=_SHA256_PATTERN)
    generated_at: datetime
    records: list[PreparedComponentRecord] = Field(min_length=1, max_length=1_000)
    electrical_status: Literal["ready", "blocked"]
    procurement_status: ReadinessStatus
    mechanical_status: ReadinessStatus
    electrical_blockers: list[str] = Field(default_factory=list, max_length=4_000)
    procurement_blockers: list[str] = Field(default_factory=list, max_length=4_000)
    mechanical_blockers: list[str] = Field(default_factory=list, max_length=4_000)
    manifest_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def _content_addressed_and_consistent(self) -> PreparedComponentManifest:
        refs = [item.ref for item in self.records]
        if len(refs) != len(set(refs)):
            raise ValueError("prepared component references must be unique")
        if any(item.requirement_sha256 != self.requirement_sha256 for item in self.records):
            raise ValueError("prepared records must bind the same requirement digest")
        expected_electrical = [
            f"{item.ref}:{blocker}"
            for item in self.records
            for blocker in item.electrical_blockers
        ]
        expected_procurement = [
            f"{item.ref}:{blocker}"
            for item in self.records
            for blocker in item.procurement_blockers
        ]
        expected_mechanical = [
            f"{item.ref}:{blocker}"
            for item in self.records
            for blocker in item.mechanical_blockers
        ]
        if self.electrical_blockers != expected_electrical:
            raise ValueError("manifest electrical blockers do not equal record blockers")
        if self.procurement_blockers != expected_procurement:
            raise ValueError("manifest procurement blockers do not equal record blockers")
        if self.mechanical_blockers != expected_mechanical:
            raise ValueError("manifest mechanical blockers do not equal record blockers")
        if self.electrical_status != (
            "blocked" if self.electrical_blockers else "ready"
        ):
            raise ValueError("manifest electrical status contradicts its blockers")
        if self.procurement_status != _aggregate_readiness(
            [item.procurement_status for item in self.records]
        ):
            raise ValueError("manifest procurement status contradicts its records")
        if self.mechanical_status != _aggregate_readiness(
            [item.mechanical_status for item in self.records]
        ):
            raise ValueError("manifest mechanical status contradicts its records")
        expected_digest = _canonical_digest(
            self.model_dump(mode="json", exclude={"manifest_sha256"})
        )
        if self.manifest_sha256 != expected_digest:
            raise ValueError("prepared component manifest digest is invalid")
        return self

    @property
    def design_ready(self) -> bool:
        return self.electrical_status == "ready" and self.mechanical_status != "blocked"


class ComponentPreparationResult(ContractModel):
    selection: SelectionPlan
    closure: LibraryClosureResult
    manifest: PreparedComponentManifest


class PreparedSelectionValidation(ContractModel):
    valid: bool
    blockers: list[str] = Field(default_factory=list, max_length=4_000)

    @model_validator(mode="after")
    def _verdict_matches_blockers(self) -> PreparedSelectionValidation:
        if self.valid is not (not self.blockers):
            raise ValueError("prepared selection verdict contradicts its blockers")
        return self


def validate_prepared_selection(
    selection: SelectionPlan,
    manifest: PreparedComponentManifest,
) -> PreparedSelectionValidation:
    """Prove that Selection still consumes the exact prepared BOM records."""

    blockers: list[str] = []
    if selection.prepared_manifest_sha256 != manifest.manifest_sha256:
        blockers.append("prepared_manifest_digest_mismatch")
    if selection.requirement_sha256 != manifest.requirement_sha256:
        blockers.append("prepared_requirement_digest_mismatch")
    records = {item.ref: item for item in manifest.records}
    selected_refs = {part.ref for part in selection.parts}
    for missing_ref in sorted(set(records) - selected_refs):
        blockers.append(f"{missing_ref}:prepared_component_removed")
    for part in selection.parts:
        record = records.get(part.ref)
        if record is None:
            blockers.append(f"{part.ref}:prepared_record_missing")
            continue
        expected = {
            "prepared_record_id": record.prepared_record_id,
            "asset_lock_digest": record.asset_lock_digest,
            "symbol": record.symbol_lib_id,
            "footprint": record.footprint_lib_id,
            "mpn": record.mpn,
            "lcsc": record.lcsc,
        }
        actual = {
            "prepared_record_id": part.prepared_record_id,
            "asset_lock_digest": part.asset_lock_digest,
            "symbol": part.symbol,
            "footprint": part.footprint,
            "mpn": part.mpn,
            "lcsc": part.lcsc,
        }
        for name, expected_value in expected.items():
            if actual[name] != expected_value:
                blockers.append(f"{part.ref}:{name}_changed_after_preparation")
        if part.supplier_evidence_ids != record.supplier_evidence_ids:
            blockers.append(f"{part.ref}:supplier_evidence_changed_after_preparation")
    return PreparedSelectionValidation(
        valid=not blockers,
        blockers=blockers,
    )


def _asset_evidence(
    kind: Literal["symbol", "footprint", "model_3d"],
    asset_id: str,
    path: Path | None,
) -> PreparedAssetEvidence | None:
    if path is None or not path.is_file():
        return None
    resolved = path.resolve()
    return PreparedAssetEvidence(
        kind=kind,
        asset_id=str(resolved) if kind == "model_3d" else asset_id,
        source_path=str(resolved),
        sha256=_sha256_file(resolved),
        size_bytes=resolved.stat().st_size,
    )


def _numbers(rows: Sequence[Mapping[str, Any]] | None) -> set[str]:
    return {
        str(row.get("number", "")).strip()
        for row in (rows or ())
        if str(row.get("number", "")).strip()
    }


def _aggregate_readiness(statuses: Sequence[ReadinessStatus]) -> ReadinessStatus:
    if any(status == "blocked" for status in statuses):
        return "blocked"
    applicable = [status for status in statuses if status != "not_applicable"]
    if not applicable:
        return "not_applicable"
    if any(status == "partial" for status in applicable):
        return "partial"
    return "ready"


class ComponentPreparationService:
    """Compose installed-library resolution into immutable preparation records."""

    def __init__(
        self,
        *,
        resolution_service: ComponentResolutionService | None = None,
        part_selector: PartSelector | None = None,
        symbol_pins: Callable[[str], Sequence[Mapping[str, Any]] | None] = (
            symbols.symbol_pins
        ),
        footprint_pads: Callable[[str], Sequence[Mapping[str, Any]] | None] = (
            footprints.footprint_pads
        ),
        symbol_path: Callable[[str], Path | None] = symbols.resolve_symbol,
        footprint_path: Callable[[str], Path | None] = footprints.footprint_path,
    ) -> None:
        self._resolution_service = resolution_service or ComponentResolutionService()
        self._part_selector = part_selector or PartSelector()
        self._symbol_pins = symbol_pins
        self._footprint_pads = footprint_pads
        self._symbol_path = symbol_path
        self._footprint_path = footprint_path

    def _catalog_evidence(
        self,
        part: SelectedPart,
        observed_at: datetime,
    ) -> list[SupplierEvidence]:
        if not (part.mpn or part.lcsc) or not self._part_selector.available():
            return []
        found: dict[tuple[str, str], PartCandidate] = {}
        for query in dict.fromkeys(item for item in (part.lcsc, part.mpn) if item):
            for candidate in self._part_selector.search(query, limit=10):
                lcsc_matches = not part.lcsc or candidate.lcsc == part.lcsc
                mpn_matches = not part.mpn or candidate.mpn.casefold() == part.mpn.casefold()
                if lcsc_matches and mpn_matches:
                    found[(candidate.lcsc, candidate.mpn)] = candidate
        return [
            build_supplier_evidence(
                supplier="JLCPCB",
                supplier_part_number=candidate.lcsc,
                mpn=candidate.mpn,
                source_id="local_jlcpcb_cache",
                observed_at=observed_at,
                stock=candidate.stock,
                unit_price=candidate.price,
            )
            for candidate in found.values()
        ]

    def _prepare_record(
        self,
        part: SelectedPart,
        resolution: ComponentResolution,
        directive: ComponentPreparationInput,
        *,
        requirement_sha256: str,
        observed_at: datetime,
    ) -> PreparedComponentRecord:
        pin_rows = self._symbol_pins(part.symbol)
        pad_rows = self._footprint_pads(part.footprint) if part.footprint else None
        pins = _numbers(pin_rows)
        pads = _numbers(pad_rows)
        connector_extra_pads = (
            part.symbol.startswith(("Connector:", "Connector_Generic:"))
            and pins.issubset(pads)
        )
        pin_pad_ok = bool(pin_rows is not None and pad_rows is not None) and (
            pins == pads or connector_extra_pads
        )

        assets = [
            item
            for item in (
                _asset_evidence("symbol", part.symbol, self._symbol_path(part.symbol)),
                _asset_evidence(
                    "footprint",
                    part.footprint,
                    self._footprint_path(part.footprint) if part.footprint else None,
                ),
                _asset_evidence(
                    "model_3d",
                    directive.model_3d_path,
                    Path(directive.model_3d_path) if directive.model_3d_path else None,
                ),
            )
            if item is not None
        ]
        asset_kinds = {item.kind for item in assets}

        supplier_evidence = list(directive.supplier_evidence)
        if not supplier_evidence:
            supplier_evidence = self._catalog_evidence(part, observed_at)
        if not part.mpn:
            explicit_mpns = {item.mpn for item in supplier_evidence if item.mpn}
            if len(explicit_mpns) == 1:
                part.mpn = explicit_mpns.pop()
        matching_supply = [
            item
            for item in supplier_evidence
            if (not part.mpn or item.mpn.casefold() == part.mpn.casefold())
            and (not part.lcsc or item.supplier_part_number == part.lcsc)
        ]
        mechanical_part = part.role.casefold() in {"mounting_hole", "fiducial"}
        if mechanical_part:
            mpn_status: VerificationStatus = "not_applicable"
        elif not part.mpn and not part.lcsc:
            mpn_status = "unverified"
        elif matching_supply:
            mpn_status = "verified"
        elif supplier_evidence:
            mpn_status = "failed"
        else:
            mpn_status = "unverified"

        consistency = ComponentConsistencyStatus(
            identity="verified" if resolution.release_ready else "failed",
            mpn=mpn_status,
            package="verified" if resolution.release_ready and pad_rows is not None else "failed",
            symbol_semantics=(
                "verified" if resolution.release_ready and pin_rows is not None else "failed"
            ),
            pin_pad="verified" if pin_pad_ok else "failed",
            asset_provenance=(
                "verified"
                if {"symbol", "footprint"}.issubset(asset_kinds)
                else "failed"
            ),
        )
        electrical_blockers = [
            f"{name}_consistency_{status}"
            for name, status in (
                ("identity", consistency.identity),
                ("package", consistency.package),
                ("symbol_semantics", consistency.symbol_semantics),
                ("pin_pad", consistency.pin_pad),
                ("asset_provenance", consistency.asset_provenance),
            )
            if status != "verified"
        ]
        if mechanical_part:
            procurement_status: ReadinessStatus = "not_applicable"
            procurement_blockers: list[str] = []
        elif mpn_status == "verified":
            procurement_status = "ready"
            procurement_blockers = []
        elif mpn_status == "failed":
            procurement_status = "blocked"
            procurement_blockers = ["supplier_identity_mismatch"]
        else:
            procurement_status = "partial"
            procurement_blockers = ["supplier_evidence_unavailable"]

        model_evidence = next(
            (item for item in assets if item.kind == "model_3d"),
            None,
        )
        if directive.require_3d and model_evidence is None:
            mechanical_status: ReadinessStatus = "blocked"
            mechanical_blockers = ["required_model_3d_missing"]
        elif model_evidence is not None:
            mechanical_status = "ready"
            mechanical_blockers = []
        else:
            mechanical_status = "not_applicable"
            mechanical_blockers = []

        asset_payload = {
            "symbol_lib_id": part.symbol,
            "footprint_lib_id": part.footprint,
            "assets": [
                {
                    "kind": item.kind,
                    "asset_id": item.asset_id,
                    "sha256": item.sha256,
                    "size_bytes": item.size_bytes,
                }
                for item in assets
            ],
        }
        payload = {
            "schema_version": "ratsnestpro.prepared-component.v1",
            "requirement_sha256": requirement_sha256,
            "ref": part.ref,
            "requested_identity": resolution.requested_identity,
            "identity_mode": resolution.identity_mode,
            "identity_provenance": resolution.identity_provenance,
            "resolution_status": str(resolution.status),
            "resolution_reason": resolution.reason_code,
            "value": part.value,
            "role": part.role,
            "manufacturer": directive.manufacturer or part.manufacturer,
            "mpn": part.mpn,
            "lcsc": part.lcsc,
            "symbol_lib_id": part.symbol,
            "footprint_lib_id": part.footprint,
            "assets": [item.model_dump(mode="json") for item in assets],
            "supplier_evidence": [
                item.model_dump(mode="json") for item in supplier_evidence
            ],
            "technical_evidence_ids": list(dict.fromkeys(directive.technical_evidence_ids)),
            "consistency": consistency.model_dump(mode="json"),
            "electrical_status": "blocked" if electrical_blockers else "ready",
            "procurement_status": procurement_status,
            "mechanical_status": mechanical_status,
            "electrical_blockers": electrical_blockers,
            "procurement_blockers": procurement_blockers,
            "mechanical_blockers": mechanical_blockers,
            "asset_lock_digest": _canonical_digest(asset_payload),
        }
        return PreparedComponentRecord.model_validate({
            **payload,
            "prepared_record_id": _canonical_digest(payload),
        })

    def prepare(
        self,
        selection: SelectionPlan,
        requirement: object,
        *,
        inputs: Mapping[str, ComponentPreparationInput | Mapping[str, Any]] | None = None,
        observed_at: datetime | None = None,
        mutate_selection: bool = True,
    ) -> ComponentPreparationResult:
        """Resolve, verify, and lock every selected physical component."""

        target = selection if mutate_selection else selection.model_copy(deep=True)
        timestamp = observed_at or datetime.now(UTC)
        requirement_sha256 = requirement_digest(requirement)
        raw_inputs = inputs or {}
        resolutions: list[ComponentResolution] = []
        records: list[PreparedComponentRecord] = []

        for part in target.parts:
            raw_directive = raw_inputs.get(part.ref, ComponentPreparationInput())
            directive = (
                raw_directive
                if isinstance(raw_directive, ComponentPreparationInput)
                else ComponentPreparationInput.model_validate(raw_directive)
            )
            trusted_mode = directive.trusted_identity_mode or part.identity_mode or None
            trusted_provenance = (
                directive.trusted_identity_provenance
                or part.identity_provenance
                or None
            )
            resolution = self._resolution_service.resolve(
                part,
                trusted_requested_identity=(
                    directive.trusted_requested_identity
                    or part.requested_identity
                    or part.value
                ),
                trusted_identity_mode=trusted_mode,  # type: ignore[arg-type]
                trusted_identity_provenance=trusted_provenance,
                pin_evidence=directive.pin_evidence,
                replacement=directive.replacement,
                fixed_identity=(
                    directive.fixed_identity or trusted_mode == "fixed_exact"
                ),
                allow_equivalent=directive.allow_equivalent,
                allow_unverified_placeholder=directive.allow_unverified_placeholder,
                mutate=True,
            )
            if resolution.release_ready:
                part.footprint_binding_status = "verified_installed"
                part.footprint_binding_basis = "prepared_component_asset_closure"
            record = self._prepare_record(
                part,
                resolution,
                directive,
                requirement_sha256=requirement_sha256,
                observed_at=timestamp,
            )
            part.manufacturer = record.manufacturer
            part.prepared_record_id = record.prepared_record_id
            part.asset_lock_digest = record.asset_lock_digest
            part.supplier_evidence_ids = record.supplier_evidence_ids
            model_evidence = next(
                (item for item in record.assets if item.kind == "model_3d"),
                None,
            )
            part.model_3d_path = model_evidence.source_path if model_evidence else ""
            part.model_3d_sha256 = model_evidence.sha256 if model_evidence else ""
            resolutions.append(resolution)
            records.append(record)

        electrical_blockers = [
            f"{record.ref}:{blocker}"
            for record in records
            for blocker in record.electrical_blockers
        ]
        procurement_blockers = [
            f"{record.ref}:{blocker}"
            for record in records
            for blocker in record.procurement_blockers
        ]
        mechanical_blockers = [
            f"{record.ref}:{blocker}"
            for record in records
            for blocker in record.mechanical_blockers
        ]
        manifest_payload = {
            "schema_version": "ratsnestpro.prepared-components.v1",
            "requirement_sha256": requirement_sha256,
            "generated_at": timestamp.isoformat().replace("+00:00", "Z"),
            "records": [item.model_dump(mode="json") for item in records],
            "electrical_status": "blocked" if electrical_blockers else "ready",
            "procurement_status": _aggregate_readiness(
                [item.procurement_status for item in records]
            ),
            "mechanical_status": _aggregate_readiness(
                [item.mechanical_status for item in records]
            ),
            "electrical_blockers": electrical_blockers,
            "procurement_blockers": procurement_blockers,
            "mechanical_blockers": mechanical_blockers,
        }
        manifest = PreparedComponentManifest.model_validate({
            **manifest_payload,
            "manifest_sha256": _canonical_digest(manifest_payload),
        })
        target.requirement_sha256 = requirement_sha256
        target.prepared_manifest_sha256 = manifest.manifest_sha256
        return ComponentPreparationResult(
            selection=target,
            closure=LibraryClosureResult(resolutions=resolutions),
            manifest=manifest,
        )
