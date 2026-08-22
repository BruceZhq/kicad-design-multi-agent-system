"""Bounded, block-wise synthesis primitives for large electrical netlists.

The 17-step pipeline still exposes one :class:`NetlistIntent`.  This module
only decomposes how that artifact is proposed: it plans stable batches, accepts
strictly additive deltas, and commits each delta atomically against real KiCad
symbol pins.  It intentionally has no knowledge of a protocol, MCU family, or
board template.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Iterable, Mapping
from typing import Literal

from pydantic import Field, ValidationError, model_validator

from ratsnestpro.domain.contracts import ContractModel
from ratsnestpro.eda import symbols
from ratsnestpro.orchestration.pipeline_contracts import (
    LogicalPin,
    NetIntent,
    NetlistIntent,
    SelectedPart,
    SelectionPlan,
    TopologyBlock,
    TopologyPlan,
)

_DEFAULT_COMPLETION_LIMIT = 8192
_DEFAULT_DIRECT_RATIO = 0.55
_DEFAULT_BATCH_TARGET_PINS = 96
_DEFAULT_DIRECT_PIN_LIMIT = 180
_DEFAULT_UNKNOWN_SYMBOL_PIN_ESTIMATE = 8
_INTEGRATION_BLOCK = "integration"

_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)
_NATURAL_REF_RE = re.compile(r"(\d+)")
_GENERIC_TOKENS = {
    "board",
    "circuit",
    "component",
    "device",
    "external",
    "function",
    "functional",
    "main",
    "module",
    "part",
    "physical",
    "support",
    "system",
}
_CENTRAL_HUB_TOKENS = {
    "central",
    "compute",
    "computing",
    "controller",
    "coordination",
    "core",
    "cpu",
    "fpga",
    "host",
    "mcu",
    "processor",
    "soc",
}
_CENTRAL_SUPPORT_TOKENS = {
    "bead",
    "button",
    "capacitor",
    "connector",
    "crystal",
    "decoupl",
    "diode",
    "filter",
    "fuse",
    "header",
    "inductor",
    "jumper",
    "led",
    "oscillator",
    "protection",
    "pull",
    "resistor",
    "strap",
    "switch",
    "tvs",
}


class ConnectionMergeError(ValueError):
    """A delta could not be committed without changing accepted connectivity."""


class ConnectionOutputEstimate(ContractModel):
    """Conservative size estimate used to choose direct or batched synthesis."""

    physical_pin_count: int = Field(ge=0)
    estimated_chars: int = Field(ge=0)
    estimated_tokens: int = Field(ge=0)
    completion_limit: int = Field(gt=0)
    direct_token_budget: int = Field(gt=0)
    direct_pin_limit: int = Field(gt=0)
    unknown_symbol_refs: list[str] = Field(default_factory=list)
    should_batch: bool = False


class ConnectionSynthesisReport(ContractModel):
    """Compact durable audit of one connection-synthesis result."""

    schema_version: int = Field(default=1, ge=1)
    mode: Literal["direct", "batched"]
    estimate: ConnectionOutputEstimate
    planned_batches: int = Field(default=0, ge=0)
    completed_batches: int = Field(default=0, ge=0)
    pending_batches: int = Field(default=0, ge=0)
    skipped_batches: int = Field(default=0, ge=0)
    failed_batches: int = Field(default=0, ge=0)
    llm_calls: int = Field(default=0, ge=0)
    round_llm_calls: int = Field(default=0, ge=0)
    resumable: bool = False
    stop_reason: str = Field(default="", max_length=4_000)
    total_pins: int = Field(default=0, ge=0)
    connected_pins: int = Field(default=0, ge=0)
    no_connect_pins: int = Field(default=0, ge=0)
    undisposed_pins: int = Field(default=0, ge=0)
    coverage_ratio: float = Field(default=0.0, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _counts_are_consistent(self) -> ConnectionSynthesisReport:
        statuses = (
            self.completed_batches
            + self.pending_batches
            + self.skipped_batches
            + self.failed_batches
        )
        if statuses != self.planned_batches:
            raise ValueError("connection report batch counts do not match its plan")
        if self.round_llm_calls > self.llm_calls:
            raise ValueError("round LLM calls cannot exceed cumulative LLM calls")
        disposed = self.total_pins - self.undisposed_pins
        if disposed < 0:
            raise ValueError("undisposed pin count cannot exceed total pin count")
        if self.connected_pins + self.no_connect_pins < disposed:
            raise ValueError("connection report disposition counts are inconsistent")
        expected_ratio = 1.0 if self.total_pins == 0 else disposed / self.total_pins
        if not math.isclose(self.coverage_ratio, expected_ratio, abs_tol=1e-9):
            raise ValueError("connection report coverage ratio is inconsistent")
        return self


class ConnectionBatchSpec(ContractModel):
    """One stable LLM work unit.

    ``owned_refs`` may be electrically dispositioned by this batch. Shared refs
    are boundary endpoints visible to every batch, but may never be marked
    no-connect by an ordinary block delta.
    """

    batch_id: str = Field(min_length=1, max_length=80)
    sequence: int = Field(ge=0)
    topology_blocks: list[str] = Field(min_length=1, max_length=200)
    owned_refs: list[str] = Field(default_factory=list, max_length=1_000)
    shared_refs: list[str] = Field(default_factory=list, max_length=1_000)
    owned_pin_count: int = Field(ge=0)

    @model_validator(mode="after")
    def _unique_refs(self) -> ConnectionBatchSpec:
        if len(self.topology_blocks) != len(set(self.topology_blocks)):
            raise ValueError("batch topology block names must be unique")
        if len(self.owned_refs) != len(set(self.owned_refs)):
            raise ValueError("batch owned refs must be unique")
        if len(self.shared_refs) != len(set(self.shared_refs)):
            raise ValueError("batch shared refs must be unique")
        overlap = set(self.owned_refs) & set(self.shared_refs)
        if overlap:
            raise ValueError(f"owned and shared refs overlap: {sorted(overlap)}")
        return self


class ConnectionBatchPlan(ContractModel):
    """Deterministic plan persisted with an in-flight connection checkpoint."""

    topology_fingerprint: str = Field(min_length=1, max_length=64)
    selection_fingerprint: str = Field(min_length=1, max_length=64)
    plan_fingerprint: str = Field(min_length=1, max_length=64)
    target_pin_count: int = Field(gt=0)
    effective_target_pin_count: int = Field(gt=0)
    max_batches: int = Field(gt=0)
    shared_refs: list[str] = Field(default_factory=list, max_length=1_000)
    oversized_atomic_refs: list[str] = Field(default_factory=list, max_length=1_000)
    batching_supported: bool = True
    batches: list[ConnectionBatchSpec] = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def _unique_batches_and_owners(self) -> ConnectionBatchPlan:
        ids = [batch.batch_id for batch in self.batches]
        if len(ids) != len(set(ids)):
            raise ValueError("connection batch IDs must be unique")
        owned = [
            ref
            for batch in self.batches
            for ref in batch.owned_refs
        ]
        if len(owned) != len(set(owned)):
            raise ValueError("a physical part may be owned by only one batch")
        sequences = [batch.sequence for batch in self.batches]
        if sequences != list(range(len(self.batches))):
            raise ValueError("connection batch sequences must be contiguous and ordered")
        if len(self.oversized_atomic_refs) != len(set(self.oversized_atomic_refs)):
            raise ValueError("oversized atomic refs must be unique")
        expected_fingerprint = _canonical_hash(_plan_fingerprint_payload(
            topology_hash=self.topology_fingerprint,
            selection_hash=self.selection_fingerprint,
            target_pin_count=self.target_pin_count,
            effective_target_pin_count=self.effective_target_pin_count,
            max_batches=self.max_batches,
            shared_refs=self.shared_refs,
            oversized_atomic_refs=self.oversized_atomic_refs,
            batching_supported=self.batching_supported,
            batches=self.batches,
        ))
        if self.plan_fingerprint != expected_fingerprint:
            raise ValueError("connection plan fingerprint is inconsistent")
        return self

    def batch(self, batch_id: str) -> ConnectionBatchSpec:
        for batch in self.batches:
            if batch.batch_id == batch_id:
                return batch
        raise KeyError(batch_id)


class NetExtension(ContractModel):
    """Pins to append to an existing net without changing its metadata."""

    name: str = Field(min_length=1, max_length=100)
    pins: list[LogicalPin] = Field(default_factory=list, max_length=500)

    @model_validator(mode="after")
    def _unique_logical_pins(self) -> NetExtension:
        keys = [pin.key().casefold() for pin in self.pins]
        if len(keys) != len(set(keys)):
            raise ValueError(f"net extension {self.name!r} contains duplicate pins")
        return self


class ConnectionDelta(ContractModel):
    """A strictly additive transaction emitted for one connection batch.

    The strict ``ContractModel`` boundary rejects destructive fields such as
    ``remove_nets`` and ``remove_pins`` instead of silently ignoring them.
    """

    batch_id: str = Field(min_length=1, max_length=80)
    base_revision: int = Field(ge=0)
    create_nets: list[NetIntent] = Field(default_factory=list, max_length=2_000)
    extend_nets: list[NetExtension] = Field(default_factory=list, max_length=2_000)
    no_connect_pins: list[LogicalPin] = Field(default_factory=list, max_length=2_000)
    rationale: str = Field(default="", max_length=2_000)

    @model_validator(mode="after")
    def _unique_targets_and_pins(self) -> ConnectionDelta:
        created = [net.name.casefold() for net in self.create_nets]
        extended = [net.name.casefold() for net in self.extend_nets]
        if len(created) != len(set(created)):
            raise ValueError("delta create-net names must be unique")
        if len(extended) != len(set(extended)):
            raise ValueError("delta extend-net names must be unique")
        overlap = set(created) & set(extended)
        if overlap:
            raise ValueError(
                f"a delta cannot create and extend the same net: {sorted(overlap)}"
            )
        dispositions: list[tuple[str, str]] = []
        for net in self.create_nets:
            dispositions.extend(
                (pin.key().casefold(), net.name.casefold())
                for pin in net.pins
            )
        for extension in self.extend_nets:
            dispositions.extend(
                (pin.key().casefold(), extension.name.casefold())
                for pin in extension.pins
            )
        dispositions.extend(
            (pin.key().casefold(), "<no-connect>")
            for pin in self.no_connect_pins
        )
        owners: dict[str, str] = {}
        conflicts: list[str] = []
        for pin_key, target in dispositions:
            previous = owners.get(pin_key)
            if previous is not None and previous != target:
                conflicts.append(f"{pin_key} in {previous} and {target}")
            owners[pin_key] = target
        if conflicts:
            raise ValueError(f"delta assigns logical pins more than once: {conflicts}")
        return self


class ConnectionBatchCheckpoint(ContractModel):
    batch_id: str = Field(min_length=1, max_length=80)
    status: Literal["pending", "completed", "failed", "skipped"] = "pending"
    attempts: int = Field(default=0, ge=0)
    delta_fingerprint: str = Field(default="", max_length=64)
    error: str = Field(default="", max_length=2_000)


class ConnectionSynthesisCheckpoint(ContractModel):
    """Crash-safe state for one in-flight block-wise synthesis."""

    schema_version: int = Field(default=1, ge=1)
    topology_fingerprint: str = Field(min_length=1, max_length=64)
    selection_fingerprint: str = Field(min_length=1, max_length=64)
    plan: ConnectionBatchPlan
    aggregate: NetlistIntent
    aggregate_revision: int = Field(default=0, ge=0)
    llm_invocations: int = Field(default=0, ge=0)
    round_llm_invocations: int = Field(default=0, ge=0)
    rounds_started: int = Field(default=0, ge=0)
    batches: list[ConnectionBatchCheckpoint] = Field(
        default_factory=list,
        max_length=128,
    )
    shared_pin_reservations: dict[str, str] = Field(
        default_factory=dict,
        max_length=5_000,
    )

    @model_validator(mode="after")
    def _batch_status_matches_plan(self) -> ConnectionSynthesisCheckpoint:
        planned = [batch.batch_id for batch in self.plan.batches]
        statuses = [batch.batch_id for batch in self.batches]
        if statuses != planned:
            raise ValueError(
                "checkpoint batch statuses must match the planned batch order"
            )
        if self.topology_fingerprint != self.plan.topology_fingerprint:
            raise ValueError("checkpoint topology fingerprint differs from its plan")
        if self.selection_fingerprint != self.plan.selection_fingerprint:
            raise ValueError("checkpoint selection fingerprint differs from its plan")
        completed = [
            batch
            for batch in self.batches
            if batch.status == "completed"
        ]
        if self.aggregate_revision != len(completed):
            raise ValueError(
                "checkpoint aggregate revision must equal completed batch count"
            )
        if self.round_llm_invocations > self.llm_invocations:
            raise ValueError(
                "checkpoint round LLM calls cannot exceed cumulative LLM calls"
            )
        incomplete_seen = False
        for batch in self.batches:
            if batch.status != "completed":
                incomplete_seen = True
                continue
            if incomplete_seen:
                raise ValueError("completed connection batches must form a prefix")
            if not batch.delta_fingerprint:
                raise ValueError(
                    f"completed batch {batch.batch_id!r} lacks a delta fingerprint"
                )
            if batch.attempts < 1:
                raise ValueError(
                    f"completed batch {batch.batch_id!r} lacks an attempt"
                )
        net_names = {net.name.casefold() for net in self.aggregate.nets}
        allowed_shared = {ref.casefold() for ref in self.plan.shared_refs}
        for physical, net_name in self.shared_pin_reservations.items():
            ref, separator, number = physical.partition(":")
            if not separator or not ref or not number:
                raise ValueError(
                    f"invalid shared pin reservation key {physical!r}"
                )
            if ref.casefold() not in allowed_shared:
                raise ValueError(
                    f"shared pin reservation {physical!r} is not a planned shared ref"
                )
            if net_name.casefold() not in net_names:
                raise ValueError(
                    f"shared pin reservation {physical!r} targets missing net "
                    f"{net_name!r}"
                )
        return self

    def batch_status(self, batch_id: str) -> ConnectionBatchCheckpoint:
        for status in self.batches:
            if status.batch_id == batch_id:
                return status
        raise KeyError(batch_id)


def _canonical_hash(value: object, length: int = 20) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:length]


def topology_fingerprint(topology: TopologyPlan) -> str:
    """Fingerprint electrical topology, preserving its intentional block order."""

    return _canonical_hash({
        "blocks": [
            {
                "name": block.name,
                "kind": block.kind,
                "description": block.description,
            }
            for block in topology.blocks
        ],
        "rails": topology.rails,
        "ground_net": topology.ground_net,
    })


def _natural_ref_key(ref: str) -> tuple[object, ...]:
    return tuple(
        int(part) if part.isdigit() else part.casefold()
        for part in _NATURAL_REF_RE.split(ref)
        if part
    )


def selection_fingerprint(selection: SelectionPlan) -> str:
    """Fingerprint only fields that can affect block assignment/connectivity."""

    return _canonical_hash([
        {
            "ref": part.ref,
            "symbol": part.symbol,
            "value": part.value,
            "footprint": part.footprint,
            "role": part.role,
            "symbol_pins": _normalized_symbol_pin_signature(part),
        }
        for part in sorted(selection.parts, key=lambda item: _natural_ref_key(item.ref))
    ])


def connection_delta_fingerprint(delta: ConnectionDelta) -> str:
    def pin_payload(pin: LogicalPin) -> dict[str, str]:
        return {
            "ref": pin.ref.strip().casefold(),
            "pin": pin.pin.strip().casefold(),
        }

    def sorted_pins(pins: Iterable[LogicalPin]) -> list[dict[str, str]]:
        return sorted(
            (pin_payload(pin) for pin in pins),
            key=lambda item: (item["ref"], item["pin"]),
        )

    return _canonical_hash({
        "batch_id": delta.batch_id,
        "create_nets": sorted(
            (
                {
                    "name": net.name.strip().casefold(),
                    "kind": net.kind.strip().casefold(),
                    "pins": sorted_pins(net.pins),
                }
                for net in delta.create_nets
            ),
            key=lambda item: (item["name"], item["kind"]),
        ),
        "extend_nets": sorted(
            (
                {
                    "name": extension.name.strip().casefold(),
                    "pins": sorted_pins(extension.pins),
                }
                for extension in delta.extend_nets
            ),
            key=lambda item: item["name"],
        ),
        "no_connect_pins": sorted_pins(delta.no_connect_pins),
    })


def _symbol_pins(part: SelectedPart) -> list[dict[str, object]] | None:
    found = symbols.symbol_pins(part.symbol)
    return list(found) if found is not None else None


def _normalized_symbol_pin_signature(
    part: SelectedPart,
) -> list[dict[str, str]] | None:
    pins = _symbol_pins(part)
    if pins is None:
        return None
    normalized = [
        {
            "number": str(pin.get("number", "")).strip().casefold(),
            "name": str(pin.get("name", "")).strip().casefold(),
            "type": str(pin.get("type", "")).strip().casefold(),
        }
        for pin in pins
        if str(pin.get("number", "")).strip()
    ]
    return sorted(
        normalized,
        key=lambda pin: (pin["number"], pin["name"], pin["type"]),
    )


def _pin_count(
    part: SelectedPart,
    *,
    unknown_symbol_pin_estimate: int = _DEFAULT_UNKNOWN_SYMBOL_PIN_ESTIMATE,
) -> tuple[int, bool]:
    pins = _symbol_pins(part)
    if pins is None:
        return unknown_symbol_pin_estimate, True
    return len([
        pin
        for pin in pins
        if str(pin.get("number", "")).strip()
    ]), False


def estimate_connection_output(
    selection: SelectionPlan,
    *,
    completion_limit: int = _DEFAULT_COMPLETION_LIMIT,
    direct_ratio: float = _DEFAULT_DIRECT_RATIO,
    direct_pin_limit: int = _DEFAULT_DIRECT_PIN_LIMIT,
    unknown_symbol_pin_estimate: int = _DEFAULT_UNKNOWN_SYMBOL_PIN_ESTIMATE,
) -> ConnectionOutputEstimate:
    """Estimate a complete ``NetlistIntent`` without assuming a circuit family."""

    if completion_limit <= 0:
        raise ValueError("completion_limit must be positive")
    if not 0 < direct_ratio <= 1:
        raise ValueError("direct_ratio must be in (0, 1]")
    if direct_pin_limit <= 0:
        raise ValueError("direct_pin_limit must be positive")
    if unknown_symbol_pin_estimate <= 0:
        raise ValueError("unknown_symbol_pin_estimate must be positive")

    pin_count = 0
    pin_json_chars = 0
    unknown_refs: list[str] = []
    for part in selection.parts:
        pins = _symbol_pins(part)
        if pins is None:
            unknown_refs.append(part.ref)
            pin_count += unknown_symbol_pin_estimate
            for index in range(unknown_symbol_pin_estimate):
                pin_json_chars += len(
                    json.dumps(
                        {"ref": part.ref, "pin": f"PIN{index + 1}"},
                        separators=(",", ":"),
                    )
                ) + 1
            continue
        for pin in pins:
            number = str(pin.get("number", "")).strip()
            if not number:
                continue
            name = str(pin.get("name", "")).strip()
            logical = name if len(name) >= len(number) else number
            pin_count += 1
            pin_json_chars += len(
                json.dumps(
                    {"ref": part.ref, "pin": logical},
                    separators=(",", ":"),
                )
            ) + 1

    # In the worst useful case, about every two endpoints form one named net.
    # Three ASCII characters per token is deliberately conservative for compact
    # JSON containing references, pin names, and punctuation.
    estimated_chars = 1024 + pin_json_chars + 48 * math.ceil(pin_count / 2)
    estimated_tokens = math.ceil(estimated_chars / 3)
    direct_token_budget = max(
        1,
        min(4096, math.floor(completion_limit * direct_ratio)),
    )
    return ConnectionOutputEstimate(
        physical_pin_count=pin_count,
        estimated_chars=estimated_chars,
        estimated_tokens=estimated_tokens,
        completion_limit=completion_limit,
        direct_token_budget=direct_token_budget,
        direct_pin_limit=direct_pin_limit,
        unknown_symbol_refs=sorted(unknown_refs, key=_natural_ref_key),
        should_batch=(
            estimated_tokens > direct_token_budget
            or pin_count > direct_pin_limit
        ),
    )


def _stem(token: str) -> str:
    for suffix in ("ation", "ments", "ment", "ing", "ers", "er", "ed", "es", "s"):
        if len(token) > len(suffix) + 3 and token.endswith(suffix):
            return token[:-len(suffix)]
    return token


def _tokens(text: str) -> set[str]:
    tokens: set[str] = set()
    for token in _TOKEN_RE.findall(text.casefold()):
        if not token or token in _GENERIC_TOKENS:
            continue
        # Retain the exact functional word for role taxonomies and add a small
        # English stem only as an extra matching aid (e.g. regulate/regulated).
        tokens.add(token)
        tokens.add(_stem(token))
    return tokens


def _block_score(part: SelectedPart, block: TopologyBlock) -> int:
    role = _tokens(part.role)
    identity = _tokens(f"{block.name} {block.kind}")
    description = _tokens(block.description)
    secondary = _tokens(f"{part.value} {part.symbol} {part.footprint}")
    return (
        5 * len(role & identity)
        + 2 * len(role & description)
        + len(secondary & (identity | description))
    )


def assign_parts_to_topology_blocks(
    topology: TopologyPlan,
    selection: SelectionPlan,
    *,
    explicit_assignments: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Assign each part to one block only when the semantic winner is unique."""

    exact_blocks = {block.name.casefold(): block.name for block in topology.blocks}
    explicit = {
        ref.casefold(): value
        for ref, value in (explicit_assignments or {}).items()
    }
    assignments: dict[str, str] = {}
    for part in sorted(selection.parts, key=lambda item: _natural_ref_key(item.ref)):
        requested = explicit.get(part.ref.casefold(), "")
        if requested:
            block_name = exact_blocks.get(requested.casefold())
            if block_name is None:
                raise ValueError(
                    f"explicit topology block {requested!r} for {part.ref} does not exist"
                )
            assignments[part.ref] = block_name
            continue
        scored = [
            (_block_score(part, block), index, block.name)
            for index, block in enumerate(topology.blocks)
        ]
        best_score = max((score for score, _, _ in scored), default=0)
        winners = [
            (index, name)
            for score, index, name in scored
            if score == best_score and score > 0
        ]
        assignments[part.ref] = (
            winners[0][1]
            if len(winners) == 1
            else _INTEGRATION_BLOCK
        )
    return assignments


