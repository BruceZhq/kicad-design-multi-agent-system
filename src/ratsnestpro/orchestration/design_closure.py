"""Content-addressed component and schematic-connectivity closure.

The adaptive pipeline already resolves selected parts and writes project-local
KiCad library bindings.  This module joins those facts into two small machine
contracts that can be evaluated before schematic/PCB work proceeds:

* a per-component identity/symbol/footprint/pin-pad manifest whose source files
  can be re-hashed to prove that the evidence is still current; and
* an exact set comparison between the pin-number DesignIR and a netlist exported
  by KiCad itself.

No LLM statement is accepted as evidence by either contract.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import xml.etree.ElementTree as ET
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, model_validator

from ratsnestpro.domain.contracts import CircuitIR, ContractModel
from ratsnestpro.eda import footprints, symbols
from ratsnestpro.eda.vendor.kicad_cli import find_kicad_cli
from ratsnestpro.orchestration.component_resolution import LibraryClosureResult
from ratsnestpro.orchestration.pipeline_contracts import PinMapPlan, SelectionPlan

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_KICAD_SYNTHETIC_UNCONNECTED_NET = re.compile(
    r"unconnected-\(.+-Pad[^)]+\)",
    re.IGNORECASE,
)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_digest(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class LibrarySourceEvidence(ContractModel):
    """One exact library file observed while closing a component."""

    kind: Literal["symbol", "footprint"]
    lib_id: str = Field(min_length=3, max_length=240)
    source_path: str = Field(min_length=1, max_length=2_000)
    sha256: str = Field(pattern=_SHA256_PATTERN)
    size_bytes: int = Field(ge=0)
    modified_ns: int = Field(ge=0)
    observed_at: datetime


class PinPadBinding(ContractModel):
    """One KiCad electrical pin number bound to the same physical pad."""

    pin_number: str = Field(min_length=1, max_length=32)
    pin_name: str = Field(default="", max_length=120)
    pad_number: str = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def _kicad_number_matches(self) -> PinPadBinding:
        if self.pin_number != self.pad_number:
            raise ValueError("KiCad symbol pin number must equal footprint pad number")
        return self


class ComponentClosureEntry(ContractModel):
    """Release evidence for exactly one selected physical component."""

    ref: str = Field(min_length=1, max_length=32)
    requested_identity: str = Field(min_length=1, max_length=200)
    identity_mode: str = Field(min_length=1, max_length=32)
    identity_provenance: str = Field(min_length=1, max_length=240)
    resolution_status: str = Field(min_length=1, max_length=64)
    symbol_lib_id: str = Field(min_length=3, max_length=200)
    footprint_lib_id: str = Field(min_length=3, max_length=240)
    symbol_pin_numbers: list[str] = Field(default_factory=list, max_length=512)
    footprint_pad_numbers: list[str] = Field(default_factory=list, max_length=512)
    pin_pad_bindings: list[PinPadBinding] = Field(default_factory=list, max_length=512)
    evidence: list[LibrarySourceEvidence] = Field(default_factory=list, max_length=8)
    release_ready: bool
    blockers: list[str] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def _internally_consistent(self) -> ComponentClosureEntry:
        pin_numbers = set(self.symbol_pin_numbers)
        pad_numbers = set(self.footprint_pad_numbers)
        mapped = {binding.pin_number for binding in self.pin_pad_bindings}
        if mapped != pin_numbers or not mapped.issubset(pad_numbers):
            if self.release_ready:
                raise ValueError("release-ready closure requires a complete pin-pad mapping")
        if self.release_ready and (self.blockers or len(self.evidence) != 2):
            raise ValueError("release-ready closure requires two current library sources")
        if self.release_ready is not (not self.blockers):
            raise ValueError("release_ready must agree with blockers")
        return self


class ComponentClosureManifest(ContractModel):
    """Versioned BOM closure receipt produced before schematic generation."""

    schema_version: Literal["ratsnestpro.component-closure.v1"] = (
        "ratsnestpro.component-closure.v1"
    )
    generated_at: datetime
    components: list[ComponentClosureEntry] = Field(min_length=1, max_length=1_000)
    release_ready: bool
    blockers: list[str] = Field(default_factory=list, max_length=4_000)
    manifest_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def _internally_consistent(self) -> ComponentClosureManifest:
        expected_blockers = [
            f"{component.ref}:{blocker}"
            for component in self.components
            for blocker in component.blockers
        ]
        if self.blockers != expected_blockers:
            raise ValueError("manifest blockers do not equal component blockers")
        if self.release_ready is not (not self.blockers):
            raise ValueError("manifest release_ready must agree with blockers")
        expected_digest = _canonical_digest(
            self.model_dump(mode="json", exclude={"manifest_sha256"})
        )
        if self.manifest_sha256 != expected_digest:
            raise ValueError("component closure manifest digest is invalid")
        return self


class ClosureFreshnessReport(ContractModel):
    """Result of re-hashing every library source recorded by a manifest."""

    current: bool
    stale_evidence: list[str] = Field(default_factory=list, max_length=4_000)
    metadata_changed: list[str] = Field(default_factory=list, max_length=4_000)


def _source_evidence(
    kind: Literal["symbol", "footprint"],
    lib_id: str,
    path: Path | None,
    observed_at: datetime,
) -> LibrarySourceEvidence | None:
    if path is None or not path.is_file():
        return None
    stat = path.stat()
    return LibrarySourceEvidence(
        kind=kind,
        lib_id=lib_id,
        source_path=str(path.resolve()),
        sha256=_sha256_file(path),
        size_bytes=stat.st_size,
        modified_ns=stat.st_mtime_ns,
        observed_at=observed_at,
    )


def _numbers(rows: Sequence[Mapping[str, Any]] | None) -> list[str]:
    return sorted({
        str(row.get("number", "")).strip()
        for row in (rows or ())
        if str(row.get("number", "")).strip()
    })


def build_component_closure_manifest(
    selection: SelectionPlan,
    closure: LibraryClosureResult,
    *,
    observed_at: datetime | None = None,
    symbol_pins: Callable[[str], Sequence[Mapping[str, Any]] | None] = symbols.symbol_pins,
    footprint_pads: Callable[[str], Sequence[Mapping[str, Any]] | None] = (
        footprints.footprint_pads
    ),
    symbol_path: Callable[[str], Path | None] = symbols.resolve_symbol,
    footprint_path: Callable[[str], Path | None] = footprints.footprint_path,
) -> ComponentClosureManifest:
    """Build a fail-closed manifest from deterministic resolver output.

    The resolver's release verdict remains necessary, but is no longer
    sufficient: every pin/pad mapping and both on-disk library sources must be
    present and content-addressed here.
    """

    timestamp = observed_at or datetime.now(UTC)
    resolutions = {item.ref: item for item in closure.resolutions}
    entries: list[ComponentClosureEntry] = []
    for part in selection.parts:
        resolution = resolutions.get(part.ref)
        blockers: list[str] = []
        if resolution is None:
            blockers.append("resolution_missing")

        pin_rows = symbol_pins(part.symbol)
        pad_rows = footprint_pads(part.footprint) if part.footprint else None
        pin_numbers = _numbers(pin_rows)
        pad_numbers = _numbers(pad_rows)
        if pin_rows is None:
            blockers.append("symbol_not_installed")
        if not part.footprint:
            blockers.append("footprint_missing")
        elif pad_rows is None:
            blockers.append("footprint_not_installed")
        if part.footprint_binding_status != "verified_installed":
            blockers.append("footprint_binding_unverified")
        connector_with_extra_pads = (
            part.symbol.startswith(("Connector:", "Connector_Generic:"))
            and set(pin_numbers).issubset(pad_numbers)
        )
        if (
            pin_rows is not None
            and pad_rows is not None
            and pin_numbers != pad_numbers
            and not connector_with_extra_pads
        ):
            blockers.append("pin_pad_mapping_incomplete")

        pin_names = {
            str(row.get("number", "")).strip(): str(row.get("name", "")).strip()
            for row in (pin_rows or ())
            if str(row.get("number", "")).strip()
        }
        bindings = [
            PinPadBinding(
                pin_number=number,
                pin_name=pin_names.get(number, ""),
                pad_number=number,
            )
            for number in pin_numbers
            if number in set(pad_numbers)
        ]

        evidence = [
            item
            for item in (
                _source_evidence(
                    "symbol", part.symbol, symbol_path(part.symbol), timestamp
                ),
                _source_evidence(
                    "footprint",
                    part.footprint,
                    footprint_path(part.footprint) if part.footprint else None,
                    timestamp,
                ),
            )
            if item is not None
        ]
        if not any(item.kind == "symbol" for item in evidence):
            blockers.append("symbol_evidence_missing")
        if not any(item.kind == "footprint" for item in evidence):
            blockers.append("footprint_evidence_missing")
        if resolution is not None and not resolution.release_ready:
            blockers.append(f"resolver:{resolution.reason_code}")

        requested_identity = (
            (resolution.requested_identity if resolution is not None else "")
            or part.requested_identity
            or part.value
        )
        identity_mode = (
            resolution.identity_mode if resolution is not None else part.identity_mode
        ) or "capability_only"
        identity_provenance = (
            resolution.identity_provenance
            if resolution is not None
            else part.identity_provenance
        ) or "selection_proposal"
        resolution_status = (
            str(resolution.status) if resolution is not None else "missing"
        )
        entries.append(ComponentClosureEntry(
            ref=part.ref,
            requested_identity=requested_identity,
            identity_mode=identity_mode,
            identity_provenance=identity_provenance,
            resolution_status=resolution_status,
            symbol_lib_id=part.symbol,
            footprint_lib_id=part.footprint or "missing:missing",
            symbol_pin_numbers=pin_numbers,
            footprint_pad_numbers=pad_numbers,
            pin_pad_bindings=bindings,
            evidence=evidence,
            release_ready=not blockers,
            blockers=list(dict.fromkeys(blockers)),
        ))

    blockers = [
        f"{component.ref}:{blocker}"
        for component in entries
        for blocker in component.blockers
    ]
    payload = {
        "schema_version": "ratsnestpro.component-closure.v1",
        "generated_at": timestamp.isoformat().replace("+00:00", "Z"),
        "components": [item.model_dump(mode="json") for item in entries],
        "release_ready": not blockers,
        "blockers": blockers,
    }
    return ComponentClosureManifest.model_validate({
        **payload,
        "manifest_sha256": _canonical_digest(payload),
    })


def validate_component_closure_freshness(
    manifest: ComponentClosureManifest,
) -> ClosureFreshnessReport:
    """Re-hash bound library files; timestamps are diagnostic, hashes decide."""

    stale: list[str] = []
    metadata_changed: list[str] = []
    for component in manifest.components:
        for evidence in component.evidence:
            label = f"{component.ref}:{evidence.kind}:{evidence.lib_id}"
            path = Path(evidence.source_path)
            if not path.is_file():
                stale.append(f"{label}:source_missing")
                continue
            stat = path.stat()
            if _sha256_file(path) != evidence.sha256:
                stale.append(f"{label}:sha256_changed")
            elif stat.st_size != evidence.size_bytes:
                stale.append(f"{label}:size_changed")
            elif stat.st_mtime_ns != evidence.modified_ns:
                metadata_changed.append(f"{label}:mtime_changed_content_equal")
    return ClosureFreshnessReport(
        current=not stale,
        stale_evidence=stale,
        metadata_changed=metadata_changed,
    )


class PinNetFact(ContractModel):
    ref: str = Field(min_length=1, max_length=32)
    pin: str = Field(min_length=1, max_length=32)
    net: str = Field(min_length=1, max_length=100)

    def key(self) -> tuple[str, str, str]:
        return self.ref, self.pin, self.net


class AmbiguousPinNet(ContractModel):
    ref: str = Field(min_length=1, max_length=32)
    pin: str = Field(min_length=1, max_length=32)
    nets: list[str] = Field(min_length=2, max_length=32)


class PinNetSet(ContractModel):
    source: Literal["design_ir", "kicad_xml_netlist"]
    source_sha256: str = Field(pattern=_SHA256_PATTERN)
    facts: list[PinNetFact] = Field(default_factory=list, max_length=100_000)
    ambiguous: list[AmbiguousPinNet] = Field(default_factory=list, max_length=10_000)


class PinNetDiff(ContractModel):
    matches: bool
    missing: list[PinNetFact] = Field(default_factory=list, max_length=100_000)
    extra: list[PinNetFact] = Field(default_factory=list, max_length=100_000)
    ambiguous: list[AmbiguousPinNet] = Field(default_factory=list, max_length=10_000)

    @model_validator(mode="after")
    def _verdict_matches_evidence(self) -> PinNetDiff:
        if self.matches is not (not self.missing and not self.extra and not self.ambiguous):
            raise ValueError("pin-net diff verdict contradicts its evidence")
        return self


def _pin_net_set(
    source: Literal["design_ir", "kicad_xml_netlist"],
    source_sha256: str,
    rows: Sequence[tuple[str, str, str]],
) -> PinNetSet:
    by_pin: dict[tuple[str, str], set[str]] = {}
    for ref, pin, net in rows:
        # KiCad exports net names as absolute sheet paths.  The leading slash
        # denotes the root sheet and is not part of the DesignIR net name.
        net = net.removeprefix("/")
        if not ref or ref.startswith("#") or not pin or not net:
            continue
        by_pin.setdefault((ref, pin), set()).add(net)
    ambiguous = [
        AmbiguousPinNet(ref=ref, pin=pin, nets=sorted(nets))
        for (ref, pin), nets in sorted(by_pin.items())
        if len(nets) > 1
    ]
    facts = [
        PinNetFact(ref=ref, pin=pin, net=net)
        for (ref, pin), nets in sorted(by_pin.items())
        for net in sorted(nets)
    ]
    return PinNetSet(
        source=source,
        source_sha256=source_sha256,
        facts=facts,
        ambiguous=ambiguous,
    )


def design_ir_pin_net_set(ir: PinMapPlan | CircuitIR) -> PinNetSet:
    """Canonical expected ``(ref, pin-number, net)`` set from versioned IR."""

    if isinstance(ir, PinMapPlan):
        rows = [
            (pin.ref, pin.number, net.name)
            for net in ir.nets
            for pin in net.pins
        ]
    else:
        rows = [
            (pin.component_ref, pin.pin, net.name)
            for net in ir.nets
            for pin in net.pins
        ]
    payload = ir.model_dump(mode="json")
    snapshot = _pin_net_set("design_ir", _canonical_digest(payload), rows)
    if snapshot.ambiguous:
        conflicts = [
            f"{item.ref}:{item.pin}={item.nets}" for item in snapshot.ambiguous
        ]
        raise ValueError(f"DesignIR assigns pins to multiple nets: {conflicts}")
    return snapshot


def read_kicad_xml_pin_net_set(netlist_path: Path) -> PinNetSet:
    """Read the XML netlist emitted by ``kicad-cli sch export netlist``."""

    root = ET.parse(netlist_path).getroot()  # noqa: S314 - local generated artifact
    rows: list[tuple[str, str, str]] = []
    nets = root.find("nets")
    if nets is not None:
        for net in nets.findall("net"):
            name = str(net.attrib.get("name", "")).strip()
            nodes = net.findall("node")
            # A real KiCad no-connect marker is exported as a one-node
            # synthetic net.  It proves disposal of the pin, not connectivity
            # that DesignIR should contain.  Keep every other unconnected net
            # fail-closed so an ordinary dangling pin still appears as extra.
            explicit_no_connect = (
                len(nodes) == 1
                and _KICAD_SYNTHETIC_UNCONNECTED_NET.fullmatch(name) is not None
                and "no_connect"
                in {
                    token.strip()
                    for token in re.split(
                        r"[+,|]",
                        str(nodes[0].attrib.get("pintype", "")).casefold(),
                    )
                }
            )
            if explicit_no_connect:
                continue
            for node in nodes:
                rows.append((
                    str(node.attrib.get("ref", "")).strip(),
                    str(node.attrib.get("pin", "")).strip(),
                    name,
                ))
    return _pin_net_set(
        "kicad_xml_netlist",
        _sha256_file(netlist_path),
        rows,
    )


def export_kicad_pin_net_set(
    schematic_path: Path,
    netlist_path: Path,
    *,
    cli_path: str | None = None,
    timeout_seconds: float = 60.0,
) -> PinNetSet:
    """Ask KiCad to export its authoritative XML netlist, then read it."""

    netlist_path.parent.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(
        [
            find_kicad_cli(cli_path),
            "sch",
            "export",
            "netlist",
            "--format",
            "kicadxml",
            "--output",
            str(netlist_path),
            str(schematic_path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_seconds,
        check=False,
    )
    if completed.returncode != 0 or not netlist_path.is_file():
        detail = (completed.stderr or completed.stdout).strip()[:1_000]
        raise RuntimeError(
            f"KiCad netlist export failed with exit {completed.returncode}: {detail}"
        )
    return read_kicad_xml_pin_net_set(netlist_path)


def diff_pin_net_sets(expected: PinNetSet, actual: PinNetSet) -> PinNetDiff:
    """Return exact missing/extra facts; ambiguous KiCad pins also fail closed."""

    expected_by_key = {fact.key(): fact for fact in expected.facts}
    actual_by_key = {fact.key(): fact for fact in actual.facts}
    missing = [expected_by_key[key] for key in sorted(expected_by_key.keys() - actual_by_key)]
    extra = [actual_by_key[key] for key in sorted(actual_by_key.keys() - expected_by_key)]
    return PinNetDiff(
        matches=not missing and not extra and not actual.ambiguous,
        missing=missing,
        extra=extra,
        ambiguous=actual.ambiguous,
    )
