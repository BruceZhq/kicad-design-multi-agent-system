"""Prepare immutable component assets before schematic generation.

This module is intentionally a thin orchestration boundary.  It delegates
identity/library decisions to :class:`ComponentResolutionService`, observes the
installed KiCad files through the existing EDA adapters, and optionally records
grounded supplier rows from :class:`PartSelector`.  It does not search the web,
parse datasheets, or generate library files itself.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
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
MountingStyle = Literal["smd", "through_hole", "mixed", "unknown"]
EvidencePolicy = Literal[
    "exact_component",
    "exact_connector",
    "controlled_generic_passive",
    "mechanical",
]
TrustedEvidenceProducer = Literal[
    "manufacturer_datasheet_adapter",
    "approved_component_pack_adapter",
]

_PRODUCER_SOURCE_KINDS = {
    "manufacturer_datasheet_adapter": "manufacturer_datasheet",
    "approved_component_pack_adapter": "approved_component_pack",
}
_RELEASE_TRUTH_SOURCE_KINDS = frozenset({
    "manufacturer_datasheet",
    "approved_component_pack",
})
_TECHNICAL_EVIDENCE_RECEIPT_DOMAIN = b"ratsnestpro.technical-evidence.v1\0"


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


class TechnicalPinFunction(ContractModel):
    number: str = Field(min_length=1, max_length=32)
    functions: list[str] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def _normalized_functions(self) -> TechnicalPinFunction:
        normalized = list(dict.fromkeys(item.strip() for item in self.functions if item.strip()))
        if not normalized:
            raise ValueError("technical pin functions must not be empty")
        object.__setattr__(self, "functions", normalized)
        return self


class TechnicalPackageEvidence(ContractModel):
    """Content-addressed proof that one exact MPN uses one physical package.

    The producer may be a manufacturer datasheet parser, an approved component
    pack, or the local distributor catalog.  This contract records a fact that
    has already been extracted; it deliberately does not let the LLM infer a
    package from a prose description at the schematic boundary.
    """

    schema_version: Literal[
        "ratsnestpro.technical-package-evidence.v1",
        "ratsnestpro.technical-package-evidence.v2",
    ] = (
        "ratsnestpro.technical-package-evidence.v2"
    )
    source_kind: Literal[
        "manufacturer_datasheet",
        "approved_component_pack",
        "distributor_catalog",
        "verified_local_kicad_binding",
    ]
    source_id: str = Field(min_length=1, max_length=1_000)
    source_sha256: str = Field(pattern=_SHA256_PATTERN)
    mpn: str = Field(default="", max_length=160)
    package: str = Field(min_length=1, max_length=160)
    mounting_style: MountingStyle = "unknown"
    pin_count: int | None = Field(default=None, ge=1, le=4_096)
    pin_functions: list[TechnicalPinFunction] = Field(
        default_factory=list,
        max_length=4_096,
    )
    # A datasheet parser/component-pack builder may bind the package to the
    # exact installed footprint. Distributor rows normally leave this empty and
    # are checked by package family/mounting style instead.
    footprint_lib_id: str = Field(default="", max_length=240)
    evidence_id: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def _content_addressed(self) -> TechnicalPackageEvidence:
        excluded = {"evidence_id"}
        if self.schema_version == "ratsnestpro.technical-package-evidence.v1":
            excluded.add("pin_functions")
        expected = _canonical_digest(self.model_dump(mode="json", exclude=excluded))
        if self.evidence_id != expected:
            raise ValueError("technical package evidence digest is invalid")
        return self


def build_technical_package_evidence(
    *,
    source_kind: Literal[
        "manufacturer_datasheet",
        "approved_component_pack",
        "distributor_catalog",
        "verified_local_kicad_binding",
    ],
    source_id: str,
    source_sha256: str,
    mpn: str = "",
    package: str,
    mounting_style: MountingStyle = "unknown",
    pin_count: int | None = None,
    pin_functions: Sequence[TechnicalPinFunction | Mapping[str, Any]] = (),
    footprint_lib_id: str = "",
) -> TechnicalPackageEvidence:
    """Build immutable package proof supplied by a trusted evidence adapter."""

    payload = {
        "schema_version": "ratsnestpro.technical-package-evidence.v2",
        "source_kind": source_kind,
        "source_id": source_id,
        "source_sha256": source_sha256,
        "mpn": mpn,
        "package": package,
        "mounting_style": mounting_style,
        "pin_count": pin_count,
        "pin_functions": [
            (
                item.model_dump(mode="json")
                if isinstance(item, TechnicalPinFunction)
                else TechnicalPinFunction.model_validate(item).model_dump(mode="json")
            )
            for item in pin_functions
        ],
        "footprint_lib_id": footprint_lib_id,
    }
    return TechnicalPackageEvidence.model_validate({
        **payload,
        "evidence_id": _canonical_digest(payload),
    })


class TrustedTechnicalEvidenceEnvelope(ContractModel):
    """Server-authenticated evidence emitted by a trusted extraction adapter.

    ``TechnicalPackageEvidence.evidence_id`` only detects accidental mutation;
    it does not authenticate who produced the fields.  This receipt is the
    boundary used when evidence crosses the prompt/browser channel.
    """

    schema_version: Literal["ratsnestpro.trusted-technical-evidence.v1"] = (
        "ratsnestpro.trusted-technical-evidence.v1"
    )
    producer: TrustedEvidenceProducer
    symbol_lib_id: str = Field(min_length=1, max_length=200)
    footprint_lib_id: str = Field(min_length=1, max_length=240)
    requested_identity: str = Field(min_length=1, max_length=200)
    evidence: TechnicalPackageEvidence
    receipt_hmac: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def _producer_matches_source_kind(self) -> TrustedTechnicalEvidenceEnvelope:
        if self.evidence.source_kind != _PRODUCER_SOURCE_KINDS[self.producer]:
            raise ValueError("technical evidence producer/source kind mismatch")
        return self

    def verifies(self, secret: str | bytes | None) -> bool:
        key = secret.encode("utf-8") if isinstance(secret, str) else secret
        if key is None or len(key) < 32:
            return False
        payload = self.model_dump(mode="json", exclude={"receipt_hmac"})
        expected = hmac.new(
            key,
            _TECHNICAL_EVIDENCE_RECEIPT_DOMAIN
            + json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(self.receipt_hmac, expected)


def build_trusted_technical_evidence_envelope(
    *,
    producer: TrustedEvidenceProducer,
    symbol_lib_id: str,
    footprint_lib_id: str,
    requested_identity: str,
    evidence: TechnicalPackageEvidence,
    secret: str | bytes,
) -> TrustedTechnicalEvidenceEnvelope:
    """Sign an evidence envelope inside a trusted producer, never in an LLM."""

    key = secret.encode("utf-8") if isinstance(secret, str) else secret
    if len(key) < 32:
        raise ValueError("technical evidence signing secret must be at least 32 bytes")
    payload = {
        "schema_version": "ratsnestpro.trusted-technical-evidence.v1",
        "producer": producer,
        "symbol_lib_id": symbol_lib_id,
        "footprint_lib_id": footprint_lib_id,
        "requested_identity": requested_identity,
        "evidence": evidence.model_dump(mode="json"),
    }
    receipt = hmac.new(
        key,
        _TECHNICAL_EVIDENCE_RECEIPT_DOMAIN
        + json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return TrustedTechnicalEvidenceEnvelope.model_validate({
        **payload,
        "receipt_hmac": receipt,
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
    technical_package_evidence: list[TechnicalPackageEvidence] = Field(
        default_factory=list,
        max_length=16,
    )
    technical_evidence_ids: list[str] = Field(default_factory=list, max_length=64)
    model_3d_path: str = Field(default="", max_length=2_000)
    require_3d: bool = False
    # None applies the deterministic device-class policy. An explicit value is
    # retained only for trusted tests/importers; model output never controls it.
    require_exact_mpn_package: bool | None = None
    workflow_revision: int = Field(default=0, ge=0)


class PreparedComponentRecord(ContractModel):
    """Immutable upstream record consumed by Selection and the locked BOM."""

    schema_version: Literal[
        "ratsnestpro.prepared-component.v1",
        "ratsnestpro.prepared-component.v2",
    ] = (
        "ratsnestpro.prepared-component.v2"
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
    technical_package_evidence: list[TechnicalPackageEvidence] = Field(
        default_factory=list,
        max_length=16,
    )
    technical_evidence_ids: list[str] = Field(default_factory=list, max_length=64)
    pin_pad_mapping_sha256: str = Field(
        default="",
        pattern=r"^(?:|[0-9a-f]{64})$",
    )
    strict_mpn_package: bool = False
    evidence_policy: EvidencePolicy = "exact_component"
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
        asset_payload: dict[str, Any] = {
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
        excluded = {"prepared_record_id"}
        if self.schema_version == "ratsnestpro.prepared-component.v1":
            excluded.update({
                "technical_package_evidence",
                "pin_pad_mapping_sha256",
                "strict_mpn_package",
                "evidence_policy",
            })
        record_payload = self.model_dump(mode="json", exclude=excluded)
        if self.prepared_record_id != _canonical_digest(record_payload):
            raise ValueError("prepared component record digest is invalid")
        if self.schema_version == "ratsnestpro.prepared-component.v2":
            if not self.pin_pad_mapping_sha256:
                raise ValueError("prepared component v2 requires a pin/pad mapping digest")
            required = (
                self.consistency.identity,
                self.consistency.package,
                self.consistency.symbol_semantics,
                self.consistency.pin_pad,
                self.consistency.asset_provenance,
            )
            if self.strict_mpn_package:
                required = (*required, self.consistency.mpn)
            if self.electrical_status == "ready" and any(
                status not in {"verified", "not_applicable"} for status in required
            ):
                raise ValueError(
                    "electrical-ready record contradicts component consistency"
                )
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

    @property
    def technical_package_evidence_ids(self) -> list[str]:
        return [item.evidence_id for item in self.technical_package_evidence]


class PreparedComponentManifest(ContractModel):
    """Requirement-bound, content-addressed preparation receipt for one BOM."""

    schema_version: Literal[
        "ratsnestpro.prepared-components.v1",
        "ratsnestpro.prepared-components.v2",
    ] = (
        "ratsnestpro.prepared-components.v2"
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
        manifest_payload = self.model_dump(mode="json", exclude={"manifest_sha256"})
        if self.schema_version == "ratsnestpro.prepared-components.v1":
            manifest_payload["records"] = [
                item.model_dump(
                    mode="json",
                    exclude={
                        "technical_package_evidence",
                        "pin_pad_mapping_sha256",
                        "strict_mpn_package",
                        "evidence_policy",
                    },
                )
                for item in self.records
            ]
        expected_digest = _canonical_digest(manifest_payload)
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


def _package_key(value: str) -> str:
    return "".join(character for character in value.upper() if character.isalnum())


def _catalog_footprint_binding(package: str, footprint_lib_id: str) -> str:
    """Bind only when the catalog package token occurs in the real footprint ID."""

    package_key = _package_key(package)
    return (
        footprint_lib_id
        if len(package_key) >= 3 and package_key in _package_key(footprint_lib_id)
        else ""
    )


def _pin_pad_mapping_digest(
    symbol_lib_id: str,
    footprint_lib_id: str,
    pin_rows: Sequence[Mapping[str, Any]] | None,
    pad_rows: Sequence[Mapping[str, Any]] | None,
) -> str:
    return _canonical_digest({
        "symbol_lib_id": symbol_lib_id,
        "footprint_lib_id": footprint_lib_id,
        "pins": sorted(
            (
                {
                    "number": str(row.get("number", "")).strip(),
                    "name": str(row.get("name", "")).strip(),
                    "type": str(
                        row.get("type") or row.get("electrical_type") or ""
                    ).strip(),
                }
                for row in pin_rows or ()
                if str(row.get("number", "")).strip()
            ),
            key=lambda item: (item["number"], item["name"]),
        ),
        "pads": sorted(
            (
                {
                    "number": str(row.get("number", "")).strip(),
                    "layers": sorted(str(item) for item in row.get("layers", [])),
                }
                for row in pad_rows or ()
                if str(row.get("number", "")).strip()
            ),
            key=lambda item: item["number"],
        ),
    })


_GENERIC_PASSIVE_SYMBOLS = frozenset({
    "r",
    "r_small",
    "r_us",
    "r_small_us",
    "c",
    "c_small",
    "c_polarized",
    "c_polarized_small",
    "l",
    "l_small",
    "l_ferrite",
    "ferrite_bead",
})
_ACTIVE_ROLE_TOKENS = (
    "adc",
    "amplifier",
    "charger",
    "controller",
    "dac",
    "driver",
    "flash",
    "ic",
    "isolator",
    "ldo",
    "memory",
    "mcu",
    "microcontroller",
    "opamp",
    "processor",
    "regulator",
    "sensor",
    "soc",
    "transceiver",
)


def _controlled_generic_passive(part: SelectedPart) -> bool:
    library, _, name = part.symbol.partition(":")
    # A functional owner is not a component type: mcu_decoupling and
    # ldo_output_capacitor are still capacitors. Match the physical primitive
    # and reference, then reject an explicitly active role head. Never infer
    # silicon identity from an arbitrary substring such as "ic" or "sensor".
    prefix = "R" if name.casefold().startswith("r") else (
        "C" if name.casefold().startswith("c") else "(?:L|FB)"
    )
    return (
        library.casefold() == "device"
        and name.casefold() in _GENERIC_PASSIVE_SYMBOLS
        and re.fullmatch(prefix + r"\d+", part.ref, re.IGNORECASE) is not None
        and not _active_role_head(part.role)
    )


def _active_role_head(role: str) -> bool:
    words = re.findall(r"[a-z0-9]+", role.casefold())
    if not words:
        return False
    if words[-1] in _ACTIVE_ROLE_TOKENS:
        return True
    support_heads = {
        "capacitor", "resistor", "inductor", "bead", "decoupling", "bypass", "bulk",
        "pullup", "pulldown", "pull", "termination", "divider", "feedback",
        "connector", "header", "socket", "button", "switch", "jumper",
    }
    return bool(set(words).intersection(_ACTIVE_ROLE_TOKENS)) and not bool(
        set(words).intersection(support_heads)
    )


def _connector_component(part: SelectedPart) -> bool:
    library = part.symbol.partition(":")[0].casefold()
    role = part.role.casefold()
    if _active_role_head(role):
        return False
    return library in {"connector", "connector_generic"} or any(
        token in role for token in ("connector", "header", "socket", "swd", "jtag")
    )


def _evidence_policy(part: SelectedPart) -> EvidencePolicy:
    if part.role.casefold() in {"mounting_hole", "fiducial"}:
        return "mechanical"
    if _controlled_generic_passive(part):
        return "controlled_generic_passive"
    if _connector_component(part):
        return "exact_connector"
    return "exact_component"


def _generic_ic_symbol_masquerade(part: SelectedPart) -> bool:
    """Reject generic primitives used for a concrete/active device identity."""

    library = part.symbol.partition(":")[0].casefold()
    if library not in {"connector", "connector_generic", "device", "jumper", "switch"}:
        return False
    if _controlled_generic_passive(part) or _connector_component(part):
        return False
    role = part.role.casefold()
    if _active_role_head(role):
        return True
    identities = (part.mpn, part.requested_identity, part.value)
    return any(
        len(compact) >= 5
        and any(character.isalpha() for character in compact)
        and any(character.isdigit() for character in compact)
        for identity in identities
        if (compact := _normalized_function(identity))
    )


def _normalized_function(value: str) -> str:
    return "".join(character for character in value.casefold() if character.isalnum())


def _pin_functions_match(
    pin_rows: Sequence[Mapping[str, Any]] | None,
    evidence: Sequence[TechnicalPackageEvidence],
    *,
    allow_local_binding: bool,
) -> bool:
    actual: dict[str, set[str]] = {}
    for row in pin_rows or ():
        number = str(row.get("number", "")).strip()
        name = _normalized_function(str(row.get("name", "")))
        if number and name:
            actual.setdefault(number, set()).add(name)
    if not actual:
        return False
    for item in evidence:
        if item.source_kind not in _RELEASE_TRUTH_SOURCE_KINDS and not (
            allow_local_binding
            and item.source_kind == "verified_local_kicad_binding"
        ):
            continue
        claimed: dict[str, set[str]] = {}
        for pin in item.pin_functions:
            claimed.setdefault(pin.number, set()).update(
                _normalized_function(function) for function in pin.functions
            )
        if set(claimed) != set(actual):
            continue
        if all(actual[number] & claimed[number] for number in actual):
            return True
    return False


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
        symbol_properties: Callable[[str], Mapping[str, str]] = symbols.symbol_properties,
        replacement_approval_secret: str | bytes | None = None,
    ) -> None:
        self._resolution_service = resolution_service or ComponentResolutionService()
        self._part_selector = part_selector or PartSelector()
        self._symbol_pins = symbol_pins
        self._footprint_pads = footprint_pads
        self._symbol_path = symbol_path
        self._footprint_path = footprint_path
        self._symbol_properties = symbol_properties
        self._replacement_approval_secret = replacement_approval_secret

    def _catalog_evidence(
        self,
        part: SelectedPart,
        observed_at: datetime,
    ) -> tuple[list[SupplierEvidence], list[TechnicalPackageEvidence]]:
        if not (part.mpn or part.lcsc) or not self._part_selector.available():
            return [], []
        found: dict[tuple[str, str], PartCandidate] = {}
        for query in dict.fromkeys(item for item in (part.lcsc, part.mpn) if item):
            for candidate in self._part_selector.search(query, limit=10):
                lcsc_matches = not part.lcsc or candidate.lcsc == part.lcsc
                mpn_matches = not part.mpn or candidate.mpn.casefold() == part.mpn.casefold()
                if lcsc_matches and mpn_matches:
                    found[(candidate.lcsc, candidate.mpn)] = candidate
        supplier = [
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
        packages: list[TechnicalPackageEvidence] = []
        for candidate in found.values():
            package = candidate.package.strip()
            if not package:
                continue
            source_payload = {
                "lcsc": candidate.lcsc,
                "mpn": candidate.mpn,
                "description": candidate.description,
                "package": package,
            }
            packages.append(build_technical_package_evidence(
                source_kind="distributor_catalog",
                source_id=f"local_jlcpcb_cache:{candidate.lcsc}",
                source_sha256=_canonical_digest(source_payload),
                mpn=candidate.mpn,
                package=package,
                footprint_lib_id=_catalog_footprint_binding(
                    package,
                    part.footprint,
                ),
            ))
        return supplier, packages

    def _local_binding_evidence(
        self,
        part: SelectedPart,
        resolution: ComponentResolution,
        pin_rows: Sequence[Mapping[str, Any]] | None,
        assets: Sequence[PreparedAssetEvidence],
        policy: EvidencePolicy,
    ) -> TechnicalPackageEvidence | None:
        """Turn a verified live KiCad binding into immutable local evidence."""

        by_kind = {item.kind: item for item in assets}
        if (
            policy in {"mechanical", "controlled_generic_passive"}
            or not resolution.release_ready
            or pin_rows is None
            or not {"symbol", "footprint"}.issubset(by_kind)
        ):
            return None
        try:
            properties = self._symbol_properties(part.symbol)
        except Exception:  # noqa: BLE001 - absent metadata remains an evidence gap
            properties = {}
        mpn = ""
        if policy == "exact_component":
            mpn = (
                part.mpn
                or str(properties.get("MPN", "")).strip()
                or resolution.requested_identity
            )
            if not mpn:
                return None
        pins = [
            {
                "number": str(row.get("number", "")).strip(),
                "functions": [str(row.get("name", "")).strip()],
            }
            for row in pin_rows
            if str(row.get("number", "")).strip()
            and str(row.get("name", "")).strip()
        ]
        if not pins:
            return None
        source_payload = {
            "symbol_lib_id": part.symbol,
            "footprint_lib_id": part.footprint,
            "symbol_sha256": by_kind["symbol"].sha256,
            "footprint_sha256": by_kind["footprint"].sha256,
            "requested_identity": resolution.requested_identity,
            "pins": pins,
        }
        return build_technical_package_evidence(
            source_kind="verified_local_kicad_binding",
            source_id=f"local-kicad:{part.symbol}|{part.footprint}",
            source_sha256=_canonical_digest(source_payload),
            mpn=mpn,
            package=part.footprint,
            pin_count=len(_numbers(pin_rows)),
            pin_functions=pins,
            footprint_lib_id=part.footprint,
        )

    def _prepare_record(
        self,
        part: SelectedPart,
        resolution: ComponentResolution,
        directive: ComponentPreparationInput,
        *,
        requirement_sha256: str,
        observed_at: datetime,
    ) -> PreparedComponentRecord:
        policy = _evidence_policy(part)
        strict_mpn_package = (
            directive.require_exact_mpn_package
            if directive.require_exact_mpn_package is not None
            else policy == "exact_component"
        )
        pin_rows = self._symbol_pins(part.symbol)
        pad_rows = self._footprint_pads(part.footprint) if part.footprint else None
        pins = _numbers(pin_rows)
        pads = _numbers(pad_rows)
        connector_extra_pads = (
            part.symbol.startswith(("Connector:", "Connector_Generic:"))
            and pins.issubset(pads)
        )
        library_pin_pad_ok = bool(pin_rows is not None and pad_rows is not None) and (
            pins == pads or connector_extra_pads
        )
        pin_pad_mapping_sha256 = _pin_pad_mapping_digest(
            part.symbol,
            part.footprint,
            pin_rows,
            pad_rows,
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
        technical_package_evidence = list(directive.technical_package_evidence)
        local_binding = self._local_binding_evidence(
            part,
            resolution,
            pin_rows,
            assets,
            policy,
        )
        if local_binding is not None:
            technical_package_evidence.append(local_binding)
        if not supplier_evidence or not technical_package_evidence:
            catalog_supplier, catalog_packages = self._catalog_evidence(
                part,
                observed_at,
            )
            if not supplier_evidence:
                supplier_evidence = catalog_supplier
            if not technical_package_evidence:
                technical_package_evidence = catalog_packages
        technical_package_evidence = list({
            item.evidence_id: item for item in technical_package_evidence
        }.values())
        if not part.mpn:
            explicit_mpns = {
                item.mpn
                for item in (*supplier_evidence, *technical_package_evidence)
                if item.mpn
            }
            if len(explicit_mpns) == 1:
                part.mpn = explicit_mpns.pop()
        matching_supply = [
            item
            for item in supplier_evidence
            if (not part.mpn or item.mpn.casefold() == part.mpn.casefold())
            and (not part.lcsc or item.supplier_part_number == part.lcsc)
        ]
        matching_package_evidence = [
            item
            for item in technical_package_evidence
            if (
                (part.mpn and item.mpn.casefold() == part.mpn.casefold())
                or (policy == "exact_connector" and not item.mpn)
            )
        ]
        release_package_evidence = [
            item
            for item in matching_package_evidence
            if (
                item.source_kind in _RELEASE_TRUTH_SOURCE_KINDS
                or (
                    policy == "exact_connector"
                    and item.source_kind == "verified_local_kicad_binding"
                )
            )
        ]
        mechanical_part = policy == "mechanical"
        if mechanical_part:
            mpn_status: VerificationStatus = "not_applicable"
        elif not part.mpn and not part.lcsc:
            mpn_status = "unverified"
        elif matching_supply or matching_package_evidence:
            mpn_status = "verified"
        elif supplier_evidence:
            mpn_status = "failed"
        else:
            mpn_status = "unverified"

        expected_pin_counts = {
            item.pin_count
            for item in release_package_evidence
            if item.pin_count is not None
        }
        package_matches = [
            item
            for item in release_package_evidence
            if item.footprint_lib_id == part.footprint
        ]
        pin_count_ok = (
            len(expected_pin_counts) <= 1
            and (
                not expected_pin_counts
                or (
                    len(pins) == next(iter(expected_pin_counts))
                    and len(pads) == next(iter(expected_pin_counts))
                )
            )
        )
        pin_function_ok = _pin_functions_match(
            pin_rows,
            package_matches,
            # Contact-number semantics are intrinsic for a generic connector;
            # active-device functions require manufacturer/approved evidence.
            allow_local_binding=policy == "exact_connector",
        )
        pin_pad_ok = library_pin_pad_ok and pin_count_ok and (
            pin_function_ok
            if policy in {"exact_component", "exact_connector"}
            else True
        )
        package_status: VerificationStatus
        if mechanical_part:
            package_status = "not_applicable"
        elif policy == "controlled_generic_passive" and library_pin_pad_ok:
            package_status = "verified"
        elif package_matches:
            package_status = "verified"
        elif release_package_evidence:
            package_status = "failed"
        else:
            package_status = "unverified"
        symbol_semantics_ok = (
            resolution.release_ready
            and pin_rows is not None
            and not _generic_ic_symbol_masquerade(part)
        )

        consistency = ComponentConsistencyStatus(
            identity="verified" if resolution.release_ready else "failed",
            mpn=mpn_status,
            package=package_status,
            symbol_semantics="verified" if symbol_semantics_ok else "failed",
            pin_pad="verified" if pin_pad_ok else "failed",
            asset_provenance=(
                "verified"
                if {"symbol", "footprint"}.issubset(asset_kinds)
                else "failed"
            ),
        )
        required_consistency: list[tuple[str, VerificationStatus]] = [
            ("identity", consistency.identity),
            ("package", consistency.package),
            ("symbol_semantics", consistency.symbol_semantics),
            ("pin_pad", consistency.pin_pad),
            ("asset_provenance", consistency.asset_provenance),
        ]
        if strict_mpn_package and not mechanical_part:
            required_consistency.insert(1, ("mpn", consistency.mpn))
        electrical_blockers = [
            f"{name}_consistency_{status}"
            for name, status in required_consistency
            if status not in {"verified", "not_applicable"}
        ]
        if mechanical_part:
            procurement_status: ReadinessStatus = "not_applicable"
            procurement_blockers: list[str] = []
        elif matching_supply:
            procurement_status = "ready"
            procurement_blockers = []
        elif mpn_status == "failed" or supplier_evidence:
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
            "schema_version": "ratsnestpro.prepared-component.v2",
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
            "technical_package_evidence": [
                item.model_dump(mode="json") for item in technical_package_evidence
            ],
            "technical_evidence_ids": list(dict.fromkeys(directive.technical_evidence_ids)),
            "pin_pad_mapping_sha256": pin_pad_mapping_sha256,
            "strict_mpn_package": strict_mpn_package,
            "evidence_policy": policy,
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
                approval_revision=directive.workflow_revision,
                replacement_approval_secret=self._replacement_approval_secret,
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
            "schema_version": "ratsnestpro.prepared-components.v2",
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