def infer_shared_refs(
    topology: TopologyPlan,
    selection: SelectionPlan,
    *,
    explicit_assignments: Mapping[str, str] | None = None,
) -> list[str]:
    """Find central boundary endpoints from generic functional role semantics."""

    # Validate explicit block assignments, but never use block prose to infer
    # that every support part in a central block is itself a central endpoint.
    assign_parts_to_topology_blocks(
        topology,
        selection,
        explicit_assignments=explicit_assignments,
    )
    shared: list[str] = []
    for part in selection.parts:
        role_tokens = _tokens(part.role)
        device_tokens = _tokens(f"{part.value} {part.symbol}")
        role_identifies_hub = bool(
            role_tokens & _CENTRAL_HUB_TOKENS
            and not role_tokens & _CENTRAL_SUPPORT_TOKENS
        )
        device_identifies_hub = bool(device_tokens & _CENTRAL_HUB_TOKENS)
        pin_count, unresolved = _pin_count(part)
        if (
            (role_identifies_hub or device_identifies_hub)
            and not unresolved
            and pin_count >= 4
        ):
            shared.append(part.ref)
    return sorted(set(shared), key=_natural_ref_key)


def _plan_fingerprint_payload(
    *,
    topology_hash: str,
    selection_hash: str,
    target_pin_count: int,
    effective_target_pin_count: int,
    max_batches: int,
    shared_refs: list[str],
    oversized_atomic_refs: list[str],
    batching_supported: bool,
    batches: list[ConnectionBatchSpec],
) -> dict[str, object]:
    return {
        "topology": topology_hash,
        "selection": selection_hash,
        "target_pin_count": target_pin_count,
        "effective_target_pin_count": effective_target_pin_count,
        "max_batches": max_batches,
        "shared_refs": shared_refs,
        "oversized_atomic_refs": oversized_atomic_refs,
        "batching_supported": batching_supported,
        "batches": [
            {
                "sequence": batch.sequence,
                "topology_blocks": batch.topology_blocks,
                "owned_refs": batch.owned_refs,
                "shared_refs": batch.shared_refs,
                "owned_pin_count": batch.owned_pin_count,
            }
            for batch in batches
        ],
    }


