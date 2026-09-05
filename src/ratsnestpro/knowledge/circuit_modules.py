"""Content-addressed circuit modules extracted from verified release runs.

The library never invents a reference circuit.  Hardware Engineer may propose
module candidates only from a release-ready pipeline state; Reviewer promotion
is the separate operation that makes those candidates searchable across runs.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import Field, model_validator

from ratsnestpro.domain.contracts import ContractModel
from ratsnestpro.orchestration.pipeline_contracts import (
    LogicalPin,
    NetlistIntent,
    SelectedPart,
    SelectionPlan,
    TopologyPlan,
)
from ratsnestpro.orchestration.release_invariants import ReleaseIdentity

_DIGEST = r"^[0-9a-f]{64}$"


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


class ModuleComponent(ContractModel):
    ref: str = Field(min_length=1, max_length=32)
    role: str = Field(default="", max_length=120)
    value: str = Field(min_length=1, max_length=200)
    mpn: str = Field(default="", max_length=160)
    symbol_lib_id: str = Field(min_length=3, max_length=200)
    footprint_lib_id: str = Field(min_length=3, max_length=240)
    prepared_record_id: str = Field(pattern=_DIGEST)
    asset_lock_digest: str = Field(pattern=_DIGEST)


class ModuleNet(ContractModel):
    name: str = Field(min_length=1, max_length=100)
    kind: str = Field(default="signal", max_length=32)
    pins: list[LogicalPin] = Field(min_length=1, max_length=500)
    boundary: bool = False


class CircuitModuleCandidate(ContractModel):
    """A functional subgraph copied from one content-bound release."""

    schema_version: Literal["ratsnestpro.circuit-module.v1"] = (
        "ratsnestpro.circuit-module.v1"
    )
    name: str = Field(min_length=1, max_length=120)
    kind: str = Field(min_length=1, max_length=60)
    source_release_identity_digest: str = Field(pattern=_DIGEST)
    source_pcb_sha256: str = Field(pattern=_DIGEST)
    components: list[ModuleComponent] = Field(min_length=1, max_length=100)
    nets: list[ModuleNet] = Field(default_factory=list, max_length=500)
    module_digest: str = Field(pattern=_DIGEST)

    @model_validator(mode="after")
    def _content_addressed(self) -> CircuitModuleCandidate:
        references = [component.ref for component in self.components]
        if len(references) != len(set(references)):
            raise ValueError("module component references must be unique")
        allowed = set(references)
        if any(pin.ref not in allowed for net in self.nets for pin in net.pins):
            raise ValueError("module nets may contain only module-owned pins")
        expected = _digest(self.model_dump(mode="json", exclude={"module_digest"}))
        if self.module_digest != expected:
            raise ValueError("circuit module digest is invalid")
        return self


def _module_component(part: SelectedPart) -> ModuleComponent | None:
    if not (
        part.release_ready
        and part.symbol
        and part.footprint
        and part.prepared_record_id
        and part.asset_lock_digest
    ):
        return None
    return ModuleComponent(
        ref=part.ref,
        role=part.role,
        value=part.value,
        mpn=part.mpn,
        symbol_lib_id=part.symbol,
        footprint_lib_id=part.footprint,
        prepared_record_id=part.prepared_record_id,
        asset_lock_digest=part.asset_lock_digest,
    )


def build_circuit_module_candidates(
    *,
    topology: TopologyPlan,
    selection: SelectionPlan,
    netlist: NetlistIntent,
    release_identity: ReleaseIdentity,
) -> list[dict[str, Any]]:
    """Extract reusable blocks without guessing ownership or connectivity."""

    selected = {part.ref: part for part in selection.parts}
    candidates: list[CircuitModuleCandidate] = []
    for block in topology.blocks[:64]:
        refs = list(dict.fromkeys(block.implementation_refs))
        if not refs or any(ref not in selected for ref in refs):
            continue
        components = [_module_component(selected[ref]) for ref in refs]
        if any(component is None for component in components):
            continue
        owned = set(refs)
        module_nets: list[ModuleNet] = []
        for net in netlist.nets:
            pins = [pin for pin in net.pins if pin.ref in owned]
            if not pins:
                continue
            module_nets.append(
                ModuleNet(
                    name=net.name,
                    kind=net.kind,
                    pins=pins,
                    boundary=any(pin.ref not in owned for pin in net.pins),
                )
            )
        payload = {
            "schema_version": "ratsnestpro.circuit-module.v1",
            "name": block.name,
            "kind": block.kind,
            "source_release_identity_digest": _digest(
                release_identity.model_dump(mode="json")
            ),
            "source_pcb_sha256": release_identity.pcb_sha256,
            "components": [
                component.model_dump(mode="json")
                for component in components
                if component is not None
            ],
            "nets": [net.model_dump(mode="json") for net in module_nets],
        }
        candidates.append(
            CircuitModuleCandidate.model_validate(
                {**payload, "module_digest": _digest(payload)}
            )
        )
    return [candidate.model_dump(mode="json") for candidate in candidates]


def validate_circuit_module_candidates(
    values: list[dict[str, Any]],
    *,
    release_identity: dict[str, Any] | None = None,
    topology: TopologyPlan | dict[str, Any] | None = None,
    selection: SelectionPlan | dict[str, Any] | None = None,
    netlist: NetlistIntent | dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Validate and deduplicate candidates at the Reviewer trust boundary."""

    identity = (
        ReleaseIdentity.model_validate(release_identity)
        if release_identity is not None
        else None
    )
    expected_identity_digest = (
        _digest(identity.model_dump(mode="json")) if identity is not None else ""
    )
    source_values = (topology, selection, netlist)
    source_validation_requested = any(value is not None for value in source_values)
    expected_modules: dict[str, dict[str, Any]] = {}
    if source_validation_requested:
        if identity is None or any(value is None for value in source_values):
            raise ValueError(
                "reviewed topology, selection, netlist, and release identity are required"
            )
        reviewed_topology = TopologyPlan.model_validate(topology)
        reviewed_selection = SelectionPlan.model_validate(selection)
        reviewed_netlist = NetlistIntent.model_validate(netlist)
        expected_modules = {
            item["module_digest"]: item
            for item in build_circuit_module_candidates(
                topology=reviewed_topology,
                selection=reviewed_selection,
                netlist=reviewed_netlist,
                release_identity=identity,
            )
        }
    validated: list[CircuitModuleCandidate] = []
    seen: set[str] = set()
    for value in values[:64]:
        candidate = CircuitModuleCandidate.model_validate(value)
        if (
            expected_identity_digest
            and candidate.source_release_identity_digest != expected_identity_digest
        ):
            raise ValueError("circuit module release identity is stale")
        if identity is not None and candidate.source_pcb_sha256 != identity.pcb_sha256:
            raise ValueError("circuit module PCB identity is stale")
        if source_validation_requested:
            expected = expected_modules.get(candidate.module_digest)
            if expected is None or candidate.model_dump(mode="json") != expected:
                raise ValueError(
                    "circuit module does not match the reviewed pipeline source"
                )
        if candidate.module_digest in seen:
            continue
        seen.add(candidate.module_digest)
        validated.append(candidate)
    return [candidate.model_dump(mode="json") for candidate in validated]


def circuit_module_search_text(values: list[dict[str, Any]]) -> str:
    """Bound the exact reusable subgraphs embedded in a knowledge result."""

    modules = validate_circuit_module_candidates(values)
    bounded: list[dict[str, Any]] = []
    encoded = "[]"
    for module in modules[:8]:
        candidate = json.dumps(
            [*bounded, module],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        if len(candidate) > 16_000:
            continue
        bounded.append(module)
        encoded = candidate
    return encoded


__all__ = [
    "CircuitModuleCandidate",
    "build_circuit_module_candidates",
    "circuit_module_search_text",
    "validate_circuit_module_candidates",
]