def plan_connection_batches(
    topology: TopologyPlan,
    selection: SelectionPlan,
    *,
    target_pin_count: int = _DEFAULT_BATCH_TARGET_PINS,
    max_batches: int = 8,
    shared_refs: Iterable[str] | None = None,
    explicit_assignments: Mapping[str, str] | None = None,
    unknown_symbol_pin_estimate: int = _DEFAULT_UNKNOWN_SYMBOL_PIN_ESTIMATE,
) -> ConnectionBatchPlan:
    """Build a deterministic, pin-budgeted work plan from topology and roles."""

    if target_pin_count <= 0:
        raise ValueError("target_pin_count must be positive")
    if max_batches <= 0:
        raise ValueError("max_batches must be positive")
    if unknown_symbol_pin_estimate <= 0:
        raise ValueError("unknown_symbol_pin_estimate must be positive")

    topology_hash = topology_fingerprint(topology)
    selection_hash = selection_fingerprint(selection)
    assignments = assign_parts_to_topology_blocks(
        topology,
        selection,
        explicit_assignments=explicit_assignments,
    )
    inferred_shared = (
        infer_shared_refs(
            topology,
            selection,
            explicit_assignments=explicit_assignments,
        )
        if shared_refs is None
        else sorted(set(shared_refs), key=_natural_ref_key)
    )
    refs = {part.ref for part in selection.parts}
    unknown_shared = sorted(set(inferred_shared) - refs, key=_natural_ref_key)
    if unknown_shared:
        raise ValueError(f"shared refs are absent from selection: {unknown_shared}")
    # Shared controller/hub endpoints are visible to every functional batch,
    # but they still need one eventual owner that can disposition any remaining
    # pins. Reserve the final batch for that integration pass. With a one-call
    # cap, ownership simply stays in the single ordinary batch.
    if inferred_shared and max_batches == 1:
        inferred_shared = []
    shared_set = set(inferred_shared)
    part_pin_counts = {
        part.ref: _pin_count(
            part,
            unknown_symbol_pin_estimate=unknown_symbol_pin_estimate,
        )[0]
        for part in selection.parts
    }
    oversized_atomic_refs = sorted(
        (
            ref
            for ref, count in part_pin_counts.items()
            if count > target_pin_count
        ),
        key=_natural_ref_key,
    )
    block_order = {
        block.name: index
        for index, block in enumerate(topology.blocks)
    }
    block_order[_INTEGRATION_BLOCK] = len(block_order)

    parts_with_counts = []
    for part in selection.parts:
        if part.ref in shared_set:
            continue
        count = part_pin_counts[part.ref]
        # Zero-pin mechanical parts require no connection LLM work.
        if count == 0:
            continue
        parts_with_counts.append((part, count, assignments[part.ref]))
    parts_with_counts.sort(key=lambda item: (
        block_order.get(item[2], len(block_order)),
        item[0].role.casefold(),
        _natural_ref_key(item[0].ref),
    ))

    oversized_owned_refs = {
        ref for ref in oversized_atomic_refs if ref not in shared_set
    }
    regular_owned_pins = sum(
        count
        for part, count, _block_name in parts_with_counts
        if part.ref not in oversized_owned_refs
    )
    shared_pin_count = sum(
        part_pin_counts[part.ref]
        for part in selection.parts
        if part.ref in shared_set
    )
    ordinary_batch_cap = max_batches - (1 if inferred_shared else 0)
    regular_batch_cap = max(
        1,
        ordinary_batch_cap - len(oversized_owned_refs),
    )
    effective_target = max(
        target_pin_count,
        (
            math.ceil(regular_owned_pins / regular_batch_cap)
            if regular_owned_pins
            else 1
        ),
    )
    raw_batches: list[tuple[list[str], list[str], int]] = []
    current_refs: list[str] = []
    current_blocks: list[str] = []
    current_pins = 0
    for part, pin_count, block_name in parts_with_counts:
        if part.ref in oversized_atomic_refs:
            if current_refs:
                raw_batches.append((current_blocks, current_refs, current_pins))
                current_refs = []
                current_blocks = []
                current_pins = 0
            raw_batches.append(([block_name], [part.ref], pin_count))
            continue
        if current_refs and current_pins + pin_count > effective_target:
            raw_batches.append((current_blocks, current_refs, current_pins))
            current_refs = []
            current_blocks = []
            current_pins = 0
        current_refs.append(part.ref)
        if block_name not in current_blocks:
            current_blocks.append(block_name)
        current_pins += pin_count
    if current_refs:
        raw_batches.append((current_blocks, current_refs, current_pins))
    if not raw_batches and not inferred_shared:
        raw_batches.append(([_INTEGRATION_BLOCK], [], 0))

    # A single physical part can exceed the target, but the computed effective
    # capacity still guarantees the call cap for ordinary splittable selections.
    while len(raw_batches) > ordinary_batch_cap:
        left_blocks, left_refs, left_pins = raw_batches[-2]
        right_blocks, right_refs, right_pins = raw_batches[-1]
        raw_batches[-2:] = [(
            list(dict.fromkeys([*left_blocks, *right_blocks])),
            [*left_refs, *right_refs],
            left_pins + right_pins,
        )]

    batches: list[ConnectionBatchSpec] = []
    for sequence, (block_names, owned_refs, owned_pins) in enumerate(raw_batches):
        identity = _canonical_hash({
            "sequence": sequence,
            "blocks": block_names,
            "owned": owned_refs,
            "shared": inferred_shared,
        }, length=10)
        batches.append(ConnectionBatchSpec(
            batch_id=f"connection-{sequence + 1:02d}-{identity}",
            sequence=sequence,
            topology_blocks=block_names,
            owned_refs=owned_refs,
            shared_refs=inferred_shared,
            owned_pin_count=owned_pins,
        ))
    if inferred_shared:
        leaf_refs = sorted(
            refs - set(inferred_shared),
            key=_natural_ref_key,
        )
        sequence = len(batches)
        identity = _canonical_hash({
            "sequence": sequence,
            "blocks": [_INTEGRATION_BLOCK],
            "owned": inferred_shared,
            "shared": leaf_refs,
        }, length=10)
        batches.append(ConnectionBatchSpec(
            batch_id=f"connection-{sequence + 1:02d}-{identity}",
            sequence=sequence,
            topology_blocks=[_INTEGRATION_BLOCK],
            owned_refs=inferred_shared,
            shared_refs=leaf_refs,
            owned_pin_count=shared_pin_count,
        ))

    payload = _plan_fingerprint_payload(
        topology_hash=topology_hash,
        selection_hash=selection_hash,
        target_pin_count=target_pin_count,
        effective_target_pin_count=effective_target,
        max_batches=max_batches,
        shared_refs=inferred_shared,
        oversized_atomic_refs=oversized_atomic_refs,
        batching_supported=True,
        batches=batches,
    )
    return ConnectionBatchPlan(
        topology_fingerprint=topology_hash,
        selection_fingerprint=selection_hash,
        plan_fingerprint=_canonical_hash(payload),
        target_pin_count=target_pin_count,
        effective_target_pin_count=effective_target,
        max_batches=max_batches,
        shared_refs=inferred_shared,
        oversized_atomic_refs=oversized_atomic_refs,
        batching_supported=True,
        batches=batches,
    )


def new_connection_checkpoint(
    topology: TopologyPlan,
    selection: SelectionPlan,
    plan: ConnectionBatchPlan | None = None,
) -> ConnectionSynthesisCheckpoint:
    """Create an empty aggregate with canonical rail/GND extension targets."""

    plan = plan or plan_connection_batches(topology, selection)
    if (
        plan.topology_fingerprint != topology_fingerprint(topology)
        or plan.selection_fingerprint != selection_fingerprint(selection)
    ):
        raise ValueError("connection plan does not match topology/selection inputs")

    ground_key = topology.ground_net.casefold()
    rail_names = list(dict.fromkeys(
        rail
        for rail in topology.rails
        if rail.casefold() != ground_key
    ))
    nets = [
        NetIntent(
            name=rail,
            kind="power",
            pins=[],
            purpose="declared topology supply rail",
        )
        for rail in rail_names
    ]
    nets.append(NetIntent(
        name=topology.ground_net,
        kind="ground",
        pins=[],
        purpose="declared topology ground",
    ))
    return ConnectionSynthesisCheckpoint(
        topology_fingerprint=plan.topology_fingerprint,
        selection_fingerprint=plan.selection_fingerprint,
        plan=plan,
        aggregate=NetlistIntent(
            nets=nets,
            supply_nets=rail_names,
            ground_net=topology.ground_net,
            rationale="block-wise connection synthesis",
        ),
        batches=[
            ConnectionBatchCheckpoint(batch_id=batch.batch_id)
            for batch in plan.batches
        ],
    )


def checkpoint_matches_inputs(
    checkpoint: ConnectionSynthesisCheckpoint,
    topology: TopologyPlan,
    selection: SelectionPlan,
) -> bool:
    try:
        validated = ConnectionSynthesisCheckpoint.model_validate(
            checkpoint.model_dump(mode="json")
        )
    except ValidationError:
        return False
    return (
        validated.topology_fingerprint == topology_fingerprint(topology)
        and validated.selection_fingerprint == selection_fingerprint(selection)
    )


def _resolve_physical_pin(part: SelectedPart, logical: str) -> str:
    pins = _symbol_pins(part)
    if not pins:
        raise ConnectionMergeError(
            f"{part.ref} symbol {part.symbol!r} has no resolvable real pins"
        )
    term = logical.strip().casefold()
    if not term:
        raise ConnectionMergeError(f"{part.ref} has an empty logical pin")

    def unique_number(candidates: list[dict[str, object]]) -> str | None:
        numbers = {
            str(pin.get("number", "")).strip()
            for pin in candidates
            if str(pin.get("number", "")).strip()
        }
        if len(numbers) == 1:
            return next(iter(numbers))
        if len(numbers) > 1:
            raise ConnectionMergeError(
                f"{part.ref}:{logical} ambiguously resolves to pins {sorted(numbers)}"
            )
        return None

    exact_number = unique_number([
        pin
        for pin in pins
        if str(pin.get("number", "")).strip().casefold() == term
    ])
    if exact_number is not None:
        return exact_number
    exact_name = unique_number([
        pin
        for pin in pins
        if str(pin.get("name", "")).strip().casefold() == term
    ])
    if exact_name is not None:
        return exact_name
    token_name = unique_number([
        pin
        for pin in pins
        if term in {
            token
            for token in re.split(
                r"[/~{}()\s]+",
                str(pin.get("name", "")).strip().casefold(),
            )
            if token
        }
    ])
    if token_name is not None:
        return token_name
    raise ConnectionMergeError(
        f"{part.ref}:{logical} does not resolve on symbol {part.symbol!r}"
    )


def _part_index(selection: SelectionPlan) -> dict[str, SelectedPart]:
    index: dict[str, SelectedPart] = {}
    for part in selection.parts:
        key = part.ref.casefold()
        if key in index:
            raise ConnectionMergeError(
                f"selection contains duplicate case-insensitive ref {part.ref}"
            )
        index[key] = part
    return index


def _physical_key(
    part_index: Mapping[str, SelectedPart],
    pin: LogicalPin,
) -> str:
    part = part_index.get(pin.ref.casefold())
    if part is None:
        raise ConnectionMergeError(
            f"delta references part {pin.ref!r} absent from selection"
        )
    number = _resolve_physical_pin(part, pin.pin)
    return f"{part.ref.upper()}:{number}"


def _is_intrinsic_no_connect(pin: Mapping[str, object]) -> bool:
    name = str(pin.get("name", "")).strip().upper()
    pin_type = str(pin.get("type", "")).strip().casefold()
    return (
        pin_type == "no_connect"
        or name in {"NC", "N/C", "DNC", "DO_NOT_CONNECT"}
    )


def connection_synthesis_report(
    selection: SelectionPlan,
    netlist: NetlistIntent,
    *,
    mode: Literal["direct", "batched"],
    estimate: ConnectionOutputEstimate | None = None,
    checkpoint: ConnectionSynthesisCheckpoint | None = None,
    llm_calls: int = 0,
    round_llm_calls: int = 0,
    resumable: bool = False,
    stop_reason: str = "",
) -> ConnectionSynthesisReport:
    """Build a conservative, same-grain coverage report from real symbol pins."""

    estimate = estimate or estimate_connection_output(selection)
    part_index = _part_index(selection)
    required: set[str] = set()
    unresolved_estimated_pins = 0
    for part in selection.parts:
        pins = _symbol_pins(part)
        if pins is None:
            unresolved_estimated_pins += _pin_count(part)[0]
            continue
        required.update(
            f"{part.ref.upper()}:{str(pin.get('number', '')).strip()}"
            for pin in pins
            if str(pin.get("number", "")).strip()
            and not _is_intrinsic_no_connect(pin)
        )

    connected: set[str] = set()
    for net in netlist.nets:
        for pin in net.pins:
            try:
                physical = _physical_key(part_index, pin)
            except ConnectionMergeError:
                continue
            if physical in required:
                connected.add(physical)
    explicit_no_connect: set[str] = set()
    for pin in netlist.no_connect_pins:
        try:
            physical = _physical_key(part_index, pin)
        except ConnectionMergeError:
            continue
        if physical in required:
            explicit_no_connect.add(physical)

    total_pins = len(required) + unresolved_estimated_pins
    disposed = len(connected | explicit_no_connect)
    undisposed = max(0, total_pins - disposed)
    statuses = [item.status for item in checkpoint.batches] if checkpoint else []
    cumulative_calls = checkpoint.llm_invocations if checkpoint else llm_calls
    current_round_calls = (
        checkpoint.round_llm_invocations
        if checkpoint
        else round_llm_calls
    )
    return ConnectionSynthesisReport(
        mode=mode,
        estimate=estimate,
        planned_batches=len(statuses),
        completed_batches=statuses.count("completed"),
        pending_batches=statuses.count("pending"),
        skipped_batches=statuses.count("skipped"),
        failed_batches=statuses.count("failed"),
        llm_calls=cumulative_calls,
        round_llm_calls=current_round_calls,
        resumable=resumable,
        stop_reason=stop_reason,
        total_pins=total_pins,
        connected_pins=len(connected),
        no_connect_pins=len(explicit_no_connect),
        undisposed_pins=undisposed,
        coverage_ratio=(1.0 if total_pins == 0 else disposed / total_pins),
    )


def _aggregate_ownership(
    aggregate: NetlistIntent,
    part_index: Mapping[str, SelectedPart],
) -> tuple[dict[str, str], dict[str, LogicalPin]]:
    net_owner: dict[str, str] = {}
    no_connect: dict[str, LogicalPin] = {}
    for net in aggregate.nets:
        for pin in net.pins:
            key = _physical_key(part_index, pin)
            previous = net_owner.get(key)
            if previous is not None and previous.casefold() != net.name.casefold():
                raise ConnectionMergeError(
                    f"aggregate pin {key} appears in {previous!r} and {net.name!r}"
                )
            net_owner[key] = net.name
    for pin in aggregate.no_connect_pins:
        key = _physical_key(part_index, pin)
        if key in net_owner:
            raise ConnectionMergeError(
                f"aggregate pin {key} is both connected and no-connect"
            )
        no_connect.setdefault(key, pin)
    return net_owner, no_connect


def _allowed_ref(
    pin: LogicalPin,
    *,
    owned: set[str],
    shared: set[str],
) -> bool:
    return pin.ref.casefold() in owned | shared


def _validated_checkpoint(
    checkpoint: ConnectionSynthesisCheckpoint,
) -> ConnectionSynthesisCheckpoint:
    try:
        return ConnectionSynthesisCheckpoint.model_validate(
            checkpoint.model_dump(mode="json")
        )
    except ValidationError as exc:
        raise ConnectionMergeError(
            f"connection checkpoint is internally inconsistent: {exc}"
        ) from exc


def _required_owned_physical_pins(
    batch: ConnectionBatchSpec,
    part_index: Mapping[str, SelectedPart],
) -> set[str]:
    missing_refs: list[str] = []
    unresolvable_refs: list[str] = []
    required: set[str] = set()
    for ref in batch.owned_refs:
        part = part_index.get(ref.casefold())
        if part is None:
            missing_refs.append(ref)
            continue
        pins = _symbol_pins(part)
        numbers = sorted({
            str(pin.get("number", "")).strip()
            for pin in (pins or [])
            if str(pin.get("number", "")).strip()
        })
        if not numbers:
            unresolvable_refs.append(f"{part.ref} ({part.symbol})")
            continue
        required.update(f"{part.ref.upper()}:{number}" for number in numbers)
    if missing_refs:
        raise ConnectionMergeError(
            f"batch {batch.batch_id} owns refs absent from selection: "
            f"{sorted(missing_refs, key=_natural_ref_key)}"
        )
    if unresolvable_refs:
        raise ConnectionMergeError(
            f"batch {batch.batch_id} has unresolvable owned refs: "
            f"{unresolvable_refs}"
        )
    return required


def merge_connection_delta(
    checkpoint: ConnectionSynthesisCheckpoint,
    delta: ConnectionDelta,
    selection: SelectionPlan,
) -> ConnectionSynthesisCheckpoint:
    """Validate and atomically commit one additive connection transaction."""

    checkpoint = _validated_checkpoint(checkpoint)
    if checkpoint.selection_fingerprint != selection_fingerprint(selection):
        raise ConnectionMergeError("checkpoint selection fingerprint is stale")
    try:
        batch = checkpoint.plan.batch(delta.batch_id)
    except KeyError as exc:
        raise ConnectionMergeError(
            f"delta batch {delta.batch_id!r} is absent from the plan"
        ) from exc
    status = checkpoint.batch_status(delta.batch_id)
    incomplete_predecessors = [
        item.batch_id
        for item in checkpoint.batches[:batch.sequence]
        if item.status != "completed"
    ]
    if incomplete_predecessors:
        raise ConnectionMergeError(
            f"batch {delta.batch_id!r} has incomplete predecessor batches: "
            f"{incomplete_predecessors}"
        )
    delta_hash = connection_delta_fingerprint(delta)
    if status.status == "completed":
        if status.delta_fingerprint == delta_hash:
            return checkpoint.model_copy(deep=True)
        raise ConnectionMergeError(
            f"batch {delta.batch_id!r} already completed with a different delta"
        )
    if delta.base_revision != checkpoint.aggregate_revision:
        raise ConnectionMergeError(
            f"delta base revision {delta.base_revision} does not match aggregate "
            f"revision {checkpoint.aggregate_revision}"
        )

    part_index = _part_index(selection)
    required_owned_pins = _required_owned_physical_pins(batch, part_index)
    owned = {ref.casefold() for ref in batch.owned_refs}
    shared = {ref.casefold() for ref in batch.shared_refs}
    for net in delta.create_nets:
        for pin in net.pins:
            if not _allowed_ref(pin, owned=owned, shared=shared):
                raise ConnectionMergeError(
                    f"{pin.ref} is neither owned nor shared by batch {batch.batch_id}"
                )
    for extension in delta.extend_nets:
        for pin in extension.pins:
            if not _allowed_ref(pin, owned=owned, shared=shared):
                raise ConnectionMergeError(
                    f"{pin.ref} is neither owned nor shared by batch {batch.batch_id}"
                )
    for pin in delta.no_connect_pins:
        if pin.ref.casefold() in shared:
            raise ConnectionMergeError(
                f"batch {batch.batch_id} cannot mark shared pin {pin.key()} no-connect"
            )
        if pin.ref.casefold() not in owned:
            raise ConnectionMergeError(
                f"{pin.ref} is not owned by batch {batch.batch_id}"
            )

    candidate = checkpoint.model_copy(deep=True)
    net_owner, no_connect = _aggregate_ownership(candidate.aggregate, part_index)
    nets_by_name = {
        net.name.casefold(): net
        for net in candidate.aggregate.nets
    }
    for net in delta.create_nets:
        key = net.name.casefold()
        if key in nets_by_name:
            raise ConnectionMergeError(f"net {net.name!r} already exists")
        target = NetIntent(
            name=net.name,
            kind=net.kind,
            pins=[],
            purpose=net.purpose,
        )
        candidate.aggregate.nets.append(target)
        nets_by_name[key] = target

    extension_targets: dict[str, NetIntent] = {}
    for extension in delta.extend_nets:
        target = nets_by_name.get(extension.name.casefold())
        if target is None:
            raise ConnectionMergeError(
                f"cannot extend missing net {extension.name!r}"
            )
        extension_targets[extension.name.casefold()] = target

    dispositions = [
        (nets_by_name[net.name.casefold()], pin)
        for net in delta.create_nets
        for pin in net.pins
    ]
    dispositions.extend(
        (extension_targets[extension.name.casefold()], pin)
        for extension in delta.extend_nets
        for pin in extension.pins
    )
    for target, pin in dispositions:
        physical = _physical_key(part_index, pin)
        existing_net = net_owner.get(physical)
        if existing_net is not None:
            if existing_net.casefold() == target.name.casefold():
                continue
            raise ConnectionMergeError(
                f"physical pin {physical} is already reserved by net "
                f"{existing_net!r}, not {target.name!r}"
            )
        if physical in no_connect:
            raise ConnectionMergeError(
                f"physical pin {physical} is already marked no-connect"
            )
        target.pins.append(pin.model_copy(deep=True))
        net_owner[physical] = target.name

    for pin in delta.no_connect_pins:
        physical = _physical_key(part_index, pin)
        existing_net = net_owner.get(physical)
        if existing_net is not None:
            raise ConnectionMergeError(
                f"physical pin {physical} is already reserved by net "
                f"{existing_net!r}"
            )
        if physical not in no_connect:
            copied = pin.model_copy(deep=True)
            candidate.aggregate.no_connect_pins.append(copied)
            no_connect[physical] = copied

    missing_owned_pins = sorted(
        required_owned_pins - set(net_owner) - set(no_connect),
        key=_natural_ref_key,
    )
    if missing_owned_pins:
        raise ConnectionMergeError(
            f"batch {batch.batch_id} does not disposition owned pins: "
            f"{missing_owned_pins}"
        )

    existing_supply = {
        name.casefold()
        for name in candidate.aggregate.supply_nets
    }
    for net in delta.create_nets:
        if (
            net.kind == "power"
            and net.name.casefold() != candidate.aggregate.ground_net.casefold()
            and net.name.casefold() not in existing_supply
        ):
            candidate.aggregate.supply_nets.append(net.name)
            existing_supply.add(net.name.casefold())

    for item in candidate.batches:
        if item.batch_id == batch.batch_id:
            item.attempts += 1
            item.delta_fingerprint = delta_hash
            item.error = ""
            item.status = "completed"
            break
    candidate.aggregate_revision += 1
    shared_refs = {ref.casefold() for ref in candidate.plan.shared_refs}
    candidate.shared_pin_reservations = {
        physical: net_name
        for physical, net_name in net_owner.items()
        if physical.partition(":")[0].casefold() in shared_refs
    }
    return _validated_checkpoint(candidate)


__all__ = [
    "ConnectionBatchCheckpoint",
    "ConnectionBatchPlan",
    "ConnectionBatchSpec",
    "ConnectionDelta",
    "ConnectionMergeError",
    "ConnectionOutputEstimate",
    "ConnectionSynthesisReport",
    "ConnectionSynthesisCheckpoint",
    "NetExtension",
    "assign_parts_to_topology_blocks",
    "checkpoint_matches_inputs",
    "connection_delta_fingerprint",
    "connection_synthesis_report",
    "estimate_connection_output",
    "infer_shared_refs",
    "merge_connection_delta",
    "new_connection_checkpoint",
    "plan_connection_batches",
    "selection_fingerprint",
    "topology_fingerprint",
]
