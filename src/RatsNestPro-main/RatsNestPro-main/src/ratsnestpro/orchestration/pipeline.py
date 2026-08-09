"""The fixed, industry-standard PCB pipeline framework.

The process is *pinned*: a fixed, ordered sequence of steps that cannot be
skipped or reordered. Every step has the same shape —

    inject knowledge  ->  LLM structured proposal  ->  bottom-line check

The LLM makes the design decisions (fed the relevant knowledge for that step);
a small, cheap "anti-board-burn" check validates the proposal against real
libraries and fab values — it never encodes business rules, it only catches
things that would ruin a board (missing pins, single-pin nets, sub-fab widths).

Design stance:
* ``offline`` mode uses each step's deterministic fallback (no model calls).
* ``auto`` uses the LLM and falls back to the deterministic path on failure.
* ``required`` must use the LLM; a failure or invalid output fails closed.

This module provides the framework plus the first two concrete steps
(requirements, topology). Later tasks add the remaining steps; each one
subclasses :class:`PipelineStepBase` and is registered in :data:`ALL_STEPS`.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
import tempfile
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel

from ratsnestpro import config
from ratsnestpro.agents.heuristics import params_from_requirement
from ratsnestpro.agents.llm import LlmError, LlmMode
from ratsnestpro.domain.contracts import (
    ComponentIdentityConstraint,
    ContractModel,
    RequirementSpec,
    Severity,
)
from ratsnestpro.eda import footprints, grounding, symbols
from ratsnestpro.eda.adapter import kicad_cli_available, run_erc
from ratsnestpro.eda.materialize import materialize_pinmapped
from ratsnestpro.families import Atmega328Params, build_ir, build_plan
from ratsnestpro.knowledge import KnowledgeBase, build_default_kb
from ratsnestpro.orchestration.ahe import (
    CapabilityGap,
    FailureEnvelope,
    Recoverability,
    RepairRecord,
    ReplanRecord,
    ahe_event,
    make_capability_gap,
    make_failure,
)
from ratsnestpro.orchestration.component_resolution import (
    ComponentResolutionService,
    LibraryClosureResult,
    ResolutionStatus,
    SymbolOnlyPlaceholderSpec,
)
from ratsnestpro.orchestration.connection_synthesis import (
    ConnectionDelta,
    ConnectionMergeError,
    ConnectionSynthesisCheckpoint,
    ConnectionSynthesisReport,
    checkpoint_matches_inputs,
    connection_synthesis_report,
    estimate_connection_output,
    merge_connection_delta,
    new_connection_checkpoint,
    plan_connection_batches,
)
from ratsnestpro.orchestration.footprint_search import footprint_candidates
from ratsnestpro.orchestration.pipeline_contracts import (
    BoardPartition,
    BoardZone,
    ErcSummary,
    FabAudit,
    LogicalPin,
    ManufactureResult,
    MappedNet,
    MappedPin,
    MaterializeResult,
    NetClass,
    NetIntent,
    NetlistIntent,
    NetlistPatch,
    PcbPlacement,
    PcbPlacementPlan,
    PcbWriteResult,
    PinMapPlan,
    PlanePlan,
    RoutePlan,
    RouteResult,
    SchLayoutPlan,
    SelectedPart,
    SelectionPatch,
    SelectionPlan,
    SheetPlacement,
    TopologyBlock,
    TopologyPlan,
)
from ratsnestpro.orchestration.placement_constraints import (
    allowed_origin_regions,
    compile_placement_constraints,
    placement_constraint_violations,
    write_placement_constraint_manifest,
)
from ratsnestpro.orchestration.requirement_identity import (
    extract_component_identity_constraints,
    identity_constraint_for_part,
    missing_fixed_identities,
)
from ratsnestpro.orchestration.selection_grounding import (
    failed_symbol_candidates,
    requirement_symbol_hints,
)

# --------------------------------------------------------------------------- #
# The pinned step sequence
# --------------------------------------------------------------------------- #


class PipelineStep(StrEnum):
    """The fixed industry-standard flow. Order is authoritative and enforced."""

    REQUIREMENTS = "requirements"
    TOPOLOGY = "topology"
    SELECTION = "selection"
    SCH_CONNECTIONS = "schematic_connections"
    SCH_PINMAP = "schematic_pinmap"
    SCH_LAYOUT = "schematic_layout"
    SCH_MATERIALIZE = "schematic_materialize"
    ERC = "erc"
    LAYOUT_PARTITION = "layout_partition"
    LAYOUT_CRITICAL = "layout_critical"
    LAYOUT_GENERAL = "layout_general"
    LAYOUT_WRITE = "layout_write"
    ROUTE_PLAN = "route_plan"
    ROUTE_PLANES = "route_planes"
    ROUTE_SIGNALS = "route_signals"
    ROUTE_FAB = "route_fab"
    MANUFACTURE = "manufacture"


# Canonical order (StrEnum preserves definition order).
CANONICAL_ORDER: list[PipelineStep] = list(PipelineStep)
_ORDER_INDEX: dict[PipelineStep, int] = {s: i for i, s in enumerate(CANONICAL_ORDER)}


# --------------------------------------------------------------------------- #
# Results and state
# --------------------------------------------------------------------------- #


class CheckResult(ContractModel):
    """One bottom-line check outcome.

    ``severity`` controls release readiness. ``blocks_execution`` is reserved
    for failures that make the next mechanical pipeline step impossible (for
    example, no schematic file exists). Keeping those meanings separate lets
    artifact-first runs produce an editable draft without calling it releasable.
    """

    name: str
    ok: bool
    severity: Severity = Severity.ERROR
    message: str = ""
    blocks_execution: bool = False


class StepResult(ContractModel):
    """Per-step outcome. The artifact itself is stored on the state object."""

    step: PipelineStep
    used_llm: bool = False
    knowledge_used: list[str] = []
    checks: list[CheckResult] = []
    failures: list[FailureEnvelope] = []
    repairs: list[RepairRecord] = []
    blocked: bool = False
    execution_blocked: bool = False
    summary: str = ""

    @property
    def error_checks(self) -> list[CheckResult]:
        return [c for c in self.checks if not c.ok and c.severity == Severity.ERROR]


@dataclass
class PipelineState:
    """Mutable state threaded through the pipeline."""

    requirement_text: str
    project_name: str = "generated_board"
    artifacts: dict[PipelineStep, BaseModel] = field(default_factory=dict)
    results: list[StepResult] = field(default_factory=list)
    revision: int = 0
    repair_history: list[RepairRecord] = field(default_factory=list)
    replan_history: list[ReplanRecord] = field(default_factory=list)
    capability_gaps: list[CapabilityGap] = field(default_factory=list)
    resume_candidates: dict[PipelineStep, tuple[BaseModel, bool]] = field(
        default_factory=dict
    )
    connection_synthesis_checkpoint: ConnectionSynthesisCheckpoint | None = None
    connection_synthesis_report: ConnectionSynthesisReport | None = None

    def artifact(self, step: PipelineStep) -> BaseModel | None:
        return self.artifacts.get(step)

    @property
    def completed(self) -> list[PipelineStep]:
        return [r.step for r in self.results]

    @property
    def blocked(self) -> bool:
        return any(r.blocked for r in self.results)

    @property
    def execution_blocked(self) -> bool:
        return any(r.execution_blocked for r in self.results)


_RELEASE_PROVEN_STATUSES = frozenset({
    ResolutionStatus.INSTALLED_EXACT.value,
    ResolutionStatus.INSTALLED_QUALIFIED_VALIDATED.value,
    ResolutionStatus.REPLACEABLE_GROUNDED.value,
})
_COMPONENT_RELEASE_MANIFEST_SCHEMA = 2
_COMPONENT_RELEASE_POLICY = "explicit_component_closure_v1"


def selection_release_issues(state: PipelineState) -> list[dict[str, str]]:
    """Return component-closure facts that forbid manufacturing release.

    This is intentionally independent of ERC, DRC, and routing. Those checks
    can prove that a draft is syntactically and geometrically consistent, but
    they cannot validate an unresolved device identity or placeholder pinout.
    """

    selection = state.artifact(PipelineStep.SELECTION)
    if not isinstance(selection, SelectionPlan):
        return [{
            "ref": "<selection>",
            "status": "missing",
            "reason": (
                "selection artifact is missing; explicit component release "
                "proof is required"
            ),
            "symbol": "",
            "footprint": "",
        }]
    if not selection.parts:
        return [{
            "ref": "<selection>",
            "status": "empty",
            "reason": (
                "selection contains no physical parts; explicit component "
                "release proof is required"
            ),
            "symbol": "",
            "footprint": "",
        }]
    issues: list[dict[str, str]] = []
    for part in selection.parts:
        status = part.resolution_status.strip()
        explicit_release_ready = getattr(part, "release_ready", None) is True
        placeholder = part.symbol.startswith("RatsNestPlaceholder:")
        release_proven = (
            explicit_release_ready
            and status in _RELEASE_PROVEN_STATUSES
            and not part.dnp
            and not part.unresolved
            and not placeholder
        )
        if release_proven:
            continue
        if part.resolution_detail.strip():
            reason = part.resolution_detail.strip()
        elif part.dnp or part.unresolved:
            reason = "component is DNP/unresolved and not release eligible"
        elif placeholder:
            reason = "placeholder symbols are not release eligible"
        elif not explicit_release_ready:
            reason = "release_ready is not explicitly true"
        elif not status:
            reason = (
                "component closure metadata is missing; explicit release "
                "proof is required"
            )
        else:
            reason = (
                f"resolution status {status!r} is not a release-proven state"
            )
        issues.append({
            "ref": part.ref,
            "status": status or "missing",
            "reason": reason,
            "symbol": part.symbol,
            "footprint": part.footprint,
        })
    return issues


@dataclass
class PipelineContext:
    """Shared services for steps: LLM mode/client and the knowledge base."""

    mode: LlmMode = LlmMode.OFFLINE
    client: object | None = None  # LLMClient | None (kept loose to avoid import cycle)
    kb: KnowledgeBase = field(default_factory=build_default_kb)
    out_dir: str | None = None  # where materialized artifacts (.kicad_sch) are written
    repair_feedback: str = ""  # bottom-line check failures fed back for LLM self-repair
    repair_attempts: int = 0  # how many times a blocked LLM step may re-propose (opt-in)
    require_freerouting: bool = False  # fail closed when real signal routing is incomplete
    capture_step_errors: bool = False
    on_step_completed: Callable[[PipelineState, StepResult], None] | None = None
    ahe_enabled: bool = True
    max_total_repair_attempts: int = 12
    max_same_failure_retries: int = 2
    ahe_deadline_monotonic: float | None = None
    max_replan_attempts: int = 2
    on_ahe_event: Callable[[dict[str, Any]], None] | None = None
    strategy_score: Callable[[str, str], float | None] | None = None
    replan_score: Callable[[str, str], float | None] | None = None
    artifact_first: bool = False
    repair_release_issues: bool = False
    execution_retry_attempts: int = 1
    design_repair_attempts: int = 0  # consecutive stagnation allowed per step
    max_design_repair_attempts_per_step: int = 2  # total attempts in one step
    max_total_design_repair_attempts: int = 8  # final run-wide safety cap
    connection_completion_limit: int = 8192
    connection_direct_pin_limit: int = 180
    connection_batch_target_pins: int = 96
    connection_max_batches: int = 8
    connection_batch_merge_retries: int = 1
    connection_max_llm_invocations: int = 16
    connection_max_total_llm_invocations: int = 32
    max_route_invocations: int = 3
    component_pin_evidence: dict[
        str,
        SymbolOnlyPlaceholderSpec | dict[str, Any],
    ] = field(default_factory=dict)
    on_progress_checkpoint: Callable[[PipelineState], None] | None = None


_MAX_REPAIR_ARTIFACT_CHARS = 80_000


def _artifact_fingerprint(artifact: BaseModel | None) -> str:
    """Identify the concrete baseline that a bounded repair is acting on.

    Failure IDs intentionally stay stable across boards so EHE can learn from
    them.  Retry budgets, however, must be local to one artifact revision:
    otherwise a newly grounded BOM inherits the exhausted budget of an older,
    materially different BOM merely because the same bottom-line check runs.
    """

    if artifact is None:
        return ""
    payload = artifact.model_dump_json(exclude={"rationale"})
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]


# --------------------------------------------------------------------------- #
# LLM proposal helper (structured, fail-closed in required mode)
# --------------------------------------------------------------------------- #

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def _extract_json(text: str) -> str:
    """Pull the first JSON object out of a model response (tolerates fences)."""
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        # drop an optional leading language tag like ``json``
        stripped = re.sub(r"^json\s*", "", stripped, flags=re.IGNORECASE)
    m = _JSON_RE.search(stripped)
    return m.group(0) if m else stripped


def _close_truncated_json(text: str) -> str | None:
    """Return the least-destructive valid JSON prefix of a truncated response."""

    return next(_truncated_json_candidates(text), None)


def _truncated_json_candidates(text: str) -> Iterable[str]:
    """Yield valid JSON prefixes from most to least complete.

    Missing closing delimiters are appended first. If output ended inside its
    final value, the incomplete tail is discarded only at an already-complete
    comma boundary. No new field or value is invented.
    """

    stack: list[str] = []
    boundaries: list[tuple[int, tuple[str, ...]]] = []
    in_string = False
    escaped = False
    pairs = {"}": "{", "]": "["}
    mismatched = False
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "{[":
            stack.append(char)
        elif char in "}]":
            if not stack or stack.pop() != pairs[char]:
                mismatched = True
                break
        elif char == "," and stack:
            boundaries.append((index, tuple(stack)))

    closers = {"{": "}", "[": "]"}
    yielded: set[str] = set()
    if (
        not mismatched
        and not in_string
        and stack
        and not text.rstrip().endswith((",", ":"))
    ):
        candidate = text + "".join(
            closers[opener] for opener in reversed(stack)
        )
        try:
            json.loads(candidate)
        except (TypeError, ValueError):
            pass
        else:
            yielded.add(candidate)
            yield candidate

    for index, openers in reversed(boundaries):
        prefix = text[:index].rstrip()
        if not prefix:
            continue
        candidate = prefix + "".join(
            closers[opener] for opener in reversed(openers)
        )
        if candidate in yielded:
            continue
        try:
            json.loads(candidate)
        except (TypeError, ValueError):
            continue
        yielded.add(candidate)
        yield candidate


class StructuredOutputError(LlmError):
    """A model returned output that stayed invalid after bounded recovery."""

    def __init__(
        self,
        model_name: str,
        attempts: int,
        last_error: Exception,
    ) -> None:
        self.model_name = model_name
        self.attempts = attempts
        self.last_error = last_error
        super().__init__(
            f"{model_name} structured output remained invalid after "
            f"{attempts} attempts: {last_error}"
        )


def propose_structured[T: BaseModel](
    ctx: PipelineContext,
    *,
    model: type[T],
    system: str,
    user: str,
    fallback: Callable[[], T],
    before_attempt: Callable[[], None] | None = None,
) -> tuple[T, bool]:
    """Get a validated ``model`` instance: LLM proposal or deterministic fallback.

    Returns ``(artifact, used_llm)``. In ``required`` mode a missing client,
    request failure, or invalid/unparseable output raises :class:`LlmError`
    (fail closed). In ``auto`` mode any such failure falls back deterministically.
    In ``offline`` mode the fallback is used directly.
    """
    client = ctx.client
    if ctx.mode == LlmMode.OFFLINE or client is None:
        if ctx.mode == LlmMode.REQUIRED:
            raise LlmError("required LLM mode but no client is available")
        return fallback(), False
    # LLMs occasionally emit truncated or slightly malformed JSON. Retry a few
    # times, tightening the instruction each round, before deciding.
    attempts = 3
    last_exc: Exception | None = None
    last_failure_was_output = False
    for i in range(attempts):
        prompt = user
        if ctx.repair_feedback:
            prompt = (
                f"{prompt}\n\nYour previous proposal was rejected by a bottom-line "
                f"check:\n{ctx.repair_feedback}\nFix exactly these problems and return "
                "corrected JSON. Do not reintroduce them."
            )
        if i:
            schema = json.dumps(
                model.model_json_schema(),
                ensure_ascii=False,
                separators=(",", ":"),
            )
            prompt = (
                f"{prompt}\n\nIMPORTANT: your previous reply was not valid JSON "
                f"({last_exc}). Reply with a SINGLE minified JSON object only — "
                "no prose, no markdown fences, no trailing commas. The reply may "
                "have failed schema validation even when it was valid JSON; correct "
                "the reported field types and constraints. Required JSON schema: "
                f"{schema[:12_000]}"
            )
        if before_attempt is not None:
            before_attempt()
        try:
            raw = client.complete(system, prompt)  # type: ignore[attr-defined]
        except Exception as exc:
            last_exc = exc
            last_failure_was_output = False
            continue

        candidate = _extract_json(raw)
        try:
            return model.model_validate_json(candidate), True
        except Exception as exc:
            last_exc = exc
            last_failure_was_output = True
        for repaired in _truncated_json_candidates(candidate):
            try:
                return model.model_validate_json(repaired), True
            except Exception as exc:
                last_exc = exc
                last_failure_was_output = True
    if ctx.mode == LlmMode.REQUIRED:
        if last_failure_was_output and last_exc is not None:
            raise StructuredOutputError(
                model.__name__,
                attempts,
                last_exc,
            ) from last_exc
        raise LlmError(f"{model.__name__} proposal failed: {last_exc}") from last_exc
    return fallback(), False


# --------------------------------------------------------------------------- #
# Step base class
# --------------------------------------------------------------------------- #


class PipelineStepBase(ABC):
    """A single pipeline step: knowledge -> proposal -> bottom-line check."""

    step: ClassVar[PipelineStep]
    knowledge_role: ClassVar[str | None] = None
    repair_strategy_id: ClassVar[str | None] = None
    repair_is_deterministic: ClassVar[bool] = False
    allow_artifact_first_design_repair: ClassVar[bool] = False

    def knowledge_query(self, state: PipelineState) -> str | None:
        """Query used to retrieve step knowledge; ``None`` skips retrieval."""
        return None

    @abstractmethod
    def propose(
        self, state: PipelineState, ctx: PipelineContext, knowledge: str
    ) -> tuple[BaseModel, bool]:
        """Produce the (validated) artifact + used_llm flag. Sets no state."""

    @abstractmethod
    def check(self, state: PipelineState, artifact: BaseModel) -> list[CheckResult]:
        """Cheap bottom-line checks against real libraries / fab values."""

    def repair(
        self,
        state: PipelineState,
        ctx: PipelineContext,
        knowledge: str,
        artifact: BaseModel,
        checks: list[CheckResult],
    ) -> tuple[BaseModel, bool]:
        """Repair a rejected proposal; subclasses may return a bounded delta."""
        return self.propose(state, ctx, knowledge)

    def repair_applicable(
        self,
        state: PipelineState,
        artifact: BaseModel,
        checks: list[CheckResult],
    ) -> bool:
        """Whether this step-local strategy can address the observed failure."""

        return True

    def prepare_resumed_artifact(
        self,
        state: PipelineState,
        artifact: BaseModel,
    ) -> BaseModel:
        """Refresh deterministic grounding before revalidating a checkpoint."""
        return artifact

    def replan(
        self,
        state: PipelineState,
        ctx: PipelineContext,
        knowledge: str,
        artifact: BaseModel,
        feedback: str,
    ) -> tuple[BaseModel, bool]:
        """Replan an upstream artifact while retaining it as a safe baseline.

        The default preserves the historical full-reproposal behaviour.  Steps
        with large artifacts can override this with a bounded delta so a
        downstream failure cannot cause unbounded upstream regeneration.
        """

        return self.propose(state, ctx, knowledge)

    def rollback_target(
        self,
        state: PipelineState,
        artifact: BaseModel,
        checks: list[CheckResult],
    ) -> PipelineStep | None:
        """Return an earlier step when repair requires upstream replanning."""

        return None

    def convergence_score(
        self,
        artifact: BaseModel,
        results: list[CheckResult],
    ) -> tuple[int, int, int]:
        """Return a lower-is-better score for accepting a bounded repair."""

        failed = [check for check in results if not check.ok]
        return (
            sum(
                not check.ok and check.severity == Severity.ERROR
                for check in results
            ),
            len(failed),
            sum(len(check.message) for check in failed),
        )

    @staticmethod
    def repair_progress_is_material(
        before: tuple[int, int, int],
        after: tuple[int, int, int],
    ) -> bool:
        """Return whether a repair made enough progress to retain and extend.

        Error/check count reductions are always meaningful. When those counts
        are unchanged, accept only a substantial reduction in structured
        diagnostic detail. This prevents tiny wording changes from consuming
        and extending the adaptive repair budget.
        """

        if after[:2] < before[:2]:
            return True
        if after[:2] != before[:2] or after[2] >= before[2]:
            return False
        required_reduction = max(80, (before[2] + 11) // 12)
        return before[2] - after[2] >= required_reduction

    def run(self, state: PipelineState, ctx: PipelineContext) -> StepResult:
        knowledge = ""
        knowledge_ids: list[str] = []
        query = self.knowledge_query(state)
        if query and self.knowledge_role is not None:
            hits = ctx.kb.retrieve(query, top_k=3, role=self.knowledge_role)
            knowledge = "\n\n".join(f"[{h.doc.id}]\n{h.doc.text.strip()}" for h in hits)
            knowledge_ids = [h.doc.id for h in hits]
        resumed = state.resume_candidates.pop(self.step, None)
        if resumed is None:
            artifact, used_llm = self.propose(state, ctx, knowledge)
        else:
            artifact, used_llm = resumed
            artifact = self.prepare_resumed_artifact(state, artifact)
            if ctx.repair_feedback:
                artifact, used_llm = self.replan(
                    state,
                    ctx,
                    knowledge,
                    artifact,
                    ctx.repair_feedback,
                )
        checks = self.check(state, artifact)
        blocked = any(not c.ok and c.severity == Severity.ERROR for c in checks)
        execution_blocked = any(
            not c.ok
            and c.severity == Severity.ERROR
            and c.blocks_execution
            for c in checks
        )
        best_artifact = artifact
        best_checks = checks
        repair_baseline_fingerprint = _artifact_fingerprint(artifact)
        repair_records: list[RepairRecord] = []

        custom_repair = type(self).repair is not PipelineStepBase.repair
        transient_retry = any(
            make_failure(
                step=self.step.value,
                check_name=check.name,
                message=check.message,
                repair_available=False,
            ).recoverability
            == Recoverability.RETRYABLE
            for check in checks
            if not check.ok and check.severity == Severity.ERROR
        )
        strategy = self.repair_strategy_id or (
            f"{self.step.value}_repair"
            if custom_repair
            else "bounded_transient_retry"
            if transient_retry
            else "llm_reproposal"
        )
        strategy_available = (
            custom_repair
            or transient_retry
            or (used_llm and ctx.mode != LlmMode.OFFLINE)
        )

        def failures_for(results: list[CheckResult]) -> list[FailureEnvelope]:
            return [
                make_failure(
                    step=self.step.value,
                    check_name=check.name,
                    message=check.message,
                    repair_available=strategy_available,
                )
                for check in results
                if not check.ok and check.severity == Severity.ERROR
            ]

        def emit(
            event: str,
            *,
            failure: FailureEnvelope | None = None,
            repair: RepairRecord | None = None,
            gap: CapabilityGap | None = None,
        ) -> None:
            if design_recovery_scope:
                return
            if ctx.on_ahe_event is None:
                return
            try:
                ctx.on_ahe_event(
                    ahe_event(
                        event,
                        step=self.step.value,
                        revision=state.revision,
                        failure=failure,
                        repair=repair,
                        gap=gap,
                    )
                )
            except Exception:  # noqa: BLE001 - observability must not break design
                return

        harness_recovery_scope = (
            not ctx.artifact_first
            or ctx.repair_release_issues
            or execution_blocked
        )
        design_recovery_scope = (
            ctx.artifact_first
            and not harness_recovery_scope
            and self.allow_artifact_first_design_repair
            and ctx.design_repair_attempts > 0
        )
        repair_scope = harness_recovery_scope or design_recovery_scope
        repair_kind = "design" if design_recovery_scope else "harness"
        detected_failures = failures_for(checks)
        if harness_recovery_scope:
            for failure in detected_failures:
                emit("failure_detected", failure=failure)

        # AHE repairs both LLM proposals and deterministic steps that provide a
        # bounded repair implementation. The total task budget prevents loops.
        can_repair = (
            blocked
            and repair_scope
            and (ctx.ahe_enabled or design_recovery_scope)
            and strategy_available
            and self.repair_applicable(state, best_artifact, best_checks)
            and (
                ctx.design_repair_attempts
                if design_recovery_scope
                else ctx.repair_attempts
            )
            > 0
            and (
                ctx.ahe_deadline_monotonic is None
                or time.monotonic() < ctx.ahe_deadline_monotonic
            )
        )
        if can_repair:
            active_failure_ids = {
                failure.failure_id for failure in failures_for(best_checks)
            }
            if design_recovery_scope:
                prior_run_design_attempts = sum(
                    record.kind == "design"
                    for record in state.repair_history
                )
                prior_step_design_attempts = sum(
                    record.kind == "design"
                    and record.step == self.step.value
                    for record in state.repair_history
                )
                remaining_run_budget = max(
                    0,
                    ctx.max_total_design_repair_attempts
                    - prior_run_design_attempts,
                )
                remaining_step_budget = max(
                    0,
                    ctx.max_design_repair_attempts_per_step
                    - prior_step_design_attempts,
                )
                remaining_task_budget = min(
                    remaining_run_budget,
                    remaining_step_budget,
                )
                per_step_attempts = ctx.design_repair_attempts
            else:
                prior_run_ahe_attempts = sum(
                    record.kind == "harness" for record in state.repair_history
                )
                same_failure_attempts = max(
                    (
                        sum(
                            failure_id in record.failure_ids
                            for record in state.repair_history
                            if record.kind == "harness"
                        )
                        for failure_id in active_failure_ids
                    ),
                    default=0,
                )
                remaining_task_budget = max(
                    0,
                    min(
                        ctx.max_total_repair_attempts - prior_run_ahe_attempts,
                        ctx.max_same_failure_retries - same_failure_attempts,
                    ),
                )
                per_step_attempts = ctx.repair_attempts
            stagnation_budget = min(
                max(0, per_step_attempts),
                remaining_task_budget,
            )
            if ctx.strategy_score is not None and stagnation_budget:
                scores = [
                    ctx.strategy_score(failure.signature, strategy)
                    for failure in failures_for(best_checks)
                ]
                known_scores = [score for score in scores if score is not None]
                if known_scores and max(known_scores) >= 0.8:
                    stagnation_budget = min(
                        stagnation_budget + 1,
                        remaining_task_budget,
                    )
                elif known_scores and max(known_scores) < 0.2:
                    stagnation_budget = max(1, stagnation_budget - 1)
            hard_attempt_limit = min(12, remaining_task_budget)
            attempt = 0
            consecutive_stagnant = 0
            while (
                attempt < hard_attempt_limit
                and consecutive_stagnant < stagnation_budget
                and (
                    ctx.ahe_deadline_monotonic is None
                    or time.monotonic() < ctx.ahe_deadline_monotonic
                )
            ):
                attempt += 1
                fails = [
                    check
                    for check in best_checks
                    if not check.ok and check.severity == Severity.ERROR
                ]
                failure_text = "\n".join(
                    f"- {c.name}: {c.message}"
                    for c in fails
                )
                rejected_json = best_artifact.model_dump_json(
                    exclude={"rationale"},
                )
                if len(rejected_json) > _MAX_REPAIR_ARTIFACT_CHARS:
                    rejected_json = (
                        f"<omitted: {len(rejected_json)} characters exceeds "
                        f"{_MAX_REPAIR_ARTIFACT_CHARS}>"
                    )
                ctx.repair_feedback = (
                    "Rejected proposal JSON:\n"
                    f"{rejected_json}\n"
                    "Failed checks:\n"
                    f"{failure_text}"
                )
                before_score = self.convergence_score(best_artifact, best_checks)
                try:
                    candidate, candidate_used_llm = self.repair(
                        state,
                        ctx,
                        knowledge,
                        best_artifact,
                        best_checks,
                    )
                except LlmError as exc:
                    # The already validated, blocked proposal and its deterministic
                    # check evidence are more useful than losing the entire step
                    # because one repair response could not be parsed.
                    record = RepairRecord(
                        kind=repair_kind,
                        step=self.step.value,
                        strategy=strategy,
                        attempt=attempt,
                        failure_ids=[
                            failure.failure_id
                            for failure in failures_for(best_checks)
                        ],
                        status="error",
                        before_score=before_score,
                        after_score=before_score,
                        detail=str(exc),
                        baseline_fingerprint=repair_baseline_fingerprint,
                    )
                    repair_records.append(record)
                    state.repair_history.append(record)
                    emit("repair_error", repair=record)
                    break
                finally:
                    ctx.repair_feedback = ""
                candidate_checks = self.check(state, candidate)
                after_score = self.convergence_score(candidate, candidate_checks)
                improved = self.repair_progress_is_material(
                    before_score,
                    after_score,
                )
                verified = after_score[0] == 0
                record = RepairRecord(
                    kind=repair_kind,
                    step=self.step.value,
                    strategy=strategy,
                    attempt=attempt,
                    failure_ids=[
                        failure.failure_id for failure in failures_for(best_checks)
                    ],
                    status=(
                        "verified"
                        if verified
                        else "improved"
                        if improved
                        else "rejected"
                    ),
                    before_score=before_score,
                    after_score=after_score,
                    baseline_fingerprint=repair_baseline_fingerprint,
                )
                if improved:
                    state.revision += 1
                repair_records.append(record)
                state.repair_history.append(record)
                emit(f"repair_{record.status}", repair=record)
                if improved:
                    best_artifact = candidate
                    best_checks = candidate_checks
                    used_llm = candidate_used_llm
                    # A materially better artifact starts a new repair epoch.
                    # The bounded hard limit still caps total work, while the
                    # stagnation allowance is fully restored for the new
                    # baseline instead of being consumed by earlier progress.
                    repair_baseline_fingerprint = _artifact_fingerprint(
                        best_artifact
                    )
                    consecutive_stagnant = 0
                elif custom_repair and self.repair_is_deterministic:
                    # A deterministic local transform will produce the same
                    # rejected patch on the next call. Preserve budget for an
                    # upstream replan instead of repeating it.
                    break
                else:
                    consecutive_stagnant += 1
                if self.convergence_score(best_artifact, best_checks)[0] == 0:
                    break
            artifact = best_artifact
            checks = best_checks
            blocked = self.convergence_score(artifact, checks)[0] > 0
            execution_blocked = any(
                not check.ok
                and check.severity == Severity.ERROR
                and check.blocks_execution
                for check in checks
            )
            harness_recovery_scope = (
                not ctx.artifact_first
                or ctx.repair_release_issues
                or execution_blocked
            )
            if harness_recovery_scope:
                # A design correction can reveal a real execution/capability
                # failure. From this point it belongs to the Harness ledger
                # and must no longer be hidden by the design-only event scope.
                design_recovery_scope = False

        failures = failures_for(checks)
        if not blocked:
            # A resumed step may pass on its first proposal, so there is no
            # failure in this invocation to match by signature. Resolve stale
            # gaps by the stable step/check identity of checks that now pass.
            passing_checks = {check.name for check in checks if check.ok}
            unresolved: list[CapabilityGap] = []
            for gap in state.capability_gaps:
                if gap.step == self.step.value and gap.check_name in passing_checks:
                    emit("capability_gap_resolved", gap=gap)
                    continue
                unresolved.append(gap)
            state.capability_gaps = unresolved
        upstream_replan_available = (
            blocked
            and harness_recovery_scope
            and ctx.ahe_enabled
            and self.rollback_target(state, artifact, checks) is not None
        )
        if blocked and harness_recovery_scope and not upstream_replan_available:
            known_gaps = {
                gap.signature: gap for gap in state.capability_gaps
            }
            for failure in failures:
                if failure.recoverability == Recoverability.HARD_CONFLICT:
                    emit("hard_constraint_conflict", failure=failure)
                    continue
                gap = make_capability_gap(failure)
                existing = known_gaps.get(gap.signature)
                if existing is None:
                    state.capability_gaps.append(gap)
                    known_gaps[gap.signature] = gap
                    emit("capability_gap", failure=failure)
                else:
                    # Stable signatures are useful for cross-task learning, but
                    # the user-facing evidence must describe the current failed
                    # artifact rather than the first occurrence in the task.
                    existing.message = gap.message
                    existing.category = gap.category
                    existing.required_capability = gap.required_capability

        state.artifacts[self.step] = artifact
        result = StepResult(
            step=self.step,
            used_llm=used_llm,
            knowledge_used=knowledge_ids,
            checks=checks,
            failures=failures,
            repairs=repair_records,
            blocked=blocked,
            execution_blocked=execution_blocked,
            summary=self.summarize(artifact),
        )
        state.results.append(result)
        return result

    def summarize(self, artifact: BaseModel) -> str:
        return type(artifact).__name__


# --------------------------------------------------------------------------- #
# Concrete steps (Task 4 seeds the first two; later tasks add the rest)
# --------------------------------------------------------------------------- #


class RequirementsStep(PipelineStepBase):
    step = PipelineStep.REQUIREMENTS

    def propose(
        self, state: PipelineState, ctx: PipelineContext, knowledge: str
    ) -> tuple[BaseModel, bool]:
        def fallback() -> RequirementSpec:
            return RequirementSpec(
                raw_text=state.requirement_text, project_name=state.project_name
            )

        system = (
            "You normalize a hardware requirement into JSON with fields "
            "raw_text, project_name, constraints[], acceptance_criteria[]. "
            "Set raw_text to the exact literal "
            "\"__RATSNEST_SOURCE_REQUIREMENT__\"; the harness restores the "
            "original source deterministically. Keep each normalized item concise "
            "and do not copy source documents or datasheet excerpts."
        )
        proposal, used_llm = propose_structured(
            ctx,
            model=RequirementSpec,
            system=system,
            user=(
                "Return raw_text as __RATSNEST_SOURCE_REQUIREMENT__.\n\n"
                f"{state.requirement_text}"
            ),
            fallback=fallback,
        )
        return (
            RequirementSpec.model_validate(
                {
                    **proposal.model_dump(),
                    "raw_text": state.requirement_text,
                    "project_name": state.project_name,
                    "component_identity_constraints": [
                        item.model_dump()
                        for item in extract_component_identity_constraints(
                            state.requirement_text
                        )
                    ],
                }
            ),
            used_llm,
        )

    def check(self, state: PipelineState, artifact: BaseModel) -> list[CheckResult]:
        assert isinstance(artifact, RequirementSpec)
        return [
            CheckResult(
                name="requirement_text_present",
                ok=bool(artifact.raw_text.strip()),
                message="requirement text must not be empty",
            )
        ]

    def summarize(self, artifact: BaseModel) -> str:
        assert isinstance(artifact, RequirementSpec)
        return f"requirement '{artifact.project_name}' ({len(artifact.raw_text)} chars)"


class TopologyStep(PipelineStepBase):
    step = PipelineStep.TOPOLOGY
    knowledge_role = "topology"

    def knowledge_query(self, state: PipelineState) -> str | None:
        return f"power tree and block topology for: {state.requirement_text}"

    def propose(
        self, state: PipelineState, ctx: PipelineContext, knowledge: str
    ) -> tuple[BaseModel, bool]:
        def fallback() -> TopologyPlan:
            params = params_from_requirement(state.requirement_text)
            rail = str(params.get("ldo_output_v", 3.3))
            rails = ["5V", f"{rail}V"]
            blocks = [
                TopologyBlock(name="power_input", kind="power_input",
                              description="external supply input + protection"),
                TopologyBlock(name="regulator", kind="regulator",
                              description=f"LDO to {rail} V rail"),
                TopologyBlock(name="mcu", kind="mcu", description="microcontroller"),
                TopologyBlock(name="oscillator", kind="oscillator",
                              description="crystal + load caps"),
                TopologyBlock(name="reset", kind="reset", description="reset pull-up + button"),
                TopologyBlock(name="headers", kind="header", description="breakout headers"),
            ]
            return TopologyPlan(
                blocks=blocks, rails=rails, ground_net="GND",
                rationale="deterministic baseline topology",
            )

        system = (
            "You design a PCB block-level topology. Return JSON with blocks[] "
            "(name, kind, description), rails[] (supply rail names), ground_net, "
            "rationale. Use the provided design knowledge."
        )
        user = f"Requirement:\n{state.requirement_text}\n\nKnowledge:\n{knowledge}"
        return propose_structured(
            ctx, model=TopologyPlan, system=system, user=user, fallback=fallback
        )

    def check(self, state: PipelineState, artifact: BaseModel) -> list[CheckResult]:
        assert isinstance(artifact, TopologyPlan)
        return [
            CheckResult(
                name="has_blocks", ok=bool(artifact.blocks),
                message="topology must define at least one functional block",
            ),
            CheckResult(
                name="has_supply_rail", ok=bool(artifact.rails),
                message="topology must define at least one supply rail",
            ),
            CheckResult(
                name="has_ground", ok=bool(artifact.ground_net.strip()),
                message="topology must define a ground net",
            ),
        ]

    def summarize(self, artifact: BaseModel) -> str:
        assert isinstance(artifact, TopologyPlan)
        return f"{len(artifact.blocks)} blocks, rails={artifact.rails}"


def _ground_mpns(parts: list[SelectedPart]) -> None:
    """Fill mpn/lcsc from the real JLCPCB cache. Never fabricate: leave empty
    when the cache is unavailable or has no match."""
    try:
        from ratsnestpro.parts.selector import PartSelector

        sel = PartSelector()
        if not sel.available():
            return
        for p in parts:
            if p.role == "mounting_hole" or not p.value:
                continue
            cands = sel.suggest(p.value, p.footprint, limit=1)
            if cands:
                p.mpn = cands[0].mpn
                p.lcsc = cands[0].lcsc
    except Exception:
        # Grounding is best-effort; absence of a cache must not break the flow.
        return


_MCU_MODEL_RE = re.compile(
    r"\b(?:RP\d{4}|ATMEGA\d+[A-Z0-9-]*|ATTINY\d+[A-Z0-9-]*|"
    r"ESP32(?:-?(?:S[23]|C[2356]|H2|P4))?"
    r"(?:-(?:WROOM|WROVER|MINI|PICO)[A-Z0-9-]*)?|"
    r"STM32[A-Z]{1,2}\d{3,4}[A-Z0-9]*(?![A-Z0-9-])|"
    r"NRF\d+[A-Z0-9-]*|SAMD\d+[A-Z0-9-]*|"
    r"PIC\d+[A-Z0-9-]*|CH32[A-Z0-9-]*)\b",
    re.IGNORECASE,
)
_MCU_NEGATION_RE = re.compile(
    r"\b(?:not|never|without|instead\s+of|rather\s+than|"
    r"do\s+not|don't|must\s+not|forbid(?:den)?)"
    r"(?:\s+(?:use|using|choose|select|replace))?\b|"
    r"(?:不要|不是|而非|禁止|不得|不能|不用|不允许)"
    r"(?:使用|采用|选用|替换(?:为)?)?",
    re.IGNORECASE,
)
_MCU_POSITIVE_RE = re.compile(
    r"\b(?:use|using|choose|select|must\s+be|required|replace\s+with)\b|"
    r"(?:主控(?:必须)?是|使用|采用|选用|改为)",
    re.IGNORECASE,
)
_MODEL_LIKE_TOKEN_RE = re.compile(
    r"\b[A-Za-z][A-Za-z0-9_.-]{2,}\d[A-Za-z0-9_.-]*\b"
)


_ARCHITECT_EVIDENCE_MARKER = "GROUNDED ARCHITECT EVIDENCE"
_VCAP_CEXT_RE = re.compile(
    r"C\s*EXT\s+Capacitance[^0-9]{0,120}"
    r"(\d+(?:\.\d+)?)\s*[uµμ]F",
    re.IGNORECASE,
)
_VCAP_VALUE_RE = re.compile(
    r"^\s*(\d+(?:\.\d+)?)\s*[uµμ]F?\s*$",
    re.IGNORECASE,
)
_VCAP_ENGINEERING_RE = re.compile(r"^\s*(\d+)[uµμ](\d+)\s*$", re.IGNORECASE)


def _original_requirement(text: str) -> str:
    """Exclude downstream evidence from user-intent parsers."""
    return text.partition(_ARCHITECT_EVIDENCE_MARKER)[0]


def _architect_evidence_excerpt(text: str, max_chars: int = 12_000) -> str:
    """Return a bounded, grounded evidence payload for downstream designers."""

    evidence = text.partition(_ARCHITECT_EVIDENCE_MARKER)[2].strip()
    if len(evidence) <= max_chars:
        return evidence
    return evidence[:max_chars] + "\n<grounded evidence excerpt truncated>"


def _model_mention_is_negated(text: str, start: int) -> bool:
    clause_start = max(
        (text.rfind(separator, 0, start) for separator in ".!?。！？;\n"),
        default=-1,
    )
    prefix = text[clause_start + 1:start]
    negations = list(_MCU_NEGATION_RE.finditer(prefix))
    positives = list(_MCU_POSITIVE_RE.finditer(prefix))
    return bool(
        negations
        and not any(
            positive.start() >= negations[-1].end()
            for positive in positives
        )
    )


def _mcu_models(text: str) -> set[str]:
    text = _original_requirement(text)
    text = re.sub(
        r"\b(?:run_name|project_name)\b\s*(?:=|:)\s*[\"']?"
        r"[a-zA-Z0-9_.-]+[\"']?",
        "",
        text,
        flags=re.IGNORECASE,
    )
    models: set[str] = set()
    for match in _MCU_MODEL_RE.finditer(text):
        if _model_mention_is_negated(text, match.start()):
            continue
        models.add(re.sub(r"[^a-z0-9]", "", match.group(0).lower()))
    # The common-family parser is authoritative when it found an explicit
    # model. Parsing every installed KiCad MCU library is a costly fallback
    # (minute-scale on the full Debian library) and is needed only for an
    # unfamiliar vendor/order code.
    if not models:
        models.update(_library_mcu_models(text))
    # Generic family mentions such as "ESP32" commonly accompany one exact
    # order code. Keep the most specific mention so one requested device does
    # not become several independent selection obligations.
    return {
        model
        for model in models
        if not any(model != other and model in other for other in models)
    }


def _mcu_model_matches(first: str, second: str) -> bool:
    """Match normalized MCU order codes, treating KiCad's ``x`` as a wildcard."""
    if first == second:
        return True

    def pattern(value: str) -> str:
        return re.escape(value).replace("x", "[a-z0-9]")

    return bool(
        re.fullmatch(pattern(first), second)
        or re.fullmatch(pattern(second), first)
    )


def _library_mcu_models(text: str) -> set[str]:
    """Recognize order codes through installed KiCad MCU libraries."""
    try:
        library_models = {
            re.sub(r"[^a-z0-9]", "", lib_id.partition(":")[2].lower())
            for lib_id in grounding.symbol_index()
            if lib_id.partition(":")[0].upper().startswith("MCU_")
        }
    except Exception:
        return set()

    requested: set[str] = set()
    for match in _MODEL_LIKE_TOKEN_RE.finditer(text):
        if _model_mention_is_negated(text, match.start()):
            continue
        model = re.sub(r"[^a-z0-9]", "", match.group(0).lower())
        if any(_mcu_model_matches(model, candidate) for candidate in library_models):
            requested.add(model)
    return requested


def _requested_mcu_symbols(requirement: str) -> list[dict[str, str]]:
    """Find exact KiCad MCU symbols and their library-defined footprints."""
    requested = _mcu_models(requirement)
    if not requested:
        return []

    # This prompt field is an identity contract, not a fuzzy discovery result.
    # Restrict it to processor libraries and accept only an exact order code or
    # KiCad's explicit lowercase-x package wildcard.  In particular, a
    # one-letter symbol such as Device:C must not match merely because its name
    # occurs inside an MCU order code.
    installed: list[tuple[str, str]] = []
    for lib_id in grounding.symbol_index():
        library, _, symbol_name = lib_id.partition(":")
        if library.upper().startswith("MCU_"):
            installed.append((lib_id, symbol_name))

    bounded_ids: list[str] = []
    seen: set[str] = set()
    for model in sorted(requested):
        exact: list[str] = []
        wildcards: list[str] = []
        for lib_id, symbol_name in installed:
            relation = grounding.symbol_identity_match_kind(
                model,
                symbol_name,
            )
            if relation == "exact":
                exact.append(lib_id)
            elif relation == "kicad_wildcard":
                wildcards.append(lib_id)
        for lib_id in exact or wildcards:
            if lib_id in seen:
                continue
            seen.add(lib_id)
            bounded_ids.append(lib_id)
            if len(bounded_ids) >= 20:
                break
        if len(bounded_ids) >= 20:
            break

    # Symbol properties parse comparatively rich library metadata.  Read them
    # only after identity filtering and the prompt-size bound have been applied.
    return [
        {
            "symbol": lib_id,
            "footprint": symbols.symbol_properties(lib_id).get(
                "Footprint",
                "",
            ),
        }
        for lib_id in bounded_ids
    ]


_COMPATIBLE_FOOTPRINT_HINTS: dict[str, tuple[str, ...]] = {
    "Device:L_Coupled": (
        "Inductor_SMD:L_CommonModeChoke_Coilcraft_1812CAN",
    ),
    "Connector:Micro_SD_Card": (
        "Connector_Card:microSD_HC_Molex_104031-0811",
        "Connector_Card:microSD_HC_Hirose_DM3AT-SF-PEJM5",
    ),
    "Jumper:Jumper_2_Open": (
        "Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical",
    ),
}
_FOOTPRINT_SEMANTIC_STOPWORDS = {
    "connector",
    "device",
    "footprint",
    "generic",
    "horizontal",
    "package",
    "socket",
    "smd",
    "smt",
    "tht",
    "vertical",
}


def _footprint_semantic_tokens(text: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z]+[a-z0-9]*", text.lower())
        if len(token) >= 2 and token not in _FOOTPRINT_SEMANTIC_STOPWORDS
    }


def _footprint_matches_symbol_family(lib_id: str, footprint: str) -> bool:
    """Reject high-confidence symbol/package family contradictions."""

    symbol_library = lib_id.partition(":")[0].lower()
    footprint_library = footprint.partition(":")[0].lower()
    if symbol_library in {"connector", "connector_generic"}:
        return footprint_library.startswith(("connector_", "terminalblock"))
    if symbol_library == "jumper":
        return footprint_library.startswith(
            ("jumper", "connector_pinheader", "connector_pinsocket")
        )
    if symbol_library.startswith(
        (
            "mcu_",
            "memory_",
            "sensor_",
            "interface_",
            "regulator_",
        )
    ):
        return not footprint_library.startswith(
            (
                "capacitor_",
                "connector_",
                "diode_",
                "inductor_",
                "led_",
                "resistor_",
                "terminalblock",
            )
        )
    return True


def _footprint_matches_part_family(
    part: SelectedPart,
    footprint: str,
) -> bool:
    """Apply high-confidence role constraints in addition to symbol family."""

    if not _footprint_matches_symbol_family(part.symbol, footprint):
        return False
    role = part.role.lower()
    footprint_library = footprint.partition(":")[0].lower()
    if any(
        token in role
        for token in ("jumper", "solder_bridge", "solder_link")
    ):
        return footprint_library.startswith(
            ("jumper", "connector_pinheader", "connector_pinsocket")
        )
    if role.endswith(("connector", "header", "receptacle", "socket")):
        return footprint_library.startswith(("connector_", "terminalblock"))
    if (
        _selection_controller_role(role)
        or any(token in role for token in ("accelerometer", "gyroscope", "sensor"))
    ):
        return not footprint_library.startswith(
            (
                "capacitor_",
                "connector_",
                "diode_",
                "inductor_",
                "jumper",
                "led_",
                "resistor_",
                "terminalblock",
            )
        )
    return True


def _compatible_footprint_hints(
    lib_id: str,
    query: str = "",
) -> list[str]:
    """Return installed footprints whose electrical pads match ``lib_id``."""
    symbol_pins = symbols.symbol_pins(lib_id) or []
    pin_numbers = {
        str(pin["number"]) for pin in symbol_pins if pin.get("number")
    }
    if not pin_numbers:
        return []
    connector = lib_id.startswith(("Connector:", "Connector_Generic:"))
    matches: list[str] = []
    for footprint in _COMPATIBLE_FOOTPRINT_HINTS.get(lib_id, ()):
        if not _footprint_matches_symbol_family(lib_id, footprint):
            continue
        pad_numbers = footprints.footprint_pad_numbers(footprint) or frozenset()
        if pad_numbers == pin_numbers or (
            connector and pin_numbers.issubset(pad_numbers)
        ):
            matches.append(footprint)
    if matches or not query:
        return matches

    wanted = _footprint_semantic_tokens(f"{lib_id} {query}")
    if not wanted:
        return []
    ranked: list[tuple[tuple[int, int, int, int], str]] = []
    exact_matches = 0
    for footprint in footprint_candidates(wanted, limit=128):
        if not _footprint_matches_symbol_family(lib_id, footprint):
            continue
        overlap = wanted & _footprint_semantic_tokens(footprint)
        if not overlap:
            continue
        pad_numbers = footprints.footprint_pad_numbers(footprint) or frozenset()
        exact = pin_numbers == pad_numbers
        compatible = exact or (
            connector and pin_numbers.issubset(pad_numbers)
        )
        if not compatible:
            continue
        ranked.append((
            (
                int(exact),
                len(overlap),
                -len(pad_numbers - pin_numbers),
                -len(footprint),
            ),
            footprint,
        ))
        if exact:
            exact_matches += 1
            # Candidates arrive in semantic relevance order. Once the result
            # limit is filled with exact electrical signatures, subset-only
            # connector matches cannot displace them.
            if exact_matches >= 12:
                break
    ranked.sort(reverse=True)
    return [footprint for _score, footprint in ranked[:12]]


def _requires_power_backfeed_protection(requirement: str) -> bool:
    """Recognize source-priority/backfeed intent across natural phrasing."""

    lower = requirement.lower()
    return bool(
        "外部直流输入优先" in requirement
        or "反向灌电" in requirement
        or "反灌" in requirement
        or "priority" in lower
        or "backfeed" in lower
        or "back-feed" in lower
        or "reverse current" in lower
    )


def _component_symbol_hints(
    requirement: str,
) -> dict[str, list[dict[str, object]]]:
    """Return identity-labelled hints from the live installed KiCad library."""

    return requirement_symbol_hints(
        _original_requirement(requirement),
        compatible_footprints=_compatible_footprint_hints,
    )


def _same_library_semantic_symbol(
    part: SelectedPart,
    pad_numbers: set[str],
) -> str | None:
    """Find one semantically clear, numbering-compatible sibling symbol."""

    library, _, current_name = part.symbol.partition(":")
    if not library or not current_name:
        return None

    current_tokens = _footprint_semantic_tokens(current_name)
    value_tokens = _footprint_semantic_tokens(part.value)
    role_tokens = _footprint_semantic_tokens(part.role)
    footprint_tokens = _footprint_semantic_tokens(part.footprint)
    context_tokens = (
        current_tokens | value_tokens | role_tokens | footprint_tokens
    )
    if not context_tokens:
        return None

    # The global symbol index is cached and cheap to scan. Bound candidates by
    # name semantics before parsing any symbol pins or rich properties.
    bounded: list[tuple[tuple[int, int, int, int], str, set[str]]] = []
    prefix = f"{library}:"
    for lib_id in grounding.symbol_index():
        if lib_id == part.symbol or not lib_id.startswith(prefix):
            continue
        name_tokens = _footprint_semantic_tokens(lib_id.partition(":")[2])
        if not name_tokens & context_tokens:
            continue
        bounded.append((
            (
                len(name_tokens & role_tokens),
                len(name_tokens & value_tokens),
                len(name_tokens & footprint_tokens),
                len(name_tokens & current_tokens),
            ),
            lib_id,
            name_tokens,
        ))
    bounded.sort(key=lambda item: (item[0], item[1]), reverse=True)

    ranked: list[tuple[tuple[int, int, int, int, int, int], str]] = []
    strong_tokens = value_tokens | role_tokens | footprint_tokens
    for _prefilter_score, lib_id, name_tokens in bounded[:32]:
        candidate_pins = symbols.symbol_pins(lib_id) or []
        candidate_numbers = {
            str(pin["number"]) for pin in candidate_pins if pin.get("number")
        }
        if candidate_numbers != pad_numbers:
            continue
        properties = symbols.symbol_properties(lib_id)
        property_tokens = _footprint_semantic_tokens(
            " ".join(str(value) for value in properties.values())
        )
        pin_tokens = _footprint_semantic_tokens(
            " ".join(str(pin.get("name", "")) for pin in candidate_pins)
        )
        candidate_tokens = name_tokens | property_tokens | pin_tokens
        if not candidate_tokens & context_tokens:
            continue
        ranked.append((
            (
                len(name_tokens & role_tokens),
                len(name_tokens & value_tokens),
                len(name_tokens & footprint_tokens),
                len(property_tokens & strong_tokens),
                len(name_tokens & current_tokens),
                len(pin_tokens & strong_tokens),
            ),
            lib_id,
        ))

    if not ranked:
        return None
    ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
    if len(ranked) > 1 and ranked[0][0] == ranked[1][0]:
        return None
    return ranked[0][1]


def _normalize_symbol_for_footprint(part: SelectedPart) -> str | None:
    """Choose a verified numbering-compatible generic symbol when unambiguous."""
    pads = footprints.footprint_pads(part.footprint) or []
    pad_numbers = {str(pad["number"]) for pad in pads if pad["number"]}
    if not pad_numbers:
        return None
    current_pins = symbols.symbol_pins(part.symbol) or []
    current_numbers = {
        str(pin["number"]) for pin in current_pins if pin["number"]
    }
    if current_numbers == pad_numbers:
        return None

    footprint_name = part.footprint.partition(":")[2]
    connector = re.search(
        r"(?:PinHeader|PinSocket)_(\d+)x(\d+)",
        footprint_name,
        re.IGNORECASE,
    )
    candidates: list[str] = []
    if connector:
        rows, columns = int(connector.group(1)), int(connector.group(2))
        candidates.append(
            f"Connector_Generic:Conn_01x{columns:02d}"
            if rows == 1
            else f"Connector_Generic:Conn_{rows:02d}x{columns:02d}_Odd_Even"
        )
    dip_switch = re.search(r"SW_DIP_SPSTx(\d+)", footprint_name, re.IGNORECASE)
    if dip_switch:
        candidates.append(f"Switch:SW_DIP_x{int(dip_switch.group(1)):02d}")
    if "Crystal" in part.symbol and "4Pin" in footprint_name:
        candidates.append("Device:Crystal_GND24")
    symbol_library = part.symbol.partition(":")[0]
    if symbol_library == "Switch":
        value_tokens = set(re.findall(r"[a-z0-9]+", part.value.lower()))
        role_tokens = set(re.findall(r"[a-z0-9]+", part.role.lower()))
        footprint_tokens = set(re.findall(r"[a-z0-9]+", footprint_name.lower()))

        def switch_score(lib_id: str) -> tuple[int, int, str]:
            name_tokens = set(
                re.findall(r"[a-z0-9]+", lib_id.partition(":")[2].lower())
            )
            score = (
                3 * len(name_tokens & value_tokens)
                + 2 * len(name_tokens & role_tokens)
                + len(name_tokens & footprint_tokens)
            )
            return score, -len(lib_id), lib_id

        compatible_switches = []
        for lib_id in grounding.symbol_index():
            if not lib_id.startswith(f"{symbol_library}:"):
                continue
            candidate_pins = symbols.symbol_pins(lib_id) or []
            candidate_numbers = {
                str(pin["number"]) for pin in candidate_pins if pin["number"]
            }
            if candidate_numbers == pad_numbers:
                compatible_switches.append(lib_id)
        if compatible_switches:
            best = max(compatible_switches, key=switch_score)
            if switch_score(best)[0] > 0:
                candidates.append(best)

    for candidate in candidates:
        candidate_pins = symbols.symbol_pins(candidate) or []
        candidate_numbers = {
            str(pin["number"]) for pin in candidate_pins if pin["number"]
        }
        if candidate_numbers == pad_numbers:
            return candidate
    return _same_library_semantic_symbol(part, pad_numbers)


def _grounded_vcap_uf(requirement: str) -> float | None:
    evidence = requirement.partition(_ARCHITECT_EVIDENCE_MARKER)[2]
    match = _VCAP_CEXT_RE.search(evidence)
    return float(match.group(1)) if match else None


def _capacitance_uf(value: str) -> float | None:
    match = _VCAP_VALUE_RE.fullmatch(value)
    if match:
        return float(match.group(1))
    engineering = _VCAP_ENGINEERING_RE.fullmatch(value)
    if engineering:
        return float(f"{engineering.group(1)}.{engineering.group(2)}")
    return None


def _symbol_power_pin_counts(lib_id: str) -> dict[str, int]:
    """Count real MCU supply pins by library pin name."""
    counts = {"VDD": 0, "VDDA": 0, "VBAT": 0, "VCAP": 0}
    for pin in symbols.symbol_pins(lib_id) or []:
        name = str(pin.get("name", "")).upper()
        if name == "VBAT":
            counts[name] += 1
        elif name == "VDDA" or "AVDD" in name:
            counts["VDDA"] += 1
        elif (
            name in {"3V3", "3V3_IN", "VCC"}
            or name == "VDD"
            or name.startswith("VDD")
            or name.endswith("VDD")
        ):
            counts["VDD"] += 1
        elif name.startswith("VCAP"):
            counts["VCAP"] += 1
    return counts


def _requires_per_supply_pin_decoupling(requirement: str) -> bool:
    original = _original_requirement(requirement)
    lower = original.lower()
    names_each_power_pin = (
        "每个电源引脚" in original
        or "each power pin" in lower
        or "each supply pin" in lower
    )
    specifies_100_nf = (
        "100 nf" in lower
        or "100nf" in lower
        or "0.1 uf" in lower
        or "0.1uf" in lower
    )
    return names_each_power_pin and specifies_100_nf


def _functional_connector_pin_requirement(
    requirement: str,
    part: SelectedPart,
) -> tuple[int, str] | None:
    """Translate an explicit interface role into its minimum real pin count."""
    original = _original_requirement(requirement)
    lower = original.lower()
    role = part.role.lower()
    is_connector = part.ref.upper().startswith("J")
    if (
        is_connector
        and "microsd" in lower
        and "microsd" in role
        and ("connector" in role or "socket" in role)
    ):
        return 9, "microSD socket"
    if (
        is_connector
        and "can" in lower
        and "can" in role
        and ("interface" in role or "connector" in role)
        and ("gnd" in lower or "ground" in lower or "接地" in original)
    ):
        return 3, "CANH/CANL/GND interface"
    if (
        is_connector
        and "swd" in lower
        and "swd" in role
        and (
            "10-pin" in lower
            or "10 pin" in lower
            or "10pin" in lower
        )
    ):
        return 10, "10-pin Cortex SWD interface"
    return None


def _normalize_grounded_values(
    parts: list[SelectedPart],
    requirement: str,
) -> None:
    expected_vcap = _grounded_vcap_uf(requirement)
    if expected_vcap is None:
        return
    for part in parts:
        if "vcap" in part.role.lower():
            part.value = f"{expected_vcap:g}uF"


def _identity_token(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def _specific_model_identity(value: str) -> str | None:
    identity = _identity_token(value)
    if (
        len(identity) >= 4
        and any(char.isalpha() for char in identity)
        and any(char.isdigit() for char in identity)
    ):
        return identity
    return None


_MODEL_VALUE_SEPARATOR_RE = re.compile(r"[\s_/,:;()[\]{}]+")


def _model_identity_candidates(value: str) -> set[str]:
    return {
        identity
        for token in _MODEL_VALUE_SEPARATOR_RE.split(value)
        if (identity := _specific_model_identity(token)) is not None
    }


def _model_token_is_contained(shorter: str, longer: str) -> bool:
    return bool(
        re.search(
            rf"(?<![A-Za-z0-9]){re.escape(shorter)}(?![A-Za-z0-9])",
            longer,
            re.IGNORECASE,
        )
    )


def _compatible_model_identity(first: str, second: str) -> bool:
    first_identity = _specific_model_identity(first)
    second_identity = _specific_model_identity(second)
    if first_identity is None or second_identity is None:
        return False
    if first_identity == second_identity:
        return True
    if _model_identity_candidates(first) & _model_identity_candidates(second):
        return True
    shorter, longer = sorted((first.strip(), second.strip()), key=len)
    return _model_token_is_contained(shorter, longer)


def _required_flash_mbit(requirement: str) -> float | None:
    requirement = _original_requirement(requirement)
    matches = re.findall(
        r"(?:至少|at\s+least|>=?)\s*(\d+(?:\.\d+)?)\s*Mbit",
        requirement,
        re.IGNORECASE,
    )
    return max((float(value) for value in matches), default=None)


def _flash_capacity_mbit(properties: dict[str, str]) -> float | None:
    description = properties.get("Description", "")
    match = re.search(r"(\d+(?:\.\d+)?)\s*Mbit", description, re.IGNORECASE)
    return float(match.group(1)) if match else None


def _specific_component_identity_error(
    part: SelectedPart,
    requirement: str,
) -> str | None:
    """Reject a display-value relabel of a different installed library device."""
    properties = symbols.symbol_properties(part.symbol)
    library_value = properties.get("Value", "")
    description = properties.get("Description", "")
    role = part.role.lower()
    is_ic = part.ref.upper().startswith("U")

    expected_library = ""
    expected_description = ""
    if is_ic and "flash" in role:
        expected_library = "Memory_Flash:"
        expected_description = "flash"
    elif is_ic and "accelerometer" in role:
        expected_library = "Sensor_Motion:"
        expected_description = "accelerometer"
    elif is_ic and (
        "regulator" in role or "dc_dc" in role or role.startswith("ldo")
    ):
        expected_library = "Regulator_"
    elif is_ic and "transceiver" in role and "can" in role:
        expected_library = "Interface_CAN_LIN:"
        expected_description = "can"
    elif is_ic and (
        "power_mux" in role or "power_path" in role
    ):
        expected_library = "Power_Management:"

    if expected_library and not part.symbol.startswith(expected_library):
        return (
            f"{part.ref} role {part.role!r} requires a {expected_library} "
            f"library device, but {part.symbol!r} is {description or 'unclassified'}"
        )
    if (
        expected_description
        and expected_description not in description.lower()
    ):
        return (
            f"{part.ref} role {part.role!r} is not supported by the real "
            f"library description for {part.symbol!r}: {description!r}"
        )

    is_indicator_led = (
        role.endswith("_led")
        and "current_limit" not in role
        and "resistor" not in role
    )
    is_critical_input_device = "reverse_polarity" in role
    generic_led = part.symbol == "Device:LED"
    identity_required = bool(expected_library) or (
        is_indicator_led and not generic_led
    ) or is_critical_input_device
    library_identity = _specific_model_identity(library_value)
    selected_identity = _specific_model_identity(part.value)
    generic_libraries = (
        "Connector:",
        "Connector_Generic:",
        "Device:",
        "Jumper:",
        "Simulation_SPICE:",
        "Switch:",
    )
    model_identity_required = bool(
        library_identity
        and selected_identity
        and not part.symbol.startswith(generic_libraries)
    )
    compatible_identity = _compatible_model_identity(
        library_value,
        part.value,
    )
    if (
        identity_required
        and library_value
        and not compatible_identity
    ):
        return (
            f"{part.ref} displays value {part.value!r}, but the installed "
            f"symbol {part.symbol!r} is the different device "
            f"{library_value!r}; select the real device instead of relabeling it"
        )
    if (
        model_identity_required
        and not compatible_identity
    ):
        return (
            f"{part.ref} displays value {part.value!r}, but the installed "
            f"symbol {part.symbol!r} is the different device "
            f"{library_value!r}; select the real device instead of relabeling it"
        )

    if is_ic and "flash" in role:
        required_mbit = _required_flash_mbit(requirement)
        actual_mbit = _flash_capacity_mbit(properties)
        if required_mbit is not None and (
            actual_mbit is None or actual_mbit < required_mbit
        ):
            return (
                f"{part.ref} must provide at least {required_mbit:g} Mbit, "
                f"but {part.symbol!r} proves "
                f"{actual_mbit if actual_mbit is not None else 'no'} Mbit "
                "in its real KiCad library description"
            )
    return None


def _required_input_rating_v(requirement: str) -> float | None:
    original = _original_requirement(requirement)
    ranges = re.findall(
        r"(\d+(?:\.\d+)?)\s*(?:-|–|—|to)\s*"
        r"(\d+(?:\.\d+)?)\s*V\b",
        original,
        re.IGNORECASE,
    )
    if not ranges:
        return None
    maximum = max(float(high) for _low, high in ranges)
    if "浪涌" in original or "surge" in original.lower():
        return maximum * 1.5
    return maximum


def _library_voltage_rating_v(part: SelectedPart) -> float | None:
    description = symbols.symbol_properties(part.symbol).get("Description", "")
    values = re.findall(r"(\d+(?:\.\d+)?)\s*V\b", description, re.IGNORECASE)
    return max((float(value) for value in values), default=None)


_DIFFERENTIAL_BUS_PATTERNS: dict[str, re.Pattern[str]] = {
    "can": re.compile(r"(?<![a-z0-9])can(?![a-z0-9])", re.IGNORECASE),
    "rs485": re.compile(
        r"(?<![a-z0-9])rs[\s_-]?485(?![a-z0-9])",
        re.IGNORECASE,
    ),
}
_SELECTABLE_TERMINATION_PATTERN = re.compile(
    r"\b(?:jumper|selectable|switchable|solder[\s_-]?bridge|link)\b"
    r"|跳线|可选(?:择)?|开关|焊桥",
    re.IGNORECASE,
)
_TERMINATION_VALUE_PATTERN = re.compile(
    r"(?<!\d)120\s*(?:ohm|r|Ω|ω|欧姆)?(?!\d)",
    re.IGNORECASE,
)
_TERMINATION_WORD_PATTERN = re.compile(
    r"\bterminat(?:e|ed|ion|or)?\b|终端|匹配",
    re.IGNORECASE,
)


def _role_bus_names(role: str) -> set[str]:
    """Return explicitly named differential buses in a semantic part role."""

    normalized = role.lower().replace("-", " ").replace("_", " ")
    return {
        bus
        for bus, pattern in _DIFFERENTIAL_BUS_PATTERNS.items()
        if pattern.search(normalized)
    }


def _required_selectable_termination_buses(requirement: str) -> set[str]:
    """Find buses explicitly requiring selectable 120-ohm termination."""

    original = _original_requirement(requirement)
    mentioned = {
        bus
        for bus, pattern in _DIFFERENTIAL_BUS_PATTERNS.items()
        if pattern.search(original)
    }
    required: set[str] = set()
    clauses = re.split(r"[\n。；;.!?]+", original)
    for clause in clauses:
        if not (
            _TERMINATION_VALUE_PATTERN.search(clause)
            and _SELECTABLE_TERMINATION_PATTERN.search(clause)
            and _TERMINATION_WORD_PATTERN.search(clause)
        ):
            continue
        required.update(
            bus
            for bus, pattern in _DIFFERENTIAL_BUS_PATTERNS.items()
            if pattern.search(clause)
        )
    if (
        not required
        and len(mentioned) == 1
        and _TERMINATION_VALUE_PATTERN.search(original)
        and _SELECTABLE_TERMINATION_PATTERN.search(original)
        and _TERMINATION_WORD_PATTERN.search(original)
    ):
        required.update(mentioned)
    return required


def _termination_parts_for_bus(
    parts: Iterable[SelectedPart],
    bus: str,
) -> list[SelectedPart]:
    """Return termination parts explicitly assigned to ``bus`` or bus-neutral."""

    selected: list[SelectedPart] = []
    for part in parts:
        role = part.role.lower()
        if "termination" not in role and "terminator" not in role:
            continue
        named_buses = _role_bus_names(role)
        if not named_buses or bus in named_buses:
            selected.append(part)
    return selected


def _is_120_ohm_termination_resistor(part: SelectedPart) -> bool:
    role = part.role.lower()
    resistor_like = part.ref.upper().startswith("R") or "resistor" in role
    return resistor_like and bool(_TERMINATION_VALUE_PATTERN.search(part.value))


def _is_termination_selector(part: SelectedPart) -> bool:
    role = part.role.lower()
    return (
        part.ref.upper().startswith(("JP", "SW"))
        or any(
            token in role
            for token in (
                "jumper",
                "switch",
                "selector",
                "enable",
                "link",
                "solder",
            )
        )
    )


_ANALOG_INPUT_PHRASE_RE = re.compile(
    r"\b(?:analog|analogue)(?:\s+(?:voltage|signal))?\s+inputs?\b"
    r"|\badc\s+inputs?\b"
    r"|\b(?:linear\s+)?(?:faders?|potentiometers?|pots?)\b"
    r"|模拟(?:量)?输入|(?:线性)?推子|电位器",
    re.IGNORECASE,
)
_EXTERNAL_ANALOG_QUALIFIER_RE = re.compile(
    r"\b(?:external|off[-\s]?board|field|connector|terminal|port|header|screw)\b"
    r"|外部|板外|现场|接口|接线|端子|连接器",
    re.IGNORECASE,
)
_ZERO_TO_TEN_V_RE = re.compile(
    r"(?<!\d)0\s*(?:[-–—~～]|to|至|到)\s*10\s*V\b",
    re.IGNORECASE,
)
_ENGLISH_NUMBER_RE = (
    r"(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|"
    r"twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|"
    r"nineteen|twenty(?:[-\s](?:one|two|three|four|five|six|seven|"
    r"eight|nine))?|thirty(?:[-\s](?:one|two))?)"
)
_CHINESE_NUMBER_RE = r"(?:[一二两三四五六七八九]?十[一二三四五六七八九]?|[一二两三四五六七八九])"
_ANALOG_COUNT_TOKEN_RE = (
    rf"(?:[1-9]|[12]\d|3[0-2]|{_ENGLISH_NUMBER_RE}|{_CHINESE_NUMBER_RE})"
)
_ANALOG_COUNT_PATTERNS = (
    re.compile(
        rf"(?<![\w])(?P<count>{_ANALOG_COUNT_TOKEN_RE})(?!\d)"
        r"\s*[-–—]?\s*(?:channels?|ch)\b",
        re.IGNORECASE,
    ),
    re.compile(
        rf"(?<![\w])(?P<count>{_ANALOG_COUNT_TOKEN_RE})(?!\d)\s+"
        r"(?:(?:external|off[-\s]?board)\s+"
        r"|0\s*(?:[-–—~～]|to|至|到)\s*10\s*V(?:\s+range)?\s+){0,3}"
        r"(?:(?:analog|analogue|adc)(?:\s+(?:voltage|signal))?\s+inputs?"
        r"|(?:linear\s+)?(?:faders?|potentiometers?|pots?))\b",
        re.IGNORECASE,
    ),
    re.compile(
        rf"(?P<count>{_ANALOG_COUNT_TOKEN_RE})\s*(?:路|个)"
        r"(?=[^。；;\n]{0,40}(?:模拟(?:量)?输入|(?:线性)?推子|电位器))",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:模拟(?:量)?输入|(?:线性)?推子|电位器)(?:通道)?"
        r"\s*(?:共|为|[:：])?\s*"
        rf"(?P<count>{_ANALOG_COUNT_TOKEN_RE})\s*(?:路|个)?",
        re.IGNORECASE,
    ),
)


@dataclass(frozen=True)
class _AnalogInputRequirement:
    channel_count: int
    requires_divider: bool
    requires_current_limit: bool
    requires_filter_cap: bool
    requires_overvoltage_protection: bool


def _count_token_value(token: str) -> int | None:
    normalized = token.strip().lower().replace("-", " ")
    if normalized.isdigit():
        value = int(normalized)
        return value if 1 <= value <= 32 else None

    english_units = {
        "one": 1,
        "two": 2,
        "three": 3,
        "four": 4,
        "five": 5,
        "six": 6,
        "seven": 7,
        "eight": 8,
        "nine": 9,
        "ten": 10,
        "eleven": 11,
        "twelve": 12,
        "thirteen": 13,
        "fourteen": 14,
        "fifteen": 15,
        "sixteen": 16,
        "seventeen": 17,
        "eighteen": 18,
        "nineteen": 19,
    }
    if normalized in english_units:
        return english_units[normalized]
    words = normalized.split()
    english_tens = {"twenty": 20, "thirty": 30}
    if words and words[0] in english_tens:
        value = english_tens[words[0]]
        if len(words) == 2:
            value += english_units.get(words[1], 99)
        return value if len(words) <= 2 and 1 <= value <= 32 else None

    chinese_digits = {
        "一": 1,
        "二": 2,
        "两": 2,
        "三": 3,
        "四": 4,
        "五": 5,
        "六": 6,
        "七": 7,
        "八": 8,
        "九": 9,
    }
    if normalized in chinese_digits:
        return chinese_digits[normalized]
    if "十" in normalized:
        tens_text, units_text = normalized.split("十", maxsplit=1)
        tens = chinese_digits.get(tens_text, 1) if tens_text else 1
        units = chinese_digits.get(units_text, 0) if units_text else 0
        value = tens * 10 + units
        return value if 1 <= value <= 32 else None
    return None


def _external_analog_input_requirement(
    requirement: str,
) -> _AnalogInputRequirement | None:
    """Extract explicit external analog-input intent without reading voltage as count."""

    original = _original_requirement(requirement)
    analog_clauses = [
        clause
        for clause in re.split(r"[\n。；;.!?]+", original)
        if _ANALOG_INPUT_PHRASE_RE.search(clause)
        and (
            _EXTERNAL_ANALOG_QUALIFIER_RE.search(clause)
            or _ZERO_TO_TEN_V_RE.search(clause)
        )
    ]
    if not analog_clauses:
        return None

    text = " ".join(analog_clauses)
    channel_count = 1
    for pattern in _ANALOG_COUNT_PATTERNS:
        match = pattern.search(text)
        if match is None:
            continue
        parsed = _count_token_value(match.group("count"))
        if parsed is not None:
            channel_count = parsed
            break

    requires_divider = bool(
        _ZERO_TO_TEN_V_RE.search(text)
        or re.search(r"\b(?:voltage\s+)?dividers?\b|分压", text, re.IGNORECASE)
    )
    requires_current_limit = bool(
        re.search(
            r"\bcurrent[-\s]?limit(?:ing|er|ors?)?\b|限流",
            text,
            re.IGNORECASE,
        )
    )
    requires_filter_cap = bool(
        re.search(
            r"\bRC(?:\s+low[-\s]?pass)?\s+(?:filters?|filtering|networks?)\b"
            r"|RC\s*滤波",
            text,
            re.IGNORECASE,
        )
    )
    requires_overvoltage_protection = bool(
        re.search(
            r"\b(?:over[-\s]?voltage|clamp(?:ing)?|TVS)\b"
            r"|过压|钳位",
            text,
            re.IGNORECASE,
        )
    )
    return _AnalogInputRequirement(
        channel_count=channel_count,
        requires_divider=requires_divider,
        requires_current_limit=requires_current_limit,
        requires_filter_cap=requires_filter_cap,
        requires_overvoltage_protection=requires_overvoltage_protection,
    )


def _analog_role_channel(role: str) -> int | None:
    lowered = role.lower()
    match = re.search(r"(?:^|_)analog_input_(\d+)(?:_|$)", lowered)
    if match is None:
        match = re.search(r"(?:^|_)analog_input_.*_(\d+)$", lowered)
    return int(match.group(1)) if match is not None else None


def _has_analog_support_role(
    parts: list[SelectedPart],
    channel: int,
    role_tokens: tuple[str, ...],
    ref_prefixes: tuple[str, ...],
) -> bool:
    return any(
        _analog_role_channel(part.role) == channel
        and any(token in part.role.lower() for token in role_tokens)
        and part.ref.upper().startswith(ref_prefixes)
        for part in parts
    )


def _selection_requirement_checks(
    requirement: str,
    parts: list[SelectedPart],
) -> list[CheckResult]:
    """Check explicit board requirements that cannot be inferred from pin count."""
    original = _original_requirement(requirement)
    lower = original.lower()
    roles = [part.role.lower() for part in parts]
    checks: list[CheckResult] = []

    for converter in (
        part
        for part in parts
        if part.ref.upper().startswith("U")
        and (
            "regulator_switching:" in part.symbol.lower()
            or "buck" in part.role.lower()
            or "switching_regulator" in part.role.lower()
        )
    ):
        pin_names = {
            str(pin.get("name", "")).upper()
            for pin in (symbols.symbol_pins(converter.symbol) or [])
        }
        related = [
            part
            for part in parts
            if part.ref != converter.ref
            and (
                "buck" in part.role.lower()
                or converter.ref.lower() in part.role.lower()
            )
        ]

        def roles_for(
            *tokens: str,
            candidates: list[SelectedPart] = related,
        ) -> list[SelectedPart]:
            return [
                part
                for part in candidates
                if all(token in part.role.lower() for token in tokens)
            ]

        def directional_capacitors(
            direction: str,
            candidates: list[SelectedPart] = related,
        ) -> list[SelectedPart]:
            return [
                part
                for part in candidates
                if direction in part.role.lower()
                and (
                    part.ref.upper().startswith("C")
                    or "capacitor" in part.role.lower()
                    or re.search(r"(?:^|_)cap(?:_|$)", part.role.lower())
                )
            ]

        missing_support: list[str] = []
        if not directional_capacitors("input"):
            missing_support.append("input capacitor")
        if not directional_capacitors("output"):
            missing_support.append("output capacitor")
        if not roles_for("inductor"):
            missing_support.append("inductor")
        bootstrap_capacitors = [
            part
            for part in related
            if "bootstrap" in part.role.lower()
            and (
                part.ref.upper().startswith("C")
                or "capacitor" in part.role.lower()
            )
        ]
        if any("BOOT" in name for name in pin_names) and not bootstrap_capacitors:
            missing_support.append("bootstrap capacitor")
        if any(name == "FB" or name.endswith("/FB") for name in pin_names):
            feedback_resistors = [
                part
                for part in related
                if (
                    "feedback" in part.role.lower()
                    or re.search(r"(?:^|_)fb(?:_|$)", part.role.lower())
                )
                and (
                    part.ref.upper().startswith("R")
                    or "resistor" in part.role.lower()
                )
            ]
            if len(feedback_resistors) < 2:
                missing_support.append("two feedback resistors")
        if any("RT" in name for name in pin_names) and not [
            part
            for part in related
            if any(
                token in part.role.lower()
                for token in ("timing", "rt_", "rtclk")
            )
            and (
                part.ref.upper().startswith("R")
                or "resistor" in part.role.lower()
            )
        ]:
            missing_support.append("RT/CLK timing resistor")
        if any("COMP" in name for name in pin_names):
            compensation = [
                part
                for part in related
                if "compensation" in part.role.lower()
                or re.search(
                    r"(?:^|_)comp(?:_|$)",
                    part.role.lower(),
                )
            ]
            has_comp_r = any(
                part.ref.upper().startswith("R")
                or "resistor" in part.role.lower()
                for part in compensation
            )
            has_comp_c = any(
                part.ref.upper().startswith("C")
                or "capacitor" in part.role.lower()
                for part in compensation
            )
            if not (has_comp_r and has_comp_c):
                missing_support.append("COMP resistor and capacitor")
        checks.append(CheckResult(
            name=f"switching_regulator_support_parts:{converter.ref}",
            ok=not missing_support,
            message=(
                f"{converter.ref} real symbol pins require explicit selected "
                f"support parts; missing={missing_support}"
            ),
        ))

    needs_sdio4 = "microsd" in lower and (
        "sdio" in lower or "4-bit" in lower or "4 bit" in lower
    )
    if needs_sdio4:
        required_pullups = {
            "sdio_cmd_pullup",
            "sdio_dat0_pullup",
            "sdio_dat1_pullup",
            "sdio_dat2_pullup",
            "sdio_dat3_pullup",
        }
        missing = sorted(required_pullups - set(roles))
        checks.append(CheckResult(
            name="microsd_sdio4_pullups",
            ok=not missing,
            message=f"missing required SDIO 4-bit pull-up roles: {missing}",
        ))
        has_esd = any(
            part.ref.upper().startswith(("D", "U"))
            and "microsd" in part.role.lower()
            and (
                "esd" in part.role.lower()
                or "tvs" in part.role.lower()
            )
            for part in parts
        )
        checks.append(CheckResult(
            name="microsd_esd_protection",
            ok=has_esd,
            message="microSD requires a dedicated ESD/TVS protection part",
        ))

    needs_can_common_mode = "can" in lower and (
        "共模" in original or "common-mode" in lower or "common mode" in lower
    )
    if needs_can_common_mode:
        has_can_filter = any(
            "can" in part.role.lower()
            and (
                "common_mode" in part.role.lower()
                or "commonmode" in part.role.lower()
                or "choke" in part.role.lower()
                or "cmc" in part.role.lower()
            )
            and len(symbols.symbol_pins(part.symbol) or []) >= 4
            and (
                part.symbol == "Device:L_Coupled"
                or "commonmode" in part.symbol.lower().replace("_", "")
                or "coupled" in part.symbol.lower()
            )
            for part in parts
        )
        checks.append(CheckResult(
            name="can_common_mode_protection",
            ok=has_can_filter,
            message="CANH/CANL require a dedicated common-mode filter/choke part",
        ))

    needs_can_tvs = (
        "canh" in lower
        and "canl" in lower
        and any(token in lower for token in ("tvs", "esd", "surge"))
    )
    if needs_can_tvs:
        can_tvs_channels = 0
        for part in parts:
            role = part.role.lower()
            if "can" not in role or not any(
                token in role for token in ("tvs", "esd", "protection")
            ):
                continue
            pin_count = len(symbols.symbol_pins(part.symbol) or [])
            can_tvs_channels += 1 if pin_count <= 2 else 2
        checks.append(CheckResult(
            name="can_differential_tvs_channels",
            ok=can_tvs_channels >= 2,
            message=(
                "CANH and CANL each require a real TVS/ESD protection channel; "
                f"grounded selection provides {can_tvs_channels}"
            ),
        ))

    for bus in sorted(_required_selectable_termination_buses(requirement)):
        termination_parts = _termination_parts_for_bus(parts, bus)
        has_resistor = any(
            _is_120_ohm_termination_resistor(part)
            for part in termination_parts
        )
        has_selector = any(
            _is_termination_selector(part)
            for part in termination_parts
        )
        checks.append(CheckResult(
            name=f"{bus}_selectable_termination_parts",
            ok=has_resistor and has_selector,
            message=(
                f"selectable {bus.upper()} termination requires both a real "
                "120-ohm resistor and a real two-terminal jumper/switch/link. "
                f"Use {bus}_termination_* roles (or bus-neutral termination "
                f"roles); selected refs={[p.ref for p in termination_parts]}"
            ),
        ))

    analog_requirement = _external_analog_input_requirement(original)
    if analog_requirement is not None:
        channel_count = analog_requirement.channel_count
        analog_connectors = [
            part
            for part in parts
            if any(
                token in part.role.lower()
                for token in ("analog", "fader", "potentiometer")
            )
            and any(
                token in part.role.lower()
                for token in ("connector", "terminal", "interface")
            )
        ]
        connector_pin_counts = [
            len(symbols.symbol_pins(part.symbol) or [])
            for part in analog_connectors
        ]
        has_analog_connector = (
            any(count >= channel_count + 1 for count in connector_pin_counts)
            or sum(count >= 2 for count in connector_pin_counts) >= channel_count
        )
        checks.append(CheckResult(
            name="analog_input_external_connector",
            ok=has_analog_connector,
            message=(
                f"{channel_count} external analog channel"
                f"{'s' if channel_count != 1 else ''} require either one "
                f">={channel_count + 1}-pin shared connector (channels plus "
                f"common return) or {channel_count} >=2-pin per-channel "
                "connectors; "
                "selected="
                f"{[(part.ref, count) for part, count in zip(
                    analog_connectors,
                    connector_pin_counts,
                    strict=True,
                )]}"
            ),
        ))

        required_channels = range(1, channel_count + 1)
        if analog_requirement.requires_divider:
            missing_dividers = [
                channel
                for channel in required_channels
                if not (
                    _has_analog_support_role(
                        parts,
                        channel,
                        ("divider_top", "divider_upper"),
                        ("R",),
                    )
                    and _has_analog_support_role(
                        parts,
                        channel,
                        ("divider_bottom", "divider_lower"),
                        ("R",),
                    )
                )
            ]
            checks.append(CheckResult(
                name="analog_input_divider_network",
                ok=not missing_dividers,
                message=(
                    "missing explicit divider-top/divider-bottom resistor roles "
                    f"for analog input channels: {missing_dividers}"
                ),
            ))
        if analog_requirement.requires_current_limit:
            missing_current_limit = [
                channel
                for channel in required_channels
                if not _has_analog_support_role(
                    parts,
                    channel,
                    ("current_limit",),
                    ("R",),
                )
            ]
            checks.append(CheckResult(
                name="analog_input_current_limit",
                ok=not missing_current_limit,
                message=(
                    "missing explicit current-limit resistor roles for analog "
                    f"input channels: {missing_current_limit}"
                ),
            ))
        if analog_requirement.requires_filter_cap:
            missing_filter_caps = [
                channel
                for channel in required_channels
                if not _has_analog_support_role(
                    parts,
                    channel,
                    ("filter_cap", "filtering_cap"),
                    ("C",),
                )
            ]
            checks.append(CheckResult(
                name="analog_input_rc_filter",
                ok=not missing_filter_caps,
                message=(
                    "missing explicit RC filter capacitor roles for analog "
                    f"input channels: {missing_filter_caps}"
                ),
            ))
        if analog_requirement.requires_overvoltage_protection:
            missing_protection = [
                channel
                for channel in required_channels
                if not _has_analog_support_role(
                    parts,
                    channel,
                    ("overvoltage", "clamp", "protection", "tvs"),
                    ("D", "U"),
                )
            ]
            checks.append(CheckResult(
                name="analog_input_overvoltage_protection",
                ok=not missing_protection,
                message=(
                    "missing explicit overvoltage/clamp protection for analog "
                    f"input channels: {missing_protection}"
                ),
            ))

    needs_power_priority = _requires_power_backfeed_protection(original)
    if needs_power_priority:
        has_power_path = any(
            part.ref.upper().startswith(("D", "Q", "U"))
            and any(
                token in part.role.lower()
                for token in (
                    "power_path",
                    "power_mux",
                    "source_priority",
                    "ideal_diode",
                    "reverse_blocking",
                    "oring",
                )
            )
            for part in parts
        )
        checks.append(CheckResult(
            name="dual_input_priority_and_backfeed",
            ok=has_power_path,
            message=(
                "dual-input design requires an explicit priority/backfeed-"
                "blocking power-path component"
            ),
        ))
    return checks


_TOPOLOGY_COVERAGE_STOPWORDS = {
    "block",
    "board",
    "connector",
    "external",
    "header",
    "interface",
    "passive",
    "physical",
    "using",
}


def _uncovered_topology_blocks(
    state: PipelineState,
    parts: list[SelectedPart],
) -> list[str]:
    """Find functional blocks with no semantic evidence in the selected BOM."""
    topology = state.artifact(PipelineStep.TOPOLOGY)
    if not isinstance(topology, TopologyPlan):
        return []
    part_tokens = [
        _semantic_role_tokens(
            f"{part.role} {part.value} {part.symbol} {part.footprint}"
        )
        for part in parts
    ]
    uncovered: list[str] = []
    for block in topology.blocks:
        tokens = _semantic_role_tokens(
            f"{block.name} {block.kind} {block.description}"
        ) - _TOPOLOGY_COVERAGE_STOPWORDS
        if tokens and not any(tokens & selected for selected in part_tokens):
            uncovered.append(block.name)
    return uncovered


def _normalize_footprint_for_symbol(part: SelectedPart) -> str | None:
    """Choose a grounded compatible footprint for known semantic parts/connectors."""
    symbol_pins = symbols.symbol_pins(part.symbol) or []
    pin_numbers = {
        str(pin["number"]) for pin in symbol_pins if pin.get("number")
    }
    current_pads = footprints.footprint_pads(part.footprint) or []
    current_numbers = {
        str(pad["number"]) for pad in current_pads if pad.get("number")
    }
    connector_symbol = part.symbol.startswith(
        ("Connector:", "Connector_Generic:")
    )
    current_compatible = (
        pin_numbers == current_numbers
        or (
            connector_symbol
            and bool(pin_numbers)
            and pin_numbers.issubset(current_numbers)
        )
    )
    current_semantic = _footprint_matches_part_family(
        part,
        part.footprint,
    )
    if current_compatible and current_semantic:
        return None

    declared_footprint = symbols.symbol_properties(part.symbol).get(
        "Footprint",
        "",
    )
    if (
        declared_footprint
        and declared_footprint != part.footprint
        and _footprint_matches_part_family(part, declared_footprint)
    ):
        declared_numbers = (
            footprints.footprint_pad_numbers(declared_footprint)
            or frozenset()
        )
        if pin_numbers == declared_numbers or (
            connector_symbol
            and bool(pin_numbers)
            and pin_numbers.issubset(declared_numbers)
        ):
            return declared_footprint

    semantic_candidates = _compatible_footprint_hints(
        part.symbol,
        f"{part.value} {part.role}",
    )
    semantic_candidates = [
        candidate
        for candidate in semantic_candidates
        if candidate != part.footprint
        and _footprint_matches_part_family(part, candidate)
    ]
    if semantic_candidates:
        wanted = _footprint_semantic_tokens(
            f"{part.symbol} {part.value} {part.role}"
        )

        def semantic_score(candidate: str) -> tuple[int, int]:
            candidate_tokens = _footprint_semantic_tokens(candidate)
            symbol_name = part.symbol.partition(":")[2].lower()
            candidate_name = candidate.partition(":")[2].lower()
            return (
                int(symbol_name in candidate_name),
                len(wanted & candidate_tokens),
            )

        ranked = sorted(
            ((semantic_score(candidate), candidate)
             for candidate in semantic_candidates),
            reverse=True,
        )
        best_score, best_candidate = ranked[0]
        if best_score > (0, 0) and (
            len(ranked) == 1 or ranked[1][0] != best_score
        ):
            return best_candidate

    # A missing footprint name can still carry a valid project-library
    # namespace.  Use that namespace only as a bounded fallback, and only
    # when it identifies one electrically compatible alternative; never rank
    # candidates from the erroneous package-name tokens themselves.
    current_library = part.footprint.partition(":")[0]
    if current_library:
        library_candidates = [
            candidate
            for candidate in _compatible_footprint_hints(
                part.symbol,
                f"{current_library} {part.value} {part.role}",
            )
            if candidate != part.footprint
            and candidate.partition(":")[0] == current_library
            and _footprint_matches_part_family(part, candidate)
        ]
        if len(library_candidates) == 1:
            return library_candidates[0]

    symbol_name = part.symbol.partition(":")[2]
    connector = re.match(r"Conn_(\d+)x(\d+)", symbol_name, re.IGNORECASE)
    if not connector:
        return None
    rows, columns = int(connector.group(1)), int(connector.group(2))
    # LLMs often emit the old shorthand ``Conn_02x08`` while current KiCad
    # libraries expose numbered variants such as ``Conn_02x08_Odd_Even``.
    # The connector dimensions still provide an unambiguous electrical pad
    # set; the following symbol-normalization pass replaces the shorthand
    # with the verified installed symbol.
    if not pin_numbers:
        pin_numbers = {str(number) for number in range(1, rows * columns + 1)}
    proposed_name = part.footprint.partition(":")[2]
    pitch_match = re.search(r"P(\d+(?:\.\d+)?)mm", proposed_name, re.IGNORECASE)
    pitch = pitch_match.group(1) if pitch_match else "2.54"
    styles = (
        ["PinSocket", "PinHeader"]
        if "PinSocket" in proposed_name
        else ["PinHeader", "PinSocket"]
    )
    for style in styles:
        library = f"Connector_{style}_{pitch}mm"
        candidate = (
            f"{library}:{style}_{rows}x{columns:02d}_P{pitch}mm_Vertical"
        )
        pads = footprints.footprint_pads(candidate) or []
        pad_numbers = {str(pad["number"]) for pad in pads if pad["number"]}
        if pad_numbers == pin_numbers:
            return candidate
    return None


_MAX_SELECTION_PARTS = 128


def _unjustified_speculative_support_parts(
    requirement: str,
    parts: list[SelectedPart],
) -> list[str]:
    """Find strong optional-support hallucinations not grounded in the request."""

    requirement_lower = _original_requirement(requirement).lower()
    requested_gpio_numbers = {
        match.group(1)
        for match in re.finditer(
            r"(?:gpio|io)\s*[-_]?\s*(\d+)",
            requirement_lower,
        )
    }
    measurement_requested = any(
        token in requirement_lower
        for token in (
            "voltage sense",
            "voltage monitor",
            "power sense",
            "power monitor",
            "measure voltage",
            "adc input",
            "analog input",
            "电压采样",
            "电压检测",
            "电源监测",
            "模拟输入",
        )
    )
    speculative: list[str] = []
    original_clauses = [
        clause.lower()
        for clause in re.split(r"[\n銆傦紱;.!?]+", _original_requirement(requirement))
    ]
    for part in parts:
        role = part.role.lower()
        gpio_match = re.search(r"(?:gpio|io)[-_]?(\d+)", role)
        if (
            gpio_match is not None
            and gpio_match.group(1) not in requested_gpio_numbers
            and any(
                token in role
                for token in (
                    "pullup",
                    "pull_up",
                    "pulldown",
                    "pull_down",
                    "decoupl",
                )
            )
        ):
            speculative.append(f"{part.ref}({part.role})")
            continue
        if (
            not measurement_requested
            and (
                "sense_" in role
                or role.endswith("_sense")
                or "voltage_divider" in role
            )
        ):
            speculative.append(f"{part.ref}({part.role})")
            continue
        if (
            "external" in role
            and "output" in role
            and any(token in role for token in ("connector", "port", "5v", "3v3"))
        ):
            rail_tokens = {
                token
                for token in ("5v", "3v3", "3.3v", "12v", "24v")
                if token in role
            }
            explicitly_requested = any(
                any(
                    token in clause
                    for token in (
                        "external output",
                        "output connector",
                        "output port",
                        "auxiliary output",
                        "澶栭儴杈撳嚭",
                        "杈撳嚭鎺ュ彛",
                    )
                )
                and (
                    not rail_tokens
                    or any(token in clause for token in rail_tokens)
                )
                for clause in original_clauses
            )
            if not explicitly_requested:
                speculative.append(f"{part.ref}({part.role})")
                continue

        # A support role that names a concrete IC pin/function must be
        # physically anchorable to a selected IC in the same semantic domain.
        # This catches repair-loop debris such as SENSOR_RESET parts around a
        # sensor whose real symbol exposes no RESET pin.
        marker_aliases = {
            "vddio": ("VDDIO", "VIO"),
            "reset": ("RESET", "NRST", "~RESET"),
            "addr": ("ADDR", "AD0", "SA0"),
            "interrupt": ("INT", "IRQ"),
            "int": ("INT", "IRQ"),
            "hold": ("HOLD", "~HOLD"),
            "wp": ("WP", "~WP"),
        }
        marker = next(
            (
                token
                for token in marker_aliases
                if re.search(rf"(?:^|_){token}(?:_|$)", role)
            ),
            None,
        )
        if marker is None or not any(
            token in role
            for token in ("pull", "decoupl", "resistor", "capacitor", "strap")
        ):
            continue
        prefix = role.partition(marker)[0].strip("_")
        domain_tokens = _semantic_role_tokens(prefix)
        if not domain_tokens:
            continue
        anchors = [
            candidate
            for candidate in parts
            if candidate.ref.upper().startswith("U")
            and candidate.ref != part.ref
            and bool(
                domain_tokens
                & _semantic_role_tokens(
                    f"{candidate.role} {candidate.value} {candidate.symbol}"
                )
            )
        ]
        if not anchors:
            continue
        real_names = {
            re.sub(r"[^A-Z0-9~]", "", str(pin.get("name", "")).upper())
            for anchor in anchors
            for pin in (symbols.symbol_pins(anchor.symbol) or [])
        }
        expected = {
            re.sub(r"[^A-Z0-9~]", "", name.upper())
            for name in marker_aliases[marker]
        }
        if not any(
            any(alias in real_name for alias in expected)
            for real_name in real_names
        ):
            speculative.append(f"{part.ref}({part.role})")
    return speculative


def _connector_part(part: SelectedPart) -> bool:
    role = part.role.lower()
    return (
        part.symbol.startswith(("Connector:", "Connector_Generic:"))
        or re.fullmatch(r"J\d+[A-Z]?", part.ref.upper()) is not None
        or any(
            role.endswith(suffix)
            for suffix in ("connector", "header", "receptacle", "socket")
        )
    )


def _advertised_connector_contacts(part: SelectedPart) -> int | None:
    """Read an explicit contact count from a connector's value/role."""

    if not _connector_part(part):
        return None
    for descriptor in (part.value, part.role):
        upper = descriptor.upper()
        matrix = re.search(
            r"(?:CONN|HEADER|SOCKET|RECEPTACLE|PINHEADER|PINSOCKET)?"
            r"[_ -]?0*(\d+)X0*(\d+)",
            upper,
        )
        if matrix:
            return int(matrix.group(1)) * int(matrix.group(2))
        jst = re.search(r"(?:^|[^A-Z0-9])B0*(\d+)B(?:[^A-Z0-9]|$)", upper)
        if jst:
            return int(jst.group(1))
        din = re.search(r"(?:^|[^A-Z0-9])DIN[_ -]?0*(\d+)", upper)
        if din:
            return int(din.group(1))
        pins = re.search(
            r"(?:^|[^A-Z0-9])0*(\d+)[_ -]?"
            r"(?:PIN|WAY|POSITION|POS)(?:[^A-Z0-9]|$)",
            upper,
        )
        if pins:
            return int(pins.group(1))
        contacts = re.search(
            r"(?:^|[_ -])0*(\d+)P(?:$|[_ -])",
            upper,
        )
        if contacts:
            return int(contacts.group(1))
    return None


def _connector_terminal_numbers(
    entries: Iterable[dict[str, object]],
) -> set[str]:
    """Return contact numbers, excluding obvious shield/mechanical pads."""

    numbers: set[str] = set()
    for entry in entries:
        number = str(entry.get("number", "")).strip()
        name = str(entry.get("name", "")).strip().lower()
        if not number:
            continue
        upper = number.upper()
        if (
            "shield" in name
            or re.fullmatch(r"(?:S|SH|MP|MH)\d*", upper)
        ):
            continue
        numbers.add(number)
    return numbers


def _functional_terminal_count_error(part: SelectedPart) -> str | None:
    """Validate arity explicitly promised by connector/jumper semantics."""

    role = part.role.lower()
    symbol_pins = symbols.symbol_pins(part.symbol) or []
    footprint_pads = footprints.footprint_pads(part.footprint) or []
    pins = _connector_terminal_numbers(symbol_pins)
    pads = _connector_terminal_numbers(footprint_pads)

    advertised = _advertised_connector_contacts(part)
    if advertised is not None and (
        len(pins) != advertised or len(pads) != advertised
    ):
        return (
            f"{part.ref} value/role advertises {advertised} electrical contacts, "
            f"but {part.symbol!r} has {len(pins)} electrical pins and "
            f"{part.footprint!r} has {len(pads)} numbered electrical pads. "
            "Select a connector symbol and footprint whose real contact count "
            "matches the advertised interface."
        )

    if not any(
        token in role
        for token in ("jumper", "solder_bridge", "solder_link")
    ):
        return None
    exact_two_terminal = any(
        token in role
        for token in ("termination", "bias", "solder_bridge", "solder_link")
    )
    maximum = 2 if exact_two_terminal else 3
    if 2 <= len(pins) <= maximum and 2 <= len(pads) <= maximum:
        return None
    return (
        f"{part.ref} role {part.role!r} promises a "
        f"{'two-terminal' if exact_two_terminal else 'two/three-terminal'} "
        f"jumper/link, but {part.symbol!r} has {len(pins)} electrical pins and "
        f"{part.footprint!r} has {len(pads)} numbered pads. Use a real Jumper "
        "symbol and a matching bounded jumper/header footprint."
    )


_ROLE_FAMILY_RULES: tuple[
    tuple[tuple[str, ...], tuple[str, ...], str],
    ...,
] = (
    (("flash",), ("memory_",), "memory/flash"),
    (("accelerometer", "gyroscope"), ("sensor_",), "sensor"),
    (("optocoupler", "isolator"), ("isolator",), "isolator"),
    (("transceiver",), ("interface_",), "interface transceiver"),
    (("charger",), ("battery_management", "power_management"), "charger"),
    (
        ("regulator", "ldo", "dc_dc"),
        ("regulator_", "power_management", "converter_dcdc"),
        "power regulator",
    ),
    (("driver",), ("driver_",), "driver"),
)


def _selection_controller_role(role: str) -> bool:
    return (
        role in {"mcu", "controller", "mcu_controller", "microcontroller"}
        or role.endswith("_mcu")
    )


def _is_processor_bearing_module(symbol: str, description: str) -> bool:
    library = symbol.partition(":")[0].lower()
    module_evidence = "module" in library or bool(
        re.search(
            r"\b(?:compute|controller|processor|mcu|soc)\s+module\b",
            description,
        )
    )
    processor_evidence = bool(
        re.search(
            r"\b(?:soc|cpu|mcu|microprocessor|microcontroller|processor)\b"
            r"|\bsystem[- ]on[- ]chip\b",
            description,
        )
    )
    return module_evidence and processor_evidence


def _is_discrete_load_driver(
    role: str,
    symbol: str,
) -> bool:
    load_tokens = (
        "coil",
        "high_side",
        "load",
        "low_side",
        "relay",
        "solenoid",
        "switch",
    )
    return (
        "driver" in role
        and any(token in role for token in load_tokens)
        and symbol.startswith(("transistor_bjt:", "transistor_fet:"))
    )


def _role_symbol_family_error(part: SelectedPart) -> str | None:
    """Reject only high-confidence role/symbol-family contradictions."""

    role = part.role.lower()
    symbol = part.symbol.lower()
    description = symbols.symbol_properties(part.symbol).get(
        "Description",
        "",
    ).lower()
    support_tokens = (
        "capacitor",
        "decoupling",
        "pullup",
        "pulldown",
        "resistor",
        "termination",
        "filter",
        "esd",
        "tvs",
    )
    primary_part = (
        part.ref.upper().startswith(("U", "Q", "ENC"))
        and not any(token in role for token in support_tokens)
    )

    if _connector_part(part) and not part.symbol.startswith(
        ("Connector:", "Connector_Generic:")
    ):
        return (
            f"{part.ref} role {part.role!r} is a connector interface, but "
            f"{part.symbol!r} is not a connector symbol family"
        )

    if (
        primary_part
        and "encoder" in role
        and "encoder" not in f"{symbol} {description}"
    ):
        return (
            f"{part.ref} role {part.role!r} requires an encoder symbol, but "
            f"{part.symbol!r} is {description or 'a different device family'}"
        )

    if not primary_part:
        return None
    if _selection_controller_role(role):
        if (
            symbol.startswith("mcu_")
            or _is_processor_bearing_module(symbol, description)
        ):
            return None
        return (
            f"{part.ref} role {part.role!r} requires the MCU symbol family "
            "or a real processor-bearing module, but selected "
            f"{part.symbol!r} "
            f"({description or 'unclassified'})"
        )
    if _is_discrete_load_driver(role, symbol):
        return None
    for role_tokens, symbol_prefixes, family_name in _ROLE_FAMILY_RULES:
        if not any(token in role for token in role_tokens):
            continue
        if any(symbol.startswith(prefix) for prefix in symbol_prefixes):
            return None
        return (
            f"{part.ref} role {part.role!r} requires the {family_name} symbol "
            f"family, but selected {part.symbol!r} "
            f"({description or 'unclassified'})"
        )
    return None


def _connector_footprint_family_error(part: SelectedPart) -> str | None:
    if not part.symbol.startswith(("Connector:", "Connector_Generic:")):
        return None
    footprint_library = part.footprint.partition(":")[0].lower()
    conflicting_families = (
        "capacitor_",
        "converter_",
        "crystal",
        "diode_",
        "inductor_",
        "led_",
        "package_",
        "resistor_",
        "sensor_",
        "transformer_",
    )
    if footprint_library.startswith(conflicting_families):
        return (
            f"{part.ref} uses connector symbol {part.symbol!r}, but "
            f"{part.footprint!r} belongs to a non-connector footprint family; "
            "select a real connector footprint family or a documented "
            "project-local connector footprint"
        )
    return None


def _package_role_semantic_error(
    requirement: str,
    part: SelectedPart,
) -> str | None:
    """Reject real-but-wrong package families selected by fuzzy grounding."""

    role = part.role.lower()
    package = f"{part.symbol} {part.footprint}".lower()
    original = _original_requirement(requirement).lower()
    role_error = _role_symbol_family_error(part)
    if role_error:
        return role_error
    footprint_error = _connector_footprint_family_error(part)
    if footprint_error:
        return footprint_error
    if (
        "microsd" in role
        and any(token in role for token in ("socket", "connector"))
        and "microsd" not in package
    ):
        hints = _compatible_footprint_hints(
            "Connector:Micro_SD_Card",
            "microSD socket",
        )
        return (
            f"{part.ref} role {part.role!r} requires a physical microSD socket; "
            f"{part.footprint!r} is a different card/package family. Grounded "
            f"microSD candidates: {hints or 'none found'}"
        )
    if (
        any(token in role for token in ("debug_connector", "swd_connector", "jtag_connector"))
        and "din41612" in package
        and "din41612" not in original
    ):
        return (
            f"{part.ref} role {part.role!r} is a compact programming/debug "
            "interface, but fuzzy grounding selected a DIN41612 backplane "
            "connector that the requirement never requested"
        )
    return None


def _ground_selected_parts(
    parts: list[SelectedPart],
    requirement: str,
) -> None:
    """Ground selected parts to installed libraries and trusted catalog data."""
    symbol_library_available = config.symbol_dir() is not None
    footprint_library_available = config.footprint_dir() is not None
    for part in parts:
        if symbol_library_available:
            grounded_symbol = grounding.ground_symbol(part.symbol)
            if grounded_symbol:
                part.symbol = grounded_symbol
        if footprint_library_available:
            grounded_footprint = grounding.ground_footprint(part.footprint)
            if grounded_footprint is not None:
                part.footprint = grounded_footprint
        if symbol_library_available and footprint_library_available:
            default_footprint = symbols.symbol_properties(part.symbol).get(
                "Footprint",
                "",
            )
            if (
                default_footprint
                and footprints.footprint_pads(default_footprint) is not None
            ):
                part.footprint = default_footprint
            compatible_footprint = _normalize_footprint_for_symbol(part)
            if compatible_footprint:
                part.footprint = compatible_footprint
            compatible_symbol = _normalize_symbol_for_footprint(part)
            if compatible_symbol:
                part.symbol = compatible_symbol
    _normalize_grounded_values(parts, requirement)
    for part in parts:
        part.mpn = ""
        part.lcsc = ""
    _ground_mpns(parts)


def _apply_selection_patch(
    plan: SelectionPlan,
    patch: SelectionPatch,
) -> SelectionPlan:
    """Merge a bounded part delta while preserving every unrelated selection."""
    removals = {ref.upper() for ref in patch.remove_refs}
    upserts = {
        part.ref.upper(): part.model_copy(
            deep=True,
            update={
                "requested_identity": "",
                "identity_mode": "",
                "identity_provenance": "",
                "resolution_status": "",
                "resolution_detail": "",
                "release_ready": False,
                "dnp": False,
                "unresolved": False,
            },
        )
        for part in patch.upsert_parts
    }
    parts: list[SelectedPart] = []
    for existing in plan.parts:
        key = existing.ref.upper()
        if key in removals:
            continue
        replacement = upserts.pop(key, None)
        if replacement is not None:
            replacement.requested_identity = (
                existing.requested_identity or existing.value
            )
            replacement.identity_mode = (
                existing.identity_mode or "capability_only"
            )
            replacement.identity_provenance = (
                existing.identity_provenance or "selection_proposal"
            )
        parts.append(
            replacement
            if replacement is not None
            else existing.model_copy(deep=True)
        )
    parts.extend(upserts.values())
    return SelectionPlan(
        parts=parts,
        rationale=patch.rationale or plan.rationale,
    )


def _state_identity_constraints(
    state: PipelineState,
) -> tuple[ComponentIdentityConstraint, ...]:
    requirements = state.artifact(PipelineStep.REQUIREMENTS)
    if isinstance(requirements, RequirementSpec):
        return tuple(requirements.component_identity_constraints)
    return tuple(extract_component_identity_constraints(state.requirement_text))


def _close_component_libraries(
    plan: SelectionPlan,
    ctx: PipelineContext | None = None,
    *,
    preserve_requested_identities: bool = False,
    identity_constraints: Iterable[ComponentIdentityConstraint] = (),
) -> LibraryClosureResult:
    """Resolve every physical part before connectivity can consume the BOM."""

    service = ComponentResolutionService(
        project_dir=ctx.out_dir if ctx is not None else None,
    )
    constraints = tuple(identity_constraints)
    trusted_identities: dict[str, str] = {}
    trusted_modes: dict[str, str] = {}
    trusted_provenance: dict[str, str] = {}
    fixed_refs: set[str] = set()
    allow_equivalent_refs: set[str] = set()
    for part in plan.parts:
        if preserve_requested_identities and part.requested_identity:
            trusted_identities[part.ref] = part.requested_identity
        if preserve_requested_identities and part.identity_mode:
            trusted_modes[part.ref] = part.identity_mode
        if preserve_requested_identities and part.identity_provenance:
            trusted_provenance[part.ref] = part.identity_provenance
        constraint = identity_constraint_for_part(part, constraints)
        if constraint is None:
            continue
        trusted_identities[part.ref] = constraint.requested_identity
        trusted_modes[part.ref] = constraint.mode
        trusted_provenance[part.ref] = constraint.provenance
        if constraint.mode == "fixed_exact":
            fixed_refs.add(part.ref)
        if constraint.allow_equivalent:
            allow_equivalent_refs.add(part.ref)
    return service.close(
        plan.parts,
        pin_evidence_by_ref=(
            ctx.component_pin_evidence
            if ctx is not None
            else None
        ),
        trusted_requested_identities=trusted_identities,
        trusted_identity_modes=trusted_modes,
        trusted_identity_provenance=trusted_provenance,
        fixed_identity_refs=fixed_refs,
        allow_equivalent_refs=allow_equivalent_refs,
        allow_unverified_placeholders=bool(
            ctx is not None and ctx.artifact_first
        ),
    )


def _library_closure_diagnostics(
    closure: LibraryClosureResult,
) -> list[CheckResult]:
    """Retain per-ref evidence as warnings while exposing one blocking cause."""

    checks: list[CheckResult] = []
    for item in closure.execution_blockers:
        if item.reason_code == "symbol_not_installed":
            name = f"symbol:{item.ref}"
        elif "footprint" in item.reason_code:
            name = f"footprint:{item.ref}"
        elif "pin_pad" in item.reason_code:
            name = f"pin_pad_compatibility:{item.ref}"
        elif "identity" in item.reason_code:
            name = f"component_identity:{item.ref}"
        else:
            name = f"component_resolution:{item.ref}"
        checks.append(CheckResult(
            name=name,
            ok=False,
            severity=Severity.WARNING,
            message=item.detail,
        ))
    return checks


class SelectionStep(PipelineStepBase):
    step = PipelineStep.SELECTION
    knowledge_role = "selection"
    repair_strategy_id = "bounded_selection_patch"
    allow_artifact_first_design_repair = True

    def knowledge_query(self, state: PipelineState) -> str | None:
        return f"component selection and packages for: {state.requirement_text}"

    def prepare_resumed_artifact(
        self,
        state: PipelineState,
        artifact: BaseModel,
    ) -> BaseModel:
        assert isinstance(artifact, SelectionPlan)
        _ground_selected_parts(artifact.parts, state.requirement_text)
        _close_component_libraries(
            artifact,
            preserve_requested_identities=True,
            identity_constraints=_state_identity_constraints(state),
        )
        return artifact

    def propose(
        self, state: PipelineState, ctx: PipelineContext, knowledge: str
    ) -> tuple[BaseModel, bool]:
        def fallback() -> SelectionPlan:
            extracted = params_from_requirement(state.requirement_text)
            try:
                params = Atmega328Params.model_validate(extracted)
            except Exception:
                params = Atmega328Params()
            ir = build_ir(params)
            parts = [
                SelectedPart(
                    ref=c.ref, symbol=c.symbol, value=c.value,
                    footprint=c.footprint, role=c.role,
                )
                for c in ir.components
            ]
            return SelectionPlan(parts=parts, rationale="deterministic family selection")

        requested_mcus = sorted(_mcu_models(state.requirement_text))
        needs_llm_library_hints = (
            ctx.mode != LlmMode.OFFLINE
            and ctx.client is not None
            and config.symbol_dir() is not None
        )
        exact_mcu_symbols = (
            _requested_mcu_symbols(state.requirement_text)
            if needs_llm_library_hints
            else []
        )
        mcu_power_pin_counts = [
            {
                "symbol": candidate["symbol"],
                "counts": _symbol_power_pin_counts(candidate["symbol"]),
            }
            for candidate in exact_mcu_symbols
        ]
        component_hints = (
            _component_symbol_hints(state.requirement_text)
            if needs_llm_library_hints
            else {}
        )
        topology = state.artifact(PipelineStep.TOPOLOGY)
        topology_blocks = (
            [block.model_dump() for block in topology.blocks]
            if isinstance(topology, TopologyPlan)
            else []
        )
        system = (
            "You choose real components for the design. Return JSON with parts[] "
            "(ref, symbol as 'Lib:Name', value, footprint as 'Lib:Name', role) and "
            "rationale. Use only real KiCad symbols/footprints; do not invent MPNs. "
            f"Keep the response bounded: select at most {_MAX_SELECTION_PARTS} "
            "physical parts, include "
            "exactly ref/symbol/value/footprint/role per part, and keep rationale "
            "under 200 characters. "
            "Every MCU explicitly named in the requirement MUST appear as a selected "
            "part, with role='mcu'. Never substitute a different MCU family. Select "
            "enough protection channels for every protected signal; a two-pin TVS "
            "protects only one signal and cannot be shared across different nets. "
            "A displayed part value MUST identify the actual installed KiCad symbol "
            "device; never relabel a lower-capacity flash, optical receiver, regulator, "
            "or high-power LED as another component. "
            "Treat explicit connector contact counts and package families as "
            "contracts: a 2-contact connector cannot use a large header, a "
            "connector cannot use an IC package footprint, and an encoder role "
            "must use a real encoder symbol/footprint family. "
            "For an SDIO 4-bit microSD bus use "
            "roles sdio_cmd_pullup and sdio_dat0_pullup through "
            "sdio_dat3_pullup plus microsd_esd. For CAN common-mode protection use "
            "can_common_mode_choke. Treat external ADC-connected faders and "
            "potentiometers as external analog channels. Number external analog "
            "channels from 1 through the channel count. For each channel N whose "
            "requirements call for "
            "0-10 V scaling, current limiting, RC filtering, and overvoltage "
            "protection, select the corresponding physical parts with roles "
            "analog_input_N_divider_top, analog_input_N_divider_bottom, "
            "analog_input_N_current_limit, analog_input_N_filter_cap, and "
            "analog_input_N_overvoltage_protection. Do not share one physical "
            "support part across channels. For dual-source priority and backfeed "
            "blocking use an explicit power_mux, power_path_controller, ideal_diode, "
            "or reverse_blocking role. "
            "For every switching regulator, inspect its real symbol pins and select "
            "the complete support network. Use explicit buck_* roles for input and "
            "output capacitors and the inductor. When the symbol exposes BOOT, FB, "
            "RT/CLK, or COMP, also select a bootstrap capacitor, two feedback "
            "resistors, a timing resistor, and both a compensation resistor and "
            "capacitor. Do not defer these physical parts to the connection step. "
            "For N external analog inputs, faders, or potentiometers select real "
            "connectors using an analog_input_connector role: either one shared "
            "connector with at least "
            "N+1 pins (N signals plus a common return), or N separate connectors "
            "with at least two pins each. "
            "For an explicitly required 10-pin Cortex SWD interface use a real "
            "10-pin symbol. A CAN connector that exposes CANH, CANL, and GND needs "
            "at least three electrical pins. For every explicitly requested "
            "differential bus with selectable 120-ohm termination, select both a "
            "real 120-ohm resistor with role <bus>_termination_resistor and a real "
            "two-terminal jumper/switch/link with role <bus>_termination_jumper. "
            "Here <bus> is the actual requested interface such as can or rs485; "
            "never invent a bus that the requirement did not request. "
            "Use the real Micro_SD_Card socket "
            "symbol for microSD and a real four-pin coupled-inductor symbol for a "
            "CAN common-mode choke. Use the supplied installed-symbol power-pin "
            "counts as authoritative: create one mcu_vdd_decoupling_N part per "
            "real digital VDD pin and one mcu_vdda_decoupling_N part per real "
            "analog VDD pin. If those counts are unavailable, select only a small "
            "design-justified set; never fill the part budget with duplicates. "
            "Do not add generic per-GPIO pull resistors, per-GPIO capacitors, "
            "unrequested voltage-sense dividers, or convenience output connectors. "
            "A downstream netlist error is normally repaired by changing nets, not "
            "by inventing more physical support parts. "
            "Every topology block listed below must have a physical implementation "
            "in parts[]. "
            "Prefer the installed symbol candidates listed below over an unlisted "
            "alternative, and pair every symbol with a footprint having exactly the "
            "same electrical pin/pad numbers. Connector footprints may additionally "
            "contain mechanical or shield pads."
        )
        user = (
            f"Requirement:\n{state.requirement_text}\n\n"
            f"Required MCU models: {requested_mcus or 'none explicitly named'}\n"
            "Exact matching MCU symbols available in the installed KiCad library: "
            f"{exact_mcu_symbols or 'none found'}\n"
            "When an exact match is listed, use both its exact symbol ID and its "
            "non-empty library-defined footprint.\n"
            "Power-pin counts read from those exact installed MCU symbols: "
            f"{mcu_power_pin_counts or 'none'}\n"
            f"Required topology blocks: {topology_blocks or 'none'}\n"
            "Other installed KiCad symbol candidates matching named components: "
            f"{component_hints or 'none'}\n\n"
            f"Knowledge:\n{knowledge}"
        )
        plan, used = propose_structured(
            ctx, model=SelectionPlan, system=system, user=user, fallback=fallback
        )
        # Ground names and procurement data exactly as later selection deltas
        # are grounded before they can be merged into this plan.
        _ground_selected_parts(plan.parts, state.requirement_text)
        _close_component_libraries(
            plan,
            ctx,
            identity_constraints=_state_identity_constraints(state),
        )
        return plan, used

    def replan(
        self,
        state: PipelineState,
        ctx: PipelineContext,
        knowledge: str,
        artifact: BaseModel,
        feedback: str,
    ) -> tuple[BaseModel, bool]:
        """Use downstream evidence without destructively rewriting a valid BOM."""

        assert isinstance(artifact, SelectionPlan)
        candidate, used = self.repair(
            state,
            ctx,
            knowledge,
            artifact,
            [
                CheckResult(
                    name="downstream_replan_feedback",
                    ok=False,
                    message=feedback,
                )
            ],
        )
        assert isinstance(candidate, SelectionPlan)
        # The baseline has already passed every selection/library gate. A
        # downstream connectivity failure is evidence for a new netlist, not
        # evidence that unrelated grounded physical parts should disappear.
        # Preserve all existing refs; accept only genuinely new physical parts
        # when the downstream evidence explicitly says a part is missing.
        permits_addition = any(
            token in feedback.lower()
            for token in (
                "missing required physical part",
                "additional_parts:",
                "no semantic physical implementation",
            )
        )
        by_ref = {
            part.ref.upper(): part.model_copy(deep=True)
            for part in artifact.parts
        }
        if permits_addition:
            for part in candidate.parts:
                key = part.ref.upper()
                if key not in by_ref:
                    by_ref[key] = part.model_copy(deep=True)
        replanned = SelectionPlan(
            parts=list(by_ref.values()),
            rationale=candidate.rationale or artifact.rationale,
        )
        _close_component_libraries(
            replanned,
            ctx,
            preserve_requested_identities=True,
            identity_constraints=_state_identity_constraints(state),
        )
        return replanned, used

    def repair(
        self,
        state: PipelineState,
        ctx: PipelineContext,
        knowledge: str,
        artifact: BaseModel,
        checks: list[CheckResult],
    ) -> tuple[BaseModel, bool]:
        """Repair only failed component choices instead of regenerating the BOM."""
        assert isinstance(artifact, SelectionPlan)
        failed = "\n".join(
            f"- {check.name}: {check.message}"
            for check in checks
            if not check.ok and check.severity == Severity.ERROR
        )
        current_parts = "\n".join(
            f"{part.ref}: symbol={part.symbol!r}, value={part.value!r}, "
            f"footprint={part.footprint!r}, role={part.role!r}"
            for part in artifact.parts
        )
        failed_refs = {
            check.name.rpartition(":")[2].upper()
            for check in checks
            if not check.ok
            and check.severity == Severity.ERROR
            and ":" in check.name
        }
        if any(
            not check.ok
            and check.severity == Severity.ERROR
            and check.name == "component_library_closure"
            for check in checks
        ):
            failed_refs.update(
                part.ref.upper()
                for part in artifact.parts
                if part.resolution_status in {
                    ResolutionStatus.UNRESOLVED_EVIDENCE_GAP.value,
                    ResolutionStatus.HARNESS_FAILURE.value,
                }
            )
        grounded_repair_hints = {
            part.ref: failed_symbol_candidates(
                role=part.role,
                value=part.value,
                proposed_symbol=part.symbol,
                footprint=part.footprint,
                compatible_footprints=_compatible_footprint_hints,
            )
            for part in artifact.parts
            if part.ref.upper() in failed_refs
        }
        system = (
            "You repair a grounded PCB component selection using a bounded JSON "
            "delta. Return upsert_parts[], remove_refs[], and rationale. Upsert "
            "only missing or invalid physical parts and replace an existing part "
            "by reusing its ref. Remove a ref only when the failed check proves "
            "that physical part is invalid. Preserve all unrelated parts, refs, "
            "roles, symbols, footprints, and values. Use only real installed "
            "KiCad symbol and footprint IDs, keep numeric symbol pins compatible "
            "with footprint pads, and never invent MPN/LCSC/stock data. A candidate "
            "with direct_repair_allowed=false is discovery evidence only and MUST "
            "NOT replace the requested component without new authoritative identity "
            "and package evidence. Do not "
            "delete a required circuit merely to silence a check. Never add "
            "speculative per-GPIO pull resistors/capacitors, unrequested sensing "
            "circuits, or convenience connectors. If the failure is connectivity "
            "and the selected physical parts are already suitable, return an empty "
            "delta so the downstream netlist can be regenerated from feedback."
        )
        user = (
            f"Requirement:\n{state.requirement_text}\n\n"
            f"Failed bottom-line checks:\n{failed}\n\n"
            f"Current grounded selection:\n{current_parts}\n\n"
            "Installed candidates for failed refs, with authoritative identity "
            "and direct-repair labels:\n"
            f"{grounded_repair_hints or 'none'}\n\n"
            "Relevant installed symbol candidates:\n"
            f"{_component_symbol_hints(state.requirement_text) or 'none'}\n\n"
            f"Knowledge:\n{knowledge}"
        )
        # The bounded prompt above already contains the failed checks and the
        # current selection. Do not append PipelineStepBase's full rejected JSON
        # a second time.
        base_feedback = ctx.repair_feedback
        ctx.repair_feedback = ""
        try:
            patch, used = propose_structured(
                ctx,
                model=SelectionPatch,
                system=system,
                user=user,
                fallback=SelectionPatch,
            )
        finally:
            ctx.repair_feedback = base_feedback
        _ground_selected_parts(patch.upsert_parts, state.requirement_text)
        repaired = _apply_selection_patch(artifact, patch)
        _close_component_libraries(
            repaired,
            ctx,
            preserve_requested_identities=True,
            identity_constraints=_state_identity_constraints(state),
        )
        return repaired, used

    def check(self, state: PipelineState, artifact: BaseModel) -> list[CheckResult]:
        assert isinstance(artifact, SelectionPlan)
        checks: list[CheckResult] = [
            CheckResult(
                name="has_parts", ok=bool(artifact.parts),
                message="selection must contain at least one part",
            ),
            CheckResult(
                name="compact_part_count",
                ok=len(artifact.parts) <= _MAX_SELECTION_PARTS,
                severity=Severity.WARNING,
                message=(
                    f"selection has {len(artifact.parts)} parts; {_MAX_SELECTION_PARTS} "
                    "is a context-efficiency target, not an electrical release gate"
                ),
            ),
        ]
        identity_constraints = _state_identity_constraints(state)
        missing_identities = missing_fixed_identities(
            artifact.parts,
            identity_constraints,
        )
        checks.append(CheckResult(
            name="fixed_component_identities_present",
            ok=not missing_identities,
            # A missing user-fixed identity invalidates manufacturing release,
            # but artifact-first execution should still produce an editable
            # KiCad draft for human correction.
            blocks_execution=False,
            message=(
                "user-fixed component identities are absent from the selected "
                f"physical BOM: {missing_identities}"
            ),
        ))
        sym_root = config.symbol_dir()
        fp_root = config.footprint_dir()
        if sym_root is not None and fp_root is not None and artifact.parts:
            closure = _close_component_libraries(
                artifact,
                preserve_requested_identities=True,
                identity_constraints=_state_identity_constraints(state),
            )
            blockers = closure.execution_blockers
            closure_ok = closure.release_ready
            checks.append(CheckResult(
                name="component_library_closure",
                ok=closure_ok,
                message=(
                    "all selected physical parts resolve to real, "
                    "identity-safe symbols/footprints with compatible pin/pad sets"
                    if closure_ok
                    else "; ".join(item.detail for item in (
                        blockers
                        or [
                            resolution
                            for resolution in closure.resolutions
                            if not resolution.release_ready
                        ]
                    ))[:8_000]
                ),
                blocks_execution=bool(blockers),
            ))
            if blockers:
                # Do not derive topology, connectivity, or package symptoms
                # from a BOM whose component library is not closed.
                checks.extend(_library_closure_diagnostics(closure))
                return checks
        speculative = _unjustified_speculative_support_parts(
            state.requirement_text,
            artifact.parts,
        )
        checks.append(CheckResult(
            name="no_unrequested_speculative_support",
            ok=not speculative,
            message=(
                "generic per-GPIO or sensing support must be grounded in the "
                f"requirement; remove or justify these refs: {speculative}"
            ),
        ))
        uncovered_blocks = _uncovered_topology_blocks(state, artifact.parts)
        checks.append(CheckResult(
            name="topology_blocks_covered",
            ok=not uncovered_blocks,
            severity=(
                Severity.WARNING
                if artifact.rationale == "deterministic family selection"
                else Severity.ERROR
            ),
            message=(
                "selected BOM has no semantic physical implementation for "
                f"topology blocks: {uncovered_blocks}"
            ),
        ))
        requested_mcus = _mcu_models(state.requirement_text)
        matched_mcu_parts: list[SelectedPart] = []
        if requested_mcus:
            selected_models = _mcu_models(
                " ".join(f"{p.value} {p.symbol}" for p in artifact.parts)
            )
            family_matches = all(
                any(
                    requested in selected
                    or selected in requested
                    or _mcu_model_matches(requested, selected)
                    for selected in selected_models
                )
                for requested in requested_mcus
            )
            checks.append(CheckResult(
                name="requested_mcu_selected",
                ok=family_matches,
                message=(
                    f"requested MCU {sorted(requested_mcus)} is not present; "
                    f"selected MCU models: {sorted(selected_models)}"
                ),
            ))
            for p in artifact.parts:
                descriptor = f"{p.value} {p.symbol}"
                symbol_library = p.symbol.partition(":")[0].upper()
                if not (
                    p.role.strip().lower() == "mcu"
                    or symbol_library.startswith("MCU_")
                    or _MCU_MODEL_RE.search(descriptor)
                ):
                    continue
                part_models = _mcu_models(descriptor)
                if not any(
                    requested in selected
                    or selected in requested
                    or _mcu_model_matches(requested, selected)
                    for requested in requested_mcus
                    for selected in part_models
                ):
                    continue
                matched_mcu_parts.append(p)
                expected_footprint = symbols.symbol_properties(p.symbol).get(
                    "Footprint", ""
                )
                if expected_footprint:
                    checks.append(CheckResult(
                        name=f"mcu_footprint:{p.ref}",
                        ok=p.footprint == expected_footprint,
                        message=(
                            f"{p.symbol} requires library footprint "
                            f"{expected_footprint!r}, got {p.footprint!r}"
                        ),
                    ))
        if matched_mcu_parts:
            expected_vdd = sum(
                _symbol_power_pin_counts(part.symbol)["VDD"]
                for part in matched_mcu_parts
            )
            expected_vdda = sum(
                _symbol_power_pin_counts(part.symbol)["VDDA"]
                for part in matched_mcu_parts
            )
            vdd_parts = [
                part for part in artifact.parts
                if re.fullmatch(
                    r"mcu_vdd_decoupling(?:_\d+)?",
                    part.role.lower(),
                )
            ]
            vdda_parts = [
                part for part in artifact.parts
                if re.fullmatch(
                    r"mcu_vdda_decoupling(?:_\d+)?",
                    part.role.lower(),
                )
            ]
            if expected_vdd or expected_vdda:
                checks.append(CheckResult(
                    name="mcu_supply_decoupling_not_excessive",
                    ok=(
                        len(vdd_parts) <= expected_vdd
                        and len(vdda_parts) <= expected_vdda
                    ),
                    message=(
                        "numbered per-pin decoupling roles exceed real MCU supply "
                        f"pins: VDD expected at most {expected_vdd}, found "
                        f"{len(vdd_parts)}; VDDA expected at most {expected_vdda}, "
                        f"found {len(vdda_parts)}"
                    ),
                ))
            if _requires_per_supply_pin_decoupling(state.requirement_text):
                checks.append(CheckResult(
                    name="mcu_supply_decoupling_count",
                    ok=(
                        len(vdd_parts) == expected_vdd
                        and len(vdda_parts) == expected_vdda
                    ),
                    message=(
                        "real MCU symbol supply pins require one 100nF capacitor "
                        f"each: VDD expected {expected_vdd}, found {len(vdd_parts)}; "
                        f"VDDA expected {expected_vdda}, found {len(vdda_parts)}"
                    ),
                ))
        expected_vcap = _grounded_vcap_uf(state.requirement_text)
        if expected_vcap is not None:
            vcap_parts = [
                part for part in artifact.parts
                if "vcap" in part.role.lower()
            ]
            invalid_vcap = [
                f"{part.ref}={part.value}"
                for part in vcap_parts
                if _capacitance_uf(part.value) != expected_vcap
            ]
            checks.append(CheckResult(
                name="grounded_vcap_capacitance",
                ok=len(vcap_parts) >= 2 and not invalid_vcap,
                message=(
                    f"official architect evidence requires two "
                    f"{expected_vcap:g}uF VCAP capacitors; found "
                    f"{[f'{part.ref}={part.value}' for part in vcap_parts]}"
                ),
            ))
        checks.extend(
            _selection_requirement_checks(
                state.requirement_text,
                artifact.parts,
            )
        )
        if sym_root is None:
            checks.append(CheckResult(
                name="symbol_library_available", ok=False, severity=Severity.WARNING,
                message="KICAD_SYMBOL_DIR not configured; cannot verify symbol pins",
            ))
        if fp_root is None:
            checks.append(CheckResult(
                name="footprint_library_available", ok=False, severity=Severity.WARNING,
                message="KICAD_FOOTPRINT_DIR not configured; cannot verify pads",
            ))
        # Per-part existence checks only when the relevant library is configured.
        # Selection verifies the symbol/footprint EXIST; pin-level verification
        # is the job of the pin-mapping step. Zero-pin symbols (e.g. a mounting
        # hole) are valid, so we resolve existence rather than requiring pins.
        for p in artifact.parts:
            resolved = None
            if sym_root is not None:
                resolved = symbols.resolve_symbol(p.symbol)
                checks.append(CheckResult(
                    name=f"symbol:{p.ref}", ok=resolved is not None,
                    message=f"symbol {p.symbol!r} for {p.ref} not found in library",
                ))
                if resolved is not None:
                    identity_error = _specific_component_identity_error(
                        p,
                        state.requirement_text,
                    )
                    checks.append(CheckResult(
                        name=f"component_identity:{p.ref}",
                        ok=identity_error is None,
                        message=identity_error or (
                            f"{p.ref} value and role match real library device "
                            f"{p.symbol!r}"
                        ),
                    ))
                    functional_requirement = (
                        _functional_connector_pin_requirement(
                            state.requirement_text,
                            p,
                        )
                    )
                    if functional_requirement is not None:
                        required_pins, interface_name = functional_requirement
                        real_pin_count = len({
                            str(pin["number"])
                            for pin in (symbols.symbol_pins(p.symbol) or [])
                            if pin["number"]
                        })
                        checks.append(CheckResult(
                            name=f"functional_pin_count:{p.ref}",
                            ok=real_pin_count >= required_pins,
                            message=(
                                f"{p.ref} role {p.role!r} is the "
                                f"{interface_name} and requires at least "
                                f"{required_pins} real electrical pins; "
                                f"{p.symbol!r} provides {real_pin_count}"
                            ),
                        ))
                    role = p.role.lower()
                    input_facing = (
                        "reverse_polarity" in role
                        or (
                            p.ref.upper().startswith("U")
                            and "dc_dc" in role
                            and "5v" in role
                        )
                    )
                    required_rating = _required_input_rating_v(
                        state.requirement_text
                    )
                    if input_facing and required_rating is not None:
                        actual_rating = _library_voltage_rating_v(p)
                        checks.append(CheckResult(
                            name=f"input_voltage_rating:{p.ref}",
                            ok=(
                                actual_rating is not None
                                and actual_rating >= required_rating
                            ),
                            message=(
                                f"{p.ref} on the protected industrial input "
                                f"requires at least {required_rating:g}V rating; "
                                f"real library description for {p.symbol!r} "
                                f"proves {actual_rating if actual_rating is not None else 'no'}V"
                            ),
                        ))
            if fp_root is not None and p.footprint:
                pads = footprints.footprint_pads(p.footprint)
                checks.append(CheckResult(
                    name=f"footprint:{p.ref}", ok=pads is not None,
                    message=f"footprint {p.footprint!r} for {p.ref} not found in library",
                ))
                symbol_pins = symbols.symbol_pins(p.symbol) if sym_root is not None else None
                if symbol_pins and pads:
                    pin_numbers = {
                        str(pin["number"]) for pin in symbol_pins if pin["number"]
                    }
                    pad_numbers = {
                        str(pad["number"]) for pad in pads if pad["number"]
                    }
                    connector_with_extra_pads = (
                        p.symbol.startswith(("Connector:", "Connector_Generic:"))
                        and pin_numbers.issubset(pad_numbers)
                    )
                    compatible_numbers = (
                        pin_numbers == pad_numbers or connector_with_extra_pads
                    )
                    footprint_hints = (
                        []
                        if compatible_numbers
                        else _compatible_footprint_hints(
                            p.symbol,
                            f"{p.value} {p.footprint} {p.role}",
                        )
                    )
                    checks.append(CheckResult(
                        name=f"pin_pad_compatibility:{p.ref}",
                        ok=compatible_numbers,
                        message=(
                            f"{p.ref} ({p.symbol}) symbol pins "
                            f"{sorted(pin_numbers)} do not match "
                            f"footprint pads {sorted(pad_numbers)}. Select a real "
                            "installed device symbol whose numeric pins match the "
                            "footprint. Grounded compatible footprint candidates: "
                            f"{footprint_hints or 'none found'}."
                        ),
                    ))
                    terminal_error = _functional_terminal_count_error(p)
                    if terminal_error is not None or (
                        _advertised_connector_contacts(p) is not None
                        or any(
                            token in p.role.lower()
                            for token in (
                                "jumper",
                                "solder_bridge",
                                "solder_link",
                            )
                        )
                    ):
                        checks.append(CheckResult(
                            name=f"functional_terminal_count:{p.ref}",
                            ok=terminal_error is None,
                            message=terminal_error or (
                                f"{p.ref} functional role matches its bounded "
                                "symbol/footprint terminal count"
                            ),
                        ))
                    package_error = _package_role_semantic_error(
                        state.requirement_text,
                        p,
                    )
                    if package_error is not None or _connector_part(p) or (
                        p.ref.upper().startswith(("U", "Q", "ENC"))
                    ):
                        checks.append(CheckResult(
                            name=f"package_role_semantics:{p.ref}",
                            ok=package_error is None,
                            message=package_error or (
                                f"{p.ref} package family matches role {p.role!r}"
                            ),
                        ))
        return checks

    def summarize(self, artifact: BaseModel) -> str:
        assert isinstance(artifact, SelectionPlan)
        grounded = sum(1 for p in artifact.parts if p.lcsc)
        return f"{len(artifact.parts)} parts ({grounded} grounded to a catalog MPN)"


_POWER_NET_HINTS = ("VBUS", "VCC", "VDD", "3V3", "5V", "3.3V", "VIN", "VBAT")


def _classify_net(name: str) -> str:
    upper = name.upper()
    if upper in {"GND", "GROUND", "VSS"}:
        return "ground"
    if any(h in upper for h in _POWER_NET_HINTS):
        return "power"
    if "XTAL" in upper or "CLK" in upper or "CLOCK" in upper:
        return "clock"
    return "signal"


def _looks_like_positive_rail(name: str) -> bool:
    """Recognise conventional positive-rail labels without matching sense nets."""

    tokens = [
        token
        for token in re.split(r"[^A-Z0-9]+", name.upper())
        if token
    ]
    if not tokens or any(
        token in {"ADC", "EN", "FB", "MON", "PG", "SENSE"}
        for token in tokens
    ):
        return False
    return any(
        re.fullmatch(
            r"(?:\d+V\d*|V(?:BUS|IN|BAT|CC|DD|DDA|REF|CORE)[A-Z0-9]*)",
            token,
        )
        is not None
        for token in tokens
    )


def _selected_parts_pin_block(selection: SelectionPlan | None) -> str:
    if selection is None:
        return ""
    lines: list[str] = []
    for part in selection.parts:
        pins = symbols.symbol_pins(part.symbol) or []
        shown = [
            (
                f"{pin['number']}={pin['name']}"
                if str(pin["name"]) not in ("", "~")
                else str(pin["number"])
            )
            for pin in pins
            if pin["number"]
        ]
        lines.append(
            f"{part.ref} role={part.role!r} value={part.value!r} "
            f"symbol={part.symbol!r} footprint={part.footprint!r} "
            f"pins=[{', '.join(shown) if shown else '(no pins)'}]"
        )
    return "\n".join(lines)


def _apply_netlist_patch(plan: NetlistIntent, patch: NetlistPatch) -> NetlistIntent:
    """Apply a bounded connection repair without regenerating valid nets."""
    removed_nets = {name.lower() for name in patch.remove_nets}
    nets = [
        net.model_copy(deep=True)
        for net in plan.nets
        if net.name.lower() not in removed_nets
    ]
    remove_keys = {pin.key().lower() for pin in patch.remove_pins}
    for net in nets:
        net.pins = [
            pin for pin in net.pins if pin.key().lower() not in remove_keys
        ]

    remove_no_connect_keys = {
        pin.key().lower() for pin in patch.remove_no_connect_pins
    }
    no_connect = {
        pin.key().lower(): pin.model_copy(deep=True)
        for pin in plan.no_connect_pins
        if pin.key().lower() not in remove_no_connect_keys
    }
    by_name = {net.name.lower(): net for net in nets}
    for update in patch.upsert_nets:
        target = by_name.get(update.name.lower())
        if target is None:
            target = NetIntent(
                name=update.name,
                kind=update.kind,
                pins=[],
                purpose=update.purpose,
            )
            nets.append(target)
            by_name[update.name.lower()] = target
        elif update.purpose:
            target.purpose = update.purpose
        for pin in update.pins:
            key = pin.key().lower()
            for net in nets:
                net.pins = [
                    existing
                    for existing in net.pins
                    if existing.key().lower() != key
                ]
            no_connect.pop(key, None)
            target.pins.append(pin.model_copy(deep=True))

    for pin in patch.add_no_connect_pins:
        key = pin.key().lower()
        for net in nets:
            net.pins = [
                existing
                for existing in net.pins
                if existing.key().lower() != key
            ]
        no_connect[key] = pin.model_copy(deep=True)

    nets = [net for net in nets if net.pins]
    net_names = {net.name for net in nets}
    supply_nets = [
        name
        for name in plan.supply_nets
        if name in net_names and name.lower() != plan.ground_net.lower()
    ]
    for update in patch.upsert_nets:
        if (
            update.kind == "power"
            and update.name.lower() != plan.ground_net.lower()
            and update.name not in supply_nets
        ):
            supply_nets.append(update.name)
    return NetlistIntent(
        additional_parts=patch.additional_parts,
        nets=nets,
        no_connect_pins=list(no_connect.values()),
        supply_nets=supply_nets,
        ground_net=plan.ground_net,
        rationale=plan.rationale,
    )


def _remove_unknown_netlist_refs(
    plan: NetlistIntent,
    selection: SelectionPlan | None,
) -> NetlistIntent:
    """Drop hallucinated pin references after a repair proposal.

    These references are not physical components: they occur in neither the
    grounded selection nor the connection step's explicit additional-parts
    delta. Removing only those invalid pin mentions lets the normal connectivity
    checks expose any net that now needs a real peer.
    """
    known_refs = {
        *(
            part.ref
            for part in (
                selection.parts if selection is not None else []
            )
        ),
        *(part.ref for part in plan.additional_parts),
    }
    return plan.model_copy(
        update={
            "nets": [
                net.model_copy(
                    update={
                        "pins": [
                            pin for pin in net.pins
                            if pin.ref in known_refs
                        ]
                    }
                )
                for net in plan.nets
            ],
            "no_connect_pins": [
                pin for pin in plan.no_connect_pins
                if pin.ref in known_refs
            ],
        },
        deep=True,
    )


def _remove_invalid_no_connect_pins(
    selection: SelectionPlan | None,
    plan: NetlistIntent,
) -> NetlistIntent:
    """Drop advisory NC entries absent from an otherwise grounded symbol."""
    parts = [
        *(selection.parts if selection is not None else []),
        *plan.additional_parts,
    ]
    by_ref = {part.ref: part for part in parts}
    valid: list[LogicalPin] = []
    for logical in plan.no_connect_pins:
        part = by_ref.get(logical.ref)
        if part is None:
            continue
        part_pins = symbols.symbol_pins(part.symbol) or []
        if not part_pins or _resolve_logical_pin(part_pins, logical.pin) is not None:
            valid.append(logical)
    return plan.model_copy(update={"no_connect_pins": valid}, deep=True)


def _normalize_declared_power_nets(plan: NetlistIntent) -> NetlistIntent:
    """Keep the explicit rail index consistent with the net kind declarations."""

    ground_names = {
        plan.ground_net.lower(),
        *(
            net.name.lower()
            for net in plan.nets
            if net.kind == "ground"
        ),
    }
    available = {net.name for net in plan.nets}
    supply: list[str] = []
    for name in [
        *plan.supply_nets,
        *(
            net.name
            for net in plan.nets
            if net.kind == "power" or _looks_like_positive_rail(net.name)
        ),
    ]:
        if (
            name in available
            and name.lower() not in ground_names
            and name not in supply
        ):
            supply.append(name)
    return plan.model_copy(update={"supply_nets": supply}, deep=True)


def _normalize_protocol_unused_pins(
    requirement: str,
    selection: SelectionPlan | None,
    plan: NetlistIntent,
) -> NetlistIntent:
    """Apply only protocol-defined NCs that are unambiguous from the request."""

    if selection is None:
        return plan
    original = _original_requirement(requirement).lower()
    microsd_spi = (
        "microsd" in original
        and "spi" in original
        and "sdio" not in original
    )
    if not microsd_spi:
        return plan
    sockets = [
        part
        for part in selection.parts
        if "microsd" in part.role.lower()
        and any(token in part.role.lower() for token in ("socket", "connector"))
    ]
    if not sockets:
        return plan
    forced: set[tuple[str, str]] = set()
    pins_by_ref: dict[str, list[dict[str, object]]] = {}
    for socket in sockets:
        physical = symbols.symbol_pins(socket.symbol) or []
        pins_by_ref[socket.ref] = physical
        for name in ("DAT1", "DAT2"):
            number = _resolve_logical_pin(physical, name)
            if number is not None:
                forced.add((socket.ref, number))

    nets: list[NetIntent] = []
    for net in plan.nets:
        kept: list[LogicalPin] = []
        for logical in net.pins:
            number = _resolve_logical_pin(
                pins_by_ref.get(logical.ref, []),
                logical.pin,
            )
            if (logical.ref, number or "") not in forced:
                kept.append(logical)
        if kept:
            nets.append(net.model_copy(update={"pins": kept}, deep=True))
    no_connects = {
        (logical.ref, logical.pin): logical.model_copy(deep=True)
        for logical in plan.no_connect_pins
        if (
            logical.ref,
            _resolve_logical_pin(
                pins_by_ref.get(logical.ref, []),
                logical.pin,
            )
            or logical.pin,
        )
        not in forced
    }
    for ref, number in forced:
        no_connects[(ref, number)] = LogicalPin(ref=ref, pin=number)
    return plan.model_copy(
        update={
            "nets": nets,
            "no_connect_pins": list(no_connects.values()),
        },
        deep=True,
    )


def _remove_no_connect_singleton_nets(
    selection: SelectionPlan | None,
    plan: NetlistIntent,
) -> NetlistIntent:
    """Remove electrically empty aliases for pins already proven unused."""

    if selection is None:
        return plan
    parts = {
        part.ref: part
        for part in [*selection.parts, *plan.additional_parts]
    }
    pins_by_ref = {
        ref: symbols.symbol_pins(part.symbol) or []
        for ref, part in parts.items()
    }
    no_connect = {
        (
            logical.ref,
            _resolve_logical_pin(
                pins_by_ref.get(logical.ref, []),
                logical.pin,
            )
            or logical.pin,
        )
        for logical in plan.no_connect_pins
    }
    nets = []
    for net in plan.nets:
        if len(net.pins) != 1:
            nets.append(net)
            continue
        logical = net.pins[0]
        key = (
            logical.ref,
            _resolve_logical_pin(
                pins_by_ref.get(logical.ref, []),
                logical.pin,
            )
            or logical.pin,
        )
        if key not in no_connect:
            nets.append(net)
    return plan.model_copy(update={"nets": nets}, deep=True)


def _normalize_additional_parts(
    selection: SelectionPlan | None,
    plan: NetlistIntent,
) -> NetlistIntent:
    """Make the connection-step part delta a case-insensitive ref upsert."""
    selected_refs = {
        part.ref.upper()
        for part in (selection.parts if selection is not None else [])
    }
    additions: dict[str, SelectedPart] = {}
    for part in plan.additional_parts:
        key = part.ref.upper()
        if key not in selected_refs:
            additions[key] = part.model_copy(deep=True)
    return plan.model_copy(
        update={"additional_parts": list(additions.values())},
        deep=True,
    )


def _normalize_standard_connector_no_connects(
    selection: SelectionPlan | None,
    plan: NetlistIntent,
) -> NetlistIntent:
    """Enforce reserved/no-connect pins of recognized standard connectors."""
    if selection is None:
        return plan
    swd_refs = {
        part.ref
        for part in [*selection.parts, *plan.additional_parts]
        if "swd" in part.role.lower()
        and len(symbols.symbol_pins(part.symbol) or []) >= 10
    }
    if not swd_refs:
        return plan

    forced_nc = {(ref, pin) for ref in swd_refs for pin in ("7", "8")}
    valid_swo_refs = {
        pin.ref
        for net in plan.nets
        if any(
            token in re.sub(r"[^A-Z0-9]", "", net.name.upper())
            for token in ("SWO", "JTDO")
        )
        for pin in net.pins
        if pin.ref in swd_refs and pin.pin == "6"
    }
    nets: list[NetIntent] = []
    for net in plan.nets:
        normalized_name = re.sub(r"[^A-Z0-9]", "", net.name.upper())
        pins: list[LogicalPin] = []
        for pin in net.pins:
            key = (pin.ref, pin.pin)
            if key in forced_nc:
                continue
            if (
                pin.ref in swd_refs
                and pin.pin == "6"
                and not any(token in normalized_name for token in ("SWO", "JTDO"))
            ):
                if pin.ref not in valid_swo_refs:
                    forced_nc.add(key)
                continue
            pins.append(pin)
        nets.append(net.model_copy(update={"pins": pins}, deep=True))

    no_connects = {
        (pin.ref, pin.pin): pin.model_copy(deep=True)
        for pin in plan.no_connect_pins
    }
    for ref, pin in forced_nc:
        no_connects[(ref, pin)] = LogicalPin(ref=ref, pin=pin)
    return plan.model_copy(
        update={
            "nets": nets,
            "no_connect_pins": list(no_connects.values()),
        },
        deep=True,
    )


def _complete_duplicate_connector_pins(
    selection: SelectionPlan | None,
    plan: NetlistIntent,
) -> NetlistIntent:
    """Join duplicated physical connector pins already proven to share a name."""

    if selection is None:
        return plan
    parts = [*selection.parts, *plan.additional_parts]
    nets = [net.model_copy(deep=True) for net in plan.nets]
    no_connects = {
        (pin.ref, pin.pin) for pin in plan.no_connect_pins
    }
    for part in parts:
        if not (
            part.ref.upper().startswith("J")
            or any(
                token in part.role.lower()
                for token in ("connector", "header", "interface")
            )
        ):
            continue
        physical = symbols.symbol_pins(part.symbol) or []
        by_name: dict[str, list[str]] = {}
        for pin in physical:
            name = re.sub(r"[^A-Z0-9]", "", str(pin.get("name", "")).upper())
            number = str(pin.get("number", ""))
            if name and number:
                by_name.setdefault(name, []).append(number)
        for numbers in by_name.values():
            if len(numbers) < 2:
                continue
            matching_nets = [
                net
                for net in nets
                if any(
                    pin.ref == part.ref
                    and _resolve_logical_pin(physical, pin.pin) in numbers
                    for pin in net.pins
                )
            ]
            if len({net.name for net in matching_nets}) != 1:
                continue
            target = matching_nets[0]
            connected = {
                _resolve_logical_pin(physical, pin.pin)
                for pin in target.pins
                if pin.ref == part.ref
            }
            for number in numbers:
                if number in connected or (part.ref, number) in no_connects:
                    continue
                target.pins.append(LogicalPin(ref=part.ref, pin=number))
    return plan.model_copy(update={"nets": nets}, deep=True)


def _normalize_grounded_crystal_pins(
    selection: SelectionPlan | None,
    plan: NetlistIntent,
) -> NetlistIntent:
    """Adapt a generic two-terminal crystal net to a grounded 4-pad symbol."""

    if selection is None:
        return plan
    nets = [net.model_copy(deep=True) for net in plan.nets]
    ground = next((net for net in nets if net.name == plan.ground_net), None)
    if ground is None:
        return plan
    for part in [*selection.parts, *plan.additional_parts]:
        if "crystal" not in part.role.lower():
            continue
        physical = symbols.symbol_pins(part.symbol) or []
        properties = symbols.symbol_properties(part.symbol)
        description = str(properties.get("Description", ""))
        match = re.search(
            r"GND\s+on\s+pins?\s+([A-Za-z0-9]+)\s+and\s+([A-Za-z0-9]+)",
            description,
            re.IGNORECASE,
        )
        if len(physical) != 4 or match is None:
            continue
        ground_numbers = {match.group(1), match.group(2)}
        all_numbers = {str(pin.get("number", "")) for pin in physical}
        signal_numbers = all_numbers - ground_numbers
        connected_signal_numbers: set[str] = set()
        wrong_signal_pins: list[tuple[NetIntent, LogicalPin, str]] = []
        for net in nets:
            if net.name == plan.ground_net:
                continue
            for logical in net.pins:
                if logical.ref != part.ref:
                    continue
                number = _resolve_logical_pin(physical, logical.pin)
                if number in signal_numbers:
                    connected_signal_numbers.add(str(number))
                elif number in ground_numbers:
                    wrong_signal_pins.append((net, logical, str(number)))
        missing_signal_numbers = signal_numbers - connected_signal_numbers
        if len(wrong_signal_pins) == 1 and len(missing_signal_numbers) == 1:
            net, logical, _ = wrong_signal_pins[0]
            replacement = next(iter(missing_signal_numbers))
            net.pins = [
                (
                    LogicalPin(ref=part.ref, pin=replacement)
                    if pin is logical
                    else pin
                )
                for pin in net.pins
            ]
        grounded = {
            _resolve_logical_pin(physical, pin.pin)
            for pin in ground.pins
            if pin.ref == part.ref
        }
        for number in sorted(ground_numbers - grounded):
            ground.pins.append(LogicalPin(ref=part.ref, pin=number))
    return plan.model_copy(update={"nets": nets}, deep=True)


def _is_usb_sink_cc_resistor(
    part: SelectedPart,
    channel: str,
) -> bool:
    """Recognize an explicitly selected USB-C sink Rd without board IDs."""

    role = part.role.lower()
    value = re.sub(r"[\sΩΩ]", "", part.value.lower())
    return (
        "usb" in role
        and channel.lower() in role
        and "resistor" in role
        and bool(re.fullmatch(r"(?:5[.]1k|5k1|5100)(?:ohm|r)?", value))
    )


def _normalize_usb_c_sink_cc(
    selection: SelectionPlan | None,
    plan: NetlistIntent,
) -> NetlistIntent:
    """Materialize every recognized sink CC pin as CC -- 5.1k Rd -- GND."""

    if selection is None:
        return plan
    parts = {part.ref: part for part in selection.parts}
    connectors = [
        part
        for part in selection.parts
        if "usb" in part.role.lower()
        and "connector" in part.role.lower()
    ]
    if not connectors:
        return plan
    pins_by_ref = {
        ref: symbols.symbol_pins(part.symbol) or []
        for ref, part in parts.items()
    }
    nets = [net.model_copy(deep=True) for net in plan.nets]

    def endpoint_number(pin: LogicalPin) -> str | None:
        return _resolve_logical_pin(
            pins_by_ref.get(pin.ref, []),
            pin.pin,
        )

    for connector in connectors:
        for channel in ("cc1", "cc2"):
            resistors = [
                part
                for part in selection.parts
                if _is_usb_sink_cc_resistor(part, channel)
            ]
            cc_number = _resolve_logical_pin(
                pins_by_ref.get(connector.ref, []),
                channel.upper(),
            )
            if len(resistors) != 1 or cc_number is None:
                continue
            resistor = resistors[0]
            resistor_numbers = sorted(
                {
                    str(pin.get("number", ""))
                    for pin in pins_by_ref.get(resistor.ref, [])
                    if pin.get("number")
                }
            )
            if len(resistor_numbers) != 2:
                continue
            existing_ground_number = next(
                (
                    number
                    for number in resistor_numbers
                    if any(
                        net.name == plan.ground_net
                        and any(
                            pin.ref == resistor.ref
                            and endpoint_number(pin) == number
                            for pin in net.pins
                        )
                        for net in nets
                    )
                ),
                None,
            )
            ground_number = (
                existing_ground_number or resistor_numbers[-1]
            )
            signal_number = next(
                number
                for number in resistor_numbers
                if number != ground_number
            )
            removed = {
                (connector.ref, cc_number),
                *((resistor.ref, number) for number in resistor_numbers),
            }
            target_name = f"USB_{channel.upper()}"
            target_pins: list[LogicalPin] = []
            for net in nets:
                kept = [
                    pin
                    for pin in net.pins
                    if (pin.ref, endpoint_number(pin)) not in removed
                ]
                if net.name == target_name:
                    target_pins.extend(kept)
                net.pins = kept
            ground = next(
                (net for net in nets if net.name == plan.ground_net),
                None,
            )
            if ground is None:
                ground = NetIntent(
                    name=plan.ground_net,
                    kind="ground",
                    pins=[],
                    purpose="common ground",
                )
                nets.append(ground)
            ground.pins.append(
                LogicalPin(ref=resistor.ref, pin=ground_number)
            )
            nets = [net for net in nets if net.name != target_name]
            nets.append(NetIntent(
                name=target_name,
                kind="signal",
                pins=[
                    *target_pins,
                    LogicalPin(ref=connector.ref, pin=cc_number),
                    LogicalPin(ref=resistor.ref, pin=signal_number),
                ],
                purpose=(
                    f"USB-C sink {channel.upper()} with independent 5.1k Rd"
                ),
            ))
    return plan.model_copy(
        update={"nets": [net for net in nets if net.pins]},
        deep=True,
    )


def _mark_evidently_safe_no_connects(
    selection: SelectionPlan | None,
    plan: NetlistIntent,
) -> NetlistIntent:
    """Mark only unused GPIO and explicitly reserved connector pins as NC."""

    if selection is None:
        return plan
    connected: set[tuple[str, str]] = set()
    for part in [*selection.parts, *plan.additional_parts]:
        physical = symbols.symbol_pins(part.symbol) or []
        for net in plan.nets:
            # A one-pin net is not an electrical connection. Treating it as
            # connected prevents an otherwise safe unused GPIO from being
            # normalized to an explicit no-connect.
            if len(net.pins) < 2:
                continue
            for logical in net.pins:
                if logical.ref != part.ref:
                    continue
                number = _resolve_logical_pin(physical, logical.pin)
                if number is not None:
                    connected.add((part.ref, number))
    no_connects = {
        (pin.ref, pin.pin): pin.model_copy(deep=True)
        for pin in plan.no_connect_pins
    }
    critical_names = re.compile(
        r"VDD|VSS|VCC|GND|VBAT|VCAP|AREF|RESET|NRST|BOOT|XTAL|OSC",
        re.IGNORECASE,
    )
    for part in [*selection.parts, *plan.additional_parts]:
        physical = symbols.symbol_pins(part.symbol) or []
        is_mcu = "mcu" in part.role.lower()
        is_connector = part.ref.upper().startswith("J")
        for pin in physical:
            number = str(pin.get("number", ""))
            name = str(pin.get("name", ""))
            pin_type = str(pin.get("type", "")).lower()
            if not number or (part.ref, number) in connected:
                continue
            safe_unused_gpio = (
                is_mcu
                and pin_type not in {"power_in", "power_out"}
                and not critical_names.search(name)
            )
            safe_reserved_connector = (
                is_connector
                and any(
                    token in name.upper()
                    for token in ("SHIELD", "SBU", "RESERVED", "NC")
                )
            )
            if safe_unused_gpio or safe_reserved_connector:
                no_connects[(part.ref, number)] = LogicalPin(
                    ref=part.ref,
                    pin=number,
                )
    return plan.model_copy(
        update={"no_connect_pins": list(no_connects.values())},
        deep=True,
    )


def _complete_evident_connector_power_pins(
    selection: SelectionPlan | None,
    plan: NetlistIntent,
) -> NetlistIntent:
    """Complete one connector power pin only when the net graph proves its rail."""
    if selection is None:
        return plan
    parts = {
        part.ref: part
        for part in [*selection.parts, *plan.additional_parts]
    }
    supply_by_lower = {name.lower(): name for name in plan.supply_nets}
    nets = [net.model_copy(deep=True) for net in plan.nets]
    pin_nets: dict[str, list[NetIntent]] = {}
    for net in nets:
        for pin in net.pins:
            pin_nets.setdefault(pin.ref, []).append(net)

    no_connect_keys = {
        (pin.ref, pin.pin)
        for pin in plan.no_connect_pins
    }
    for connector in parts.values():
        role = connector.role.lower()
        if not any(token in role for token in ("connector", "header", "interface")):
            continue
        physical_pins = symbols.symbol_pins(connector.symbol) or []
        if not 2 <= len(physical_pins) <= 16:
            continue
        connected_numbers = {
            number
            for net in nets
            for pin in net.pins
            if pin.ref == connector.ref
            and (
                number := _resolve_logical_pin(physical_pins, pin.pin)
            ) is not None
        }
        no_connect_numbers = {
            number
            for ref, logical in no_connect_keys
            if ref == connector.ref
            and (
                number := _resolve_logical_pin(physical_pins, logical)
            ) is not None
        }
        missing = [
            str(pin["number"])
            for pin in physical_pins
            if str(pin["number"]) not in connected_numbers
            and str(pin["number"]) not in no_connect_numbers
            and str(pin.get("type", "")).lower() != "no_connect"
        ]
        connector_nets = pin_nets.get(connector.ref, [])
        if len(missing) != 1 or any(
            net.name.lower() in supply_by_lower
            for net in connector_nets
        ):
            continue

        rail_votes: dict[str, int] = {}
        for signal_net in connector_nets:
            if signal_net.kind in {"power", "ground"}:
                continue
            for peer_pin in signal_net.pins:
                peer = parts.get(peer_pin.ref)
                if peer is None or peer.ref == connector.ref:
                    continue
                if len(symbols.symbol_pins(peer.symbol) or []) > 4:
                    continue
                for peer_net in pin_nets.get(peer.ref, []):
                    rail = supply_by_lower.get(peer_net.name.lower())
                    if rail is not None:
                        rail_votes[rail] = rail_votes.get(rail, 0) + 1
        ranked = sorted(
            rail_votes.items(),
            key=lambda item: (-item[1], item[0]),
        )
        if not ranked or ranked[0][1] < 2:
            continue
        if len(ranked) > 1 and ranked[0][1] == ranked[1][1]:
            continue
        target = next(
            net for net in nets if net.name.lower() == ranked[0][0].lower()
        )
        target.pins.append(
            LogicalPin(ref=connector.ref, pin=missing[0])
        )
        pin_nets.setdefault(connector.ref, []).append(target)

    return plan.model_copy(update={"nets": nets}, deep=True)


_REPAIR_ROLE_DOMAINS: dict[str, tuple[str, ...]] = {
    "buck": ("buck", "switching_regulator"),
    "power_mux": ("power_mux", "power_path", "pmux"),
    "flash": ("flash",),
    "accelerometer": ("accelerometer", "motion_sensor"),
    "usb": ("usb",),
    "can": ("can",),
    "rs485": ("rs485",),
    "led": ("led",),
    "swd": ("swd",),
    "debug": ("debug", "jtag", "uart"),
    "crystal": ("crystal", "oscillator"),
    "analog": ("analog",),
    "microsd": ("microsd", "sdio"),
    "i2c": ("i2c",),
}
_SEMANTIC_ROLE_STOPWORDS = {
    "board",
    "capacitor",
    "circuit",
    "component",
    "control",
    "controller",
    "device",
    "input",
    "interface",
    "output",
    "power",
    "protection",
    "resistor",
    "signal",
    "support",
}


def _semantic_role_tokens(text: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z][a-z0-9]{2,}", text.lower())
        if token not in _SEMANTIC_ROLE_STOPWORDS
    }


def _net_interface_prefix(name: str) -> str | None:
    """Return a conservative prefix for a visibly grouped interface net."""
    tokens = [
        token
        for token in re.split(r"[^A-Z0-9]+", name.upper())
        if token
    ]
    if len(tokens) < 2 or len(tokens[0]) < 2:
        return None
    return tokens[0]


def _connection_repair_scope(
    selection: SelectionPlan | None,
    plan: NetlistIntent,
    checks: list[CheckResult],
) -> tuple[set[str], set[str], str]:
    """Build a compact, failure-related repair context."""
    failure_text = "\n".join(
        f"- {check.name}: {check.message}"
        for check in checks
        if not check.ok and check.severity == Severity.ERROR
    )
    related_refs = set(re.findall(r"\b[A-Z]{1,4}\d+\b", failure_text))
    single_nets = {
        net.name for net in plan.nets
        if len(net.pins) < 2
    }
    single_prefixes = {
        prefix
        for name in single_nets
        if (prefix := _net_interface_prefix(name)) is not None
    }
    interface_nets = {
        net.name
        for net in plan.nets
        if (
            (prefix := _net_interface_prefix(net.name)) is not None
            and prefix in single_prefixes
        )
    }
    for net in plan.nets:
        if net.name in single_nets or net.name in interface_nets:
            related_refs.update(pin.ref for pin in net.pins)

    scope_text = " ".join(
        [
            failure_text.lower(),
            *(name.lower() for name in single_nets),
        ]
    )
    parts = selection.parts if selection is not None else []
    by_ref = {part.ref: part for part in [*parts, *plan.additional_parts]}
    for ref in tuple(related_refs):
        part = by_ref.get(ref)
        if part is not None:
            scope_text += f" {part.role.lower()}"

    dynamic_tokens = _semantic_role_tokens(scope_text)
    domains = {
        domain
        for domain, aliases in _REPAIR_ROLE_DOMAINS.items()
        if any(alias in scope_text for alias in aliases)
    }
    for part in by_ref.values():
        role = part.role.lower()
        if any(
            any(alias in role for alias in _REPAIR_ROLE_DOMAINS[domain])
            for domain in domains
        ) or (_semantic_role_tokens(role) & dynamic_tokens):
            related_refs.add(part.ref)
    mentioned_nets = {
        net.name
        for net in plan.nets
        if net.name.lower() in failure_text.lower()
    }
    small_related_refs = {
        ref
        for ref in related_refs
        if (
            (part := by_ref.get(ref)) is not None
            and len(symbols.symbol_pins(part.symbol) or []) <= 4
        )
    }
    relevant_nets = {
        net.name
        for net in plan.nets
        if net.name in single_nets
        or net.name in interface_nets
        or net.name in mentioned_nets
        or any(pin.ref in small_related_refs for pin in net.pins)
    }
    # A failed connector/interface mapping must show the repair model all nets
    # on that bounded connector, not only the one whose label was rejected.
    # Avoid expanding around large ICs/MCUs, which would make the patch unsafe.
    explicitly_failed_refs = set(
        re.findall(r"\b[A-Z]{1,4}\d+\b", failure_text)
    )
    bounded_failed_refs = {
        ref
        for ref in explicitly_failed_refs
        if (
            (part := by_ref.get(ref)) is not None
            and len(symbols.symbol_pins(part.symbol) or []) <= 16
        )
    }
    relevant_nets.update(
        net.name
        for net in plan.nets
        if any(pin.ref in bounded_failed_refs for pin in net.pins)
    )
    if "power_input_net_has_source" in failure_text:
        relevant_nets.update(plan.supply_nets)
        # A failed power-input island may belong on an active device's internal
        # regulator output rather than an external supply rail. Expose only the
        # power-output nets of the failed devices so the repair model can choose
        # the grounded source without receiving every MCU signal net.
        for net in plan.nets:
            for logical in net.pins:
                if logical.ref not in explicitly_failed_refs:
                    continue
                part = by_ref.get(logical.ref)
                part_pins = (
                    symbols.symbol_pins(part.symbol) or []
                    if part is not None
                    else []
                )
                number = _resolve_logical_pin(part_pins, logical.pin)
                physical = next(
                    (
                        pin
                        for pin in part_pins
                        if str(pin.get("number", "")) == number
                    ),
                    None,
                )
                if physical is not None and str(
                    physical.get("type", "")
                ).lower() in {"power_out", "power_output"}:
                    relevant_nets.add(net.name)
                    break
    if "component_pins_accounted" in failure_text:
        failed_connectors = {
            ref
            for ref in bounded_failed_refs
            if (
                (part := by_ref.get(ref)) is not None
                and any(
                    token in part.role.lower()
                    for token in ("connector", "header", "interface")
                )
            )
        }
        if failed_connectors:
            relevant_nets.update(plan.supply_nets)
            relevant_nets.add(plan.ground_net)
    if re.search(
        r"\b(?:[A-Z0-9_]*VDD[A-Z0-9_]*|VCC|VBAT|VSUPPLY)\b",
        failure_text,
        re.IGNORECASE,
    ) or "power-output" in failure_text.lower():
        relevant_nets.update(plan.supply_nets)
    if re.search(
        r"\b(?:[A-Z0-9_]*VSS[A-Z0-9_]*|GND|GROUND|AGND)\b",
        failure_text,
        re.IGNORECASE,
    ):
        relevant_nets.add(plan.ground_net)
    # Interface repairs may need one previously unused MCU GPIO. Add the MCU
    # only after calculating the failed-net set; otherwise every valid MCU net
    # would enter the compact repair scope.
    if domains & {
        "led",
        "analog",
        "swd",
        "debug",
        "microsd",
        "rs485",
        "i2c",
        "can",
        "usb",
    }:
        related_refs.update(
            part.ref
            for part in by_ref.values()
            if part.role.lower() in {"mcu", "controller"}
            or part.symbol.lower().startswith("mcu_")
        )
    compact = {
        "failed_checks": failure_text,
        "related_refs": sorted(related_refs),
        "relevant_nets": [
            net.model_dump()
            for net in plan.nets
            if net.name in relevant_nets
        ],
        "relevant_no_connect_pins": [
            pin.model_dump()
            for pin in plan.no_connect_pins
            if pin.ref in related_refs
        ],
        "ground_net": plan.ground_net,
        "supply_nets": plan.supply_nets,
    }
    return related_refs, relevant_nets, json.dumps(
        compact,
        ensure_ascii=False,
    )


def _limit_netlist_patch_to_scope(
    patch: NetlistPatch,
    plan: NetlistIntent,
    related_refs: set[str],
    relevant_nets: set[str],
) -> NetlistPatch:
    """Prevent a repair delta from rewriting unrelated, already-valid nets."""
    additional_parts = patch.additional_parts[:8]
    allowed_refs = {
        *related_refs,
        *(part.ref for part in additional_parts),
    }
    existing_names = {net.name for net in plan.nets}
    protected_names = {
        plan.ground_net,
        *plan.supply_nets,
    }
    allowed_existing_names = {
        *relevant_nets,
        *protected_names,
    }
    upserts: list[NetIntent] = []
    for update in patch.upsert_nets:
        if (
            update.name in existing_names
            and update.name not in allowed_existing_names
        ):
            continue
        pins = [
            pin for pin in update.pins
            if pin.ref in allowed_refs
        ]
        if pins:
            upserts.append(update.model_copy(update={"pins": pins}, deep=True))
    return NetlistPatch(
        additional_parts=additional_parts,
        remove_nets=[
            name for name in patch.remove_nets
            if name in relevant_nets and name not in protected_names
        ],
        remove_pins=[
            pin for pin in patch.remove_pins
            if pin.ref in allowed_refs
        ],
        upsert_nets=upserts,
        add_no_connect_pins=[
            pin for pin in patch.add_no_connect_pins
            if pin.ref in allowed_refs
        ],
        remove_no_connect_pins=[
            pin for pin in patch.remove_no_connect_pins
            if pin.ref in allowed_refs
        ],
    )


def _connection_repair_cohort(
    checks: list[CheckResult],
) -> list[CheckResult]:
    """Select one causally coherent connectivity failure for the next patch.

    Large boards can expose a dozen independent topology failures at once.
    Asking one model response to rewrite all of them defeats bounded repair and
    commonly damages already-correct nets.  The controller still evaluates the
    complete artifact after every patch; this function only narrows one repair
    action, allowing successive improvements to converge within the AHE budget.
    """

    failed = [
        check
        for check in checks
        if not check.ok and check.severity == Severity.ERROR
    ]
    priorities = (
        "no_pin_on_multiple_nets",
        "no_physical_pin_on_multiple_nets",
        "no_connect_pins_not_connected",
        "logical_pins_resolve",
        "additional_part_budget",
        "additional_part_refs_new",
        "additional_parts:",
        "power_pin_rail_polarity",
        "power_pin_rail_class",
        "two_terminal_parts_span_distinct_nets",
        "small_parts_fully_connected",
        "single_power_output_per_net",
        "power_input_net_has_source",
        "external_input_protection_chain",
        "source_backfeed_isolation",
        "power_mux_distinct_inputs_output",
        "buck_core_topology:",
        "buck_compensation_topology:",
        "mcu_reset_boot_support:",
        "usb_c_sink_cc_rd:",
        "microsd_",
        "rs485_",
        "can_",
        "led_current_limit_in_series:",
        "bounded_interface_connector:",
        "debug_connector_end_to_end:",
        "critical_power_reset_pins_connected",
        "no_single_pin_nets",
        "selected_components_used",
        "component_pins_accounted",
    )
    for prefix in priorities:
        match = next(
            (check for check in failed if check.name.startswith(prefix)),
            None,
        )
        if match is not None:
            return [match]
    return failed[:1]


def _rewire_two_terminal_part(
    plan: NetlistIntent,
    part: SelectedPart,
    endpoint_a: str | None,
    endpoint_b: str | None,
) -> NetlistIntent:
    """Rewire a grounded two-pin part only when both endpoints are known."""

    if (
        not endpoint_a
        or not endpoint_b
        or endpoint_a == endpoint_b
    ):
        return plan
    physical = symbols.symbol_pins(part.symbol) or []
    numbers = [str(pin.get("number", "")) for pin in physical]
    if len(numbers) != 2 or not all(numbers):
        return plan

    nets = [net.model_copy(deep=True) for net in plan.nets]
    for net in nets:
        net.pins = [pin for pin in net.pins if pin.ref != part.ref]
    by_name = {net.name: net for net in nets}
    for name, number in zip((endpoint_a, endpoint_b), numbers, strict=True):
        target = by_name.get(name)
        if target is None:
            target = NetIntent(
                name=name,
                kind=(
                    "ground"
                    if name == plan.ground_net
                    else "power"
                    if name in plan.supply_nets
                    else "signal"
                ),
            )
            nets.append(target)
            by_name[name] = target
        target.pins.append(LogicalPin(ref=part.ref, pin=number))
    protected = {plan.ground_net, *plan.supply_nets}
    nets = [
        net for net in nets
        if net.pins or net.name in protected
    ]
    return plan.model_copy(
        update={
            "nets": nets,
            "no_connect_pins": [
                pin for pin in plan.no_connect_pins
                if pin.ref != part.ref
            ],
        },
        deep=True,
    )


def _series_pair_is_valid(
    view: _ConnectivityView,
    first: SelectedPart,
    second: SelectedPart,
    endpoints: set[str],
) -> bool:
    first_nets = view.part_nets(first)
    second_nets = view.part_nets(second)
    shared = first_nets & second_nets
    return (
        len(first_nets) == 2
        and len(second_nets) == 2
        and len(shared) == 1
        and (first_nets | second_nets) - shared == endpoints
    )


def _available_link_net(
    plan: NetlistIntent,
    base: str,
    refs: set[str],
) -> str:
    """Return a reusable private link net or a collision-free equivalent."""

    by_name = {net.name: net for net in plan.nets}
    candidate = base
    suffix = 2
    while candidate in by_name:
        if {
            pin.ref for pin in by_name[candidate].pins
        } <= refs:
            return candidate
        candidate = f"{base}_{suffix}"
        suffix += 1
    return candidate


def _normalize_selectable_bus_chains(
    requirement: str,
    selection: SelectionPlan | None,
    plan: NetlistIntent,
) -> NetlistIntent:
    """Canonicalize uniquely identified CAN/RS485 termination and bias links."""

    if selection is None:
        return plan
    normalized = plan
    parts = selection.parts
    buses = _required_selectable_termination_buses(requirement)
    buses.update(
        bus
        for part in parts
        if "termination" in part.role.lower()
        for bus in _role_bus_names(part.role)
    )
    for bus in sorted(buses):
        view = _ConnectivityView.build(selection, normalized)
        termination = _termination_parts_for_bus(parts, bus)
        resistors = [
            part for part in termination
            if _is_120_ohm_termination_resistor(part)
        ]
        selectors = [
            part for part in termination
            if _is_termination_selector(part)
        ]
        transceivers = [
            part for part in parts
            if bus in _role_bus_names(part.role)
            and "transceiver" in part.role.lower()
        ]
        if (
            len(resistors) != 1
            or len(selectors) != 1
            or len(transceivers) != 1
        ):
            continue
        transceiver = transceivers[0]
        if bus == "can":
            endpoint_a = view.named_pin_net(transceiver, "CANH")
            endpoint_b = view.named_pin_net(transceiver, "CANL")
        else:
            endpoint_a = view.named_pin_net(transceiver, "A")
            endpoint_b = view.named_pin_net(transceiver, "B")
        endpoints = {endpoint_a, endpoint_b}
        if None in endpoints or len(endpoints) != 2:
            continue
        resistor = resistors[0]
        selector = selectors[0]
        if _series_pair_is_valid(
            view,
            resistor,
            selector,
            {str(endpoint_a), str(endpoint_b)},
        ):
            continue
        middle = _available_link_net(
            normalized,
            f"{bus.upper()}_TERM_LINK",
            {resistor.ref, selector.ref},
        )
        normalized = _rewire_two_terminal_part(
            normalized,
            resistor,
            str(endpoint_a),
            middle,
        )
        normalized = _rewire_two_terminal_part(
            normalized,
            selector,
            middle,
            str(endpoint_b),
        )

    view = _ConnectivityView.build(selection, normalized)
    transceivers = [
        part for part in parts
        if "rs485" in _role_bus_names(part.role)
        and "transceiver" in part.role.lower()
    ]
    if len(transceivers) != 1:
        return normalized
    transceiver = transceivers[0]
    supply = view.named_pin_net(transceiver, "VCC", "VDD")
    bus_nets = {
        "a": view.named_pin_net(transceiver, "A"),
        "b": view.named_pin_net(transceiver, "B"),
    }
    for side, rail in (("a", supply), ("b", normalized.ground_net)):
        side_parts = [
            part for part in parts
            if "rs485" in part.role.lower()
            and re.search(
                rf"(?:^|_)bias_?{side}(?:_|$)",
                part.role.lower(),
            )
        ]
        resistors = [
            part for part in side_parts
            if part.ref.upper().startswith("R")
        ]
        selectors = [
            part for part in side_parts
            if _is_termination_selector(part)
        ]
        bus_net = bus_nets[side]
        if (
            len(resistors) != 1
            or len(selectors) != 1
            or not rail
            or not bus_net
        ):
            continue
        view = _ConnectivityView.build(selection, normalized)
        endpoints = {str(rail), str(bus_net)}
        if _series_pair_is_valid(
            view,
            resistors[0],
            selectors[0],
            endpoints,
        ):
            continue
        middle = _available_link_net(
            normalized,
            f"RS485_BIAS_{side.upper()}_LINK",
            {resistors[0].ref, selectors[0].ref},
        )
        normalized = _rewire_two_terminal_part(
            normalized,
            resistors[0],
            str(rail),
            middle,
        )
        normalized = _rewire_two_terminal_part(
            normalized,
            selectors[0],
            middle,
            str(bus_net),
        )
    return normalized


def _normalize_buck_support(
    selection: SelectionPlan | None,
    plan: NetlistIntent,
) -> NetlistIntent:
    """Complete unique buck inductor and timing-resistor endpoints."""

    if selection is None:
        return plan
    normalized = plan
    parts = selection.parts
    converters = [
        part for part in parts
        if "buck" in part.role.lower()
        and any(
            token in part.role.lower()
            for token in ("converter", "regulator")
        )
    ]
    inductors = [
        part for part in parts
        if "buck" in part.role.lower()
        and "inductor" in part.role.lower()
    ]
    if len(converters) != 1 or len(inductors) != 1:
        return normalized
    view = _ConnectivityView.build(selection, normalized)
    converter = converters[0]
    switch = view.named_pin_net(converter, "SW")
    feedback = view.named_pin_net(converter, "FB")
    output_votes: list[str] = []
    for part in parts:
        role = part.role.lower()
        nets = view.part_nets(part)
        if (
            "buck" in role
            and "output" in role
            and "capacitor" in role
        ):
            output_votes.extend(nets - view.ground_nets)
        if (
            "buck" in role
            and "feedback" in role
            and any(token in role for token in ("top", "high", "upper"))
        ):
            output_votes.extend(nets - {feedback})
    candidates = {
        net for net in output_votes
        if net and net != switch and net not in view.ground_nets
    }
    if switch and len(candidates) == 1:
        output = next(iter(candidates))
        if view.part_nets(inductors[0]) != {switch, output}:
            normalized = _rewire_two_terminal_part(
                normalized,
                inductors[0],
                switch,
                output,
            )
    view = _ConnectivityView.build(selection, normalized)
    timing = view.named_pin_net(converter, "RT/CLK", "RT")
    timing_resistors = [
        part for part in parts
        if "buck" in part.role.lower()
        and (
            "timing" in part.role.lower()
            or re.search(r"(?:^|_)rt(?:_|$)", part.role.lower())
        )
        and part.ref.upper().startswith("R")
    ]
    if timing and len(timing_resistors) == 1:
        expected = {timing, normalized.ground_net}
        if view.part_nets(timing_resistors[0]) != expected:
            normalized = _rewire_two_terminal_part(
                normalized,
                timing_resistors[0],
                timing,
                normalized.ground_net,
            )
    return normalized


def _normalize_control_support(
    selection: SelectionPlan | None,
    plan: NetlistIntent,
) -> NetlistIntent:
    """Complete uniquely inferable MCU reset/boot RC support endpoints."""

    if selection is None:
        return plan
    normalized = plan
    parts = selection.parts
    mcus = [part for part in parts if _is_mcu_role(part.role)]
    if len(mcus) != 1:
        return normalized
    view = _ConnectivityView.build(selection, normalized)
    mcu = mcus[0]
    controls = {
        "reset": view.named_pin_net(mcu, "NRST", "RESET", "EN"),
        "boot": view.named_pin_net(mcu, "BOOT0", "BOOT", "IO0", "GPIO0"),
    }
    supply = view.named_pin_net(mcu, "VDD", "VCC", "3V3")
    for kind, control_net in controls.items():
        if not control_net:
            continue
        for part in parts:
            role = part.role.lower()
            if kind not in role and not (
                kind == "reset"
                and any(token in role for token in ("en_", "en-"))
            ):
                continue
            if len(symbols.symbol_pins(part.symbol) or []) != 2:
                continue
            nets = view.part_nets(part)
            if len(nets) >= 2:
                continue
            is_switch = (
                part.ref.upper().startswith("SW")
                or part.symbol.lower().startswith("switch:")
            )
            is_capacitor = (
                part.ref.upper().startswith("C")
                or any(
                    token in role
                    for token in ("capacitor", "decoupling")
                )
            )
            if is_switch or is_capacitor:
                other = normalized.ground_net
            elif supply and part.ref.upper().startswith("R"):
                other = supply
            else:
                continue
            normalized = _rewire_two_terminal_part(
                normalized,
                part,
                control_net,
                other,
            )
            view = _ConnectivityView.build(selection, normalized)
    return normalized


def _normalize_orphan_decoupling(
    selection: SelectionPlan | None,
    plan: NetlistIntent,
) -> NetlistIntent:
    """Complete one-ended decouplers when their domain has one proven rail."""

    if selection is None:
        return plan
    normalized = plan
    for capacitor in selection.parts:
        role = capacitor.role.lower()
        if (
            "decoupling" not in role
            or len(symbols.symbol_pins(capacitor.symbol) or []) != 2
        ):
            continue
        view = _ConnectivityView.build(selection, normalized)
        nets = view.part_nets(capacitor)
        if len(nets) != 1:
            continue
        current = next(iter(nets))
        if current in view.supply_nets:
            target = normalized.ground_net
        elif current in view.ground_nets:
            domain_tokens = _semantic_role_tokens(role)
            rail_votes: list[str] = []
            for peer in selection.parts:
                if peer.ref == capacitor.ref:
                    continue
                if not (
                    _semantic_role_tokens(peer.role) & domain_tokens
                ):
                    continue
                rail_votes.extend(view.part_nets(peer) & view.supply_nets)
            rails = set(rail_votes)
            if len(rails) != 1:
                continue
            target = next(iter(rails))
        else:
            continue
        normalized = _rewire_two_terminal_part(
            normalized,
            capacitor,
            current,
            target,
        )
    return normalized


def _normalize_netlist_intent(
    requirement: str,
    selection: SelectionPlan | None,
    plan: NetlistIntent,
) -> NetlistIntent:
    """Apply the same grounded normalizers to proposals, repairs, and resumes."""

    normalized = _normalize_additional_parts(selection, plan)
    normalized = _normalize_declared_power_nets(normalized)
    normalized = _normalize_standard_connector_no_connects(
        selection,
        normalized,
    )
    normalized = _complete_evident_connector_power_pins(
        selection,
        normalized,
    )
    normalized = _complete_duplicate_connector_pins(selection, normalized)
    normalized = _normalize_grounded_crystal_pins(selection, normalized)
    normalized = _normalize_selectable_bus_chains(
        requirement,
        selection,
        normalized,
    )
    normalized = _normalize_buck_support(selection, normalized)
    normalized = _normalize_control_support(selection, normalized)
    normalized = _normalize_orphan_decoupling(selection, normalized)
    normalized = _mark_evidently_safe_no_connects(selection, normalized)
    normalized = _remove_invalid_no_connect_pins(selection, normalized)
    normalized = _normalize_protocol_unused_pins(
        requirement,
        selection,
        normalized,
    )
    normalized = _remove_no_connect_singleton_nets(selection, normalized)
    normalized = _normalize_usb_c_sink_cc(selection, normalized)
    return _normalize_declared_power_nets(normalized)


class SchConnectionsStep(PipelineStepBase):
    """Schematic connection design: the electrical netlist *intent*.

    Produces named nets of logical pins (no real pin numbers yet — that is the
    pin-mapping step). Bottom-line check: no single-pin/empty nets, and both a
    supply rail and a ground net must exist.
    """

    step = PipelineStep.SCH_CONNECTIONS
    allow_artifact_first_design_repair = True
    knowledge_role = "schematic"
    repair_strategy_id = "bounded_netlist_patch"

    def knowledge_query(self, state: PipelineState) -> str | None:
        return f"net and connectivity design, power ground signals for: {state.requirement_text}"

    @staticmethod
    def _persist_connection_progress(
        state: PipelineState,
        ctx: PipelineContext,
    ) -> None:
        if ctx.on_progress_checkpoint is None:
            return
        try:
            ctx.on_progress_checkpoint(state)
        except Exception:  # noqa: BLE001 - persistence telemetry is best effort
            return

    def _propose_batched(
        self,
        state: PipelineState,
        ctx: PipelineContext,
        knowledge: str,
        topology: TopologyPlan,
        selection: SelectionPlan,
    ) -> tuple[NetlistIntent, bool]:
        """Synthesize a large intent as bounded, additive transactions."""

        checkpoint = state.connection_synthesis_checkpoint
        if (
            checkpoint is None
            or not checkpoint_matches_inputs(checkpoint, topology, selection)
        ):
            plan = plan_connection_batches(
                topology,
                selection,
                target_pin_count=max(1, ctx.connection_batch_target_pins),
                max_batches=max(1, ctx.connection_max_batches),
            )
            checkpoint = new_connection_checkpoint(topology, selection, plan)
            state.connection_synthesis_checkpoint = checkpoint
            self._persist_connection_progress(state, ctx)

        checkpoint = checkpoint.model_copy(deep=True)
        checkpoint.rounds_started += 1
        checkpoint.round_llm_invocations = 0
        state.connection_synthesis_checkpoint = checkpoint
        estimate = estimate_connection_output(
            selection,
            completion_limit=max(1, ctx.connection_completion_limit),
            direct_pin_limit=max(1, ctx.connection_direct_pin_limit),
        )
        self._persist_connection_progress(state, ctx)

        original_requirement = _original_requirement(state.requirement_text)
        blocks_by_name = {block.name: block for block in topology.blocks}
        used_llm = False

        def connection_budget_exhaustion() -> str:
            round_limit = max(1, ctx.connection_max_llm_invocations)
            total_limit = max(1, ctx.connection_max_total_llm_invocations)
            total_exhausted = checkpoint.llm_invocations >= total_limit
            round_exhausted = checkpoint.round_llm_invocations >= round_limit
            if not total_exhausted and not round_exhausted:
                return ""
            exhausted_scope = "global" if total_exhausted else "round"
            exhausted_limit = total_limit if total_exhausted else round_limit
            return (
                "connection synthesis exhausted its bounded "
                f"{exhausted_scope} LLM call budget ({exhausted_limit})"
            )

        def before_connection_attempt() -> None:
            nonlocal used_llm
            exhausted = connection_budget_exhaustion()
            if exhausted:
                raise ConnectionMergeError(exhausted)
            checkpoint.llm_invocations += 1
            checkpoint.round_llm_invocations += 1
            used_llm = True
            state.connection_synthesis_checkpoint = checkpoint
            self._persist_connection_progress(state, ctx)

        def best_effort_partial(reason: str) -> tuple[NetlistIntent, bool]:
            aggregate = checkpoint.aggregate.model_copy(deep=True)
            aggregate.rationale = (
                "bounded block-wise synthesis stopped after its repair budget; "
                f"editable partial connectivity retained: {reason}"
            )
            aggregate = _normalize_netlist_intent(
                state.requirement_text,
                selection,
                aggregate,
            )
            state.connection_synthesis_checkpoint = checkpoint
            state.connection_synthesis_report = connection_synthesis_report(
                selection,
                aggregate,
                mode="batched",
                estimate=estimate,
                checkpoint=checkpoint,
                resumable=(
                    checkpoint.llm_invocations
                    < max(1, ctx.connection_max_total_llm_invocations)
                ),
                stop_reason=reason,
            )
            self._persist_connection_progress(state, ctx)
            return aggregate, used_llm

        for batch in checkpoint.plan.batches:
            batch_status = checkpoint.batch_status(batch.batch_id)
            if batch_status.status == "completed":
                continue
            attempts = 0
            merge_error = ""
            while attempts <= max(0, ctx.connection_batch_merge_retries):
                exhausted = connection_budget_exhaustion()
                if exhausted:
                    if ctx.artifact_first:
                        return best_effort_partial(exhausted)
                    raise ConnectionMergeError(exhausted)
                attempts += 1
                # Ordinary batches need the central shared endpoints. The final
                # integration batch owns those endpoints and only needs the
                # already accepted net ledger for its leaf boundaries.
                visible_refs = set(batch.owned_refs)
                if batch.sequence < len(checkpoint.plan.batches) - 1:
                    visible_refs.update(batch.shared_refs)
                visible_parts = [
                    part
                    for part in selection.parts
                    if part.ref in visible_refs
                ]
                refs_block = _selected_parts_pin_block(
                    SelectionPlan(parts=visible_parts)
                    if visible_parts
                    else None
                )
                relevant_refs = set(batch.owned_refs) | set(batch.shared_refs)
                aggregate_nets = [
                    {
                        "name": net.name,
                        "kind": net.kind,
                        "purpose": net.purpose,
                        "pins": [
                            pin.model_dump(mode="json")
                            for pin in net.pins
                            if pin.ref in relevant_refs
                        ],
                    }
                    for net in checkpoint.aggregate.nets
                ]
                topology_context = [
                    {
                        "name": name,
                        "kind": (
                            blocks_by_name[name].kind
                            if name in blocks_by_name
                            else "integration"
                        ),
                        "description": (
                            blocks_by_name[name].description
                            if name in blocks_by_name
                            else "cross-block endpoint finalization"
                        ),
                    }
                    for name in batch.topology_blocks
                ]
                system = (
                    "You synthesize exactly one bounded electrical-connectivity "
                    "transaction. Return one JSON ConnectionDelta with batch_id, "
                    "base_revision, create_nets[], extend_nets[], "
                    "no_connect_pins[], rationale. This transaction is strictly "
                    "additive: never remove, rename, overwrite, or move an accepted "
                    "net or pin. Use create_nets only for a new net name and "
                    "extend_nets only for a name already present in EXISTING_NETS. "
                    "Use exact component references and exact real pin names or "
                    "numbers from the supplied pin list. Every real pin owned by "
                    "OWNED_REFS must be connected exactly once or explicitly placed "
                    "in no_connect_pins when genuinely unused. Shared refs are "
                    "boundary endpoints: they may be connected, but this batch must "
                    "never mark them no-connect. Every created functional net should "
                    "contain all endpoints available in this batch. Extend the "
                    "declared supply and ground nets for power pins. Do not invent "
                    "parts, pins, rails, interfaces, or support components. Preserve "
                    "the user's requested topology and use the accepted net ledger "
                    "to avoid duplicate aliases."
                )
                user = (
                    f"BATCH_ID: {batch.batch_id}\n"
                    f"BASE_REVISION: {checkpoint.aggregate_revision}\n"
                    f"OWNED_REFS: {','.join(batch.owned_refs)}\n"
                    f"SHARED_REFS: {','.join(batch.shared_refs)}\n\n"
                    "TOPOLOGY_BLOCKS:\n"
                    f"{json.dumps(topology_context, ensure_ascii=False)}\n\n"
                    "EXISTING_NETS (extend these exact names; pins shown are "
                    "already reserved):\n"
                    f"{json.dumps(aggregate_nets, ensure_ascii=False)}\n\n"
                    "COMPONENTS WITH REAL PINS:\n"
                    f"{refs_block}\n\n"
                    "USER REQUIREMENT:\n"
                    f"{original_requirement}\n\n"
                    "GROUNDED DESIGN KNOWLEDGE:\n"
                    f"{knowledge[:12_000]}"
                )
                previous_feedback = ctx.repair_feedback
                if merge_error:
                    ctx.repair_feedback = (
                        "The previous batch delta was atomically rejected and no "
                        f"state changed: {merge_error}. Correct only this batch."
                    )
                try:
                    try:
                        delta, delta_used_llm = propose_structured(
                            ctx,
                            model=ConnectionDelta,
                            system=system,
                            user=user,
                            fallback=lambda batch_id=batch.batch_id, base_revision=(
                                checkpoint.aggregate_revision
                            ): ConnectionDelta(
                                batch_id=batch_id,
                                base_revision=base_revision,
                            ),
                            before_attempt=before_connection_attempt,
                        )
                    except ConnectionMergeError as exc:
                        if not ctx.artifact_first:
                            raise
                        return best_effort_partial(str(exc))
                    except LlmError as exc:
                        if not ctx.artifact_first:
                            raise
                        used_llm = True
                        reason = (
                            f"connection batch {batch.batch_id} proposal failed: "
                            f"{type(exc).__name__}: {exc}"
                        )
                        failed = checkpoint.model_copy(deep=True)
                        failed_status = failed.batch_status(batch.batch_id)
                        failed_status.status = "failed"
                        failed_status.attempts += 1
                        failed_status.error = reason[:2_000]
                        checkpoint = failed
                        state.connection_synthesis_checkpoint = checkpoint
                        return best_effort_partial(reason)
                finally:
                    ctx.repair_feedback = previous_feedback
                used_llm = used_llm or delta_used_llm
                try:
                    checkpoint = merge_connection_delta(
                        checkpoint,
                        delta,
                        selection,
                    )
                except ConnectionMergeError as exc:
                    merge_error = str(exc)
                    failed = checkpoint.model_copy(deep=True)
                    failed_status = failed.batch_status(batch.batch_id)
                    failed_status.status = "failed"
                    failed_status.attempts += 1
                    failed_status.error = merge_error
                    checkpoint = failed
                    state.connection_synthesis_checkpoint = checkpoint
                    self._persist_connection_progress(state, ctx)
                    if attempts > max(0, ctx.connection_batch_merge_retries):
                        if ctx.artifact_first:
                            return best_effort_partial(merge_error)
                        raise
                    continue
                state.connection_synthesis_checkpoint = checkpoint
                self._persist_connection_progress(state, ctx)
                break

        if any(item.status != "completed" for item in checkpoint.batches):
            pending = [
                item.batch_id
                for item in checkpoint.batches
                if item.status != "completed"
            ]
            message = f"connection synthesis ended with pending batches: {pending}"
            if ctx.artifact_first:
                return best_effort_partial(message)
            raise ConnectionMergeError(message)
        aggregate = checkpoint.aggregate.model_copy(deep=True)
        aggregate.rationale = (
            f"bounded block-wise synthesis; {len(checkpoint.batches)} batches; "
            f"{checkpoint.llm_invocations} proposal attempt(s)"
        )
        aggregate = _normalize_netlist_intent(
            state.requirement_text,
            selection,
            aggregate,
        )
        state.connection_synthesis_report = connection_synthesis_report(
            selection,
            aggregate,
            mode="batched",
            estimate=estimate,
            checkpoint=checkpoint,
        )
        state.connection_synthesis_checkpoint = None
        self._persist_connection_progress(state, ctx)
        return aggregate, used_llm

    def propose(
        self, state: PipelineState, ctx: PipelineContext, knowledge: str
    ) -> tuple[BaseModel, bool]:
        selection = state.artifact(PipelineStep.SELECTION)
        topology = state.artifact(PipelineStep.TOPOLOGY)
        estimate = (
            estimate_connection_output(
                selection,
                completion_limit=max(1, ctx.connection_completion_limit),
                direct_pin_limit=max(1, ctx.connection_direct_pin_limit),
            )
            if isinstance(selection, SelectionPlan)
            else None
        )
        if (
            isinstance(selection, SelectionPlan)
            and isinstance(topology, TopologyPlan)
            and ctx.mode != LlmMode.OFFLINE
            and ctx.client is not None
        ):
            assert estimate is not None
            # Atomic batch merging requires real symbol pins. When a library
            # entry is unavailable, retain artifact-first direct synthesis and
            # let Selection/Reviewer report the grounded capability gap.
            if estimate.should_batch and not estimate.unknown_symbol_refs:
                prospective_plan = plan_connection_batches(
                    topology,
                    selection,
                    target_pin_count=max(1, ctx.connection_batch_target_pins),
                    max_batches=max(1, ctx.connection_max_batches),
                )
                if getattr(prospective_plan, "batching_supported", True):
                    return self._propose_batched(
                        state,
                        ctx,
                        knowledge,
                        topology,
                        selection,
                    )
        if state.connection_synthesis_checkpoint is not None:
            state.connection_synthesis_checkpoint = None
            self._persist_connection_progress(state, ctx)

        def fallback() -> NetlistIntent:
            extracted = params_from_requirement(state.requirement_text)
            try:
                params = Atmega328Params.model_validate(extracted)
            except Exception:
                params = Atmega328Params()
            ir = build_ir(params)
            nets: list[NetIntent] = []
            for n in ir.nets:
                kind = _classify_net(n.name)
                nets.append(
                    NetIntent(
                        name=n.name,
                        kind=kind,
                        pins=[LogicalPin(ref=p.component_ref, pin=p.pin) for p in n.pins],
                        purpose=str(n.properties.get("purpose", "")),
                    )
                )
            supply = [n.name for n in nets if n.kind == "power"]
            return NetlistIntent(
                nets=nets, supply_nets=supply, ground_net="GND",
                rationale="deterministic family connectivity",
            )

        sel = state.artifact(PipelineStep.SELECTION)
        selected_count = len(sel.parts) if isinstance(sel, SelectionPlan) else 0
        additional_budget = min(
            8, max(0, _MAX_SELECTION_PARTS - selected_count)
        )
        additional_policy = (
            "additional_parts MUST be an empty array; the supplied selection is "
            "authoritative and already contains the required physical parts. Reuse "
            "those parts according to their roles."
            if additional_budget == 0
            else (
                f"At most {additional_budget} genuinely missing physical parts may "
                "be defined in additional_parts. Reuse a supplied part with the "
                "needed role before adding anything."
            )
        )
        original_requirement = _original_requirement(state.requirement_text)
        explicit_swd = (
            "swd" in original_requirement.lower()
            or (
                isinstance(sel, SelectionPlan)
                and any("swd" in part.role.lower() for part in sel.parts)
            )
        )
        swd_guidance = (
            "For an explicitly requested standard 10-pin Cortex SWD connector "
            "use pin 1 VTref, 2 SWDIO, 3 GND, 4 SWCLK, 5 GND, 6 SWO, "
            "7 NC/key, 8 NC, 9 GNDDetect, 10 NRST. VTref must join the MCU "
            "I/O supply rail; reserved/key pins never join a rail. "
            if explicit_swd
            else (
                "Do not assume Cortex SWD for a generic debug request. Choose "
                "UART, JTAG, USB debug, or another protocol only when the "
                "selected MCU pins and grounded architect evidence support it. "
            )
        )
        system = (
            "You design the electrical connectivity as JSON: additional_parts[], "
            "nets[] with name, kind (power/ground/signal/clock), pins[] "
            "({ref, pin}), purpose; no_connect_pins[] ({ref, pin}); plus "
            "supply_nets[], ground_net, rationale. "
            f"{additional_policy} "
            "Every terminal of an added two-pin component must be connected. "
            "Every real two-terminal component must span two distinct nets; "
            "never place both terminals of a fuse, capacitor, TVS/ESD diode, "
            "inductor, resistor, LED, crystal, jumper, or link on one net. "
            "Every real pin of every selected component must occur in exactly one "
            "net or in no_connect_pins. Every selected component must have at least "
            "one genuinely connected pin. Explicitly list unused MCU GPIO, connector "
            "reserved/SBU pins, and IC NC pins in no_connect_pins. Never abandon a "
            "selected passive, protection, switch, crystal, diode, transistor, or "
            "other <=4-pin part with a no-connect marker; connect it or remove it. "
            "For an unused connector pin, use an explicit no-connect instead of "
            "wiring it to an unrelated MCU GPIO merely to account for the pin. "
            "Every net needs >= 2 pins. "
            "A component pin may appear on exactly one electrical net; never reuse "
            "one TVS/protection pin or crystal pin on multiple nets. "
            "A net label is an electrical identity, not merely a component pin "
            "name. Repeated labels such as BOOT, EN, SW, or RESET on unrelated "
            "devices must be namespaced by function (for example BUCK_BOOT versus "
            "MCU_BOOT) and never merged. When one signal has multiple protocol "
            "aliases such as microSD DAT3/CS, choose one canonical net and mention "
            "the alias only in purpose; never emit a second net for the same "
            "physical pin. "
            "Use the single declared ground_net for every ground return. Do not "
            "create per-channel GND aliases or connect one MCU VSS pin to multiple "
            "named nets. Likewise, reuse the declared supply net instead of making "
            "a one-pin rail alias. "
            "Never emit an unused pin or a test point as a standalone one-pin net: "
            "either attach a test point to the existing functional net, connect a "
            "required signal to its selected peer components, or omit that net. "
            "NRST, BOOT, SWD, buses, LEDs, and clocks are not valid one-pin nets. "
            "Use component references from the supplied list or from your explicit "
            "additional_parts definitions. Never emit an undefined R/C/U/J/D "
            "reference. Do not add a component when a datasheet-approved direct "
            "strap or an existing selected part is sufficient. "
            "IMPORTANT: for each component pin, use ONLY a pin name (or number) "
            "from the exact list given for that component below. Do not invent "
            "pin names such as VIN/AGND/ANODE if they are not in the list. "
            f"{swd_guidance}"
            "Connect every selected bootstrap capacitor directly between its "
            "converter BOOT and SW nets. Strap power-mux mode/priority inputs to a "
            "valid existing rail or GND according to the device function; do not "
            "create one-pin configuration nets. When backfeed isolation is required, "
            "keep raw USB VBUS and external-input connector rails distinct and join "
            "them to the downstream rail only through the selected power-path, "
            "ideal-diode, reverse-blocking, or source-priority device. An external "
            "DC input protection chain must be series-connected from connector "
            "through its fuse, reverse-polarity element, and input filter to the "
            "regulator VIN; TVS and filter capacitors are shunts to ground, never "
            "series elements or grounded fuses/inductors. For every "
            "requested differential "
            "bus with selectable termination, create a series chain "
            "BUS_P--120R--TERM_LINK--jumper/link--BUS_N, not two parts "
            "independently across the pair. Apply this to the actual requested bus "
            "(for example CANH/CANL or RS485_A/RS485_B) and never invent another "
            "interface. Route both CAN choke "
            "input pins from the transceiver and both output pins to the connector. "
            "For microSD SPI mode, map DAT3/CD=CS, CMD=MOSI, CLK=CLK, and "
            "DAT0=MISO end to end; selected pull-ups and ESD parts must land on "
            "those socket nets, and unused DAT1/DAT2 must not be dummy GPIOs. "
            "An RS485 transceiver DI, RO, DE, and /RE must reach MCU GPIOs; its "
            "A/B pins must reach the connector and grounded TVS channels, while "
            "optional bias jumpers stay in series with their bias resistors. "
            "All reset/enable and boot pulls, buttons, and RC capacitors must "
            "terminate on the actual MCU reset/boot pin nets. "
            "Never create a one-pin net for a converter or power-path control pin: "
            "connect its selected support network/strap, or mark it no-connect only "
            "when the exact device allows that state. Tie unused active-low SPI "
            "flash WP and HOLD/RESET control pins to the I/O supply using a "
            "datasheet-valid direct strap or selected pull-up; never mark RESET as "
            "no-connect. Wire each LED and its own resistor as a real series path: "
            "supply-or-MCU -- resistor -- unique intermediate net -- LED -- "
            "ground-or-supply. The LED and resistor must share exactly one net. "
            "For a USB-C sink/device, wire CC1 and CC2 to independent 5.1k "
            "Rd resistors to the declared ground net. Never put a sink CC pin "
            "or its Rd signal terminal on 3V3, 5V, or VBUS. "
            "Never connect two power-output pins to one rail. Every real power-input "
            "pin must be on a declared supply rail or on a rail driven by exactly "
            "one real power-output pin; do not create isolated signal nets for "
            "power-input pins. An IC's internal "
            "regulator output must use its own datasheet rail for core-supply pins "
            "and decoupling, not the external regulator output rail."
        )
        refs_block = _selected_parts_pin_block(
            sel if isinstance(sel, SelectionPlan) else None
        )
        user = (
            f"Requirement:\n{original_requirement}\n\n"
            "Grounded architect evidence (authoritative where present):\n"
            f"{_architect_evidence_excerpt(state.requirement_text)}\n\n"
            "Selected components with their roles and REAL pin names/numbers:\n"
            f"{refs_block}\n\n"
            f"Knowledge:\n{knowledge}"
        )
        plan, used = propose_structured(
            ctx, model=NetlistIntent, system=system, user=user, fallback=fallback
        )
        _ground_selected_parts(
            plan.additional_parts,
            state.requirement_text,
        )
        selected = sel if isinstance(sel, SelectionPlan) else None
        normalized = _normalize_netlist_intent(
            state.requirement_text,
            selected,
            plan,
        )
        if selected is not None:
            direct_reason = ""
            if estimate is not None and estimate.should_batch:
                if estimate.unknown_symbol_refs:
                    direct_reason = (
                        "batching unavailable because symbol pins are unresolved: "
                        f"{estimate.unknown_symbol_refs}"
                    )
                elif ctx.mode == LlmMode.OFFLINE or ctx.client is None:
                    direct_reason = "batching unavailable without an active LLM client"
            state.connection_synthesis_report = connection_synthesis_report(
                selected,
                normalized,
                mode="direct",
                estimate=estimate,
                llm_calls=int(used),
                round_llm_calls=int(used),
                stop_reason=direct_reason,
            )
        return normalized, used

    def repair(
        self,
        state: PipelineState,
        ctx: PipelineContext,
        knowledge: str,
        artifact: BaseModel,
        checks: list[CheckResult],
    ) -> tuple[BaseModel, bool]:
        assert isinstance(artifact, NetlistIntent)
        if not artifact.nets:
            # An empty object is schema-valid because checkpoints and
            # deterministic checks must be able to represent an incomplete
            # intent, but it is not a useful baseline for a bounded delta.
            # Re-propose the whole netlist within this step's repair budget.
            previous_feedback = ctx.repair_feedback
            ctx.repair_feedback = (
                "The previous connectivity proposal contained no nets. "
                "Regenerate the complete electrical netlist; an empty nets array "
                "is not a valid design."
            )
            try:
                return self.propose(state, ctx, knowledge)
            finally:
                ctx.repair_feedback = previous_feedback
        selection = state.artifact(PipelineStep.SELECTION)
        repair_checks = _connection_repair_cohort(checks)
        related_refs, relevant_nets, compact_context = _connection_repair_scope(
            selection if isinstance(selection, SelectionPlan) else None,
            artifact,
            repair_checks,
        )
        scoped_parts = [
            part
            for part in [
                *(
                    selection.parts
                    if isinstance(selection, SelectionPlan)
                    else []
                ),
                *artifact.additional_parts,
            ]
            if part.ref in related_refs
        ]
        refs_block = _selected_parts_pin_block(
            SelectionPlan(parts=scoped_parts)
            if scoped_parts
            else None
        )
        original_requirement = _original_requirement(state.requirement_text)
        explicit_swd = (
            "swd" in original_requirement.lower()
            or (
                isinstance(selection, SelectionPlan)
                and any(
                    "swd" in part.role.lower()
                    for part in selection.parts
                )
            )
        )
        swd_repair_guidance = (
            "For a standard 10-pin Cortex SWD connector preserve pin 1 VTref, "
            "2 SWDIO, 3 GND, 4 SWCLK, 5 GND, 6 SWO, 7 NC/key, 8 NC, "
            "9 GNDDetect, and 10 NRST. On a rejected SWD mapping, remove "
            "reserved pins from rails and mark unused pins no-connect. "
            if explicit_swd
            else (
                "Do not convert a generic debug connector into Cortex SWD. "
                "Repair it only with a protocol and GPIO mapping supported by "
                "the selected MCU and grounded architect evidence. "
            )
        )
        system = (
            "Repair an existing electrical netlist by returning one JSON patch with "
            "fields: additional_parts (the COMPLETE replacement list, maximum 8), "
            "remove_nets[], remove_pins[] ({ref,pin}), upsert_nets[] "
            "({name,kind,pins[],purpose}), add_no_connect_pins[] and "
            "remove_no_connect_pins[]. Do not return the full netlist. Pins in an "
            "upsert net are moved from their old net. Preserve correct existing nets. "
            "Never solve a duplicate assignment by merging unrelated nets. Every "
            "selected <=4-pin non-connector physical part must remain fully "
            "connected. Unused connector pins must be explicit no-connects, never "
            "dummy MCU GPIO connections. Remove every "
            "pin whose component ref is absent from the selected/additional part list. "
            "Resolve every reported unaccounted pin: connect a required functional pin "
            "or add an explicit no-connect marker for a genuinely unused pin. A series "
            "LED path is supply-or-MCU -- resistor -- unique intermediate net -- LED "
            "-- ground-or-supply. The LED and its channel resistor share exactly one "
            "net; do not place both parts in parallel on the same two nets. Remove "
            "the obsolete isolated endpoint nets after moving their pins. Move all "
            "ground-return pins into the existing ground_net named in the compact "
            "context; never create per-channel GND aliases or reuse one MCU VSS pin "
            "as their peer. Move supply endpoints into an existing declared supply "
            "net in the same way. A USB-C sink/device requires CC1 and CC2 on "
            "separate nets, each through its own 5.1k Rd to the declared ground; "
            "remove CC pins and Rd signal terminals from positive supply rails. "
            "Repair every reported two-terminal self-short by restoring the "
            "part's functional series or shunt topology; do not silence it with "
            "a no-connect marker or by deleting the required part. Use an "
            "unused MCU GPIO from the supplied no-connect list for a controllable "
            "status LED. Tie unused active-low SPI flash WP and HOLD/RESET pins to "
            "the I/O supply using a datasheet-valid direct strap or selected pull-up; "
            "never leave RESET as no-connect. A "
            "selectable differential-bus termination path needs the 120-ohm "
            "resistor and jumper/link in series between the actual positive and "
            "negative bus nets (for example CANH/CANL or RS485_A/RS485_B). "
            "Never leave two power-output pins on one rail; "
            "every power-input pin must be on a declared supply rail or a rail with "
            "exactly one real power-output pin. Move an internal regulator output "
            "and its core-supply/decoupling pins "
            "to their own datasheet rail. For a backfeed-isolated design, split any "
            "raw USB/external source net that was directly merged and reconnect it "
            "through the selected power-path element. Rebuild a rejected external "
            "input chain as connector -- fuse -- reverse-polarity element -- filter "
            "-- regulator VIN, with TVS and filter capacitors from protected nodes "
            "to ground. When one member of a named "
            "interface bus "
            "is single-ended, inspect every supplied net with the same interface "
            "prefix and rebuild a one-to-one mapping between the endpoint devices; "
            "real pin labels may use direction or IO aliases rather than the net "
            f"name. {swd_repair_guidance}"
            " For an unaccounted pin on a bounded external connector/header, use "
            "the connector role and its already-wired peers to decide whether it "
            "is the interface power pin. Join the appropriate existing supply "
            "rail when the interface exposes power; do not invent a one-pin net "
            "or mark a required connector power pin as unused. Rebuild rejected "
            "microSD SPI, RS485 control/bias, and MCU reset/boot families end to "
            "end; do not satisfy them with isolated aliases or spare GPIO filler."
        )
        user = (
            "Patch only the failed checks in the rejected netlist. Use exact selected "
            "references and exact real pin names/numbers below. If a genuinely missing "
            "support part is necessary, declare it in additional_parts with a real "
            "symbol and footprint; otherwise keep the current grounded additions.\n\n"
            "Grounded architect evidence:\n"
            f"{_architect_evidence_excerpt(state.requirement_text, 6000)}\n\n"
            f"Selected components:\n{refs_block}"
        )
        ctx.repair_feedback = (
            "Repair only this compact failure scope. Do not restate or modify "
            "unlisted nets/components:\n"
            f"{compact_context}"
        )
        patch, used = propose_structured(
            ctx,
            model=NetlistPatch,
            system=system,
            user=user,
            fallback=NetlistPatch,
        )
        patch = _limit_netlist_patch_to_scope(
            patch,
            artifact,
            related_refs,
            relevant_nets,
        )
        repaired = _remove_unknown_netlist_refs(
            _apply_netlist_patch(artifact, patch),
            selection if isinstance(selection, SelectionPlan) else None,
        )
        _ground_selected_parts(
            repaired.additional_parts,
            state.requirement_text,
        )
        selected = selection if isinstance(selection, SelectionPlan) else None
        return (
            _normalize_netlist_intent(
                state.requirement_text,
                selected,
                repaired,
            ),
            used,
        )

    def prepare_resumed_artifact(
        self,
        state: PipelineState,
        artifact: BaseModel,
    ) -> BaseModel:
        """Re-apply deterministic normalizers before validating a checkpoint."""

        assert isinstance(artifact, NetlistIntent)
        selection = state.artifact(PipelineStep.SELECTION)
        return _normalize_netlist_intent(
            state.requirement_text,
            selection if isinstance(selection, SelectionPlan) else None,
            artifact,
        )

    def check(self, state: PipelineState, artifact: BaseModel) -> list[CheckResult]:
        assert isinstance(artifact, NetlistIntent)
        checks: list[CheckResult] = [
            CheckResult(
                name="has_nets", ok=bool(artifact.nets),
                message="connectivity must define at least one net",
            )
        ]
        # No single-pin or empty nets (the classic wiring mistake).
        singles = [n.name for n in artifact.nets if len(n.pins) < 2]
        checks.append(CheckResult(
            name="no_single_pin_nets", ok=not singles,
            message=f"single-pin/empty nets: {singles}",
        ))
        # A supply rail and a ground net must both exist.
        net_names = {n.name for n in artifact.nets}
        has_power = bool(artifact.supply_nets) or any(n.kind == "power" for n in artifact.nets)
        checks.append(CheckResult(
            name="has_supply_net", ok=has_power,
            message="no supply/power net defined",
        ))
        checks.append(CheckResult(
            name="has_ground_net", ok=artifact.ground_net in net_names
            or any(n.kind == "ground" for n in artifact.nets),
            message=f"ground net {artifact.ground_net!r} not present in the netlist",
        ))
        ground_names = {
            artifact.ground_net.lower(),
            *(n.name.lower() for n in artifact.nets if n.kind == "ground"),
        }
        ground_in_supply = sorted(
            name for name in artifact.supply_nets
            if name.lower() in ground_names
        )
        checks.append(CheckResult(
            name="ground_not_declared_as_supply",
            ok=not ground_in_supply,
            message=f"ground nets cannot be listed as supply rails: {ground_in_supply}",
        ))
        # A connection step may discover required support parts that selection
        # could not anticipate. They are accepted only as an explicit, grounded
        # delta whose combined selection still passes every selection gate.
        sel = state.artifact(PipelineStep.SELECTION)
        combined_selection = sel if isinstance(sel, SelectionPlan) else None
        existing_refs = (
            {part.ref for part in sel.parts}
            if isinstance(sel, SelectionPlan)
            else set()
        )
        selected_count = len(sel.parts) if isinstance(sel, SelectionPlan) else 0
        additional_budget = min(
            8, max(0, _MAX_SELECTION_PARTS - selected_count)
        )
        checks.append(CheckResult(
            name="additional_part_budget",
            ok=len(artifact.additional_parts) <= additional_budget,
            message=(
                f"connection step added {len(artifact.additional_parts)} parts; "
                f"budget is {additional_budget} for a {selected_count}-part selection"
            ),
        ))
        additional_refs = {part.ref for part in artifact.additional_parts}
        conflicting_refs = sorted(existing_refs & additional_refs)
        if artifact.additional_parts:
            checks.append(CheckResult(
                name="additional_part_refs_new",
                ok=isinstance(sel, SelectionPlan) and not conflicting_refs,
                message=(
                    "additional parts require an existing selection and must use "
                    f"new references; conflicts: {conflicting_refs}"
                ),
            ))
            if isinstance(sel, SelectionPlan) and not conflicting_refs:
                combined_selection = SelectionPlan(
                    parts=[*sel.parts, *artifact.additional_parts],
                    rationale=sel.rationale,
                )
                for selection_check in SelectionStep().check(
                    state,
                    combined_selection,
                ):
                    checks.append(CheckResult(
                        name=f"additional_parts:{selection_check.name}",
                        ok=selection_check.ok,
                        severity=selection_check.severity,
                        message=selection_check.message,
                    ))

            used_by_ref: dict[str, list[str]] = {
                part.ref: [] for part in artifact.additional_parts
            }
            for net in artifact.nets:
                for pin in net.pins:
                    if pin.ref in used_by_ref:
                        used_by_ref[pin.ref].append(pin.pin)
            unused = sorted(
                ref for ref, pins in used_by_ref.items() if not pins
            )
            checks.append(CheckResult(
                name="additional_parts_used",
                ok=not unused,
                message=f"declared additional parts not used in any net: {unused}",
            ))

            incomplete: list[str] = []
            for part in artifact.additional_parts:
                part_pins = symbols.symbol_pins(part.symbol) or []
                logical_pins = used_by_ref[part.ref]
                if len(part_pins) == 2:
                    expected_numbers = {
                        str(pin["number"])
                        for pin in part_pins
                        if pin["number"]
                    }
                    used_numbers = {
                        number
                        for logical in logical_pins
                        if (
                            number := _resolve_logical_pin(
                                part_pins,
                                logical,
                            )
                        ) is not None
                    }
                    missing = sorted(expected_numbers - used_numbers)
                    if missing:
                        incomplete.append(
                            f"{part.ref} missing terminal(s) {missing}"
                        )
                elif not part_pins and len(set(logical_pins)) < 2:
                    incomplete.append(
                        f"{part.ref} has fewer than two connected terminals"
                    )
            checks.append(CheckResult(
                name="additional_two_pin_parts_fully_connected",
                ok=not incomplete,
                message=f"incomplete additional two-pin parts: {incomplete}",
            ))

        # Consistency: every net ref must be selected or declared above.
        if isinstance(sel, SelectionPlan) and sel.parts:
            known = (
                {part.ref for part in combined_selection.parts}
                if isinstance(combined_selection, SelectionPlan)
                else existing_refs
            )
            unknown = sorted(
                {p.ref for n in artifact.nets for p in n.pins if p.ref not in known}
            )
            checks.append(CheckResult(
                name="pins_reference_selected_parts", ok=not unknown,
                message=f"nets reference unknown component refs: {unknown}",
            ))
        # No logical pin on two nets (a short). Catching it here — at an LLM
        # step — lets the repair loop feed it back and fix it, instead of
        # failing later at deterministic pin-mapping where no self-repair runs.
        seen_pins: dict[str, str] = {}
        shorted: list[str] = []
        for n in artifact.nets:
            for p in n.pins:
                key = f"{p.ref}:{p.pin}"
                if key in seen_pins and seen_pins[key] != n.name:
                    shorted.append(f"{key} in {seen_pins[key]} & {n.name}")
                seen_pins[key] = n.name
        checks.append(CheckResult(
            name="no_pin_on_multiple_nets", ok=not shorted,
            message=f"logical pin(s) on multiple nets (short): {shorted}",
        ))
        # Every logical pin must resolve to a real pin on its part's symbol, so
        # deterministic pin-mapping downstream cannot fail. Verified here — an
        # LLM step — so the repair loop can fix a bad/invented pin name.
        if (
            isinstance(combined_selection, SelectionPlan)
            and combined_selection.parts
            and config.symbol_dir() is not None
        ):
            ref_syms = {
                part.ref: part.symbol
                for part in combined_selection.parts
            }
            bad_pins: list[str] = []
            for n in artifact.nets:
                for p in n.pins:
                    part_pins = symbols.symbol_pins(ref_syms.get(p.ref, "")) or []
                    if not part_pins:
                        continue  # zero-pin symbol (e.g. mounting hole)
                    nums = {str(x["number"]) for x in part_pins}
                    ok = _resolve_logical_pin(part_pins, p.pin) is not None or (
                        p.pin.isdigit() and p.pin in nums
                    )
                    if not ok:
                        bad_pins.append(f"{p.ref}:{p.pin}")
            checks.append(CheckResult(
                name="logical_pins_resolve", ok=not bad_pins,
                message=f"logical pins not found on the part symbol: {bad_pins}",
            ))
            ref_parts = {
                part.ref: part
                for part in combined_selection.parts
            }
            connected_numbers: set[str] = set()
            physical_owners: dict[str, str] = {}
            physical_shorts: list[str] = []
            physical_endpoints_by_net: dict[str, set[str]] = {}
            for net in artifact.nets:
                physical_endpoints_by_net[net.name] = set()
                for logical in net.pins:
                    part = ref_parts.get(logical.ref)
                    part_pins = (
                        symbols.symbol_pins(part.symbol) or []
                        if part is not None
                        else []
                    )
                    number = _resolve_logical_pin(part_pins, logical.pin)
                    if number is not None:
                        key = f"{logical.ref}:{number}"
                        physical_endpoints_by_net[net.name].add(key)
                        previous_net = physical_owners.get(key)
                        if previous_net is not None and previous_net != net.name:
                            physical_shorts.append(
                                f"{key} in {previous_net} & {net.name}"
                            )
                        physical_owners[key] = net.name
                        connected_numbers.add(key)
            checks.append(CheckResult(
                name="no_physical_pin_on_multiple_nets",
                ok=not physical_shorts,
                message=(
                    "resolved physical pin(s) on multiple nets (short): "
                    f"{physical_shorts}"
                ),
            ))
            single_physical_nets = sorted(
                net.name
                for net in artifact.nets
                if (
                    len(net.pins) >= 2
                    and len(physical_endpoints_by_net[net.name]) < 2
                )
            )
            checks.append(CheckResult(
                name="no_single_physical_pin_nets",
                ok=not single_physical_nets,
                message=(
                    "nets collapse to fewer than two distinct physical pins "
                    f"after pin-name resolution: {single_physical_nets}"
                ),
            ))
            rail_polarity_conflicts: list[str] = []
            supply_names = {name.lower() for name in artifact.supply_nets}
            ground_names = {
                artifact.ground_net.lower(),
                *(
                    net.name.lower()
                    for net in artifact.nets
                    if net.kind == "ground"
                ),
            }
            for net in artifact.nets:
                for logical in net.pins:
                    part = ref_parts.get(logical.ref)
                    part_pins = (
                        symbols.symbol_pins(part.symbol) or []
                        if part is not None
                        else []
                    )
                    number = _resolve_logical_pin(part_pins, logical.pin)
                    physical = next(
                        (
                            pin
                            for pin in part_pins
                            if str(pin.get("number", "")) == number
                        ),
                        None,
                    )
                    if physical is None:
                        continue
                    pin_name = str(physical.get("name", "")).strip().upper()
                    pin_type = str(physical.get("type", "")).lower()
                    is_ground_pin = bool(
                        re.search(
                            r"(?:^|[_/])(GND[A-Z0-9]*|[A-Z]*VSS[A-Z0-9]*)(?:$|[_/])",
                            pin_name,
                        )
                    )
                    if (
                        net.name.lower() in ground_names
                        and pin_type in {"power_in", "power_input"}
                        and not is_ground_pin
                    ):
                        rail_polarity_conflicts.append(
                            f"{logical.ref}:{number}({pin_name}) positive power "
                            f"input is on ground net {net.name}"
                        )
                    elif (
                        net.name.lower() in supply_names
                        and is_ground_pin
                    ):
                        rail_polarity_conflicts.append(
                            f"{logical.ref}:{number}({pin_name}) ground pin is "
                            f"on supply net {net.name}"
                        )
            checks.append(CheckResult(
                name="power_pin_rail_polarity",
                ok=not rail_polarity_conflicts,
                message=(
                    "power and ground pin polarity conflicts: "
                    f"{rail_polarity_conflicts}"
                ),
            ))
            power_output_conflicts: list[str] = []
            for net in artifact.nets:
                outputs: set[str] = set()
                drivers: set[tuple[str, str]] = set()
                for logical in net.pins:
                    part = ref_parts.get(logical.ref)
                    part_pins = (
                        symbols.symbol_pins(part.symbol) or []
                        if part is not None
                        else []
                    )
                    number = _resolve_logical_pin(part_pins, logical.pin)
                    physical = next(
                        (
                            pin
                            for pin in part_pins
                            if str(pin.get("number", "")) == number
                        ),
                        None,
                    )
                    if physical is None or str(
                        physical.get("type", "")
                    ).lower() not in {"power_out", "power_output"}:
                        continue
                    pin_name = re.sub(
                        r"[^a-z0-9]+",
                        "",
                        str(physical.get("name", "")).lower(),
                    )
                    drivers.add((logical.ref.upper(), pin_name))
                    outputs.add(
                        f"{logical.ref}:{number}({physical.get('name', '')})"
                    )
                # Package variants often expose the same regulator output on
                # several physical pads.  Those pads are one driver, whereas
                # different references or differently named outputs are a real
                # contention risk.
                if len(drivers) > 1:
                    power_output_conflicts.append(
                        f"{net.name} has power outputs {sorted(outputs)}"
                    )
            checks.append(CheckResult(
                name="single_power_output_per_net",
                ok=not power_output_conflicts,
                message=(
                    "multiple power-output pins must not drive one rail: "
                    f"{power_output_conflicts}"
                ),
            ))
            power_input_source_gaps: list[str] = []
            for net in artifact.nets:
                if (
                    net.name.lower() in supply_names
                    or net.name.lower() in ground_names
                ):
                    continue
                inputs: set[str] = set()
                outputs: set[str] = set()
                for logical in net.pins:
                    part = ref_parts.get(logical.ref)
                    part_pins = (
                        symbols.symbol_pins(part.symbol) or []
                        if part is not None
                        else []
                    )
                    number = _resolve_logical_pin(part_pins, logical.pin)
                    physical = next(
                        (
                            pin
                            for pin in part_pins
                            if str(pin.get("number", "")) == number
                        ),
                        None,
                    )
                    if physical is None:
                        continue
                    pin_type = str(physical.get("type", "")).lower()
                    endpoint = (
                        f"{logical.ref}:{number}({physical.get('name', '')})"
                    )
                    if pin_type in {"power_in", "power_input"}:
                        inputs.add(endpoint)
                    elif pin_type in {"power_out", "power_output"}:
                        outputs.add(endpoint)
                if inputs and not outputs:
                    power_input_source_gaps.append(
                        f"{net.name} has power inputs {sorted(inputs)} but no "
                        "power output and is not a declared supply rail"
                    )
            checks.append(CheckResult(
                name="power_input_net_has_source",
                ok=not power_input_source_gaps,
                message=(
                    "power-input-only islands must join a declared supply rail or "
                    "a net with one real power output: "
                    f"{power_input_source_gaps}"
                ),
            ))

            no_connect_numbers: set[str] = set()
            invalid_no_connects: list[str] = []
            for logical in artifact.no_connect_pins:
                part = ref_parts.get(logical.ref)
                part_pins = (
                    symbols.symbol_pins(part.symbol) or []
                    if part is not None
                    else []
                )
                number = _resolve_logical_pin(part_pins, logical.pin)
                if number is None:
                    invalid_no_connects.append(logical.key())
                    continue
                no_connect_numbers.add(f"{logical.ref}:{number}")
            checks.append(CheckResult(
                name="no_connect_pins_resolve",
                ok=not invalid_no_connects,
                message=(
                    "no-connect pins not found on the selected symbol: "
                    f"{invalid_no_connects}"
                ),
            ))

            conflicting_no_connects = sorted(
                connected_numbers & no_connect_numbers
            )
            checks.append(CheckResult(
                name="no_connect_pins_not_connected",
                ok=not conflicting_no_connects,
                message=(
                    "pins cannot be both connected and marked no-connect: "
                    f"{conflicting_no_connects}"
                ),
            ))

            unused_components: list[str] = []
            missing_pin_disposition: list[str] = []
            abandoned_small_parts: list[str] = []
            for part in combined_selection.parts:
                part_pins = symbols.symbol_pins(part.symbol) or []
                pin_by_number = {
                    str(pin["number"]): pin
                    for pin in part_pins
                    if pin["number"]
                }
                if not pin_by_number:
                    continue
                connected_for_part = {
                    key.partition(":")[2]
                    for key in connected_numbers
                    if key.startswith(f"{part.ref}:")
                }
                no_connect_for_part = {
                    key.partition(":")[2]
                    for key in no_connect_numbers
                    if key.startswith(f"{part.ref}:")
                }
                if not connected_for_part:
                    unused_components.append(part.ref)
                for number, pin in pin_by_number.items():
                    name = str(pin.get("name", "")).strip().upper()
                    pin_type = str(pin.get("type", "")).lower()
                    library_no_connect = (
                        pin_type == "no_connect"
                        or name in {"NC", "N/C", "DNC", "DO_NOT_CONNECT"}
                    )
                    if (
                        number not in connected_for_part
                        and number not in no_connect_for_part
                        and not library_no_connect
                    ):
                        missing_pin_disposition.append(
                            f"{part.ref}:{number}({name or '~'})"
                        )
                if (
                    len(pin_by_number) <= 4
                    and not _is_connector_part(part)
                    and not _is_mounting_hole_role(part.role)
                ):
                    for number in no_connect_for_part:
                        pin = pin_by_number.get(number, {})
                        name = str(pin.get("name", "")).strip().upper()
                        pin_type = str(pin.get("type", "")).lower()
                        if (
                            pin_type != "no_connect"
                            and name not in {"NC", "N/C", "DNC", "DO_NOT_CONNECT"}
                        ):
                            abandoned_small_parts.append(
                                f"{part.ref}:{number}({name or '~'})"
                            )
            checks.append(CheckResult(
                name="selected_components_used",
                ok=not unused_components,
                message=(
                    "selected components absent from every electrical net: "
                    f"{sorted(unused_components)}"
                ),
            ))
            checks.append(CheckResult(
                name="component_pins_accounted",
                ok=not missing_pin_disposition,
                message=(
                    "real pins must be connected or explicitly marked no-connect: "
                    f"{sorted(missing_pin_disposition)}"
                ),
            ))
            checks.append(CheckResult(
                name="small_parts_fully_connected",
                ok=not abandoned_small_parts,
                message=(
                    "selected <=4-pin parts cannot be abandoned with no-connect "
                    f"markers: {sorted(abandoned_small_parts)}"
                ),
            ))
            checks.extend(
                _functional_connection_checks(
                    combined_selection,
                    artifact,
                    state.requirement_text,
                )
            )
        return checks

    def rollback_target(
        self,
        state: PipelineState,
        artifact: BaseModel,
        checks: list[CheckResult],
    ) -> PipelineStep | None:
        """Replan parts only after bounded connection repair cannot converge."""

        upstream_selection_checks = {
            "selected_components_used",
            "component_pins_accounted",
            "small_parts_fully_connected",
            "two_terminal_parts_span_distinct_nets",
        }
        for check in checks:
            if check.ok or check.severity != Severity.ERROR:
                continue
            if (
                check.name in upstream_selection_checks
                or check.name.endswith("_selectable_termination_across_pair")
                or check.name.startswith("additional_parts:")
            ):
                return PipelineStep.SELECTION
        return None

    def run(self, state: PipelineState, ctx: PipelineContext) -> StepResult:
        result = super().run(state, ctx)
        artifact = state.artifact(PipelineStep.SCH_CONNECTIONS)
        selection = state.artifact(PipelineStep.SELECTION)
        report = state.connection_synthesis_report
        if (
            isinstance(artifact, NetlistIntent)
            and isinstance(selection, SelectionPlan)
            and report is not None
        ):
            refreshed = connection_synthesis_report(
                selection,
                artifact,
                mode=report.mode,
                estimate=report.estimate,
                checkpoint=state.connection_synthesis_checkpoint,
                llm_calls=report.llm_calls,
                round_llm_calls=report.round_llm_calls,
                resumable=report.resumable,
                stop_reason=report.stop_reason,
            )
            if state.connection_synthesis_checkpoint is None:
                refreshed = ConnectionSynthesisReport.model_validate({
                    **refreshed.model_dump(mode="json"),
                    "planned_batches": report.planned_batches,
                    "completed_batches": report.completed_batches,
                    "pending_batches": report.pending_batches,
                    "skipped_batches": report.skipped_batches,
                    "failed_batches": report.failed_batches,
                })
            state.connection_synthesis_report = refreshed
            result.summary = (
                f"{self.summarize(artifact)}; connectivity coverage "
                f"{refreshed.total_pins - refreshed.undisposed_pins}/"
                f"{refreshed.total_pins} ({refreshed.coverage_ratio:.1%}); "
                f"mode={refreshed.mode}"
            )
            if refreshed.stop_reason:
                result.summary += f"; stop_reason={refreshed.stop_reason[:500]}"
        if result.blocked:
            return result
        if (
            isinstance(artifact, NetlistIntent)
            and artifact.additional_parts
            and isinstance(selection, SelectionPlan)
        ):
            merged_selection = SelectionPlan(
                parts=[*selection.parts, *artifact.additional_parts],
                rationale=selection.rationale,
            )
            state.artifacts[PipelineStep.SELECTION] = merged_selection
            for previous_result in state.results:
                if previous_result.step == PipelineStep.SELECTION:
                    previous_result.summary = SelectionStep().summarize(
                        merged_selection
                    )
                    break
        return result

    def summarize(self, artifact: BaseModel) -> str:
        assert isinstance(artifact, NetlistIntent)
        return (
            f"{len(artifact.nets)} nets, supply={artifact.supply_nets}, "
            f"gnd={artifact.ground_net}, "
            f"additional_parts={len(artifact.additional_parts)}"
        )


def _resolve_logical_pin(pins: list[dict[str, object]], logical: str) -> str | None:
    """Map a logical pin (name or number) to a real device pin number.

    Resolution order: exact number, exact name, name token match (e.g. 'XTAL1'
    inside 'XTAL1/PB6'), then substring. If still unresolved and the name is a
    known power/ground synonym (e.g. 'AGND' on a part with no dedicated analog
    ground), retry with the base name ('GND'). Returns the pin number or None.
    """
    ll = logical.strip().lower()
    if not ll:
        return None

    def _try(term: str) -> str | None:
        for p in pins:  # exact number
            if str(p["number"]).lower() == term:
                return str(p["number"])
        for p in pins:  # exact name
            if str(p["name"]).lower() == term:
                return str(p["number"])
        for p in pins:  # token inside a slash/brace-delimited name
            tokens = re.split(r"[/~{}() ]+", str(p["name"]).lower())
            if term in [t for t in tokens if t]:
                return str(p["number"])
        for p in pins:  # substring fallback
            if term in str(p["name"]).lower():
                return str(p["number"])
        return None

    hit = _try(ll)
    if hit is None and ll in _PIN_SYNONYMS:
        hit = _try(_PIN_SYNONYMS[ll])
    if hit is None and len(pins) == 2 and (ll in _ANODE_TERMS or ll in _CATHODE_TERMS):
        # 2-terminal polarized part (diode/LED/TVS): KiCad names pins K/A (or
        # A1/A2 for a bidirectional TVS), never 'anode'/'cathode'. Map by the
        # A/K name when present, else by pin-number order, keeping the two
        # terminals distinct.
        by_num = sorted(pins, key=lambda p: str(p["number"]))
        if ll in _ANODE_TERMS:
            named = next((p for p in pins if str(p["name"]).upper() == "A"), None)
            return str((named or by_num[-1])["number"])
        named = next((p for p in pins if str(p["name"]).upper() == "K"), None)
        return str((named or by_num[0])["number"])
    if hit is None and len(pins) == 2 and (ll in _FIRST_TERMS or ll in _SECOND_TERMS):
        # Generic 2-terminal part (fuse/ferrite/jumper) with unnamed numbered
        # pins: map input-side terms to pin 1, output-side terms to pin 2.
        by_num = sorted(pins, key=lambda p: str(p["number"]))
        return str((by_num[0] if ll in _FIRST_TERMS else by_num[-1])["number"])
    return hit


# Power/ground synonyms: many parts have no dedicated analog rail pin, so the
# analog name collapses onto its base (AGND->GND, VDDA->AVCC, ...). Used only as
# a last-resort fallback after direct name/number/token matching fails.
_PIN_SYNONYMS = {
    "agnd": "gnd",
    "dgnd": "gnd",
    "pgnd": "gnd",
    "vss": "gnd",
    "gnda": "gnd",
    "avdd": "avcc",
    "vdda": "avcc",
    "dvcc": "vcc",
    "vddio": "vcc",
    "vdd": "vcc",
    # Regulator / supply in-out.
    "vout": "out",
    "vo": "out",
    "vin": "in",
    "vi": "in",
    # MOSFET terminals.
    "drain": "d",
    "gate": "g",
    "source": "s",
    # USB data pair (KiCad names them "D+"/"D-").
    "dplus": "d+",
    "dminus": "d-",
    "dp": "d+",
    "dm": "d-",
    "d_plus": "d+",
    "d_minus": "d-",
    "usbdp": "d+",
    "usbdm": "d-",
}

# Polarity terms for 2-terminal parts, used only as a positional fallback.
_ANODE_TERMS = {"anode", "an", "a", "pos", "positive", "+"}
_CATHODE_TERMS = {"cathode", "cat", "cath", "k", "c", "neg", "negative", "-"}
# Generic 2-terminal in/out terms (fuse, ferrite, jumper, crystal) -> pin1/pin2.
_FIRST_TERMS = {"1", "in", "input", "vin", "vi", "p1", "pri", "primary", "l1",
                "x1", "xtal1", "xin", "osc1"}
_SECOND_TERMS = {"2", "out", "output", "vout", "vo", "p2", "sec", "secondary", "l2",
                 "x2", "xtal2", "xout", "osc2"}


def _library_no_connect(pin: dict[str, object]) -> bool:
    name = str(pin.get("name", "")).strip().upper()
    pin_type = str(pin.get("type", "")).lower()
    return (
        pin_type == "no_connect"
        or name in {"NC", "N/C", "DNC", "DO_NOT_CONNECT"}
    )


@dataclass
class _ConnectivityView:
    """Resolved physical-pin view used by role-based topology checks."""

    parts: dict[str, SelectedPart]
    pins: dict[str, list[dict[str, object]]]
    pin_nets: dict[tuple[str, str], str]
    no_connect: set[tuple[str, str]]
    ground_nets: set[str]
    supply_nets: set[str]

    @classmethod
    def build(
        cls,
        selection: SelectionPlan,
        intent: NetlistIntent,
    ) -> _ConnectivityView:
        parts = {part.ref: part for part in selection.parts}
        pins = {
            ref: symbols.symbol_pins(part.symbol) or []
            for ref, part in parts.items()
        }
        pin_nets: dict[tuple[str, str], str] = {}
        for net in intent.nets:
            for logical in net.pins:
                number = _resolve_logical_pin(
                    pins.get(logical.ref, []),
                    logical.pin,
                )
                if number is not None:
                    pin_nets[(logical.ref, number)] = net.name
        no_connect: set[tuple[str, str]] = set()
        for logical in intent.no_connect_pins:
            number = _resolve_logical_pin(
                pins.get(logical.ref, []),
                logical.pin,
            )
            if number is not None:
                no_connect.add((logical.ref, number))
        ground_nets = {
            intent.ground_net,
            *(net.name for net in intent.nets if net.kind == "ground"),
        }
        supply_nets = {
            *intent.supply_nets,
            *(net.name for net in intent.nets if net.kind == "power"),
        } - ground_nets
        return cls(
            parts=parts,
            pins=pins,
            pin_nets=pin_nets,
            no_connect=no_connect,
            ground_nets=ground_nets,
            supply_nets=supply_nets,
        )

    def part_nets(self, part: SelectedPart) -> set[str]:
        return {
            net
            for (ref, _number), net in self.pin_nets.items()
            if ref == part.ref
        }

    def named_pin_net(
        self,
        part: SelectedPart,
        *names: str,
    ) -> str | None:
        for name in names:
            number = _resolve_logical_pin(self.pins.get(part.ref, []), name)
            if number is not None:
                net = self.pin_nets.get((part.ref, number))
                if net is not None:
                    return net
        return None

    def net_has_mcu_pin(self, net: str, *tokens: str) -> bool:
        wanted = tuple(token.upper() for token in tokens)
        for ref, part in self.parts.items():
            if "mcu" not in part.role.lower():
                continue
            for pin in self.pins.get(ref, []):
                number = str(pin.get("number", ""))
                if self.pin_nets.get((ref, number)) != net:
                    continue
                name = str(pin.get("name", "")).upper()
                if any(token in name for token in wanted):
                    return True
        return False

    def net_has_any_mcu_pin(self, net: str) -> bool:
        return any(
            self.pin_nets.get((ref, str(pin.get("number", "")))) == net
            for ref, part in self.parts.items()
            if "mcu" in part.role.lower()
            for pin in self.pins.get(ref, [])
        )


def _role_parts(
    view: _ConnectivityView,
    *tokens: str,
) -> list[SelectedPart]:
    lowered = tuple(token.lower() for token in tokens)
    return [
        part
        for part in view.parts.values()
        if all(token in part.role.lower() for token in lowered)
    ]


def _two_terminal_grounded(
    view: _ConnectivityView,
    part: SelectedPart | None,
    signal_net: str,
) -> bool:
    if part is None:
        return False
    nets = view.part_nets(part)
    return (
        len(nets) == 2
        and signal_net in nets
        and bool(nets & view.ground_nets)
    )


def _two_terminal_self_short_checks(
    view: _ConnectivityView,
) -> list[CheckResult]:
    """Reject physical two-terminal parts whose terminals share one net.

    KiCad allows both pads of a passive to carry the same net, so ERC/DRC
    cannot distinguish an intentional same-net copper shape from a miswired
    fuse, capacitor, TVS, inductor, resistor, LED, crystal, or link.  At the
    logical-design stage every real two-terminal component must span two
    distinct nets to perform a function.
    """

    failures: list[str] = []
    for part in view.parts.values():
        numbers = {
            str(pin.get("number", ""))
            for pin in view.pins.get(part.ref, [])
            if str(pin.get("number", ""))
        }
        if len(numbers) != 2:
            continue
        assigned = {
            number: view.pin_nets.get((part.ref, number))
            for number in numbers
        }
        nets = {net for net in assigned.values() if net is not None}
        if len(nets) == 1 and all(assigned.values()):
            failures.append(
                f"{part.ref} terminals {sorted(numbers)} both on "
                f"{next(iter(nets))} (role={part.role})"
            )
    return [
        CheckResult(
            name="two_terminal_parts_span_distinct_nets",
            ok=not failures,
            message=(
                "real two-terminal components must not have both terminals on "
                f"the same net: {failures}"
            ),
        )
    ]


def _critical_function_pin_checks(
    view: _ConnectivityView,
) -> list[CheckResult]:
    role_tokens = (
        "buck",
        "regulator",
        "ldo",
        "power_mux",
        "power_path",
        "ideal_diode",
        "reverse_blocking",
        "flash",
        "can_transceiver",
    )
    abandoned_hard: list[str] = []
    abandoned_advisory: list[str] = []
    for part in view.parts.values():
        role = part.role.lower()
        if not any(token in role for token in role_tokens):
            continue
        for pin in view.pins.get(part.ref, []):
            number = str(pin.get("number", ""))
            if (
                number
                and not _library_no_connect(pin)
                and (part.ref, number) in view.no_connect
            ):
                item = f"{part.ref}:{number}({pin.get('name') or '~'})"
                pin_type = str(pin.get("type", "")).lower()
                pin_name = str(pin.get("name", "")).upper()
                if (
                    pin_type in {"power_in", "power_out"}
                    or any(
                        token in pin_name
                        for token in ("BOOT", "RESET", "NRST", "VCAP")
                    )
                ):
                    abandoned_hard.append(item)
                else:
                    abandoned_advisory.append(item)
    return [
        CheckResult(
            name="critical_power_reset_pins_connected",
            ok=not abandoned_hard,
            message=(
                "power, reset, boot, and internal-regulator capacitor pins "
                f"cannot be abandoned as no-connect: {sorted(abandoned_hard)}"
            ),
        ),
        CheckResult(
            name="functional_control_pins_reviewed",
            ok=not abandoned_advisory,
            severity=Severity.WARNING,
            message=(
                "functional IC control pins were explicitly left open; confirm "
                "each choice against the selected device datasheet during review: "
                f"{sorted(abandoned_advisory)}"
            ),
        ),
    ]


def _power_pin_rail_checks(view: _ConnectivityView) -> list[CheckResult]:
    """Keep real ground and positive-supply pins on the correct rail class."""
    misplaced: list[str] = []
    for ref, pins in view.pins.items():
        for pin in pins:
            number = str(pin.get("number", ""))
            if not number or _library_no_connect(pin):
                continue
            name = re.sub(
                r"[^A-Z0-9_+-]",
                "",
                str(pin.get("name", "")).upper(),
            )
            net = view.pin_nets.get((ref, number))
            is_ground = name.startswith(("GND", "AGND", "PGND", "VSS"))
            is_positive_supply = name.startswith(
                ("VDD", "VDDA", "VCC", "AVCC", "VBAT")
            )
            if is_ground and net not in view.ground_nets:
                misplaced.append(f"{ref}:{number}({name})->{net or 'unconnected'}")
            elif is_positive_supply and net not in view.supply_nets:
                misplaced.append(f"{ref}:{number}({name})->{net or 'unconnected'}")
    return [
        CheckResult(
            name="power_pin_rail_class",
            ok=not misplaced,
            message=(
                "real ground pins must join ground_net and positive supply pins "
                f"must join a declared supply rail: {sorted(misplaced)}"
            ),
        )
    ]


def _crystal_topology_checks(view: _ConnectivityView) -> list[CheckResult]:
    checks: list[CheckResult] = []
    for part in view.parts.values():
        role = part.role.lower()
        if (
            "crystal" not in role
            or not (
                part.ref.upper().startswith("Y")
                or "crystal" in part.symbol.lower()
            )
        ):
            continue
        real_pins = [
            pin for pin in view.pins.get(part.ref, [])
            if pin.get("number") and not _library_no_connect(pin)
        ]
        if len(real_pins) != 2:
            continue
        nets = [
            view.pin_nets.get((part.ref, str(pin["number"])))
            for pin in real_pins
        ]
        ok = (
            None not in nets
            and len(set(nets)) == 2
            and not (set(nets) & view.ground_nets)
            and not (set(nets) & view.supply_nets)
        )
        checks.append(CheckResult(
            name=f"crystal_two_distinct_signal_nets:{part.ref}",
            ok=ok,
            message=(
                f"{part.ref} crystal terminals must connect the two distinct MCU "
                f"oscillator nets, never GND/supply or one shared net; got {nets}"
            ),
        ))
    return checks


def _is_led_emitter_part(part: SelectedPart) -> bool:
    """Identify the emitting diode from grounded symbol evidence, not its role."""

    library, _, symbol_name = part.symbol.lower().partition(":")
    if library == "led" or re.match(r"^led(?:_|$)", symbol_name):
        return True
    description = symbols.symbol_properties(part.symbol).get(
        "Description",
        "",
    ).lower()
    return bool(
        re.search(
            r"\b(?:light|infrared|ultraviolet)[ -]emitting diode\b",
            description,
        )
    )


def _led_series_checks(view: _ConnectivityView) -> list[CheckResult]:
    checks: list[CheckResult] = []
    for led in view.parts.values():
        role = led.role.lower()
        if not _is_led_emitter_part(led):
            continue
        channel = next(
            (token for token in ("power", "system", "status", "user")
             if token in role),
            None,
        )
        if channel is None:
            continue
        resistors = [
            part
            for part in view.parts.values()
            if part.ref.upper().startswith("R")
            and "led" in part.role.lower()
            and any(
                token in part.role.lower()
                for token in ("current", "limit", "resistor")
            )
        ]
        led_nets = view.part_nets(led)
        valid = [
            resistor.ref
            for resistor in resistors
            if (
                len(led_nets) == 2
                and len(view.part_nets(resistor)) == 2
                and len(led_nets & view.part_nets(resistor)) == 1
                and not (
                    (led_nets & view.part_nets(resistor))
                    & (view.ground_nets | view.supply_nets)
                )
                and len(led_nets | view.part_nets(resistor)) == 3
            )
        ]
        checks.append(CheckResult(
            name=f"led_current_limit_in_series:{led.ref}",
            ok=bool(valid),
            message=(
                f"{led.ref} must be in series with its channel current-limit "
                f"resistor, not wired in parallel; LED nets={sorted(led_nets)}, "
                f"candidate resistors={[p.ref for p in resistors]}"
            ),
        ))
    return checks


def _swd_topology_checks(view: _ConnectivityView) -> list[CheckResult]:
    checks: list[CheckResult] = []
    for connector in _role_parts(view, "swd"):
        pins = view.pins.get(connector.ref, [])
        if len({str(pin.get("number", "")) for pin in pins}) < 10:
            continue
        pin_net = {
            number: view.pin_nets.get((connector.ref, number))
            for number in map(str, range(1, 11))
        }
        failures: list[str] = []
        if pin_net["1"] not in view.supply_nets:
            failures.append(f"pin1 VTref -> supply, got {pin_net['1']}")
        for number in ("3", "5", "9"):
            if pin_net[number] not in view.ground_nets:
                failures.append(
                    f"pin{number} GND/GNDDetect -> ground, got {pin_net[number]}"
                )
        expected_mcu = {
            "2": ("SWDIO", "JTMS"),
            "4": ("SWCLK", "JTCK"),
            "10": ("NRST",),
        }
        for number, tokens in expected_mcu.items():
            net = pin_net[number]
            normalized_net = re.sub(r"[^A-Z0-9]", "", (net or "").upper())
            semantically_named = any(
                re.sub(r"[^A-Z0-9]", "", token.upper()) in normalized_net
                for token in tokens
            )
            grounded_to_mcu = (
                net is not None
                and (
                    view.net_has_mcu_pin(net, *tokens)
                    or (
                        semantically_named
                        and view.net_has_any_mcu_pin(net)
                    )
                )
            )
            if not grounded_to_mcu:
                failures.append(
                    f"pin{number} -> MCU {'/'.join(tokens)}, got {net}"
                )
        pin6_net = pin_net["6"]
        pin6_nc = (connector.ref, "6") in view.no_connect
        if pin6_net is not None and not (
            view.net_has_mcu_pin(pin6_net, "SWO", "JTDO")
            or (
                any(token in re.sub(r"[^A-Z0-9]", "", pin6_net.upper())
                    for token in ("SWO", "JTDO"))
                and view.net_has_any_mcu_pin(pin6_net)
            )
        ):
            failures.append(f"pin6 SWO or NC, got {pin6_net}")
        elif pin6_net is None and not pin6_nc:
            failures.append("pin6 SWO or explicit NC is not accounted")
        for number in ("7", "8"):
            if pin_net[number] is not None:
                failures.append(
                    f"pin{number} reserved/key must be NC, got {pin_net[number]}"
                )
            elif (connector.ref, number) not in view.no_connect:
                failures.append(f"pin{number} reserved/key lacks explicit NC")
        checks.append(CheckResult(
            name=f"cortex_swd_10pin_mapping:{connector.ref}",
            ok=not failures,
            message=(
                f"{connector.ref} must follow the standard 10-pin Cortex SWD "
                f"mapping; errors: {failures}"
            ),
        ))
    return checks


def _differential_pair_matches(bus: str, endpoints: set[str]) -> bool:
    normalized = {
        re.sub(r"[^a-z0-9]", "", net.lower())
        for net in endpoints
    }
    if bus == "can":
        return (
            any("canh" in name for name in normalized)
            and any("canl" in name for name in normalized)
        )
    if bus == "rs485":
        return (
            any(
                (
                    ("rs485" in name or "485" in name)
                    and name.endswith(("a", "p", "plus"))
                )
                or name in {"a", "p", "plus"}
                for name in normalized
            )
            and any(
                (
                    ("rs485" in name or "485" in name)
                    and name.endswith(("b", "n", "minus"))
                )
                or name in {"b", "n", "minus"}
                for name in normalized
            )
        )
    return False


def _differential_termination_topology_checks(
    view: _ConnectivityView,
    requirement: str,
) -> list[CheckResult]:
    checks: list[CheckResult] = []
    required_buses = _required_selectable_termination_buses(requirement)
    if not required_buses:
        required_buses = {
            bus
            for part in view.parts.values()
            if "termination" in part.role.lower()
            for bus in _role_bus_names(part.role)
        }
    for bus in sorted(required_buses):
        termination = _termination_parts_for_bus(view.parts.values(), bus)
        resistors = [
            part
            for part in termination
            if _is_120_ohm_termination_resistor(part)
        ]
        selectors = [
            part
            for part in termination
            if _is_termination_selector(part)
        ]
        chain_ok = False
        chain_detail: list[str] = []
        for resistor in resistors:
            resistor_nets = view.part_nets(resistor)
            for selector in selectors:
                if selector.ref == resistor.ref:
                    continue
                selector_nets = view.part_nets(selector)
                shared = resistor_nets & selector_nets
                endpoints = (resistor_nets | selector_nets) - shared
                valid = (
                    len(resistor_nets) == 2
                    and len(selector_nets) == 2
                    and len(shared) == 1
                    and len(endpoints) == 2
                    and not (endpoints & view.ground_nets)
                    and _differential_pair_matches(bus, endpoints)
                )
                chain_detail.append(
                    f"{resistor.ref}{sorted(resistor_nets)} + "
                    f"{selector.ref}{sorted(selector_nets)}"
                )
                chain_ok = chain_ok or valid
        checks.append(CheckResult(
            name=f"{bus}_selectable_termination_across_pair",
            ok=chain_ok,
            message=(
                f"the {bus.upper()} termination resistor and jumper/link must "
                "form one selectable series path across that bus pair, never to "
                f"GND; termination refs={[p.ref for p in termination]}, "
                f"candidates={chain_detail}"
            ),
        ))
    return checks


def _can_topology_checks(view: _ConnectivityView) -> list[CheckResult]:
    checks: list[CheckResult] = []

    tvs_parts = [
        part for part in view.parts.values()
        if "can" in part.role.lower()
        and any(token in part.role.lower() for token in ("tvs", "esd"))
    ]
    if tvs_parts:
        covered: set[str] = set()
        details: list[str] = []
        for part in tvs_parts:
            nets = view.part_nets(part)
            details.append(f"{part.ref}{sorted(nets)}")
            if not (nets & view.ground_nets):
                continue
            for net in nets - view.ground_nets:
                normalized = re.sub(r"[^a-z0-9]", "", net.lower())
                if "canh" in normalized:
                    covered.add("CANH")
                if "canl" in normalized:
                    covered.add("CANL")
        checks.append(CheckResult(
            name="can_tvs_connected_to_both_lines",
            ok=covered == {"CANH", "CANL"},
            message=(
                "real grounded TVS channels must protect both CANH and CANL; "
                f"covered={sorted(covered)}, connections={details}"
            ),
        ))
    return checks


def _bounded_interface_connector_checks(
    view: _ConnectivityView,
) -> list[CheckResult]:
    """Reject unrelated signals used only to fill spare interface pins."""

    checks: list[CheckResult] = []
    for connector in view.parts.values():
        role = connector.role.lower()
        if not _is_connector_part(connector):
            continue
        nets = view.part_nets(connector)
        normalized = {
            net: re.sub(r"[^a-z0-9]", "", net.lower())
            for net in nets
        }
        failures: list[str] = []
        if "i2c" in role:
            allowed = {
                net
                for net, name in normalized.items()
                if (
                    net in view.supply_nets
                    or net in view.ground_nets
                    or "sda" in name
                    or "scl" in name
                )
            }
            if not any("sda" in name for name in normalized.values()):
                failures.append("missing SDA")
            if not any("scl" in name for name in normalized.values()):
                failures.append("missing SCL")
            if not (nets & view.supply_nets):
                failures.append("missing interface supply")
            if not (nets & view.ground_nets):
                failures.append("missing interface ground")
            extras = sorted(nets - allowed)
            if extras:
                failures.append(f"unrelated connected nets {extras}")
        elif "rs485" in role or "can" in role:
            bus = "rs485" if "rs485" in role else "can"
            allowed = {
                net
                for net, name in normalized.items()
                if (
                    net in view.supply_nets
                    or net in view.ground_nets
                    or "shield" in name
                    or "chassis" in name
                    or (
                        bus == "rs485"
                        and (
                            "rs485" in name
                            or name in {"a", "b", "p", "n", "plus", "minus"}
                        )
                    )
                    or (
                        bus == "can"
                        and (
                            "canh" in name
                            or "canl" in name
                            or name in {"h", "l"}
                        )
                    )
                )
            }
            if not _differential_pair_matches(bus, nets):
                failures.append(f"missing {bus.upper()} differential pair")
            if not (nets & view.ground_nets):
                failures.append("missing interface ground")
            extras = sorted(nets - allowed)
            if extras:
                failures.append(f"unrelated connected nets {extras}")
        if failures:
            checks.append(CheckResult(
                name=f"bounded_interface_connector:{connector.ref}",
                ok=False,
                message=(
                    f"{connector.ref} ({connector.role}) may expose only its "
                    "declared interface, optional supply/shield, and ground; "
                    f"unused pins must be explicit no-connects: {failures}"
                ),
            ))
    return checks


def _debug_connector_topology_checks(
    view: _ConnectivityView,
) -> list[CheckResult]:
    """Require selected debug/programming connectors to be electrically useful."""

    checks: list[CheckResult] = []
    for connector in view.parts.values():
        role = connector.role.lower()
        if (
            not _is_connector_part(connector)
            or not connector.ref.upper().startswith(("J", "P", "CN"))
            or not any(token in role for token in ("debug", "jtag", "swd", "uart"))
        ):
            continue
        nets = view.part_nets(connector)
        signal_nets = nets - view.supply_nets - view.ground_nets
        mcu_signal_nets = {
            net for net in signal_nets if view.net_has_any_mcu_pin(net)
        }
        failures: list[str] = []
        if not (nets & view.ground_nets):
            failures.append("missing reference ground")
        if len(mcu_signal_nets) < 2:
            failures.append(
                "fewer than two connector signal nets reach the selected MCU; "
                f"got {sorted(mcu_signal_nets)}"
            )
        checks.append(CheckResult(
            name=f"debug_connector_end_to_end:{connector.ref}",
            ok=not failures,
            message=(
                f"{connector.ref} ({connector.role}) must expose a usable "
                "programming/debug path with ground and at least two real MCU "
                f"signals: {failures}"
            ),
        ))
    return checks


def _requirement_uses_microsd_spi(requirement: str) -> bool:
    lower = requirement.lower()
    index = lower.find("microsd")
    if index < 0:
        return False
    local = lower[index:index + 320]
    return "spi" in local and "sdio" not in local


def _microsd_spi_topology_checks(
    view: _ConnectivityView,
    requirement: str,
) -> list[CheckResult]:
    if not _requirement_uses_microsd_spi(requirement):
        return []
    sockets = [
        part for part in view.parts.values()
        if "microsd" in part.role.lower()
        and any(token in part.role.lower() for token in ("socket", "connector"))
    ]
    checks: list[CheckResult] = []
    for socket in sockets:
        signals = {
            "cs": view.named_pin_net(socket, "DAT3/CD", "DAT3"),
            "mosi": view.named_pin_net(socket, "CMD"),
            "clk": view.named_pin_net(socket, "CLK"),
            "miso": view.named_pin_net(socket, "DAT0"),
        }
        failures: list[str] = []
        for channel, net in signals.items():
            if (
                net is None
                or net in view.ground_nets
                or net in view.supply_nets
                or not view.net_has_any_mcu_pin(net)
            ):
                failures.append(
                    f"{channel.upper()} socket net must reach one MCU pin, got {net}"
                )
            pullups = [
                part for part in view.parts.values()
                if "microsd" in part.role.lower()
                and channel in part.role.lower()
                and "pullup" in part.role.lower()
            ]
            if pullups and not any(
                net in view.part_nets(part)
                and bool(view.part_nets(part) & view.supply_nets)
                and len(view.part_nets(part)) == 2
                for part in pullups
            ):
                failures.append(
                    f"{channel.upper()} pull-up is not on socket net {net}: "
                    f"{[(p.ref, sorted(view.part_nets(p))) for p in pullups]}"
                )

        esd_channels = {
            "cmd": signals["mosi"],
            "dat0": signals["miso"],
            "clk": signals["clk"],
        }
        for channel, net in esd_channels.items():
            protectors = [
                part for part in view.parts.values()
                if "microsd" in part.role.lower()
                and channel in part.role.lower()
                and any(
                    token in part.role.lower()
                    for token in ("esd", "tvs", "protection")
                )
            ]
            if protectors and not any(
                _two_terminal_grounded(view, part, net or "")
                for part in protectors
            ):
                failures.append(
                    f"{channel.upper()} ESD is not from socket net {net} to "
                    f"ground: {[(p.ref, sorted(view.part_nets(p))) for p in protectors]}"
                )

        for pin_name in ("DAT1", "DAT2"):
            number = _resolve_logical_pin(
                view.pins.get(socket.ref, []),
                pin_name,
            )
            if number is None:
                continue
            net = view.pin_nets.get((socket.ref, number))
            if (
                net is not None
                and view.net_has_any_mcu_pin(net)
            ):
                failures.append(
                    f"unused SPI-mode {pin_name} must not consume an unrelated "
                    f"MCU GPIO; got {net}"
                )
            if net is None and (socket.ref, number) not in view.no_connect:
                failures.append(
                    f"unused SPI-mode {pin_name} needs an explicit no-connect "
                    "or a documented pull-up"
                )
        checks.append(CheckResult(
            name=f"microsd_spi_bus:{socket.ref}",
            ok=not failures,
            message=(
                f"{socket.ref} SPI mode requires DAT3=CS, CMD=MOSI, CLK, and "
                f"DAT0=MISO to reach the MCU with their selected pull-ups/ESD; "
                f"DAT1/DAT2 cannot be dummy GPIOs: {failures}"
            ),
        ))
    return checks


def _rs485_topology_checks(view: _ConnectivityView) -> list[CheckResult]:
    checks: list[CheckResult] = []
    transceivers = [
        part for part in view.parts.values()
        if "rs485" in part.role.lower()
        and "transceiver" in part.role.lower()
    ]
    connectors = [
        part for part in view.parts.values()
        if "rs485" in part.role.lower() and _is_connector_part(part)
    ]
    for transceiver in transceivers:
        bus_a = view.named_pin_net(transceiver, "A")
        bus_b = view.named_pin_net(transceiver, "B")
        driver = view.named_pin_net(transceiver, "DI", "D")
        receiver = view.named_pin_net(transceiver, "RO", "R")
        enable = view.named_pin_net(transceiver, "DE")
        receive_enable = view.named_pin_net(transceiver, "~RE", "/RE", "RE")
        failures: list[str] = []
        for label, net in (
            ("DI", driver),
            ("RO", receiver),
            ("DE", enable),
            ("/RE", receive_enable),
        ):
            if net is None or not view.net_has_any_mcu_pin(net):
                failures.append(f"{label} must reach an MCU GPIO, got {net}")
        for label, net in (("A", bus_a), ("B", bus_b)):
            if net is None or not any(
                net in view.part_nets(connector)
                for connector in connectors
            ):
                failures.append(
                    f"bus {label} must reach an RS485 connector, got {net}"
                )
            protectors = [
                part for part in view.parts.values()
                if "rs485" in part.role.lower()
                and label.lower() in part.role.lower()
                and any(token in part.role.lower() for token in ("tvs", "esd"))
            ]
            if protectors and not any(
                _two_terminal_grounded(view, part, net or "")
                for part in protectors
            ):
                failures.append(
                    f"bus {label} TVS must shunt {net} to ground: "
                    f"{[(p.ref, sorted(view.part_nets(p))) for p in protectors]}"
                )

        for label, bus_net, rail_kind in (
            ("a", bus_a, "supply"),
            ("b", bus_b, "ground"),
        ):
            resistors = [
                part for part in view.parts.values()
                if "rs485" in part.role.lower()
                and f"bias_{label}" in part.role.lower()
                and "jumper" not in part.role.lower()
            ]
            jumpers = [
                part for part in view.parts.values()
                if "rs485" in part.role.lower()
                and f"bias_{label}" in part.role.lower()
                and "jumper" in part.role.lower()
            ]
            if not resistors:
                continue
            target_rails = (
                view.supply_nets if rail_kind == "supply" else view.ground_nets
            )
            if jumpers:
                chain_ok = any(
                    len(view.part_nets(resistor)) == 2
                    and len(view.part_nets(jumper)) == 2
                    and len(
                        view.part_nets(resistor) & view.part_nets(jumper)
                    ) == 1
                    and bus_net in (
                        view.part_nets(resistor) | view.part_nets(jumper)
                    )
                    and bool(
                        (
                            view.part_nets(resistor)
                            | view.part_nets(jumper)
                        )
                        & target_rails
                    )
                    for resistor in resistors
                    for jumper in jumpers
                )
            else:
                chain_ok = any(
                    bus_net in view.part_nets(resistor)
                    and bool(view.part_nets(resistor) & target_rails)
                    and len(view.part_nets(resistor)) == 2
                    for resistor in resistors
                )
            if not chain_ok:
                failures.append(
                    f"selectable bias {label.upper()} path must form one series "
                    f"chain between {bus_net} and {rail_kind}; resistors="
                    f"{[(p.ref, sorted(view.part_nets(p))) for p in resistors]}, "
                    f"jumpers={[(p.ref, sorted(view.part_nets(p))) for p in jumpers]}"
                )
        checks.append(CheckResult(
            name=f"rs485_transceiver_bus:{transceiver.ref}",
            ok=not failures,
            message=(
                f"{transceiver.ref} logic, differential pair, TVS, and optional "
                f"bias links must be end-to-end connected: {failures}"
            ),
        ))
    return checks


def _mcu_control_topology_checks(
    view: _ConnectivityView,
) -> list[CheckResult]:
    checks: list[CheckResult] = []
    for mcu in (
        part for part in view.parts.values()
        if _is_mcu_role(part.role)
    ):
        reset = view.named_pin_net(mcu, "NRST", "RESET", "EN")
        boot = view.named_pin_net(mcu, "BOOT0", "BOOT", "IO0", "GPIO0")
        failures: list[str] = []
        reset_parts = [
            part for part in view.parts.values()
            if (
                any(token in part.role.lower() for token in ("reset", "en_"))
                and not any(
                    domain in part.role.lower()
                    for domain in (
                        "sensor",
                        "buck",
                        "regulator",
                        "power_mux",
                        "power_path",
                        "rs485",
                        "can_",
                        "flash",
                        "microsd",
                        "usb",
                    )
                )
                and part.ref != mcu.ref
            )
        ]
        for part in reset_parts:
            role = part.role.lower()
            nets = view.part_nets(part)
            is_switch = (
                part.ref.upper().startswith("SW")
                or part.symbol.lower().startswith("switch:")
            )
            if is_switch:
                ok = reset in nets and bool(nets & view.ground_nets)
            elif part.ref.upper().startswith("C") or "capacitor" in role:
                ok = _two_terminal_grounded(view, part, reset or "")
            else:
                ok = (
                    reset in nets
                    and bool(nets & view.supply_nets)
                    and len(nets) == 2
                )
            if not ok:
                failures.append(
                    f"{part.ref} ({part.role}) must support MCU reset net "
                    f"{reset}, got {sorted(nets)}"
                )
        boot_parts = [
            part for part in view.parts.values()
            if "boot" in part.role.lower()
            and "bootstrap" not in part.role.lower()
        ]
        for part in boot_parts:
            role = part.role.lower()
            nets = view.part_nets(part)
            is_switch = (
                part.ref.upper().startswith("SW")
                or part.symbol.lower().startswith("switch:")
            )
            is_capacitor = (
                part.ref.upper().startswith("C")
                or any(token in role for token in ("capacitor", "decoupling"))
            )
            if is_switch:
                expected = (
                    boot in nets
                    and bool(nets & view.ground_nets)
                    and len(nets) == 2
                )
            elif is_capacitor:
                expected = _two_terminal_grounded(view, part, boot or "")
            else:
                expected = (
                    boot in nets
                    and bool(nets & view.supply_nets)
                    and len(nets) == 2
                )
            if not expected:
                failures.append(
                    f"{part.ref} ({part.role}) must support MCU boot net "
                    f"{boot}, got {sorted(nets)}"
                )
        checks.append(CheckResult(
            name=f"mcu_reset_boot_support:{mcu.ref}",
            ok=not failures,
            message=(
                f"{mcu.ref} reset/enable and boot controls must terminate on "
                f"the actual MCU pins with valid pulls/buttons/capacitors: {failures}"
            ),
        ))
    return checks


def _analog_input_topology_checks(
    view: _ConnectivityView,
    requirement: str,
) -> list[CheckResult]:
    analog_requirement = _external_analog_input_requirement(requirement)
    if analog_requirement is None:
        return []

    required_names: list[str] = []
    if analog_requirement.requires_divider:
        required_names.extend(("divider_top", "divider_bottom"))
    if analog_requirement.requires_current_limit:
        required_names.append("current_limit")
    if analog_requirement.requires_filter_cap:
        required_names.append("filter_cap")
    if analog_requirement.requires_overvoltage_protection:
        required_names.append("overvoltage_protection")
    if not required_names:
        return []

    checks: list[CheckResult] = []
    for channel in range(1, analog_requirement.channel_count + 1):
        def belongs(
            part: SelectedPart,
            channel_number: int = channel,
        ) -> bool:
            return _analog_role_channel(part.role) == channel_number

        def one(*suffixes: str) -> SelectedPart | None:
            return next(
                (
                    part for part in view.parts.values()
                    if belongs(part)
                    and any(
                        suffix in part.role.lower()
                        for suffix in suffixes
                    )
                ),
                None,
            )

        present = {
            "divider_top": one("divider_top", "divider_upper"),
            "divider_bottom": one("divider_bottom", "divider_lower"),
            "current_limit": one("current_limit"),
            "filter_cap": one("filter_cap", "filtering_cap"),
            "overvoltage_protection": one(
                "tvs",
                "overvoltage",
                "clamp",
                "protection",
            ),
        }
        missing = [
            name
            for name in required_names
            if present[name] is None
        ]
        ok = not missing
        details: list[str] = []
        if ok:
            required_parts = [
                present[name]
                for name in required_names
                if present[name] is not None
            ]
            sense_candidates = {
                net
                for part in required_parts
                for net in view.part_nets(part)
                if net not in view.ground_nets
                and net not in view.supply_nets
                if view.net_has_any_mcu_pin(net)
            }
            sense = (
                next(iter(sense_candidates))
                if len(sense_candidates) == 1
                else None
            )
            ok = sense is not None
            details.append(f"sense={sense}")

            divider: str | None = None
            if analog_requirement.requires_divider:
                top = present["divider_top"]
                bottom = present["divider_bottom"]
                assert top is not None
                assert bottom is not None
                top_nets = view.part_nets(top)
                bottom_nets = view.part_nets(bottom)
                divider_candidates = bottom_nets - view.ground_nets
                divider = (
                    next(iter(divider_candidates))
                    if len(divider_candidates) == 1
                    else None
                )
                ok = (
                    ok
                    and len(top_nets) == 2
                    and len(bottom_nets) == 2
                    and bool(bottom_nets & view.ground_nets)
                    and divider is not None
                    and divider in top_nets
                    and not (top_nets & view.ground_nets)
                )
                details.extend((
                    f"{top.ref}={sorted(top_nets)}",
                    f"{bottom.ref}={sorted(bottom_nets)}",
                    f"scaled_node={divider}",
                ))

            if analog_requirement.requires_current_limit:
                current = present["current_limit"]
                assert current is not None
                current_nets = view.part_nets(current)
                expected_endpoints = {sense}
                if divider is not None:
                    expected_endpoints.add(divider)
                ok = (
                    ok
                    and len(current_nets) == 2
                    and expected_endpoints <= current_nets
                    and not (current_nets & view.ground_nets)
                )
                details.append(f"{current.ref}={sorted(current_nets)}")
            elif divider is not None:
                ok = ok and divider == sense

            if analog_requirement.requires_filter_cap:
                filter_cap = present["filter_cap"]
                assert filter_cap is not None
                ok = ok and _two_terminal_grounded(view, filter_cap, sense or "")
                details.append(
                    f"{filter_cap.ref}={sorted(view.part_nets(filter_cap))}"
                )

            if analog_requirement.requires_overvoltage_protection:
                protection = present["overvoltage_protection"]
                assert protection is not None
                protection_nets = view.part_nets(protection)
                protected_nodes = {sense}
                if divider is not None:
                    protected_nodes.add(divider)
                signal_nodes = protection_nets - view.ground_nets
                ok = (
                    ok
                    and len(protection_nets) == 2
                    and bool(protection_nets & view.ground_nets)
                    and len(signal_nodes) == 1
                    and signal_nodes <= protected_nodes
                )
                details.append(
                    f"{protection.ref}={sorted(protection_nets)}"
                )
        checks.append(CheckResult(
            name=f"analog_input_safe_chain:{channel}",
            ok=ok,
            message=(
                f"analog channel {channel} must implement only the explicitly "
                f"requested support roles {required_names} around one MCU ADC "
                "sense node; "
                f"missing={missing}, topology={details}"
            ),
        ))
    return checks


def _passive_edges(
    view: _ConnectivityView,
) -> list[tuple[SelectedPart, set[str]]]:
    return [
        (part, view.part_nets(part))
        for part in view.parts.values()
        if part.ref.upper().startswith(("R", "C"))
        and len(view.part_nets(part)) == 2
    ]


def _passive_path_to_ground(
    view: _ConnectivityView,
    start: str | None,
    max_edges: int,
) -> bool:
    if start is None:
        return False
    frontier = {start}
    visited = {start}
    for _ in range(max_edges):
        following: set[str] = set()
        for _part, nets in _passive_edges(view):
            if not (nets & frontier):
                continue
            following.update(nets - visited)
        if following & view.ground_nets:
            return True
        visited.update(following)
        frontier = following
    return False


def _buck_topology_checks(view: _ConnectivityView) -> list[CheckResult]:
    checks: list[CheckResult] = []
    converters = [
        part for part in view.parts.values()
        if "buck" in part.role.lower()
        and any(token in part.role.lower() for token in ("converter", "regulator"))
    ]
    inductors = _role_parts(view, "buck", "inductor")
    for converter in converters:
        vin = view.named_pin_net(converter, "VIN")
        switch = view.named_pin_net(converter, "SW")
        boot = view.named_pin_net(converter, "BOOT")
        feedback = view.named_pin_net(converter, "FB")
        timing = view.named_pin_net(converter, "RT/CLK", "RT")
        compensation = view.named_pin_net(converter, "COMP")
        output: str | None = None
        inductor_detail: list[str] = []
        for inductor in inductors:
            nets = view.part_nets(inductor)
            inductor_detail.append(f"{inductor.ref}={sorted(nets)}")
            if len(nets) == 2 and switch in nets:
                output = next(iter(nets - {switch}))
                break

        output_caps = _role_parts(view, "buck", "output", "capacitor")
        input_caps = [
            part for part in view.parts.values()
            if "buck" in part.role.lower()
            and "input" in part.role.lower()
            and "capacitor" in part.role.lower()
        ]
        boot_cap = any(
            nets == {boot, switch}
            for part, nets in _passive_edges(view)
            if part.ref.upper().startswith("C")
        )
        feedback_high = any(
            nets == {output, feedback}
            for part, nets in _passive_edges(view)
            if part.ref.upper().startswith("R")
        )
        feedback_low = any(
            feedback in nets and bool(nets & view.ground_nets)
            for part, nets in _passive_edges(view)
            if part.ref.upper().startswith("R")
        )
        timing_grounded = any(
            timing in nets and bool(nets & view.ground_nets)
            for part, nets in _passive_edges(view)
            if part.ref.upper().startswith("R")
        )
        failures: list[str] = []
        advisory: list[str] = []
        if (
            output is None
            or output in view.ground_nets
            or output == switch
        ):
            failures.append(
                f"SW must feed a two-terminal buck inductor and a distinct "
                f"non-ground output; SW={switch}, inductors={inductor_detail}"
            )
        if not any(
            _two_terminal_grounded(view, cap, output or "")
            for cap in output_caps
        ):
            failures.append(
                f"buck output capacitor must connect {output} to ground"
            )
        if not any(
            _two_terminal_grounded(view, cap, vin or "")
            for cap in input_caps
        ):
            failures.append(f"buck input capacitor must connect {vin} to ground")
        if boot is not None and not boot_cap:
            failures.append(
                f"bootstrap capacitor must connect BOOT={boot} to SW={switch}"
            )
        if feedback is not None and not (feedback_high and feedback_low):
            failures.append(
                f"feedback divider must connect output={output} -> FB={feedback} "
                "-> ground"
            )
        if timing is not None and not timing_grounded:
            failures.append(
                f"timing resistor must connect RT/CLK={timing} to ground"
            )
        enable = view.named_pin_net(converter, "EN", "ENABLE")
        enable_top = [
            part for part in view.parts.values()
            if "buck" in part.role.lower()
            and "en" in part.role.lower()
            and any(token in part.role.lower() for token in ("top", "upper"))
        ]
        enable_bottom = [
            part for part in view.parts.values()
            if "buck" in part.role.lower()
            and "en" in part.role.lower()
            and any(token in part.role.lower() for token in ("bottom", "lower"))
        ]
        if enable_top or enable_bottom:
            top_ok = any(
                view.part_nets(part) == {vin, enable}
                for part in enable_top
            )
            bottom_ok = any(
                enable in view.part_nets(part)
                and bool(view.part_nets(part) & view.ground_nets)
                and len(view.part_nets(part)) == 2
                for part in enable_bottom
            )
            if not (top_ok and bottom_ok):
                failures.append(
                    f"enable divider must connect VIN={vin} -> EN={enable} -> "
                    f"ground; top={[(p.ref, sorted(view.part_nets(p))) for p in enable_top]}, "
                    f"bottom={[(p.ref, sorted(view.part_nets(p))) for p in enable_bottom]}"
                )
        if (
            compensation is not None
            and not _passive_path_to_ground(view, compensation, max_edges=2)
        ):
            advisory.append(
                f"COMP={compensation} needs a grounded compensation network"
            )
        checks.append(CheckResult(
            name=f"buck_core_topology:{converter.ref}",
            ok=not failures,
            message=(
                f"{converter.ref} required input/output, inductor, bootstrap, "
                f"feedback, timing, and enable support topology errors: {failures}"
            ),
        ))
        checks.append(CheckResult(
            name=f"buck_compensation_topology:{converter.ref}",
            ok=not advisory,
            severity=Severity.WARNING,
            message=(
                f"{converter.ref} compensation topology is device-specific and "
                f"requires datasheet review: {advisory}"
            ),
        ))
    return checks


def _power_mux_topology_checks(view: _ConnectivityView) -> list[CheckResult]:
    checks: list[CheckResult] = []
    for mux in (
        part for part in _role_parts(view, "power_mux")
        if part.ref.upper().startswith("U")
        and not any(
            token in part.role.lower()
            for token in ("decoupling", "capacitor", "resistor")
        )
    ):
        vin1 = view.named_pin_net(mux, "VIN1")
        vin2 = view.named_pin_net(mux, "VIN2")
        ground = view.named_pin_net(mux, "GND")
        vout_numbers = [
            str(pin.get("number", ""))
            for pin in view.pins.get(mux.ref, [])
            if str(pin.get("name", "")).upper() == "VOUT"
        ]
        vout_nets = {
            view.pin_nets.get((mux.ref, number))
            for number in vout_numbers
        }
        failures: list[str] = []
        if (
            vin1 is None
            or vin2 is None
            or vin1 == vin2
            or vin1 in view.ground_nets
            or vin2 in view.ground_nets
        ):
            failures.append(f"VIN1/VIN2 must be distinct sources, got {vin1}/{vin2}")
        if ground not in view.ground_nets:
            failures.append(f"GND pin must be grounded, got {ground}")
        if None in vout_nets or len(vout_nets) != 1:
            failures.append(f"all VOUT pins must share one rail, got {vout_nets}")
        output = next(iter(vout_nets), None)
        if output in {vin1, vin2}:
            failures.append(
                f"VOUT rail must be distinct from both input rails, got {output}"
            )
        checks.append(CheckResult(
            name=f"power_mux_distinct_inputs_output:{mux.ref}",
            ok=not failures,
            message=f"{mux.ref} source-priority topology errors: {failures}",
        ))
    return checks


def _backfeed_isolation_topology_checks(
    view: _ConnectivityView,
    requirement: str,
) -> list[CheckResult]:
    """Keep raw USB and external sources distinct until an isolation element."""

    if not _requires_power_backfeed_protection(requirement):
        return []
    usb_connectors = [
        part for part in view.parts.values()
        if "usb" in part.role.lower() and _is_connector_part(part)
    ]
    external_connectors = [
        part for part in view.parts.values()
        if (
            "power" in part.role.lower()
            and "input" in part.role.lower()
            and "usb" not in part.role.lower()
            and _is_connector_part(part)
        )
    ]
    usb_raw = {
        net
        for connector in usb_connectors
        for net in [view.named_pin_net(connector, "VBUS")]
        if net is not None
    }
    external_raw = {
        net
        for connector in external_connectors
        for net in view.part_nets(connector) - view.ground_nets
    }
    path_parts = [
        part for part in view.parts.values()
        if any(
            token in part.role.lower()
            for token in (
                "power_path",
                "power_mux",
                "source_priority",
                "ideal_diode",
                "reverse_blocking",
                "oring",
            )
        )
    ]
    isolated_usb_paths = [
        f"{part.ref}{sorted(view.part_nets(part))}"
        for part in path_parts
        if (
            view.part_nets(part) & usb_raw
            and len(
                view.part_nets(part)
                - usb_raw
                - view.ground_nets
            ) >= 1
        )
    ]
    failures: list[str] = []
    if not usb_raw:
        failures.append("USB connector VBUS source is not identifiable")
    if not external_raw:
        failures.append("external power-input source is not identifiable")
    merged = sorted(usb_raw & external_raw)
    if merged:
        failures.append(
            f"raw USB and external inputs are directly merged on {merged}"
        )
    if not isolated_usb_paths:
        failures.append(
            "no selected power-path/ideal-diode/reverse-blocking element "
            "bridges raw USB VBUS to a distinct downstream rail"
        )
    return [
        CheckResult(
            name="source_backfeed_isolation",
            ok=not failures,
            message=(
                "raw input sources must remain separate until a real isolation "
                f"element; USB={sorted(usb_raw)}, external={sorted(external_raw)}, "
                f"paths={isolated_usb_paths}, errors={failures}"
            ),
        )
    ]


def _external_input_protection_topology_checks(
    view: _ConnectivityView,
) -> list[CheckResult]:
    """Verify a role-described external-input protection chain end to end."""

    connectors = [
        part for part in view.parts.values()
        if (
            "power" in part.role.lower()
            and "input" in part.role.lower()
            and "usb" not in part.role.lower()
            and _is_connector_part(part)
        )
    ]
    series_parts = [
        part for part in view.parts.values()
        if (
            ("fuse" in part.role.lower() and "input" in part.role.lower())
            or (
                "reverse" in part.role.lower()
                and "polarity" in part.role.lower()
            )
            or (
                "input" in part.role.lower()
                and "filter" in part.role.lower()
                and any(
                    token in part.role.lower()
                    for token in ("inductor", "ferrite", "bead")
                )
            )
        )
    ]
    converters = [
        part for part in view.parts.values()
        if (
            any(token in part.role.lower() for token in ("buck", "regulator"))
            and any(
                token in part.role.lower()
                for token in ("converter", "regulator")
            )
        )
    ]
    if not connectors or not series_parts or not converters:
        return []

    raw_nets = {
        net
        for connector in connectors
        for net in view.part_nets(connector) - view.ground_nets
    }
    target_nets = {
        net
        for converter in converters
        for net in [view.named_pin_net(converter, "VIN", "VI", "IN")]
        if net is not None
    }
    failures: list[str] = []
    edges: list[tuple[str, str, str]] = []
    for part in series_parts:
        nets = view.part_nets(part)
        signal_nets = nets - view.ground_nets
        if len(signal_nets) == 2 and len(nets) == 2:
            first, second = sorted(signal_nets)
            edges.append((first, second, part.ref))
            continue
        drain = view.named_pin_net(part, "D", "DRAIN")
        source = view.named_pin_net(part, "S", "SOURCE")
        if (
            drain is not None
            and source is not None
            and drain != source
            and drain not in view.ground_nets
            and source not in view.ground_nets
        ):
            edges.append((drain, source, part.ref))
            continue
        failures.append(
            f"{part.ref} ({part.role}) must be a series element between two "
            f"distinct non-ground rails, got {sorted(nets)}"
        )

    required_refs = {part.ref for part in series_parts}
    graph: dict[str, list[tuple[str, str]]] = {}
    for first, second, ref in edges:
        graph.setdefault(first, []).append((second, ref))
        graph.setdefault(second, []).append((first, ref))

    valid_path_nets: set[str] = set()
    valid_path_refs: set[str] = set()

    def walk(
        net: str,
        visited_nets: set[str],
        used_refs: set[str],
    ) -> bool:
        nonlocal valid_path_nets, valid_path_refs
        if net in target_nets and required_refs <= used_refs:
            valid_path_nets = set(visited_nets)
            valid_path_refs = set(used_refs)
            return True
        for following, ref in graph.get(net, []):
            if following in visited_nets:
                continue
            if walk(
                following,
                visited_nets | {following},
                used_refs | {ref},
            ):
                return True
        return False

    chain_ok = any(
        walk(raw, {raw}, set())
        for raw in raw_nets
    )
    if not chain_ok:
        failures.append(
            "no continuous connector-to-regulator input path traverses every "
            f"selected series protection element; raw={sorted(raw_nets)}, "
            f"VIN={sorted(target_nets)}, edges={edges}"
        )

    tvs_parts = [
        part for part in view.parts.values()
        if (
            "input" in part.role.lower()
            and any(token in part.role.lower() for token in ("tvs", "surge"))
        )
    ]
    for part in tvs_parts:
        nets = view.part_nets(part)
        signal = nets - view.ground_nets
        if (
            len(nets) != 2
            or not (nets & view.ground_nets)
            or len(signal) != 1
            or (chain_ok and not (signal & valid_path_nets))
        ):
            failures.append(
                f"{part.ref} input TVS must shunt one protected chain node to "
                f"ground, got {sorted(nets)}"
            )

    filter_caps = [
        part for part in view.parts.values()
        if (
            "input" in part.role.lower()
            and "filter" in part.role.lower()
            and "capacitor" in part.role.lower()
        )
    ]
    for part in filter_caps:
        nets = view.part_nets(part)
        signal = nets - view.ground_nets
        if (
            len(nets) != 2
            or not (nets & view.ground_nets)
            or len(signal) != 1
            or (chain_ok and not (signal & valid_path_nets))
        ):
            failures.append(
                f"{part.ref} input filter capacitor must shunt one protected "
                f"chain node to ground, got {sorted(nets)}"
            )

    return [
        CheckResult(
            name="external_input_protection_chain",
            ok=not failures,
            message=(
                "external power must traverse the selected fuse, reverse-polarity "
                "element, and input filter before regulator VIN, with TVS/filter "
                f"capacitors as shunts: errors={failures}, path_refs="
                f"{sorted(valid_path_refs)}"
            ),
        )
    ]


def _usb_c_sink_cc_topology_checks(
    view: _ConnectivityView,
) -> list[CheckResult]:
    """Require each recognized USB-C sink Rd to terminate one CC pin to GND."""

    checks: list[CheckResult] = []
    connectors = [
        part
        for part in view.parts.values()
        if "usb" in part.role.lower()
        and "connector" in part.role.lower()
    ]
    for connector in connectors:
        failures: list[str] = []
        checked = False
        for channel in ("cc1", "cc2"):
            resistors = [
                part
                for part in view.parts.values()
                if _is_usb_sink_cc_resistor(part, channel)
            ]
            if not resistors:
                continue
            checked = True
            cc_net = view.named_pin_net(connector, channel.upper())
            grounded = [
                resistor.ref
                for resistor in resistors
                if cc_net is not None
                and _two_terminal_grounded(view, resistor, cc_net)
            ]
            if (
                cc_net is None
                or cc_net in view.supply_nets
                or cc_net in view.ground_nets
                or len(grounded) != 1
            ):
                candidates = [
                    (part.ref, sorted(view.part_nets(part)))
                    for part in resistors
                ]
                failures.append(
                    f"{connector.ref} {channel.upper()} net={cc_net}; "
                    f"Rd candidates={candidates}"
                )
        if checked:
            checks.append(CheckResult(
                name=f"usb_c_sink_cc_rd:{connector.ref}",
                ok=not failures,
                message=(
                    "USB-C sink CC1 and CC2 must each use an independent "
                    f"5.1k Rd to ground, never a positive rail: {failures}"
                ),
            ))
    return checks


def _functional_connection_checks(
    selection: SelectionPlan,
    intent: NetlistIntent,
    requirement: str = "",
) -> list[CheckResult]:
    """Validate safety-critical topology after all logical pins resolve."""
    view = _ConnectivityView.build(selection, intent)
    checks: list[CheckResult] = []
    checks.extend(_two_terminal_self_short_checks(view))
    checks.extend(_power_pin_rail_checks(view))
    checks.extend(_critical_function_pin_checks(view))
    checks.extend(_crystal_topology_checks(view))
    checks.extend(_led_series_checks(view))
    checks.extend(_swd_topology_checks(view))
    checks.extend(_differential_termination_topology_checks(view, requirement))
    checks.extend(_can_topology_checks(view))
    checks.extend(_bounded_interface_connector_checks(view))
    checks.extend(_debug_connector_topology_checks(view))
    checks.extend(_microsd_spi_topology_checks(view, requirement))
    checks.extend(_rs485_topology_checks(view))
    checks.extend(_mcu_control_topology_checks(view))
    checks.extend(_usb_c_sink_cc_topology_checks(view))
    checks.extend(_analog_input_topology_checks(view, requirement))
    checks.extend(_buck_topology_checks(view))
    checks.extend(_power_mux_topology_checks(view))
    checks.extend(_backfeed_isolation_topology_checks(view, requirement))
    checks.extend(_external_input_protection_topology_checks(view))
    return checks


_POWER_PIN_NAMES = ("VCC", "VDD", "AVCC", "VBAT", "VIN", "GND", "VSS", "AGND")


class SchPinMapStep(PipelineStepBase):
    """Map each net's logical pins to real device pin numbers (grounded).

    The mapping is always verified against the real symbol library: a mapped
    number must be a genuine pin of that component's symbol. Bottom-line: no
    unresolved pins, no pin assigned to two nets; floating power/ground pins
    are surfaced as warnings.
    """

    step = PipelineStep.SCH_PINMAP
    knowledge_role = "schematic"

    def _ref_symbols(self, state: PipelineState) -> dict[str, str]:
        sel = state.artifact(PipelineStep.SELECTION)
        if isinstance(sel, SelectionPlan):
            return {p.ref: p.symbol for p in sel.parts}
        return {}

    def _deterministic_map(self, state: PipelineState) -> PinMapPlan:
        intent = state.artifact(PipelineStep.SCH_CONNECTIONS)
        ref_syms = self._ref_symbols(state)
        nets: list[MappedNet] = []
        unresolved: list[str] = []
        if not isinstance(intent, NetlistIntent):
            return PinMapPlan(nets=nets, unresolved=unresolved)
        for net in intent.nets:
            mapped: list[MappedPin] = []
            used_in_net: set[str] = set()
            for lp in net.pins:
                sym = ref_syms.get(lp.ref)
                pins = symbols.symbol_pins(sym) if sym else None
                number = _resolve_logical_pin(pins, lp.pin) if pins else None
                if number is None:
                    # Without a symbol library there is no authoritative pin
                    # number to resolve. Preserve the logical pin as a virtual
                    # number so the offline netlist keeps its full cardinality;
                    # the unavailable-library warning remains explicit.
                    if config.symbol_dir() is None:
                        number = lp.pin
                    elif lp.pin.isdigit():
                        number = lp.pin
                    else:
                        unresolved.append(f"{lp.ref}:{lp.pin}")
                        continue
                key = f"{lp.ref}:{number}"
                if key in used_in_net:
                    # Redundant WITHIN this net (e.g. an analog-ground alias
                    # collapsing onto a GND pin already on this net). Skip.
                    # Cross-net duplicates are deliberately kept so the
                    # no_double_assigned_pins check can flag a real short.
                    continue
                used_in_net.add(key)
                mapped.append(MappedPin(ref=lp.ref, logical=lp.pin, number=number))
            nets.append(MappedNet(name=net.name, kind=net.kind, pins=mapped))
        return PinMapPlan(
            nets=nets, unresolved=unresolved, rationale="deterministic pin mapping",
        )

    def propose(
        self, state: PipelineState, ctx: PipelineContext, knowledge: str
    ) -> tuple[BaseModel, bool]:
        # Deterministic, library-grounded mapping is authoritative even in LLM
        # modes: the LLM cannot invent pin numbers. (A future refinement may let
        # the LLM disambiguate multi-match names, still verified below.)
        return self._deterministic_map(state), False

    def check(self, state: PipelineState, artifact: BaseModel) -> list[CheckResult]:
        assert isinstance(artifact, PinMapPlan)
        checks: list[CheckResult] = []
        if config.symbol_dir() is None:
            checks.append(CheckResult(
                name="symbol_library_available", ok=False, severity=Severity.WARNING,
                message="KICAD_SYMBOL_DIR not configured; cannot verify pin numbers",
            ))
            return checks
        ref_syms = self._ref_symbols(state)
        # Every mapped number must be a real pin of that symbol.
        bad: list[str] = []
        for net in artifact.nets:
            for mp in net.pins:
                pins = symbols.symbol_pins(ref_syms.get(mp.ref, ""))
                numbers = {str(p["number"]) for p in pins} if pins else set()
                if mp.number not in numbers:
                    bad.append(f"{mp.ref}:{mp.number}({net.name})")
        checks.append(CheckResult(
            name="mapped_pins_exist", ok=not bad,
            message=f"mapped pins not found in symbol: {bad}",
        ))
        checks.append(CheckResult(
            name="all_pins_resolved", ok=not artifact.unresolved,
            message=f"unresolved logical pins: {artifact.unresolved}",
        ))
        # No real pin assigned to two different nets.
        seen: dict[str, str] = {}
        dup: list[str] = []
        for net in artifact.nets:
            for mp in net.pins:
                key = f"{mp.ref}:{mp.number}"
                if key in seen and seen[key] != net.name:
                    dup.append(f"{key} in {seen[key]} & {net.name}")
                seen[key] = net.name
        checks.append(CheckResult(
            name="no_double_assigned_pins", ok=not dup,
            message=f"pins on multiple nets: {dup}",
        ))
        # Floating power/ground pins (warning — surfaced, not blocking).
        floating = self._floating_power_pins(ref_syms, artifact)
        checks.append(CheckResult(
            name="power_pins_connected", ok=not floating, severity=Severity.WARNING,
            message=f"unconnected power/ground pins: {floating}",
        ))
        return checks

    def _floating_power_pins(
        self, ref_syms: dict[str, str], artifact: PinMapPlan
    ) -> list[str]:
        connected: set[str] = {
            f"{mp.ref}:{mp.number}" for net in artifact.nets for mp in net.pins
        }
        floating: list[str] = []
        for ref, sym in ref_syms.items():
            pins = symbols.symbol_pins(sym)
            if not pins:
                continue
            for p in pins:
                name = str(p["name"]).upper()
                ptype = str(p["type"])
                is_power = ptype in ("power_in", "power_out") or any(
                    name == pn or name.startswith(pn) for pn in _POWER_PIN_NAMES
                )
                if is_power and f"{ref}:{p['number']}" not in connected:
                    floating.append(f"{ref}:{p['number']}({name})")
        return floating

    def summarize(self, artifact: BaseModel) -> str:
        assert isinstance(artifact, PinMapPlan)
        total = sum(len(n.pins) for n in artifact.nets)
        return f"{total} pins mapped across {len(artifact.nets)} nets, " \
               f"{len(artifact.unresolved)} unresolved"


_SHEET_COLS = 6
_SYMBOL_HALF_MM = 6.0
_SHEET_CLEARANCE_MM = 5.08
_SHEET_PACK_GAP_MM = 2 * _SHEET_CLEARANCE_MM


def _symbol_half_extents(symbol: str | None, rotation: float = 0.0) -> tuple[float, float]:
    """Return a conservative symbol half-width/height from real pin geometry."""
    pins = symbols.symbol_pins(symbol or "") or []
    half_width = max(
        [_SYMBOL_HALF_MM, *(abs(float(pin["x"])) for pin in pins)]
    )
    half_height = max(
        [_SYMBOL_HALF_MM, *(abs(float(pin["y"])) for pin in pins)]
    )
    radians = math.radians(rotation % 360.0)
    cos_value = abs(math.cos(radians))
    sin_value = abs(math.sin(radians))
    return (
        cos_value * half_width + sin_value * half_height,
        sin_value * half_width + cos_value * half_height,
    )


def _sheet_overlaps(
    placements: list[SheetPlacement],
    symbol_by_ref: dict[str, str] | None = None,
) -> list[str]:
    """Return pairs whose real symbol envelopes overlap on the sheet."""
    out: list[str] = []
    items = list(placements)
    symbol_by_ref = symbol_by_ref or {}
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            a, b = items[i], items[j]
            aw, ah = _symbol_half_extents(symbol_by_ref.get(a.ref), a.rotation)
            bw, bh = _symbol_half_extents(symbol_by_ref.get(b.ref), b.rotation)
            if (
                abs(a.x - b.x) < aw + bw + _SHEET_CLEARANCE_MM
                and abs(a.y - b.y) < ah + bh + _SHEET_CLEARANCE_MM
            ):
                out.append(f"{a.ref}&{b.ref}")
    return out


def _reflow_sheet_placements(
    placements: list[SheetPlacement],
    symbol_by_ref: dict[str, str],
) -> list[SheetPlacement]:
    """Pack symbols in rows using their real pin extents and a safe clearance."""
    rows = [
        placements[index:index + _SHEET_COLS]
        for index in range(0, len(placements), _SHEET_COLS)
    ]
    result: list[SheetPlacement] = []
    y_cursor = _SHEET_PACK_GAP_MM
    for row in rows:
        extents = [
            _symbol_half_extents(symbol_by_ref.get(item.ref), item.rotation)
            for item in row
        ]
        row_half_height = max((height for _, height in extents), default=_SYMBOL_HALF_MM)
        center_y = y_cursor + row_half_height
        x_cursor = _SHEET_PACK_GAP_MM
        for item, (half_width, _) in zip(row, extents, strict=True):
            center_x = x_cursor + half_width
            result.append(SheetPlacement(
                ref=item.ref,
                x=center_x,
                y=center_y,
                rotation=item.rotation,
            ))
            x_cursor = center_x + half_width + _SHEET_PACK_GAP_MM
        y_cursor = center_y + row_half_height + _SHEET_PACK_GAP_MM
    return result


class SchLayoutStep(PipelineStepBase):
    """Schematic sheet layout: place symbols and choose wire vs net-label.

    Bottom-line: every part is placed, symbols do not overlap on the sheet, and
    every net drawn as a label names a real net (so the label netlist matches).
    """

    step = PipelineStep.SCH_LAYOUT
    knowledge_role = "schematic"

    def knowledge_query(self, state: PipelineState) -> str | None:
        return "schematic readability, sheet layout, wires vs net labels"

    def _refs(self, state: PipelineState) -> list[str]:
        sel = state.artifact(PipelineStep.SELECTION)
        return [p.ref for p in sel.parts] if isinstance(sel, SelectionPlan) else []

    def _symbol_by_ref(self, state: PipelineState) -> dict[str, str]:
        sel = state.artifact(PipelineStep.SELECTION)
        if not isinstance(sel, SelectionPlan):
            return {}
        return {part.ref: part.symbol for part in sel.parts}

    def _net_names(self, state: PipelineState) -> list[str]:
        intent = state.artifact(PipelineStep.SCH_CONNECTIONS)
        return [n.name for n in intent.nets] if isinstance(intent, NetlistIntent) else []

    def propose(
        self, state: PipelineState, ctx: PipelineContext, knowledge: str
    ) -> tuple[BaseModel, bool]:
        def fallback() -> SchLayoutPlan:
            placements = _reflow_sheet_placements(
                [SheetPlacement(ref=ref, x=0.0, y=0.0) for ref in self._refs(state)],
                self._symbol_by_ref(state),
            )
            intent = state.artifact(PipelineStep.SCH_CONNECTIONS)
            labels: list[str] = []
            if isinstance(intent, NetlistIntent):
                labels = [intent.ground_net, *intent.supply_nets]
                labels = [n for n in labels if any(x.name == n for x in intent.nets)]
            return SchLayoutPlan(
                placements=placements, label_nets=labels,
                rationale="deterministic grid sheet layout; power/ground as labels",
            )

        system = (
            "You lay out a schematic sheet as JSON: placements[] ({ref, x, y, "
            "rotation}) in mm, label_nets[] (nets drawn as labels vs local wires), "
            "rationale. Keep symbols from overlapping; power/ground as labels."
        )
        user = f"Components: {self._refs(state)}\nNets: {self._net_names(state)}\n\n{knowledge}"
        plan, used = propose_structured(
            ctx, model=SchLayoutPlan, system=system, user=user, fallback=fallback
        )
        # Keep the LLM's grouping/rotation choices, but reflow unsafe geometry.
        # Real KiCad symbols can extend tens of millimetres beyond their origin;
        # checking only origin distance can put pins from different nets at the
        # same coordinate and create a real electrical short.
        symbol_by_ref = self._symbol_by_ref(state)
        if isinstance(plan, SchLayoutPlan) and _sheet_overlaps(
            plan.placements, symbol_by_ref
        ):
            plan.placements = _reflow_sheet_placements(
                plan.placements, symbol_by_ref
            )
        return plan, used

    def check(self, state: PipelineState, artifact: BaseModel) -> list[CheckResult]:
        assert isinstance(artifact, SchLayoutPlan)
        refs = set(self._refs(state))
        placed = {p.ref for p in artifact.placements}
        checks: list[CheckResult] = []
        if refs:
            missing = sorted(refs - placed)
            checks.append(CheckResult(
                name="all_parts_placed", ok=not missing,
                message=f"unplaced components: {missing}",
            ))
        # No symbol overlaps on the sheet.
        overlaps = _sheet_overlaps(artifact.placements, self._symbol_by_ref(state))
        checks.append(CheckResult(
            name="no_symbol_overlap", ok=not overlaps,
            message=f"overlapping symbols: {overlaps}",
        ))
        # Every label net must be a real net (label netlist round-trips).
        net_names = set(self._net_names(state))
        if net_names:
            bad = sorted(set(artifact.label_nets) - net_names)
            checks.append(CheckResult(
                name="labels_match_netlist", ok=not bad,
                message=f"label nets not in the netlist: {bad}",
            ))
        return checks

    def summarize(self, artifact: BaseModel) -> str:
        assert isinstance(artifact, SchLayoutPlan)
        return f"{len(artifact.placements)} symbols placed, {len(artifact.label_nets)} label nets"


class SchMaterializeStep(PipelineStepBase):
    """Write the real ``.kicad_sch``, embedding real symbol pin geometry.

    Deterministic (no LLM): assembles the selected parts at their sheet
    placements and labels every mapped pin at its true coordinate. Bottom-line:
    the reloaded label netlist round-trips to the pin map (same net names and
    per-net pin counts) and all components are present.
    """

    step = PipelineStep.SCH_MATERIALIZE

    def _components(self, state: PipelineState) -> list[dict[str, Any]]:
        sel = state.artifact(PipelineStep.SELECTION)
        layout = state.artifact(PipelineStep.SCH_LAYOUT)
        places: dict[str, SheetPlacement] = {}
        if isinstance(layout, SchLayoutPlan):
            places = {p.ref: p for p in layout.placements}
        out: list[dict[str, Any]] = []
        if isinstance(sel, SelectionPlan):
            for i, p in enumerate(sel.parts):
                pl = places.get(p.ref)
                out.append({
                    "ref": p.ref, "symbol": p.symbol, "value": p.value,
                    "footprint": p.footprint,
                    "dnp": p.dnp,
                    "unresolved": p.unresolved,
                    "release_ready": getattr(p, "release_ready", None),
                    "resolution_status": p.resolution_status,
                    "resolution_detail": p.resolution_detail,
                    "requested_identity": p.requested_identity,
                    "identity_mode": p.identity_mode,
                    "identity_provenance": p.identity_provenance,
                    "x": pl.x if pl else 25.4 * (i % 6),
                    "y": pl.y if pl else 25.4 * (i // 6),
                    "rotation": pl.rotation if pl else 0.0,
                })
        return out

    def _nets(self, state: PipelineState) -> list[dict[str, Any]]:
        pm = state.artifact(PipelineStep.SCH_PINMAP)
        if not isinstance(pm, PinMapPlan):
            return []
        return [
            {"name": n.name, "pins": [{"ref": mp.ref, "number": mp.number} for mp in n.pins]}
            for n in pm.nets
        ]

    def _no_connect_pins(self, state: PipelineState) -> list[dict[str, str]]:
        intent = state.artifact(PipelineStep.SCH_CONNECTIONS)
        sel = state.artifact(PipelineStep.SELECTION)
        if not isinstance(intent, NetlistIntent) or not isinstance(sel, SelectionPlan):
            return []
        ref_symbols = {part.ref: part.symbol for part in sel.parts}
        out: list[dict[str, str]] = []
        for logical in intent.no_connect_pins:
            pins = symbols.symbol_pins(ref_symbols.get(logical.ref, "")) or []
            number = _resolve_logical_pin(pins, logical.pin)
            if number is not None:
                out.append({"ref": logical.ref, "number": number})
        return out

    def propose(
        self, state: PipelineState, ctx: PipelineContext, knowledge: str
    ) -> tuple[BaseModel, bool]:
        components = self._components(state)
        nets = self._nets(state)
        no_connect_pins = self._no_connect_pins(state)
        intent = state.artifact(PipelineStep.SCH_CONNECTIONS)
        supply = intent.supply_nets if isinstance(intent, NetlistIntent) else []
        ground = intent.ground_net if isinstance(intent, NetlistIntent) else "GND"
        doc = materialize_pinmapped(
            components,
            nets,
            no_connect_pins=no_connect_pins,
            supply_nets=supply,
            ground_net=ground,
        )

        out_dir = Path(ctx.out_dir) if ctx.out_dir else Path(tempfile.mkdtemp(prefix="rnp_sch_"))
        out_dir.mkdir(parents=True, exist_ok=True)
        sch_path = out_dir / f"{state.project_name}.kicad_sch"
        doc.save(sch_path)
        pm = state.artifact(PipelineStep.SCH_PINMAP)
        label_count = sum(len(n.pins) for n in pm.nets) if isinstance(pm, PinMapPlan) else 0
        result = MaterializeResult(
            sch_path=str(sch_path), component_count=len(components),
            net_count=len(nets), label_count=label_count,
        )
        return result, False

    def check(self, state: PipelineState, artifact: BaseModel) -> list[CheckResult]:
        assert isinstance(artifact, MaterializeResult)
        from ratsnestpro.eda import SchematicDoc

        checks: list[CheckResult] = []
        doc = SchematicDoc.load(artifact.sch_path)
        # All components present.
        sel = state.artifact(PipelineStep.SELECTION)
        expected_refs = {p.ref for p in sel.parts} if isinstance(sel, SelectionPlan) else set()
        actual_refs = set(doc.references())
        checks.append(CheckResult(
            name="all_components_written", ok=expected_refs <= actual_refs,
            message=f"missing components in sch: {sorted(expected_refs - actual_refs)}",
        ))
        # Label netlist round-trips to the pin map (names + per-net counts).
        pm = state.artifact(PipelineStep.SCH_PINMAP)
        netlist = doc.label_netlist()
        if isinstance(pm, PinMapPlan):
            expected = {n.name: len(n.pins) for n in pm.nets if n.pins}
            got = {name: len(coords) for name, coords in netlist.items()}
            missing_nets = sorted(set(expected) - set(got))
            checks.append(CheckResult(
                name="netlist_names_round_trip", ok=not missing_nets,
                message=f"nets missing after materialize: {missing_nets}",
            ))
            count_mismatch = sorted(
                f"{k}({got.get(k, 0)}!={v})" for k, v in expected.items() if got.get(k, 0) != v
            )
            checks.append(CheckResult(
                name="netlist_pin_counts_round_trip", ok=not count_mismatch,
                message=f"label count != pin count: {count_mismatch}",
            ))
        # Symbol graphics embedded in lib_symbols → the sheet renders and is
        # self-contained. Without the symbol library configured we can't embed,
        # so this surfaces as a non-blocking WARNING rather than a hard failure.
        embedded = set(doc.lib_symbol_ids())
        want_symbols = (
            {p.symbol for p in sel.parts} if isinstance(sel, SelectionPlan) else set()
        )
        missing_syms = sorted(want_symbols - embedded)
        if config.symbol_dir() is None:
            checks.append(CheckResult(
                name="lib_symbols_embedded", ok=False, severity=Severity.WARNING,
                message="KICAD_SYMBOL_DIR not configured; symbol graphics not embedded",
            ))
        else:
            checks.append(CheckResult(
                name="lib_symbols_embedded", ok=not missing_syms,
                message=f"symbol graphics missing from lib_symbols: {missing_syms}",
            ))
        return checks

    def summarize(self, artifact: BaseModel) -> str:
        assert isinstance(artifact, MaterializeResult)
        return (
            f"wrote {artifact.sch_path} "
            f"({artifact.component_count} parts, {artifact.net_count} nets, "
            f"{artifact.label_count} pin labels)"
        )


class ErcStep(PipelineStepBase):
    """Schematic ERC bottom-line.

    Deterministic, authoritative checks (block on failure): no shorted nets,
    no single-pin nets, and zero real kicad-cli ERC errors when the CLI is
    available. kicad-cli being unavailable is reported as a warning, never as
    a pass.
    """

    step = PipelineStep.ERC

    def propose(
        self, state: PipelineState, ctx: PipelineContext, knowledge: str
    ) -> tuple[BaseModel, bool]:
        from ratsnestpro.eda import SchematicDoc

        mat = state.artifact(PipelineStep.SCH_MATERIALIZE)
        if not isinstance(mat, MaterializeResult):
            return ErcSummary(sch_path=""), False
        doc = SchematicDoc.load(mat.sch_path)
        shorted = doc.shorted_nets()
        single = [name for name, coords in doc.label_netlist().items() if len(coords) < 2]
        erc = run_erc(mat.sch_path)
        return (
            ErcSummary(
                sch_path=mat.sch_path,
                shorted_nets=shorted,
                single_pin_nets=single,
                cli_available=erc.available,
                cli_ran=erc.ran,
                cli_error_count=erc.error_count,
                cli_warning_count=erc.warning_count,
                cli_error_details=[
                    f"{violation.rule_id}: {violation.message}"
                    for violation in erc.violations
                    if violation.severity == "error"
                ],
                cli_report_path=erc.report_path or "",
            ),
            False,
        )

    def check(self, state: PipelineState, artifact: BaseModel) -> list[CheckResult]:
        assert isinstance(artifact, ErcSummary)
        if not artifact.sch_path:
            return [CheckResult(
                name="schematic_available", ok=False,
                message="no materialized schematic to check",
                blocks_execution=True,
            )]
        checks = [
            CheckResult(
                name="no_shorted_nets", ok=not artifact.shorted_nets,
                message=f"shorted nets: {artifact.shorted_nets}",
            ),
            CheckResult(
                name="no_single_pin_nets", ok=not artifact.single_pin_nets,
                message=f"single-pin nets: {artifact.single_pin_nets}",
            ),
        ]
        # kicad-cli ERC: unavailable is a warning (never a pass); real ERC
        # errors are authoritative and must stop the production pipeline.
        if not artifact.cli_available:
            checks.append(CheckResult(
                name="kicad_cli_erc", ok=False, severity=Severity.WARNING,
                message="kicad-cli unavailable; real ERC skipped (not a pass)",
            ))
        else:
            checks.append(CheckResult(
                name="kicad_cli_erc", ok=artifact.cli_error_count == 0,
                message=(
                    f"kicad-cli ERC reported {artifact.cli_error_count} error(s)"
                    + (
                        f": {artifact.cli_error_details}"
                        if artifact.cli_error_details
                        else ""
                    )
                ),
            ))
        return checks

    def rollback_target(
        self,
        state: PipelineState,
        artifact: BaseModel,
        checks: list[CheckResult],
    ) -> PipelineStep | None:
        if any(
            not check.ok and check.severity == Severity.ERROR
            for check in checks
        ):
            return PipelineStep.SCH_CONNECTIONS
        return None

    def summarize(self, artifact: BaseModel) -> str:
        assert isinstance(artifact, ErcSummary)
        cli = "unavailable" if not artifact.cli_available else f"{artifact.cli_error_count} err"
        return (
            f"shorts={len(artifact.shorted_nets)}, single-pin={len(artifact.single_pin_nets)}, "
            f"cli ERC={cli}"
        )


class LayoutPartitionStep(PipelineStepBase):
    """Board outline + functional zones. Bottom-line: zones lie within the board."""

    step = PipelineStep.LAYOUT_PARTITION
    knowledge_role = "layout"

    def knowledge_query(self, state: PipelineState) -> str | None:
        return f"board partitioning and functional zones for: {state.requirement_text}"

    def propose(
        self, state: PipelineState, ctx: PipelineContext, knowledge: str
    ) -> tuple[BaseModel, bool]:
        def fallback() -> BoardPartition:
            extracted = params_from_requirement(state.requirement_text)
            try:
                params = Atmega328Params.model_validate(extracted)
            except Exception:
                params = Atmega328Params()
            outline = build_plan(params).outline
            w, h = outline.width_mm, outline.height_mm
            zones = [
                BoardZone(name="power", kind="power", x1=0.5, y1=0.5, x2=w * 0.33, y2=h - 0.5),
                BoardZone(name="mcu_digital", kind="digital",
                          x1=w * 0.33, y1=0.5, x2=w * 0.75, y2=h - 0.5),
                BoardZone(name="connectors", kind="connector",
                          x1=w * 0.75, y1=0.5, x2=w - 0.5, y2=h - 0.5),
            ]
            return BoardPartition(
                board_width=w, board_height=h, zones=zones,
                rationale="deterministic power|digital|connector partition",
            )

        system = (
            "You partition a PCB into functional zones. Return JSON: board_width, "
            "board_height (mm), zones[] ({name, kind, x1, y1, x2, y2}), rationale. "
            "Zones must fit inside the board."
        )
        user = f"Requirement:\n{state.requirement_text}\n\nKnowledge:\n{knowledge}"
        artifact, used_llm = propose_structured(
            ctx, model=BoardPartition, system=system, user=user, fallback=fallback
        )
        assert isinstance(artifact, BoardPartition)
        compiled = compile_placement_constraints(
            artifact,
            _roles(state),
            state.requirement_text,
        )
        return artifact.model_copy(update={"placement_constraints": compiled}), used_llm

    def check(self, state: PipelineState, artifact: BaseModel) -> list[CheckResult]:
        assert isinstance(artifact, BoardPartition)
        w, h = artifact.board_width, artifact.board_height
        out_of_bounds = [
            z.name for z in artifact.zones
            if z.x1 < 0 or z.y1 < 0 or z.x2 > w or z.y2 > h
        ]
        return [
            CheckResult(
                name="has_board_outline", ok=w > 0 and h > 0,
                message="board must have positive dimensions",
            ),
            CheckResult(
                name="zones_within_board", ok=not out_of_bounds,
                message=f"zones outside the board outline: {out_of_bounds}",
            ),
        ]

    def summarize(self, artifact: BaseModel) -> str:
        assert isinstance(artifact, BoardPartition)
        return (
            f"board {artifact.board_width}x{artifact.board_height} mm, "
            f"{len(artifact.zones)} zones"
        )


_LAYOUT_GRID_MM = 0.5
_LEGAL_ROTATIONS = (0.0, 90.0, 180.0, 270.0)
_EDGE_MARGIN_MM = 12.0
_CRYSTAL_NEAR_MM = 15.0
_DECOUPLE_NEAR_MM = 15.0
_LOCAL_SUPPORT_NEAR_MM = 35.0
_PLACE_SPACING_MM = 10.0
_PLACE_MARGIN_MM = 5.0
_PLACEMENT_TARGET_WEIGHT = 2.0


def _roles(state: PipelineState) -> dict[str, str]:
    sel = state.artifact(PipelineStep.SELECTION)
    return {p.ref: p.role for p in sel.parts} if isinstance(sel, SelectionPlan) else {}


def _footprints_of(state: PipelineState) -> dict[str, str]:
    sel = state.artifact(PipelineStep.SELECTION)
    return {p.ref: p.footprint for p in sel.parts} if isinstance(sel, SelectionPlan) else {}


def _functional_anchor_refs(state: PipelineState) -> set[str]:
    """Return non-passive parts that may anchor a local support cluster."""

    selection = state.artifact(PipelineStep.SELECTION)
    if not isinstance(selection, SelectionPlan):
        return set()
    anchors: set[str] = set()
    for part in selection.parts:
        library, _, symbol_name = part.symbol.partition(":")
        name = symbol_name.lower()
        passive_symbol = (
            library.lower() in {"device", "simulation_spice"}
            and (
                name in {
                    "r",
                    "r_small",
                    "c",
                    "c_small",
                    "l",
                    "l_small",
                    "d",
                    "diode",
                    "led",
                }
                or name.startswith((
                    "r_",
                    "c_",
                    "l_",
                    "d_",
                    "diode_",
                    "led_",
                ))
            )
        )
        if passive_symbol or library.lower() == "jumper":
            continue
        anchors.add(part.ref)
    return anchors


def _board_dims(state: PipelineState) -> tuple[float, float]:
    part = state.artifact(PipelineStep.LAYOUT_PARTITION)
    if isinstance(part, BoardPartition):
        return part.board_width, part.board_height
    return 70.0, 50.0


def _snap(v: float, grid: float = _LAYOUT_GRID_MM) -> float:
    return round(round(v / grid) * grid, 3)


def _dist(a: PcbPlacement, b: PcbPlacement) -> float:
    return ((a.x - b.x) ** 2 + (a.y - b.y) ** 2) ** 0.5


def _grid_cells(
    w: float,
    h: float,
    spacing: float = _PLACE_SPACING_MM,
) -> list[tuple[float, float]]:
    """Row-major grid of placement cells inside the board margins."""
    cells: list[tuple[float, float]] = []
    y = _PLACE_MARGIN_MM
    while y <= h - _PLACE_MARGIN_MM + 1e-6:
        x = _PLACE_MARGIN_MM
        while x <= w - _PLACE_MARGIN_MM + 1e-6:
            cells.append((_snap(x), _snap(y)))
            x += spacing
        y += spacing
    return cells


def _is_mcu_role(role: str) -> bool:
    text = role.lower()
    return text in {"mcu", "controller", "mcu_controller"} or text.endswith("_mcu")


def _is_crystal_role(role: str) -> bool:
    text = role.lower()
    return any(token in text for token in ("crystal", "xtal", "oscillator"))


def _is_decoupling_role(role: str) -> bool:
    text = role.lower()
    return (
        any(token in text for token in ("decoupling", "bypass", "vcap"))
        or (
            "capacitor" in text
            and any(
                token in text
                for token in ("input", "output", "supply", "bulk", "vcc")
            )
        )
    )


def _is_close_memory_role(role: str) -> bool:
    text = role.lower()
    return any(
        token in text
        for token in ("flash", "qspi", "memory", "sram", "sdram", "storage")
    )


def _is_local_support_role(role: str) -> bool:
    """Return whether placement near a functional anchor materially matters."""

    text = role.lower()
    return any(
        token in text
        for token in (
            "series",
            "esd",
            "tvs",
            "termination",
            "terminator",
            "common_mode",
            "commonmode",
            "line_filter",
            "input_filter",
            "output_filter",
            "bias",
            "bootstrap",
            "feedback",
            "compensation",
            "soft_start",
            "softstart",
            "ss_capacitor",
            "snubber",
            "gate_resistor",
            "timing_resistor",
            "cc_rd",
            "cc1",
            "cc2",
            "pullup",
            "pulldown",
            "pull_up",
            "pull_down",
            "divider",
            "shield_rc",
        )
    )


def _local_support_near_mm(role: str) -> float:
    """Return an evidence-oriented maximum distance for local support.

    High-speed interface protection and series parts need a tighter cluster
    than generic bias/filter support.  The decision is semantic and electrical
    rather than tied to a reference or board family.
    """

    text = role.lower()
    if any(
        token in text
        for token in (
            "usb",
            "differential",
            "esd",
            "tvs",
            "cc1",
            "cc2",
            "cc_rd",
        )
    ):
        return 12.0
    if any(
        token in text
        for token in (
            "bootstrap",
            "feedback",
            "compensation",
            "snubber",
            "gate_resistor",
        )
    ):
        return 15.0
    return _LOCAL_SUPPORT_NEAR_MM


def _is_proximity_sensitive_role(role: str) -> bool:
    return (
        _is_decoupling_role(role)
        or _is_crystal_role(role)
        or _is_close_memory_role(role)
        or _is_local_support_role(role)
    )


def _is_connector_role(role: str) -> bool:
    tokens = re.findall(r"[a-z0-9]+", role.lower())
    if not tokens:
        return False
    if tokens[-1] in {
        "connector",
        "header",
        "socket",
        "receptacle",
        "terminal",
        "port",
    }:
        return True
    if tokens[-2:] == ["terminal", "block"]:
        return True
    return tokens in (["power", "input"], ["breakout"])


def _is_connector_part(part: SelectedPart) -> bool:
    """Classify a physical connector from role, symbol library, or reference."""

    symbol_library = part.symbol.partition(":")[0].lower()
    connector_symbol = symbol_library.startswith("connector")
    connector_ref = bool(
        re.fullmatch(r"(?:J|P|CN)\d+[A-Z0-9]*", part.ref, flags=re.IGNORECASE)
    )
    return connector_symbol or connector_ref or _is_connector_role(part.role)


def _is_mounting_hole_role(role: str) -> bool:
    text = role.lower()
    return "mounting" in text and "hole" in text


_PLACEMENT_ROLE_NOISE = {
    "bypass",
    "bulk",
    "cap",
    "capacitor",
    "crystal",
    "decoupling",
    "external",
    "input",
    "load",
    "memory",
    "oscillator",
    "output",
    "storage",
    "vcap",
    "vdd",
    "vdda",
    "xtal",
}


def _connected_refs_by_ref(
    state: PipelineState,
) -> dict[str, dict[str, float]]:
    """Build a weighted connectivity graph from the verified pin map.

    Rare point-to-point nets carry more placement information than a global
    ground or supply rail.  Inverse-fanout weighting prevents an ESD diode on
    GND from being anchored to an unrelated connector merely because both
    happen to share ground.
    """

    pinmap = state.artifact(PipelineStep.SCH_PINMAP)
    if not isinstance(pinmap, PinMapPlan):
        return {}
    connected: dict[str, dict[str, float]] = {}
    for net in pinmap.nets:
        refs = {pin.ref for pin in net.pins}
        if len(refs) < 2:
            continue
        net_text = f"{net.kind} {net.name}".lower()
        global_rail = (
            net.kind.lower() in {"ground", "power", "supply"}
            or net.name.upper() in {"GND", "AGND", "DGND"}
        )
        weight = 1.0 / max(len(refs) - 1, 1)
        if global_rail or any(
            token in net_text
            for token in ("ground", "supply", "power rail")
        ):
            weight *= 0.05
        for ref in refs:
            scores = connected.setdefault(ref, {})
            for other in refs - {ref}:
                scores[other] = scores.get(other, 0.0) + weight
    return connected


def _functional_anchor_ref(
    ref: str,
    role: str,
    roles: dict[str, str],
    targets: dict[str, tuple[float, float]],
    *,
    connected_refs: dict[str, dict[str, float]] | None = None,
    allow_connectors: bool = False,
    eligible_anchor_refs: set[str] | None = None,
) -> str | None:
    wanted = _semantic_role_tokens(role) - _PLACEMENT_ROLE_NOISE
    electrical_neighbors = (
        connected_refs.get(ref)
        if connected_refs is not None
        else None
    )
    candidates: list[tuple[int, float, int, str]] = []
    for candidate_ref, candidate_role in roles.items():
        if (
            candidate_ref == ref
            or candidate_ref not in targets
            or (
                eligible_anchor_refs is not None
                and candidate_ref not in eligible_anchor_refs
            )
            or _is_decoupling_role(candidate_role)
            or _is_crystal_role(candidate_role)
            or _is_local_support_role(candidate_role)
            or (
                _is_connector_role(candidate_role)
                and not allow_connectors
            )
            or (
                electrical_neighbors is not None
                and candidate_ref not in electrical_neighbors
            )
        ):
            continue
        score = len(wanted & _semantic_role_tokens(candidate_role))
        anchor_priority = (
            2
            if _is_connector_role(candidate_role)
            else 1
            if _is_mcu_role(candidate_role)
            else 0
        )
        if score or electrical_neighbors is not None:
            electrical_strength = (
                electrical_neighbors.get(candidate_ref, 0.0)
                if electrical_neighbors is not None
                else 0.0
            )
            candidates.append((
                score,
                electrical_strength,
                anchor_priority,
                candidate_ref,
            ))
    if candidates:
        _, _, _, anchor_ref = max(
            candidates,
            key=lambda item: (
                item[0],
                item[1],
                item[2],
                -list(roles).index(item[3]),
            ),
        )
        return anchor_ref
    if electrical_neighbors is not None:
        return None
    return next(
        (
            candidate_ref
            for candidate_ref, candidate_role in roles.items()
            if candidate_ref in targets and _is_mcu_role(candidate_role)
        ),
        None,
    )


def _functional_anchor_target(
    ref: str,
    role: str,
    roles: dict[str, str],
    targets: dict[str, tuple[float, float]],
    *,
    connected_refs: dict[str, dict[str, float]] | None = None,
    allow_connectors: bool = False,
    eligible_anchor_refs: set[str] | None = None,
) -> tuple[float, float] | None:
    """Find the active functional block a proximity-sensitive part serves."""
    anchor_ref = _functional_anchor_ref(
        ref,
        role,
        roles,
        targets,
        connected_refs=connected_refs,
        allow_connectors=allow_connectors,
        eligible_anchor_refs=eligible_anchor_refs,
    )
    return targets.get(anchor_ref) if anchor_ref is not None else None


class LayoutCriticalStep(PipelineStepBase):
    """Place strongly-constrained parts: MCU central, its crystal/decoupling
    clustered next to it, connectors on the edge. Parts occupy distinct grid
    cells so the baseline is overlap-free; the bottom-line verifies the
    proximity/edge constraints hold."""

    step = PipelineStep.LAYOUT_CRITICAL
    knowledge_role = "layout"
    repair_is_deterministic = True
    repair_strategy_id = "functional_anchor_target_rebuild"

    def knowledge_query(self, state: PipelineState) -> str | None:
        return "critical placement constraints: decoupling, crystal, connectors"

    def propose(
        self, state: PipelineState, ctx: PipelineContext, knowledge: str
    ) -> tuple[BaseModel, bool]:
        roles = _roles(state)
        connected_refs = _connected_refs_by_ref(state)
        eligible_anchor_refs = _functional_anchor_refs(state)
        w, h = _board_dims(state)
        proximity_count = sum(
            1
            for role in roles.values()
            if (
                _is_mcu_role(role)
                or _is_crystal_role(role)
                or _is_close_memory_role(role)
                or _is_decoupling_role(role)
            )
        )
        # This stage records proximity targets rather than manufacturing-ready
        # courtyard placement.  A fixed 10 mm grid falsely rejects MCUs with
        # many supply pins because their legitimate decouplers spill outside
        # the proximity radius.  Densify the target grid as the cluster grows;
        # LayoutGeneralStep later performs the real courtyard-aware packing.
        target_spacing = min(
            _PLACE_SPACING_MM,
            max(
                0.5,
                _DECOUPLE_NEAR_MM / (math.sqrt(max(proximity_count, 1)) + 1.0),
            ),
        )
        cells = _grid_cells(w, h, spacing=target_spacing)
        used: set[tuple[float, float]] = set()
        placements: list[PcbPlacement] = []

        def take_near(cx: float, cy: float) -> tuple[float, float]:
            free = [c for c in cells if c not in used]
            best = min(free, key=lambda c: (c[0] - cx) ** 2 + (c[1] - cy) ** 2)
            used.add(best)
            return best

        def take_edge(left: bool) -> tuple[float, float]:
            free = [c for c in cells if c not in used]
            key = (lambda c: (c[0], c[1])) if left else (lambda c: (-c[0], c[1]))
            best = min(free, key=key)
            used.add(best)
            return best

        cx, cy = w / 2, h / 2
        zone_targets = _zone_targets(state)
        anchor_search_targets = {
            ref: zone_targets.get(ref, (cx, cy))
            for ref in eligible_anchor_refs
        }
        proximity_refs = list(dict.fromkeys(
            [r for r, role in roles.items() if _is_crystal_role(role)]
            + [r for r, role in roles.items() if _is_close_memory_role(role)]
            + [r for r, role in roles.items() if _is_decoupling_role(role)]
        ))
        anchors_by_ref: dict[str, str] = {}
        for ref in proximity_refs:
            role = roles[ref]
            use_connectivity = (
                _is_local_support_role(role)
                or _is_decoupling_role(role)
            )
            anchor_ref = _functional_anchor_ref(
                ref,
                role,
                roles,
                anchor_search_targets,
                connected_refs=(
                    connected_refs if use_connectivity else None
                ),
                allow_connectors=use_connectivity,
                eligible_anchor_refs=eligible_anchor_refs,
            )
            if anchor_ref is not None and anchor_ref != ref:
                anchors_by_ref[ref] = anchor_ref

        # Place the MCU and any non-connector functional anchors before their
        # dependent crystal/memory/decoupling targets.  This prevents a sensor,
        # transceiver, or storage decoupler from being incorrectly clustered
        # around the MCU merely because its real anchor was not yet present.
        anchor_order = list(dict.fromkeys(
            [r for r, role in roles.items() if _is_mcu_role(role)]
            + list(anchors_by_ref.values())
        ))
        for ref in anchor_order:
            if _is_connector_role(roles.get(ref, "")):
                continue
            target = zone_targets.get(ref, (cx, cy))
            x, y = take_near(*target)
            placements.append(PcbPlacement(ref=ref, x=x, y=y))
        for ref, role in roles.items():
            if not _is_connector_role(role):
                continue
            x, y = take_edge(
                left=any(token in role.lower() for token in ("usb", "power", "input"))
            )
            placements.append(PcbPlacement(ref=ref, x=x, y=y))
        by_ref = {placement.ref: placement for placement in placements}
        for ref in proximity_refs:
            if ref in by_ref:
                continue
            anchor = by_ref.get(anchors_by_ref.get(ref, ""))
            target = (
                (anchor.x, anchor.y)
                if anchor is not None
                else zone_targets.get(ref, (cx, cy))
            )
            x, y = take_near(*target)
            placement = PcbPlacement(ref=ref, x=x, y=y)
            placements.append(placement)
            by_ref[ref] = placement
        plan = PcbPlacementPlan(
            board_width=w, board_height=h, placements=placements,
            rationale="critical parts clustered by the MCU; connectors on the edge",
        )
        return plan, False

    def check(self, state: PipelineState, artifact: BaseModel) -> list[CheckResult]:
        assert isinstance(artifact, PcbPlacementPlan)
        roles = _roles(state)
        by_ref = artifact.by_ref()
        w, h = artifact.board_width, artifact.board_height
        mcus = [
            by_ref[ref]
            for ref, role in roles.items()
            if _is_mcu_role(role) and ref in by_ref
        ]

        placement_targets = {
            ref: (placement.x, placement.y)
            for ref, placement in by_ref.items()
        }
        connected_refs = _connected_refs_by_ref(state)
        eligible_anchor_refs = _functional_anchor_refs(state)

        def near_functional_anchor(ref: str) -> float:
            placement = by_ref[ref]
            role = roles[ref]
            use_connectivity = (
                _is_local_support_role(role)
                or _is_decoupling_role(role)
            )
            anchor = _functional_anchor_target(
                ref,
                role,
                roles,
                placement_targets,
                connected_refs=(
                    connected_refs if use_connectivity else None
                ),
                allow_connectors=use_connectivity,
                eligible_anchor_refs=eligible_anchor_refs,
            )
            if anchor is None:
                return min((_dist(placement, m) for m in mcus), default=0.0)
            return math.dist((placement.x, placement.y), anchor)

        bad_edge = [
            ref for ref, role in roles.items()
            if _is_connector_role(role) and ref in by_ref
            and min(
                by_ref[ref].x,
                w - by_ref[ref].x,
                by_ref[ref].y,
                h - by_ref[ref].y,
            ) > _EDGE_MARGIN_MM
        ]
        far_xtal = [
            ref for ref, role in roles.items()
            if _is_crystal_role(role) and ref in by_ref
            and near_functional_anchor(ref) > _CRYSTAL_NEAR_MM
        ]
        far_dec = [
            ref for ref, role in roles.items()
            if _is_decoupling_role(role) and ref in by_ref
            and near_functional_anchor(ref) > _DECOUPLE_NEAR_MM
        ]
        far_memory = [
            ref for ref, role in roles.items()
            if _is_close_memory_role(role) and ref in by_ref
            and not _is_decoupling_role(role)
            and near_functional_anchor(ref) > 20.0
        ]
        return [
            CheckResult(name="connectors_on_edge", ok=not bad_edge,
                        message=f"connectors not near a board edge: {bad_edge}"),
            CheckResult(name="crystal_near_mcu", ok=not far_xtal,
                        message=f"crystal too far from MCU: {far_xtal}"),
            CheckResult(name="decoupling_near_mcu", ok=not far_dec,
                        message=f"decoupling too far from MCU: {far_dec}"),
            CheckResult(name="memory_near_mcu", ok=not far_memory,
                        message=f"close-coupled memory too far from MCU: {far_memory}"),
        ]

    def repair(
        self,
        state: PipelineState,
        ctx: PipelineContext,
        knowledge: str,
        artifact: BaseModel,
        checks: list[CheckResult],
    ) -> tuple[BaseModel, bool]:
        # Critical placement is a target plan rather than a materialized PCB.
        # Rebuilding it from the current role/connectivity graph is bounded and
        # safer than trying to move stale targets one at a time.
        return self.propose(state, ctx, knowledge)

    def summarize(self, artifact: BaseModel) -> str:
        assert isinstance(artifact, PcbPlacementPlan)
        return f"{len(artifact.placements)} critical parts placed"


_UNKNOWN_FOOTPRINT_HALF_MM = 1.5


def _placement_bbox(fp: str) -> tuple[float, float, float, float]:
    bbox = footprints.footprint_courtyard_bbox(fp) if fp else None
    if bbox is None:
        half = _UNKNOWN_FOOTPRINT_HALF_MM
        return -half, -half, half, half
    return bbox


def _rotated_bbox(
    bbox: tuple[float, float, float, float],
    rotation: float,
) -> tuple[float, float, float, float]:
    radians = math.radians(rotation % 360.0)
    cos_value, sin_value = math.cos(radians), math.sin(radians)
    points = [
        (
            x * cos_value + y * sin_value,
            -x * sin_value + y * cos_value,
        )
        for x in (bbox[0], bbox[2])
        for y in (bbox[1], bbox[3])
    ]
    return (
        min(point[0] for point in points),
        min(point[1] for point in points),
        max(point[0] for point in points),
        max(point[1] for point in points),
    )


def _role_group(role: str) -> str:
    text = role.lower()
    if "mcu" in text:
        return "digital"
    if "usb" in text:
        return "usb"
    if "can" in text:
        return "can"
    if any(token in text for token in ("microsd", "sdio", "flash", "storage")):
        return "storage"
    if any(token in text for token in ("analog", "adc")):
        return "analog"
    if any(token in text for token in ("sensor", "accelerometer", "i2c")):
        return "sensor"
    if any(token in text for token in ("led", "button", "user")):
        return "interface"
    if any(token in text for token in ("connector", "header", "socket")):
        return "connector"
    if any(
        token in text
        for token in (
            "crystal",
            "xtal",
            "oscillator",
            "decoupling",
            "bypass",
            "vcap",
        )
    ):
        return "digital"
    if any(token in text for token in ("power", "regulator", "buck", "ldo", "fuse")):
        return "power"
    return ""


def _resolved_zone_targets(
    state: PipelineState,
) -> tuple[dict[str, tuple[float, float]], dict[str, list[str]]]:
    """Resolve one zone target per reference without silently breaking ties."""

    partition = state.artifact(PipelineStep.LAYOUT_PARTITION)
    if not isinstance(partition, BoardPartition):
        return {}, {}
    roles = _roles(state)
    zones = partition.zones
    targets: dict[str, tuple[float, float]] = {}
    ambiguities: dict[str, list[str]] = {}
    aliases = {
        "digital": ("digital", "mcu"),
        "usb": ("usb",),
        "can": ("can",),
        "storage": ("storage", "flash", "sd"),
        "analog": ("analog",),
        "sensor": ("sensor", "mixed", "i2c"),
        "interface": ("interface", "user"),
        "connector": ("connector",),
        "power": ("power",),
    }
    for ref, role in roles.items():
        exact = [
            zone
            for zone in zones
            if zone.name.strip().casefold() == ref.strip().casefold()
        ]
        if len(exact) == 1:
            zone = exact[0]
            targets[ref] = (
                (zone.x1 + zone.x2) / 2,
                (zone.y1 + zone.y2) / 2,
            )
            continue
        if len(exact) > 1:
            ambiguities[ref] = [zone.name for zone in exact]
            continue
        group = _role_group(role)
        role_tokens = _semantic_role_tokens(role)
        scored: list[tuple[int, int, BoardZone]] = []
        for index, zone in enumerate(zones):
            zone_text = f"{zone.kind} {zone.name}".lower()
            fixed_match = bool(
                group
                and any(token in zone_text for token in aliases[group])
            )
            overlap = len(role_tokens & _semantic_role_tokens(zone_text))
            score = (100 if fixed_match else 0) + overlap
            if score:
                scored.append((score, -index, zone))
        if not scored:
            continue
        best_score = max(item[0] for item in scored)
        best = [item for item in scored if item[0] == best_score]
        if len(best) > 1:
            ambiguities[ref] = [item[2].name for item in best]
            continue
        zone = best[0][2]
        targets[ref] = ((zone.x1 + zone.x2) / 2, (zone.y1 + zone.y2) / 2)
    return targets, ambiguities


def _zone_targets(state: PipelineState) -> dict[str, tuple[float, float]]:
    return _resolved_zone_targets(state)[0]


def _placement_constraints(state: PipelineState):
    partition = state.artifact(PipelineStep.LAYOUT_PARTITION)
    if not isinstance(partition, BoardPartition):
        return compile_placement_constraints(
            BoardPartition(board_width=1, board_height=1),
            {},
            state.requirement_text,
        )
    constraints = partition.placement_constraints
    if constraints.constraint_digest:
        return constraints
    return compile_placement_constraints(partition, _roles(state), state.requirement_text)


def _exact_reference_zones(state: PipelineState) -> dict[str, BoardZone]:
    partition = state.artifact(PipelineStep.LAYOUT_PARTITION)
    if not isinstance(partition, BoardPartition):
        return {}
    selected_refs = set(_roles(state))
    by_name: dict[str, list[BoardZone]] = {}
    for zone in partition.zones:
        by_name.setdefault(zone.name.strip().casefold(), []).append(zone)
    return {
        ref: matches[0]
        for ref in selected_refs
        if len(matches := by_name.get(ref.strip().casefold(), [])) == 1
    }


def _prune_free_rectangles(
    rectangles: list[tuple[float, float, float, float]],
) -> list[tuple[float, float, float, float]]:
    out: list[tuple[float, float, float, float]] = []
    for index, rect in enumerate(rectangles):
        x, y, width, height = rect
        if width <= 1e-6 or height <= 1e-6:
            continue
        contained = any(
            index != other_index
            and x >= other[0] - 1e-6
            and y >= other[1] - 1e-6
            and x + width <= other[0] + other[2] + 1e-6
            and y + height <= other[1] + other[3] + 1e-6
            for other_index, other in enumerate(rectangles)
        )
        if not contained:
            out.append(rect)
    return out


def _split_free_rectangles(
    rectangles: list[tuple[float, float, float, float]],
    used: tuple[float, float, float, float],
) -> list[tuple[float, float, float, float]]:
    ux, uy, used_width, used_height = used
    result: list[tuple[float, float, float, float]] = []
    for x, y, width, height in rectangles:
        if (
            ux >= x + width
            or ux + used_width <= x
            or uy >= y + height
            or uy + used_height <= y
        ):
            result.append((x, y, width, height))
            continue
        if ux > x:
            result.append((x, y, ux - x, height))
        if ux + used_width < x + width:
            result.append((
                ux + used_width,
                y,
                x + width - ux - used_width,
                height,
            ))
        if uy > y:
            result.append((x, y, width, uy - y))
        if uy + used_height < y + height:
            result.append((
                x,
                uy + used_height,
                width,
                y + height - uy - used_height,
            ))
    return _prune_free_rectangles(result)


def _maxrect_pack(
    order: list[str],
    fps: dict[str, str],
    board_width: float,
    board_height: float,
    clearance: float,
    targets: dict[str, tuple[float, float]] | None = None,
    priority_refs: set[str] | None = None,
    edge_refs: set[str] | None = None,
    fixed_target_refs: set[str] | None = None,
    allowed_regions: dict[str, tuple[float, float, float, float]] | None = None,
) -> tuple[list[PcbPlacement], list[str]]:
    """Pack real footprint courtyards inside the fixed board outline."""
    targets = targets or {}
    priority_refs = priority_refs or set()
    edge_refs = edge_refs or set()
    fixed_target_refs = fixed_target_refs or set()
    allowed_regions = allowed_regions or {}
    edge = max(config.process_capability().min_board_edge_clearance, 0.5)
    available_pitch = math.sqrt(
        board_width * board_height / max(len(order), 1)
    )
    routing_channel = min(0.75, max(0.25, available_pitch * 0.08))
    pad = max(clearance, 0.2) + routing_channel
    free = [(
        edge,
        edge,
        board_width - 2 * edge,
        board_height - 2 * edge,
    )]
    boxes = {ref: _placement_bbox(fps.get(ref, "")) for ref in order}
    order_index = {ref: index for index, ref in enumerate(order)}
    packed_order = sorted(
        order,
        key=lambda ref: (
            0 if ref in priority_refs else 1,
            (
                order_index[ref]
                if ref in priority_refs
                else -max(
                    boxes[ref][2] - boxes[ref][0],
                    boxes[ref][3] - boxes[ref][1],
                )
            ),
            (
                0.0
                if ref in priority_refs
                else -(
                    (boxes[ref][2] - boxes[ref][0])
                    * (boxes[ref][3] - boxes[ref][1])
                )
            ),
            order_index[ref],
        ),
    )
    placements: list[PcbPlacement] = []
    unplaced: list[str] = []
    diagonal = max(math.hypot(board_width, board_height), 1.0)
    for ref in packed_order:
        target = targets.get(ref)
        candidates: list[
            tuple[
                tuple[float, float, float, float],
                tuple[float, float, float, float],
                float,
                float,
                float,
            ]
        ] = []
        for free_rect in free:
            fx, fy, free_width, free_height = free_rect
            for rotation in (0.0, 90.0):
                bbox = _rotated_bbox(boxes[ref], rotation)
                width = bbox[2] - bbox[0] + 2 * pad
                height = bbox[3] - bbox[1] + 2 * pad
                if width > free_width + 1e-6 or height > free_height + 1e-6:
                    continue
                desired = [(fx, fy)]
                if target is not None:
                    desired.append((
                        min(max(target[0] - width / 2, fx), fx + free_width - width),
                        min(max(target[1] - height / 2, fy), fy + free_height - height),
                    ))
                for desired_x, desired_y in desired:
                    # Keep the exact packing coordinates for geometry tests.
                    # Rounding here can move the used rectangle a fraction of a
                    # micron outside its free rectangle; after the first split,
                    # every later candidate can then be rejected as out of bounds.
                    origin_x = desired_x + pad - bbox[0]
                    origin_y = desired_y + pad - bbox[1]
                    used_x = origin_x + bbox[0] - pad
                    used_y = origin_y + bbox[1] - pad
                    if (
                        used_x < fx - 1e-6
                        or used_y < fy - 1e-6
                        or used_x + width > fx + free_width + 1e-6
                        or used_y + height > fy + free_height + 1e-6
                    ):
                        continue
                    region = allowed_regions.get(ref)
                    if region is not None and not (
                        region[0] <= origin_x <= region[2]
                        and region[1] <= origin_y <= region[3]
                    ):
                        continue
                    if (
                        ref in edge_refs
                        and min(
                            origin_x,
                            board_width - origin_x,
                            origin_y,
                            board_height - origin_y,
                        ) > _EDGE_MARGIN_MM
                    ):
                        continue
                    short_left = min(free_width - width, free_height - height)
                    long_left = max(free_width - width, free_height - height)
                    center_x = origin_x + (bbox[0] + bbox[2]) / 2
                    center_y = origin_y + (bbox[1] + bbox[3]) / 2
                    distance = (
                        math.dist((center_x, center_y), target) / diagonal
                        if target is not None
                        else 0.0
                    )
                    if (
                        ref in fixed_target_refs
                        and target is not None
                        and math.dist((origin_x, origin_y), target)
                        > _EDGE_MARGIN_MM
                    ):
                        continue
                    fit_score = (short_left + 0.1 * long_left) / diagonal
                    # MaxRects still owns geometric legality, but a functional
                    # partition must materially affect the chosen rectangle.
                    # The old near-zero weight routinely put the MCU outside
                    # its digital zone and increased routing congestion.
                    score = fit_score + _PLACEMENT_TARGET_WEIGHT * distance
                    candidates.append((
                        (score, short_left, long_left, used_y),
                        (used_x, used_y, width, height),
                        origin_x,
                        origin_y,
                        rotation,
                    ))
        if not candidates:
            unplaced.append(ref)
            continue
        _, used, origin_x, origin_y, rotation = min(
            candidates, key=lambda candidate: candidate[0]
        )
        placements.append(PcbPlacement(
            ref=ref,
            x=origin_x,
            y=origin_y,
            rotation=rotation,
        ))
        free = _split_free_rectangles(free, used)
    return placements, unplaced


def _placement_shape(
    fp: str,
) -> tuple[tuple[float, float, float, float], ...]:
    rectangles = footprints.footprint_courtyard_rects(fp) if fp else None
    return rectangles or (_placement_bbox(fp),)


def _rotated_shape(
    rectangles: tuple[tuple[float, float, float, float], ...],
    rotation: float,
    padding: float,
) -> tuple[tuple[float, float, float, float], ...]:
    return tuple(
        (
            rotated[0] - padding,
            rotated[1] - padding,
            rotated[2] + padding,
            rotated[3] + padding,
        )
        for rectangle in rectangles
        for rotated in (_rotated_bbox(rectangle, rotation),)
    )


def _shape_target_error(
    placements: list[PcbPlacement],
    targets: dict[str, tuple[float, float]],
    diagonal: float,
) -> float:
    distances = [
        math.dist((placement.x, placement.y), targets[placement.ref]) / diagonal
        for placement in placements
        if placement.ref in targets
    ]
    return sum(distances) / max(len(distances), 1)


def _absolute_placement_shape(
    placement: PcbPlacement,
    fp: str,
) -> tuple[tuple[float, float, float, float], ...]:
    return tuple(
        (
            placement.x + rectangle[0],
            placement.y + rectangle[1],
            placement.x + rectangle[2],
            placement.y + rectangle[3],
        )
        for rectangle in _rotated_shape(
            _placement_shape(fp),
            placement.rotation,
            0.0,
        )
    )


def _courtyard_shape_pack(
    order: list[str],
    fps: dict[str, str],
    board_width: float,
    board_height: float,
    clearance: float,
    targets: dict[str, tuple[float, float]] | None = None,
    *,
    target_weight: float,
    preserve_order: bool = False,
    dependency: dict[str, str] | None = None,
    edge_refs: set[str] | None = None,
    fixed_target_refs: set[str] | None = None,
    priority_refs: set[str] | None = None,
    fixed_placements: list[PcbPlacement] | None = None,
    allowed_regions: dict[str, tuple[float, float, float, float]] | None = None,
) -> tuple[list[PcbPlacement], list[str]]:
    """Grid-pack decomposed concave courtyards inside a fixed board outline.

    MaxRects is retained as the fast path for ordinary rectangular courtyards.
    This fallback is used when an edge-mounted module has a concave courtyard:
    treating its antenna/connector keepout bounding box as a solid rectangle
    can incorrectly consume most of a small board.
    """

    targets = targets or {}
    dependency = dependency or {}
    edge_refs = edge_refs or set()
    fixed_target_refs = fixed_target_refs or set()
    priority_refs = priority_refs or set()
    fixed_placements = fixed_placements or []
    allowed_regions = allowed_regions or {}
    edge = max(config.process_capability().min_board_edge_clearance, 0.5)
    # Courtyard geometry already carries the library author's assembly
    # clearance. Add only the configured fabrication clearance here. Adding a
    # second per-component "routing channel" makes dense but manufacturable
    # boards mathematically impossible before the router can use their copper
    # layers.
    padding = max(clearance, 0.15)
    shapes = {
        ref: _placement_shape(fps.get(ref, ""))
        for ref in order
    }
    fixed_refs = {placement.ref for placement in fixed_placements}
    packed_order = [ref for ref in order if ref not in fixed_refs]
    if not preserve_order:
        order_index = {ref: index for index, ref in enumerate(order)}
        packed_order.sort(
            key=lambda ref: (
                0 if ref in priority_refs else 1,
                -sum(
                    (rectangle[2] - rectangle[0])
                    * (rectangle[3] - rectangle[1])
                    for rectangle in shapes[ref]
                ),
                -max(
                    max(
                        rectangle[2] - rectangle[0],
                        rectangle[3] - rectangle[1],
                    )
                    for rectangle in shapes[ref]
                ),
                order_index[ref],
            ),
        )
    occupied: list[tuple[float, float, float, float]] = [
        (
            placement.x + rectangle[0],
            placement.y + rectangle[1],
            placement.x + rectangle[2],
            placement.y + rectangle[3],
        )
        for placement in fixed_placements
        for rectangle in _rotated_shape(
            shapes[placement.ref],
            placement.rotation,
            padding,
        )
    ]
    placements: list[PcbPlacement] = [
        placement.model_copy(deep=True)
        for placement in fixed_placements
    ]
    unplaced: list[str] = []
    diagonal = max(math.hypot(board_width, board_height), 1.0)

    for ref in packed_order:
        target = targets.get(ref)
        anchor_ref = dependency.get(ref)
        if anchor_ref is not None:
            anchor = next(
                (
                    placement
                    for placement in placements
                    if placement.ref == anchor_ref
                ),
                None,
            )
            if anchor is not None:
                target = (anchor.x, anchor.y)
        candidates: list[
            tuple[tuple[float, float, float, float], PcbPlacement]
        ] = []
        for rotation in (0.0, 90.0):
            local_rectangles = _rotated_shape(
                shapes[ref],
                rotation,
                padding,
            )
            local_x1 = min(rectangle[0] for rectangle in local_rectangles)
            local_y1 = min(rectangle[1] for rectangle in local_rectangles)
            local_x2 = max(rectangle[2] for rectangle in local_rectangles)
            local_y2 = max(rectangle[3] for rectangle in local_rectangles)
            origin_x_min = edge - local_x1
            origin_y_min = edge - local_y1
            origin_x_max = board_width - edge - local_x2
            origin_y_max = board_height - edge - local_y2
            if (
                origin_x_min > origin_x_max + 1e-6
                or origin_y_min > origin_y_max + 1e-6
            ):
                continue
            x_start = math.ceil(origin_x_min / _LAYOUT_GRID_MM) * _LAYOUT_GRID_MM
            y_start = math.ceil(origin_y_min / _LAYOUT_GRID_MM) * _LAYOUT_GRID_MM
            x_end = math.floor(origin_x_max / _LAYOUT_GRID_MM) * _LAYOUT_GRID_MM
            y_end = math.floor(origin_y_max / _LAYOUT_GRID_MM) * _LAYOUT_GRID_MM
            x_values = [
                x_start + index * _LAYOUT_GRID_MM
                for index in range(
                    max(0, int(round((x_end - x_start) / _LAYOUT_GRID_MM))) + 1
                )
            ]
            y_values = [
                y_start + index * _LAYOUT_GRID_MM
                for index in range(
                    max(0, int(round((y_end - y_start) / _LAYOUT_GRID_MM))) + 1
                )
            ]
            if target is not None:
                preferred_x = min(max(_snap(target[0]), x_start), x_end)
                preferred_y = min(max(_snap(target[1]), y_start), y_end)
                x_values = list(dict.fromkeys([preferred_x, *x_values]))
                y_values = list(dict.fromkeys([preferred_y, *y_values]))
            for origin_y in y_values:
                for origin_x in x_values:
                    region = allowed_regions.get(ref)
                    if region is not None and not (
                        region[0] <= origin_x <= region[2]
                        and region[1] <= origin_y <= region[3]
                    ):
                        continue
                    if (
                        ref in edge_refs
                        and min(
                            origin_x,
                            board_width - origin_x,
                            origin_y,
                            board_height - origin_y,
                        ) > _EDGE_MARGIN_MM
                    ):
                        continue
                    if (
                        ref in fixed_target_refs
                        and target is not None
                        and math.dist((origin_x, origin_y), target)
                        > _EDGE_MARGIN_MM
                    ):
                        continue
                    absolute = tuple(
                        (
                            origin_x + rectangle[0],
                            origin_y + rectangle[1],
                            origin_x + rectangle[2],
                            origin_y + rectangle[3],
                        )
                        for rectangle in local_rectangles
                    )
                    if any(
                        _boxes_overlap(rectangle, other, 0.0)
                        for rectangle in absolute
                        for other in occupied
                    ):
                        continue
                    distance = (
                        math.dist((origin_x, origin_y), target) / diagonal
                        if target is not None
                        else 0.0
                    )
                    combined = [*occupied, *absolute]
                    used_x1 = min(rectangle[0] for rectangle in combined)
                    used_y1 = min(rectangle[1] for rectangle in combined)
                    used_x2 = max(rectangle[2] for rectangle in combined)
                    used_y2 = max(rectangle[3] for rectangle in combined)
                    compactness = (
                        (used_x2 - used_x1) * (used_y2 - used_y1)
                        / max(board_width * board_height, 1.0)
                    )
                    bottom_left = (
                        used_y2 / max(board_height, 1.0)
                        + 0.2 * used_x2 / max(board_width, 1.0)
                    )
                    score = (
                        target_weight * distance
                        + 0.65 * compactness
                        + 0.05 * bottom_left
                    )
                    candidates.append((
                        (score, distance, origin_y, origin_x),
                        PcbPlacement(
                            ref=ref,
                            x=origin_x,
                            y=origin_y,
                            rotation=rotation,
                        ),
                    ))
        if not candidates:
            unplaced.append(ref)
            continue
        placement = min(candidates, key=lambda candidate: candidate[0])[1]
        placements.append(placement)
        occupied.extend(
            (
                placement.x + rectangle[0],
                placement.y + rectangle[1],
                placement.x + rectangle[2],
                placement.y + rectangle[3],
            )
            for rectangle in _rotated_shape(
                shapes[ref],
                placement.rotation,
                padding,
            )
        )
    return placements, unplaced


def _repair_proximity_placements(
    state: PipelineState,
    plan: PcbPlacementPlan,
) -> PcbPlacementPlan:
    """Move only proximity offenders using bounded, collision-safe local search."""

    roles = _roles(state)
    footprints_by_ref = _footprints_of(state)
    placements = {
        placement.ref: placement.model_copy(deep=True)
        for placement in plan.placements
    }
    cap = config.process_capability()
    connected_refs = _connected_refs_by_ref(state)
    eligible_anchor_refs = _functional_anchor_refs(state)

    def limit_for(role: str) -> float | None:
        if _is_crystal_role(role):
            return _CRYSTAL_NEAR_MM
        if _is_decoupling_role(role):
            return _DECOUPLE_NEAR_MM
        if _is_close_memory_role(role):
            return 20.0
        if _is_local_support_role(role):
            return _local_support_near_mm(role)
        return None

    for ref, role in roles.items():
        current = placements.get(ref)
        limit = limit_for(role)
        if current is None or limit is None:
            continue
        targets = {
            placed_ref: (placed.x, placed.y)
            for placed_ref, placed in placements.items()
        }
        use_connectivity = (
            _is_local_support_role(role)
            or _is_decoupling_role(role)
        )
        anchor = _functional_anchor_target(
            ref,
            role,
            roles,
            targets,
            connected_refs=(connected_refs if use_connectivity else None),
            allow_connectors=use_connectivity,
            eligible_anchor_refs=eligible_anchor_refs,
        )
        if anchor is None or math.dist((current.x, current.y), anchor) <= limit:
            continue

        candidates: list[tuple[tuple[float, float], PcbPlacement]] = []
        seen: set[tuple[float, float]] = set()
        radius = _LAYOUT_GRID_MM
        while radius <= limit + 1e-6:
            for angle_degrees in range(0, 360, 15):
                angle = math.radians(angle_degrees)
                x = _snap(anchor[0] + radius * math.cos(angle))
                y = _snap(anchor[1] + radius * math.sin(angle))
                if (x, y) in seen:
                    continue
                seen.add((x, y))
                candidate = current.model_copy(update={"x": x, "y": y})
                candidate_shape = _absolute_placement_shape(
                    candidate,
                    footprints_by_ref.get(ref, ""),
                )
                edge = cap.min_board_edge_clearance
                if any(
                    rectangle[0] < edge
                    or rectangle[1] < edge
                    or rectangle[2] > plan.board_width - edge
                    or rectangle[3] > plan.board_height - edge
                    for rectangle in candidate_shape
                ):
                    continue
                collision = False
                for other_ref, other in placements.items():
                    if other_ref == ref:
                        continue
                    other_shape = _absolute_placement_shape(
                        other,
                        footprints_by_ref.get(other_ref, ""),
                    )
                    if any(
                        _boxes_overlap(
                            rectangle,
                            other_rectangle,
                            cap.min_clearance,
                        )
                        for rectangle in candidate_shape
                        for other_rectangle in other_shape
                    ):
                        collision = True
                        break
                if collision:
                    continue
                anchor_distance = math.dist((x, y), anchor)
                movement = math.dist((x, y), (current.x, current.y))
                candidates.append(((anchor_distance, movement), candidate))
            radius += _LAYOUT_GRID_MM
        if candidates:
            placements[ref] = min(candidates, key=lambda item: item[0])[1]

    return PcbPlacementPlan(
        board_width=plan.board_width,
        board_height=plan.board_height,
        placements=[
            placements[placement.ref]
            for placement in plan.placements
            if placement.ref in placements
        ],
        rationale=(
            f"{plan.rationale}; AHE collision-safe functional-anchor local repair"
        ),
    )


def _repair_substitution_overlaps(
    state: PipelineState,
    plan: PcbPlacementPlan,
    changed_refs: set[str],
) -> PcbPlacementPlan:
    """Relocate only local support newly overlapped by a changed footprint."""

    roles = _roles(state)
    footprints_by_ref = _footprints_of(state)
    connected_refs = _connected_refs_by_ref(state)
    eligible_anchor_refs = _functional_anchor_refs(state)
    cap = config.process_capability()
    placements = {
        placement.ref: placement.model_copy(deep=True)
        for placement in plan.placements
    }
    overlaps, _ = _placement_geometry_violations(state, plan)
    movable: list[str] = []
    for pair in overlaps:
        left, _, right = pair.partition("&")
        if left in changed_refs and _is_local_support_role(roles.get(right, "")):
            movable.append(right)
        elif right in changed_refs and _is_local_support_role(roles.get(left, "")):
            movable.append(left)
    for ref in dict.fromkeys(movable):
        current = placements.get(ref)
        role = roles.get(ref, "")
        if current is None:
            continue
        targets = {
            placed_ref: (placement.x, placement.y)
            for placed_ref, placement in placements.items()
        }
        anchor = _functional_anchor_target(
            ref,
            role,
            roles,
            targets,
            connected_refs=connected_refs,
            allow_connectors=True,
            eligible_anchor_refs=eligible_anchor_refs,
        )
        if anchor is None:
            continue
        candidates: list[tuple[tuple[float, float], PcbPlacement]] = []
        seen: set[tuple[float, float, float]] = set()
        radius = _LAYOUT_GRID_MM
        limit = _local_support_near_mm(role)
        while radius <= limit + 1e-6:
            for angle_degrees in range(0, 360, 15):
                angle = math.radians(angle_degrees)
                x = _snap(anchor[0] + radius * math.cos(angle))
                y = _snap(anchor[1] + radius * math.sin(angle))
                for rotation in _LEGAL_ROTATIONS:
                    key = (x, y, rotation)
                    if key in seen:
                        continue
                    seen.add(key)
                    candidate = current.model_copy(
                        update={"x": x, "y": y, "rotation": rotation}
                    )
                    candidate_shape = _absolute_placement_shape(
                        candidate,
                        footprints_by_ref.get(ref, ""),
                    )
                    edge = cap.min_board_edge_clearance
                    if any(
                        rectangle[0] < edge
                        or rectangle[1] < edge
                        or rectangle[2] > plan.board_width - edge
                        or rectangle[3] > plan.board_height - edge
                        for rectangle in candidate_shape
                    ):
                        continue
                    if any(
                        _boxes_overlap(
                            rectangle,
                            other_rectangle,
                            cap.min_clearance,
                        )
                        for other_ref, other in placements.items()
                        if other_ref != ref
                        for rectangle in candidate_shape
                        for other_rectangle in _absolute_placement_shape(
                            other,
                            footprints_by_ref.get(other_ref, ""),
                        )
                    ):
                        continue
                    candidates.append((
                        (
                            math.dist((x, y), anchor),
                            math.dist((x, y), (current.x, current.y)),
                        ),
                        candidate,
                    ))
            radius += _LAYOUT_GRID_MM
        if candidates:
            placements[ref] = min(candidates, key=lambda item: item[0])[1]
    return PcbPlacementPlan(
        board_width=plan.board_width,
        board_height=plan.board_height,
        placements=[
            placements[item.ref]
            for item in plan.placements
        ],
        rationale=(
            f"{plan.rationale}; AHE relocated only local support overlapped "
            "by the substituted footprint"
        ),
    )


def _recover_layout_from_existing_board(
    state: PipelineState,
    ctx: PipelineContext,
) -> PcbPlacementPlan | None:
    """Recover a verified placement after a pin-compatible footprint replan.

    A schematic or manufacturing repair may leave the selected component set
    intact. Repacking the entire board would unnecessarily discard verified
    placement. Reuse is allowed only when every selected reference exists on
    the real board, every footprint is either unchanged or explicitly selected
    as its replacement, all current semantic placement checks pass, and the
    real courtyards remain legal.
    """

    from ratsnestpro.eda.vendor.pcb import PcbBoard

    if not ctx.out_dir:
        return None
    pcb_path = Path(ctx.out_dir) / f"{state.project_name}.kicad_pcb"
    if not pcb_path.is_file():
        return None
    selection = state.artifact(PipelineStep.SELECTION)
    if not isinstance(selection, SelectionPlan):
        return None
    try:
        board = PcbBoard.load(pcb_path)
        entries = {
            str(item.get("reference", "")): item
            for item in board.list_footprints()
            if item.get("reference") and item.get("at")
        }
    except Exception:
        return None
    selected = {part.ref: part for part in selection.parts}
    if set(entries) != set(selected):
        return None
    changed_refs = {
        ref
        for ref, part in selected.items()
        if str(entries[ref].get("lib_id", "")) != part.footprint
    }
    width, height = _board_dims(state)
    plan = PcbPlacementPlan(
        board_width=width,
        board_height=height,
        placements=[
            PcbPlacement(
                ref=part.ref,
                x=float(entries[part.ref]["at"]["x"]),
                y=float(entries[part.ref]["at"]["y"]),
                rotation=float(entries[part.ref]["at"]["rotation"]),
                side=(
                    "back"
                    if str(entries[part.ref].get("layer", "")).startswith("B.")
                    else "front"
                ),
            )
            for part in selection.parts
        ],
        rationale=(
            "AHE recovered verified real-PCB placement after a compatible "
            "upstream replan"
        ),
    )
    plan = _repair_substitution_overlaps(state, plan, changed_refs)
    if any(
        not check.ok and check.severity == Severity.ERROR
        for check in LayoutGeneralStep().check(state, plan)
    ):
        return None
    overlaps, out_of_bounds = _placement_geometry_violations(state, plan)
    if overlaps or out_of_bounds:
        return None
    return plan


class LayoutGeneralStep(PipelineStepBase):
    """Place the remaining parts in free grid cells and tidy: snap to grid,
    normalize rotation. Bottom-line: all parts placed, on-grid, legal orient."""

    step = PipelineStep.LAYOUT_GENERAL
    repair_is_deterministic = True
    knowledge_role = "layout"
    repair_strategy_id = "functional_anchor_local_search"

    def knowledge_query(self, state: PipelineState) -> str | None:
        return "placement alignment, grid, orientation, tidy layout"

    def propose(
        self, state: PipelineState, ctx: PipelineContext, knowledge: str
    ) -> tuple[BaseModel, bool]:
        recovered = _recover_layout_from_existing_board(state, ctx)
        if recovered is not None:
            return recovered, False
        roles = _roles(state)
        connected_refs = _connected_refs_by_ref(state)
        eligible_anchor_refs = _functional_anchor_refs(state)
        w, h = _board_dims(state)
        fps = _footprints_of(state)
        crit = state.artifact(PipelineStep.LAYOUT_CRITICAL)
        crit_refs = [p.ref for p in crit.placements] if isinstance(crit, PcbPlacementPlan) else []
        # Only geometrically dominant critical parts are packed ahead of size
        # sorting. Small decouplers and crystals still keep their critical
        # targets, but packing all of them first fragments the board before the
        # MCU and connectors can be placed.
        priority_refs = {
            ref
            for ref, role in roles.items()
            if (
                _is_mcu_role(role)
                or (
                    _is_close_memory_role(role)
                    and not _is_decoupling_role(role)
                )
                or _is_connector_role(role)
                or _is_mounting_hole_role(role)
            )
        }
        edge_refs = {
            ref
            for ref, role in roles.items()
            if (
                _is_connector_role(role)
                or _is_mounting_hole_role(role)
            )
        }
        priority_order = [
            ref
            for ref, role in roles.items()
            if _is_mounting_hole_role(role)
        ] + [
            ref
            for ref, role in roles.items()
            if _is_mcu_role(role)
        ] + [
            ref
            for ref, role in roles.items()
            if (
                _is_close_memory_role(role)
                and not _is_decoupling_role(role)
                and not _is_mcu_role(role)
            )
        ] + [
            ref
            for ref, role in roles.items()
            if _is_connector_role(role)
        ]
        order = list(dict.fromkeys([
            *priority_order,
            *crit_refs,
            *roles,
        ]))
        cap = config.process_capability()
        targets = _zone_targets(state)
        constraints = _placement_constraints(state)
        allowed_regions = allowed_origin_regions(constraints)
        if isinstance(crit, PcbPlacementPlan):
            targets.update({
                placement.ref: (placement.x, placement.y)
                for placement in crit.placements
            })
        mounting_refs = [
            ref
            for ref, role in roles.items()
            if _is_mounting_hole_role(role)
        ]
        corner_margin = max(_EDGE_MARGIN_MM / 2, 3.0)
        corners = (
            (corner_margin, corner_margin),
            (w - corner_margin, corner_margin),
            (corner_margin, h - corner_margin),
            (w - corner_margin, h - corner_margin),
        )
        for ref, target in zip(mounting_refs, corners, strict=False):
            targets[ref] = target
        fixed_mounting_placements = [
            PcbPlacement(ref=ref, x=target[0], y=target[1])
            for ref, target in zip(mounting_refs, corners, strict=False)
        ]
        for ref, role in roles.items():
            if not _is_proximity_sensitive_role(role):
                continue
            use_connectivity = (
                _is_local_support_role(role)
                or _is_decoupling_role(role)
            )
            anchor = _functional_anchor_target(
                ref,
                role,
                roles,
                targets,
                connected_refs=(
                    connected_refs if use_connectivity else None
                ),
                allow_connectors=use_connectivity,
                eligible_anchor_refs=eligible_anchor_refs,
            )
            if anchor is not None:
                targets[ref] = anchor
        placements, _unplaced = _maxrect_pack(
            order,
            fps,
            w,
            h,
            cap.min_clearance,
            targets,
            priority_refs,
            edge_refs,
            set(mounting_refs),
            allowed_regions,
        )
        # Critical-plan coordinates are targets, not the final packed
        # coordinates.  Refine dependent targets from the first pass so a
        # regulator/flash decoupler follows the regulator/flash's *actual*
        # location rather than its stale critical-plan location.
        actual_targets = dict(targets)
        actual_targets.update({
            placement.ref: (placement.x, placement.y)
            for placement in placements
        })
        refined_targets = dict(targets)
        for ref, role in roles.items():
            if not _is_proximity_sensitive_role(role):
                continue
            use_connectivity = (
                _is_local_support_role(role)
                or _is_decoupling_role(role)
            )
            anchor = _functional_anchor_target(
                ref,
                role,
                roles,
                actual_targets,
                connected_refs=(
                    connected_refs if use_connectivity else None
                ),
                allow_connectors=use_connectivity,
                eligible_anchor_refs=eligible_anchor_refs,
            )
            if anchor is not None:
                refined_targets[ref] = anchor
        placements, _unplaced = _maxrect_pack(
            order,
            fps,
            w,
            h,
            cap.min_clearance,
            refined_targets,
            priority_refs,
            edge_refs,
            set(mounting_refs),
            allowed_regions,
        )
        if _unplaced:
            diagonal = max(math.hypot(w, h), 1.0)
            shape_areas = {
                ref: sum(
                    (rectangle[2] - rectangle[0])
                    * (rectangle[3] - rectangle[1])
                    for rectangle in _placement_shape(fps.get(ref, ""))
                )
                for ref in order
            }
            dependency: dict[str, str] = {}
            for ref, role in roles.items():
                if not _is_proximity_sensitive_role(role):
                    continue
                use_connectivity = (
                    _is_local_support_role(role)
                    or _is_decoupling_role(role)
                )
                anchor_ref = _functional_anchor_ref(
                    ref,
                    role,
                    roles,
                    refined_targets,
                    connected_refs=(
                        connected_refs if use_connectivity else None
                    ),
                    allow_connectors=use_connectivity,
                    eligible_anchor_refs=eligible_anchor_refs,
                )
                if anchor_ref is not None and anchor_ref != ref:
                    dependency[ref] = anchor_ref
            children: dict[str, list[str]] = {}
            for ref, anchor_ref in dependency.items():
                children.setdefault(anchor_ref, []).append(ref)

            def subtree_area(ref: str, visiting: set[str]) -> float:
                if ref in visiting:
                    return shape_areas.get(ref, 0.0)
                return shape_areas.get(ref, 0.0) + sum(
                    subtree_area(child, {*visiting, ref})
                    for child in children.get(ref, [])
                )

            dependency_order: list[str] = []
            emitted: set[str] = set()

            def emit_group(ref: str) -> None:
                if ref in emitted:
                    return
                emitted.add(ref)
                dependency_order.append(ref)
                for child in sorted(
                    children.get(ref, []),
                    key=lambda item: (
                        -subtree_area(item, set()),
                        order.index(item),
                    ),
                ):
                    emit_group(child)

            roots = [ref for ref in order if ref not in dependency]
            for root in sorted(
                roots,
                key=lambda item: (
                    0 if item in priority_refs else 1,
                    -subtree_area(item, set()),
                    order.index(item),
                ),
            ):
                emit_group(root)
            for ref in order:
                emit_group(ref)
            shape_candidates: list[
                tuple[
                    tuple[int, float],
                    list[PcbPlacement],
                    list[str],
                ]
            ] = []
            packing_profiles = (
                (True, 1.25),
                (False, 1.25),
                (True, 0.55),
                (True, 0.0),
            )
            for preserve_order, target_weight in packing_profiles:
                candidate_placements, candidate_unplaced = (
                    _courtyard_shape_pack(
                        dependency_order,
                        fps,
                        w,
                        h,
                        cap.min_clearance,
                        refined_targets,
                        target_weight=target_weight,
                        preserve_order=preserve_order,
                        dependency=dependency,
                        edge_refs=edge_refs,
                        fixed_target_refs=set(mounting_refs),
                        priority_refs=priority_refs,
                        fixed_placements=fixed_mounting_placements,
                        allowed_regions=allowed_regions,
                    )
                )
                shape_candidates.append((
                    (
                        len(candidate_unplaced),
                        _shape_target_error(
                            candidate_placements,
                            refined_targets,
                            diagonal,
                        ),
                    ),
                    candidate_placements,
                    candidate_unplaced,
                ))
                if not candidate_unplaced:
                    break
            _, placements, _unplaced = min(
                shape_candidates,
                key=lambda candidate: candidate[0],
            )
        plan = PcbPlacementPlan(
            board_width=w,
            board_height=h,
            placements=placements,
            rationale=(
                "real-courtyard placement inside the fixed board outline; "
                "functional zones used as placement targets; concave courtyards "
                "use rectangular shape decomposition"
            ),
        )
        return plan, False

    def check(self, state: PipelineState, artifact: BaseModel) -> list[CheckResult]:
        assert isinstance(artifact, PcbPlacementPlan)
        roles = _roles(state)
        placed = {p.ref for p in artifact.placements}
        missing = sorted(set(roles) - placed)
        off_grid = [
            p.ref for p in artifact.placements
            if abs(p.x / _LAYOUT_GRID_MM - round(p.x / _LAYOUT_GRID_MM)) > 1e-6
            or abs(p.y / _LAYOUT_GRID_MM - round(p.y / _LAYOUT_GRID_MM)) > 1e-6
        ]
        bad_rot = [p.ref for p in artifact.placements if p.rotation not in _LEGAL_ROTATIONS]
        expected_width, expected_height = _board_dims(state)
        resized = (
            abs(artifact.board_width - expected_width) > 1e-6
            or abs(artifact.board_height - expected_height) > 1e-6
        )
        placements = {
            placement.ref: placement
            for placement in artifact.placements
        }
        targets = {
            ref: (placement.x, placement.y)
            for ref, placement in placements.items()
        }
        connected_refs = _connected_refs_by_ref(state)
        eligible_anchor_refs = _functional_anchor_refs(state)
        _, ambiguous_zones = _resolved_zone_targets(state)
        constraint_violations = placement_constraint_violations(
            _placement_constraints(state),
            placements,
        )
        far_local_support: list[str] = []
        for ref, role in roles.items():
            if not _is_local_support_role(role) or ref not in placements:
                continue
            anchor_ref = _functional_anchor_ref(
                ref,
                role,
                roles,
                targets,
                connected_refs=connected_refs,
                allow_connectors=True,
                eligible_anchor_refs=eligible_anchor_refs,
            )
            if anchor_ref is None or anchor_ref not in placements:
                continue
            distance = _dist(placements[ref], placements[anchor_ref])
            if distance > _local_support_near_mm(role):
                far_local_support.append(
                    f"{ref}->{anchor_ref} ({distance:.1f} mm)"
                )
        checks = [
            CheckResult(name="all_parts_placed", ok=not missing,
                        message=f"unplaced parts: {missing}"),
            CheckResult(
                name="board_outline_preserved",
                ok=not resized,
                message=(
                    f"placement changed board from {expected_width}x{expected_height} "
                    f"to {artifact.board_width}x{artifact.board_height} mm"
                ),
            ),
            CheckResult(
                name="grid_aligned",
                ok=not off_grid,
                severity=Severity.WARNING,
                message=f"off-grid placements: {off_grid}",
            ),
            CheckResult(name="legal_rotation", ok=not bad_rot,
                        message=f"illegal rotations: {bad_rot}"),
            CheckResult(
                name="local_support_near_anchor",
                ok=not far_local_support,
                message=(
                    "electrically connected local support parts are too far "
                    f"from their functional anchors: {far_local_support}"
                ),
            ),
            CheckResult(
                name="placement_constraints_satisfied",
                ok=not constraint_violations,
                message=(
                    "hard placement constraint violations: "
                    f"{constraint_violations}"
                ),
            ),
            CheckResult(
                name="unambiguous_zone_binding",
                ok=not ambiguous_zones,
                message=f"ambiguous placement zone bindings: {ambiguous_zones}",
            ),
        ]
        # The critical plan supplies targets, not immutable coordinates. Verify
        # the final packed placement still satisfies those functional gates.
        checks.extend(LayoutCriticalStep().check(state, artifact))
        return checks

    def repair(
        self,
        state: PipelineState,
        ctx: PipelineContext,
        knowledge: str,
        artifact: BaseModel,
        checks: list[CheckResult],
    ) -> tuple[BaseModel, bool]:
        assert isinstance(artifact, PcbPlacementPlan)
        if any(
            check.name in {
                "all_parts_placed",
                "local_support_near_anchor",
                "placement_constraints_satisfied",
                "unambiguous_zone_binding",
            }
            and not check.ok
            for check in checks
        ):
            # A checkpoint may contain an older partial packing. Moving only
            # existing coordinates cannot restore omitted components or a
            # fragmented interface cluster, so rerun the bounded deterministic
            # dependency packer before proximity repair.
            return self.propose(state, ctx, knowledge)
        return _repair_proximity_placements(state, artifact), False

    def summarize(self, artifact: BaseModel) -> str:
        assert isinstance(artifact, PcbPlacementPlan)
        return f"{len(artifact.placements)} parts placed and aligned"


def _boxes_overlap(
    a: tuple[float, float, float, float], b: tuple[float, float, float, float], margin: float
) -> bool:
    return (
        a[0] - margin < b[2] and b[0] - margin < a[2]
        and a[1] - margin < b[3] and b[1] - margin < a[3]
    )


def _placement_geometry_violations(
    state: PipelineState,
    plan: PcbPlacementPlan,
) -> tuple[list[str], list[str]]:
    """Return real-courtyard overlaps and board-edge violations for a plan."""

    footprints_by_ref = _footprints_of(state)
    cap = config.process_capability()
    shapes = {
        placement.ref: _absolute_placement_shape(
            placement,
            footprints_by_ref.get(placement.ref, ""),
        )
        for placement in plan.placements
    }
    overlaps: list[str] = []
    refs = list(shapes)
    for index, left_ref in enumerate(refs):
        for right_ref in refs[index + 1:]:
            if any(
                _boxes_overlap(left, right, cap.min_clearance)
                for left in shapes[left_ref]
                for right in shapes[right_ref]
            ):
                overlaps.append(f"{left_ref}&{right_ref}")
    edge = cap.min_board_edge_clearance
    out_of_bounds = [
        ref
        for ref, rectangles in shapes.items()
        if any(
            rectangle[0] < edge
            or rectangle[1] < edge
            or rectangle[2] > plan.board_width - edge
            or rectangle[3] > plan.board_height - edge
            for rectangle in rectangles
        )
    ]
    return overlaps, out_of_bounds


class LayoutWriteStep(PipelineStepBase):
    """Courtyard overlap / out-of-bounds / spacing bottom-line, then write the
    .kicad_pcb with real footprint geometry embedded."""

    step = PipelineStep.LAYOUT_WRITE

    def propose(
        self, state: PipelineState, ctx: PipelineContext, knowledge: str
    ) -> tuple[BaseModel, bool]:
        from ratsnestpro.eda.vendor.footprint import load_footprint_node
        from ratsnestpro.eda.vendor.pcb import PcbBoard

        plan = state.artifact(PipelineStep.LAYOUT_GENERAL)
        fps = _footprints_of(state)
        w, h = _board_dims(state)
        if isinstance(plan, PcbPlacementPlan):
            w, h = plan.board_width, plan.board_height

        overlaps: list[str] = []
        oob: list[str] = []
        if isinstance(plan, PcbPlacementPlan):
            overlaps, oob = _placement_geometry_violations(state, plan)

        out_dir = Path(ctx.out_dir) if ctx.out_dir else Path(tempfile.mkdtemp(prefix="rnp_pcb_"))
        out_dir.mkdir(parents=True, exist_ok=True)
        pcb_path = out_dir / f"{state.project_name}.kicad_pcb"
        board = PcbBoard.blank()
        board.set_board_outline(0.0, 0.0, w, h)
        count = 0
        if isinstance(plan, PcbPlacementPlan):
            sel = state.artifact(PipelineStep.SELECTION)
            values = {p.ref: p.value for p in sel.parts} if isinstance(sel, SelectionPlan) else {}
            for p in plan.placements:
                fp = fps.get(p.ref, "")
                embed = None
                fp_path = footprints.footprint_path(fp) if fp else None
                if fp_path is not None:
                    try:
                        embed = load_footprint_node(fp_path)
                    except Exception:
                        embed = None
                try:
                    board.add_footprint(
                        lib_id=fp or "unknown:unknown", reference=p.ref,
                        value=values.get(p.ref, ""), x=p.x, y=p.y, rotation=p.rotation,
                        embed_node=embed,
                    )
                    count += 1
                except Exception:
                    continue
        board.save(pcb_path)
        placement_constraints_path = pcb_path.with_suffix(
            ".placement_constraints.json"
        )
        write_placement_constraint_manifest(
            placement_constraints_path,
            _placement_constraints(state),
        )
        # Routing mutates the PCB in place. Keep the deterministic layout output
        # so retries and layer escalation always restart from identical geometry
        # instead of accumulating tracks from a failed attempt.
        baseline_path = pcb_path.with_name(f"{pcb_path.stem}.unrouted.kicad_pcb")
        shutil.copy2(pcb_path, baseline_path)
        # Re-read to confirm the Edge.Cuts outline is really on disk (a board
        # with no outline is unmanufacturable — fab houses reject it).
        from ratsnestpro.eda.vendor.sexpr import find_all, find_first

        has_outline = False
        try:
            reloaded = PcbBoard.load(pcb_path)
            for node in find_all(reloaded.root, "gr_line"):
                layer = find_first(node, "layer")
                if layer is not None and any(
                    str(tok) == "Edge.Cuts" for tok in layer[1:]
                ):
                    has_outline = True
                    break
        except Exception:
            has_outline = False
        return (
            PcbWriteResult(
                pcb_path=str(pcb_path), component_count=count,
                overlaps=overlaps, out_of_bounds=oob,
                has_board_outline=has_outline,
                placement_constraints_path=str(placement_constraints_path),
            ),
            False,
        )

    def check(self, state: PipelineState, artifact: BaseModel) -> list[CheckResult]:
        assert isinstance(artifact, PcbWriteResult)
        checks: list[CheckResult] = []
        if config.footprint_dir() is None:
            checks.append(CheckResult(
                name="footprint_library_available", ok=False, severity=Severity.WARNING,
                message="KICAD_FOOTPRINT_DIR not configured; courtyard checks skipped",
            ))
        checks.append(CheckResult(
            name="no_courtyard_overlap", ok=not artifact.overlaps,
            message=f"overlapping footprints: {artifact.overlaps}",
        ))
        checks.append(CheckResult(
            name="within_board", ok=not artifact.out_of_bounds,
            message=f"footprints past the board edge: {artifact.out_of_bounds}",
        ))
        checks.append(CheckResult(
            name="board_written", ok=bool(artifact.pcb_path),
            message="no .kicad_pcb written",
            blocks_execution=True,
        ))
        checks.append(CheckResult(
            name="board_outline_present", ok=artifact.has_board_outline,
            message="no Edge.Cuts board outline written (board is unmanufacturable)",
        ))
        checks.append(CheckResult(
            name="placement_constraint_manifest_written",
            ok=bool(artifact.placement_constraints_path)
            and Path(artifact.placement_constraints_path).is_file(),
            message="placement constraint manifest was not persisted beside the PCB",
            blocks_execution=True,
        ))
        return checks

    def summarize(self, artifact: BaseModel) -> str:
        assert isinstance(artifact, PcbWriteResult)
        return (
            f"wrote {artifact.pcb_path} ({artifact.component_count} footprints, "
            f"{len(artifact.overlaps)} overlaps, {len(artifact.out_of_bounds)} out-of-bounds)"
        )


def _layer_count_mentions(text: str) -> list[tuple[int, int, int]]:
    """Return actual PCB layer phrases, never unrelated cardinal words.

    Requiring the ``layer`` noun is important: datasheet evidence frequently
    contains ordinary prose such as "two powerful modules".  Treating the bare
    word ``two`` as a stackup request silently changes the user's design.
    """

    mentions: list[tuple[int, int, int]] = []
    for match in re.finditer(r"\b(1[0-6]|[2-9])[\s-]*layers?\b", text):
        mentions.append((match.start(), match.end(), int(match.group(1))))

    english_counts = {"two": 2, "four": 4, "six": 6, "eight": 8}
    for word, count in english_counts.items():
        for match in re.finditer(
            rf"(?<![a-z]){word}[\s-]*layers?(?![a-z])",
            text,
        ):
            mentions.append((match.start(), match.end(), count))

    chinese_counts = {
        "二层": 2,
        "两层": 2,
        "四层": 4,
        "六层": 6,
        "八层": 8,
        "十层": 10,
        "十二层": 12,
        "十六层": 16,
    }
    for word, count in chinese_counts.items():
        for match in re.finditer(rf"{word}(?:板)?", text):
            mentions.append((match.start(), match.end(), count))
    return sorted(mentions)


def _layer_mention_is_preference(text: str, start: int, end: int) -> bool:
    """Distinguish a soft stackup preference from a hard layer constraint."""

    context = text[max(0, start - 40):min(len(text), end + 24)]
    preference_markers = (
        r"\bprefer(?:red|ably)?\b",
        r"\bideally\b",
        r"\bpriority\b",
        r"优先",
        r"首选",
        r"优选",
        r"建议(?:采用|使用)?",
    )
    return any(re.search(marker, context) for marker in preference_markers)


def _explicit_requested_layer_count(requirement: str) -> int | None:
    text = requirement.lower()
    candidates = [
        (start, count)
        for start, end, count in _layer_count_mentions(text)
        if not _model_mention_is_negated(text, start)
        and not _layer_mention_is_preference(text, start, end)
    ]
    return max(candidates)[1] if candidates else None


def _preferred_layer_count(requirement: str) -> int | None:
    text = requirement.lower()
    candidates = [
        (start, count)
        for start, end, count in _layer_count_mentions(text)
        if not _model_mention_is_negated(text, start)
        and _layer_mention_is_preference(text, start, end)
    ]
    return max(candidates)[1] if candidates else None


def _requested_layer_count(requirement: str) -> int:
    return (
        _explicit_requested_layer_count(requirement)
        or _preferred_layer_count(requirement)
        or 2
    )


def _has_explicit_routing_geometry(requirement: str) -> bool:
    """Return whether the user fixed trace/clearance/via dimensions."""
    text = requirement.lower()
    geometry = (
        r"(?:track|trace|line[\s-]*width|clearance|spacing|via|"
        r"线宽|间距|过孔)"
    )
    dimension = r"\d+(?:\.\d+)?\s*mm\b"
    return bool(
        re.search(rf"{geometry}[^\n.;]{{0,48}}{dimension}", text)
        or re.search(rf"{dimension}[^\n.;]{{0,32}}{geometry}", text)
    )


def _requested_min_clearance_mm(requirement: str) -> float | None:
    """Extract one explicit global clearance floor from the user request."""

    text = _original_requirement(requirement)
    label = r"(?:clearance|spacing|间距)"
    minimum = (
        r"(?:minimum|min\.?|at\s+least|not\s+less\s+than|"
        r">=|≥|最小|不小于|至少)"
    )
    value = r"(?P<clearance>\d+(?:\.\d+)?)\s*mm\b"
    gap = r"[^\n.;]{0,32}?"
    patterns = (
        rf"{minimum}{gap}{label}{gap}{value}",
        rf"{label}{gap}{minimum}{gap}{value}",
        rf"{value}{gap}{minimum}{gap}{label}",
        rf"{value}{gap}{label}{gap}{minimum}",
    )
    matches = [
        float(match.group("clearance"))
        for pattern in patterns
        for match in re.finditer(pattern, text, re.IGNORECASE)
    ]
    return min(matches) if matches else None


class RoutePlanStep(PipelineStepBase):
    """Stackup + net-class routing rules. Bottom-line: every rule value is at
    or above the fab process minimum (widths, clearance, via)."""

    step = PipelineStep.ROUTE_PLAN
    knowledge_role = "routing"

    def knowledge_query(self, state: PipelineState) -> str | None:
        return "layer stackup, net classes, trace width and clearance rules"

    def propose(
        self, state: PipelineState, ctx: PipelineContext, knowledge: str
    ) -> tuple[BaseModel, bool]:
        cap = config.process_capability()
        requested_layers = _requested_layer_count(state.requirement_text)
        requested_clearance = _requested_min_clearance_mm(
            state.requirement_text
        )

        def fallback() -> RoutePlan:
            w = max(cap.min_track_width, 0.2)
            cl = max(
                cap.min_clearance,
                (
                    requested_clearance
                    if requested_clearance is not None
                    else 0.2
                ),
            )
            via = max(cap.min_via_diameter, 0.6)
            drill = max(cap.min_via_drill, 0.3)
            classes = [
                NetClass(name="power", width=max(w, 0.5), clearance=cl,
                         via_diameter=via, via_drill=drill, layer="F.Cu"),
                NetClass(name="signal", width=max(w, 0.25), clearance=cl,
                         via_diameter=via, via_drill=drill, layer="F.Cu"),
                NetClass(name="default", width=max(w, 0.25), clearance=cl,
                         via_diameter=via, via_drill=drill, layer="F.Cu"),
            ]
            return RoutePlan(
                layers=requested_layers,
                net_classes=classes,
                rationale=(
                    f"{requested_layers}-layer stackup; power wider than signal"
                ),
            )

        system = (
            "You define a PCB stackup and net classes. Return JSON: layers (2-16), "
            "net_classes[] ({name, width, clearance, via_diameter, via_drill, layer}) "
            "in mm, rationale. Values must meet the fab minimums."
        )
        user = (
            f"Requirement:\n{state.requirement_text}\n\n"
            f"Fab minimums: {cap.model_dump()}\n\nKnowledge:\n{knowledge}"
        )
        artifact, used_llm = propose_structured(
            ctx,
            model=RoutePlan,
            system=system,
            user=user,
            fallback=fallback,
        )
        # A syntactically valid LLM response may still omit the entire
        # net-class table.  That is a recoverable planning omission, not a
        # reason to exhaust identical LLM retries.  Preserve valid LLM values,
        # but fill the structurally mandatory table from process capabilities.
        # An explicit user layer count is likewise authoritative.
        updates: dict[str, Any] = {}
        if not artifact.net_classes:
            updates["net_classes"] = fallback().net_classes
        if requested_clearance is not None:
            normalized_clearance = max(
                cap.min_clearance,
                requested_clearance,
            )
            current_classes = updates.get(
                "net_classes",
                artifact.net_classes,
            )
            if any(
                abs(net_class.clearance - normalized_clearance) > 1e-9
                for net_class in current_classes
            ):
                updates["net_classes"] = [
                    net_class.model_copy(
                        update={"clearance": normalized_clearance}
                    )
                    for net_class in current_classes
                ]
        explicit_layers = _explicit_requested_layer_count(state.requirement_text)
        if explicit_layers is not None and artifact.layers != explicit_layers:
            updates["layers"] = explicit_layers
        preferred_layers = _preferred_layer_count(state.requirement_text)
        if (
            explicit_layers is None
            and preferred_layers is not None
            and artifact.layers < preferred_layers
        ):
            updates["layers"] = preferred_layers
        if updates:
            updates["rationale"] = (
                f"{artifact.rationale}; deterministic normalization of "
                "mandatory routing constraints"
            )
            artifact = artifact.model_copy(update=updates)
        return artifact, used_llm

    def check(self, state: PipelineState, artifact: BaseModel) -> list[CheckResult]:
        assert isinstance(artifact, RoutePlan)
        cap = config.process_capability()
        thin = [c.name for c in artifact.net_classes if c.width < cap.min_track_width]
        tight = [c.name for c in artifact.net_classes if c.clearance < cap.min_clearance]
        small_via = [c.name for c in artifact.net_classes if c.via_diameter < cap.min_via_diameter]
        small_drill = [
            c.name for c in artifact.net_classes
            if c.via_drill < cap.min_via_drill
        ]
        small_annular = [
            c.name for c in artifact.net_classes
            if (c.via_diameter - c.via_drill) / 2 < cap.min_annular_ring
        ]
        explicit_layers = _explicit_requested_layer_count(
            state.requirement_text
        )
        return [
            CheckResult(
                name="requested_layer_count",
                ok=(
                    explicit_layers is None
                    or artifact.layers == explicit_layers
                ),
                message=(
                    f"explicit requirement is {explicit_layers} layers, "
                    f"but route plan selected {artifact.layers}"
                ),
            ),
            CheckResult(name="has_net_classes", ok=bool(artifact.net_classes),
                        message="no net classes defined"),
            CheckResult(name="track_width_ok", ok=not thin,
                        message=f"net classes below min track width: {thin}"),
            CheckResult(name="clearance_ok", ok=not tight,
                        message=f"net classes below min clearance: {tight}"),
            CheckResult(name="via_ok", ok=not small_via,
                        message=f"net classes below min via: {small_via}"),
            CheckResult(name="via_drill_ok", ok=not small_drill,
                        message=f"net classes below min drill: {small_drill}"),
            CheckResult(name="annular_ring_ok", ok=not small_annular,
                        message=f"net classes below min annular ring: {small_annular}"),
        ]

    def summarize(self, artifact: BaseModel) -> str:
        assert isinstance(artifact, RoutePlan)
        return f"{artifact.layers}-layer, {len(artifact.net_classes)} net classes"


class RoutePlanesStep(PipelineStepBase):
    """Power/ground planes + critical-net priority. Bottom-line: a ground plane
    exists and the critical nets are known."""

    step = PipelineStep.ROUTE_PLANES
    knowledge_role = "routing"

    def knowledge_query(self, state: PipelineState) -> str | None:
        return "power and ground planes, return paths, critical net routing first"

    def propose(
        self, state: PipelineState, ctx: PipelineContext, knowledge: str
    ) -> tuple[BaseModel, bool]:
        intent = state.artifact(PipelineStep.SCH_CONNECTIONS)
        ground = intent.ground_net if isinstance(intent, NetlistIntent) else "GND"

        def fallback() -> PlanePlan:
            critical: list[str] = []
            if isinstance(intent, NetlistIntent):
                critical = [n.name for n in intent.nets if n.kind in ("clock", "power")]
            return PlanePlan(
                ground_net=ground, planes=[f"B.Cu:{ground}"], critical_nets=critical,
                rationale="ground pour on B.Cu; clock/power nets routed first",
            )

        system = (
            "You plan copper planes and critical-net priority. Return JSON: "
            "ground_net, planes[] ('Layer:NET'), critical_nets[], rationale."
        )
        user = f"Ground net: {ground}\n\nKnowledge:\n{knowledge}"
        return propose_structured(ctx, model=PlanePlan, system=system, user=user, fallback=fallback)

    def check(self, state: PipelineState, artifact: BaseModel) -> list[CheckResult]:
        assert isinstance(artifact, PlanePlan)
        has_gnd_plane = any(artifact.ground_net in p for p in artifact.planes)
        return [
            CheckResult(name="ground_plane_present", ok=has_gnd_plane,
                        message=f"no ground plane for {artifact.ground_net!r}"),
        ]

    def summarize(self, artifact: BaseModel) -> str:
        assert isinstance(artifact, PlanePlan)
        return f"{len(artifact.planes)} planes, {len(artifact.critical_nets)} critical nets"


_DRC_NET_RE = re.compile(r"\[([^\]]+)\]")
_DRC_COPPER_LAYER_RE = re.compile(r"\bon\s+((?:F|B|In\d+)\.Cu)\b")
_DRC_PAD_REF_RE = re.compile(
    r"\b(?:Pad|PTH pad)\s+(\S+).*?\bof\s+([A-Za-z][A-Za-z0-9_.-]*)\b"
)


@dataclass(frozen=True)
class _DrcEndpoint:
    x: float
    y: float
    net: str
    layer: str
    ref: str | None = None
    pad_number: str | None = None


@dataclass(frozen=True)
class _DrcGap:
    left: _DrcEndpoint
    right: _DrcEndpoint


@dataclass(frozen=True)
class _DrcSnapshot:
    findings: tuple[str, ...]
    non_connectivity_errors: tuple[str, ...]
    gaps: tuple[_DrcGap, ...]
    parse_error: bool = False

    @property
    def unconnected(self) -> int:
        return len(self.gaps)


def _routing_seed(
    state: PipelineState,
    *,
    layer_count: int,
    attempt: int,
) -> int:
    """Return a reproducible, project-independent Freerouting seed."""

    material = (
        f"{state.project_name}\0{state.requirement_text}\0"
        f"{layer_count}\0{attempt}"
    )
    digest = hashlib.sha256(material.encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") & 0x7FFFFFFF


def _endpoint_from_drc_item(item: Any) -> _DrcEndpoint | None:
    if not isinstance(item, dict):
        return None
    description = str(item.get("description", ""))
    pos = item.get("pos")
    net_match = _DRC_NET_RE.search(description)
    layer_match = _DRC_COPPER_LAYER_RE.search(description)
    if (
        not isinstance(pos, dict)
        or net_match is None
        or layer_match is None
    ):
        return None
    try:
        pad_match = _DRC_PAD_REF_RE.search(description)
        return _DrcEndpoint(
            x=float(pos["x"]),
            y=float(pos["y"]),
            net=net_match.group(1),
            layer=layer_match.group(1),
            ref=pad_match.group(2) if pad_match is not None else None,
            pad_number=pad_match.group(1) if pad_match is not None else None,
        )
    except (KeyError, TypeError, ValueError):
        return None


def _read_drc_snapshot(report_path: Path) -> _DrcSnapshot:
    try:
        data = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        finding = "kicad_cli: final DRC did not produce a parseable report"
        return _DrcSnapshot(
            findings=(finding,),
            non_connectivity_errors=(finding,),
            gaps=(),
            parse_error=True,
        )

    findings: list[str] = []
    non_connectivity: list[str] = []
    gaps: list[_DrcGap] = []
    for key in ("violations", "schematic_parity", "unconnected_items"):
        for finding in data.get(key, []):
            if (
                not isinstance(finding, dict)
                or str(finding.get("severity", "error")) != "error"
            ):
                continue
            message = (
                "kicad_cli:"
                f"{finding.get('type', 'unknown')}:"
                f"{finding.get('description', 'DRC error')}"
            )
            findings.append(message)
            if key != "unconnected_items":
                non_connectivity.append(message)
                continue
            items = finding.get("items", [])
            if not isinstance(items, list) or len(items) != 2:
                continue
            left = _endpoint_from_drc_item(items[0])
            right = _endpoint_from_drc_item(items[1])
            if (
                left is not None
                and right is not None
                and left.net == right.net
                and left.layer == right.layer
            ):
                gaps.append(_DrcGap(left=left, right=right))
    return _DrcSnapshot(
        findings=tuple(findings),
        non_connectivity_errors=tuple(non_connectivity),
        gaps=tuple(gaps),
    )


def _run_kicad_drc_snapshot(
    cli: str,
    pcb_path: Path,
    report_path: Path,
) -> _DrcSnapshot:
    """Run the authoritative PCB DRC and retain structured gap evidence."""

    import subprocess

    report_path.unlink(missing_ok=True)
    try:
        subprocess.run(
            [
                cli,
                "pcb",
                "drc",
                "--format",
                "json",
                "--severity-all",
                "--output",
                str(report_path),
                "--exit-code-violations",
                str(pcb_path),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return _DrcSnapshot(
            findings=("kicad_cli: final DRC execution failed",),
            non_connectivity_errors=("kicad_cli: final DRC execution failed",),
            gaps=(),
            parse_error=True,
        )
    return _read_drc_snapshot(report_path)


def _candidate_copper_paths(
    gap: _DrcGap,
) -> list[list[tuple[tuple[float, float], tuple[float, float]]]]:
    """Produce bounded, board-independent Manhattan candidates."""

    start = (gap.left.x, gap.left.y)
    end = (gap.right.x, gap.right.y)
    x_mid = (start[0] + end[0]) / 2
    y_mid = (start[1] + end[1]) / 2
    points = [
        [start, end],
        [start, (end[0], start[1]), end],
        [start, (start[0], end[1]), end],
        [start, (x_mid, start[1]), (x_mid, end[1]), end],
        [start, (start[0], y_mid), (end[0], y_mid), end],
    ]
    for offset in (1.0, -1.0, 2.0, -2.0):
        points.append(
            [
                start,
                (x_mid + offset, start[1]),
                (x_mid + offset, end[1]),
                end,
            ]
        )

    paths: list[list[tuple[tuple[float, float], tuple[float, float]]]] = []
    seen: set[tuple[tuple[float, float], ...]] = set()
    for candidate in points:
        compact = [candidate[0]]
        for point in candidate[1:]:
            if point != compact[-1]:
                compact.append(point)
        key = tuple(compact)
        if len(compact) < 2 or key in seen:
            continue
        seen.add(key)
        paths.append(
            [
                (compact[index], compact[index + 1])
                for index in range(len(compact) - 1)
            ]
        )
    return paths


def _repair_drc_connectivity_gaps(
    state: PipelineState,
    ctx: PipelineContext,
    artifact: RouteResult,
) -> RouteResult:
    """Greedily close DRC gaps, accepting only monotonic, DRC-safe patches."""

    from ratsnestpro.eda.vendor.pcb import PcbBoard

    write = state.artifact(PipelineStep.LAYOUT_WRITE)
    if not isinstance(write, PcbWriteResult):
        return artifact
    pcb_path = Path(write.pcb_path)
    if not pcb_path.is_file():
        return artifact
    cli = kicad_cli_available()
    if not cli:
        return artifact

    backup_path = pcb_path.with_suffix(".ahe-route-backup.kicad_pcb")
    shutil.copy2(pcb_path, backup_path)
    report_path = pcb_path.with_suffix(".ahe-route.drc.json")
    baseline = _run_kicad_drc_snapshot(cli, pcb_path, report_path)
    if baseline.parse_error or not baseline.gaps:
        backup_path.unlink(missing_ok=True)
        return artifact

    added_tracks = 0
    closed_gaps = 0
    try:
        route_plan = state.artifact(PipelineStep.ROUTE_PLAN)
        cap = config.process_capability()
        width = cap.min_track_width
        if isinstance(route_plan, RoutePlan) and route_plan.net_classes:
            width = max(
                cap.min_track_width,
                min(net_class.width for net_class in route_plan.net_classes),
            )
        max_candidates = 18
        candidate_count = 0
        while baseline.gaps and candidate_count < max_candidates:
            improved = False
            ordered_gaps = sorted(
                baseline.gaps,
                key=lambda gap: math.hypot(
                    gap.left.x - gap.right.x,
                    gap.left.y - gap.right.y,
                ),
            )
            for gap in ordered_gaps:
                for path in _candidate_copper_paths(gap):
                    candidate_count += 1
                    if candidate_count > max_candidates:
                        break
                    candidate_backup = pcb_path.with_suffix(
                        ".ahe-route-candidate.kicad_pcb"
                    )
                    shutil.copy2(pcb_path, candidate_backup)
                    board = PcbBoard.load(pcb_path)
                    for start, end in path:
                        board.add_track(
                            start[0],
                            start[1],
                            end[0],
                            end[1],
                            width=width,
                            layer=gap.left.layer,
                            net=gap.left.net,
                        )
                    board.save(pcb_path)
                    after = _run_kicad_drc_snapshot(
                        cli,
                        pcb_path,
                        report_path,
                    )
                    safe = (
                        not after.parse_error
                        and after.unconnected < baseline.unconnected
                        and set(after.non_connectivity_errors).issubset(
                            baseline.non_connectivity_errors
                        )
                    )
                    if safe:
                        closed_gaps += baseline.unconnected - after.unconnected
                        added_tracks += len(path)
                        baseline = after
                        candidate_backup.unlink(missing_ok=True)
                        improved = True
                        break
                    shutil.copy2(candidate_backup, pcb_path)
                    candidate_backup.unlink(missing_ok=True)
                if improved or candidate_count >= max_candidates:
                    break
            if not improved:
                break
        if not closed_gaps:
            return artifact

        backup_path.unlink(missing_ok=True)
        remaining = baseline.unconnected
        return artifact.model_copy(
            update={
                "routed_nets": (
                    artifact.total_nets if remaining == 0 else artifact.routed_nets
                ),
                "routed_connections": -1,
                "total_connections": -1,
                "metric_basis": "kicad_drc_unconnected_after_repair",
                "routed_tracks": artifact.routed_tracks + added_tracks,
                "unconnected": remaining,
                "note": (
                    f"{artifact.note}; AHE DRC-monotonic gap closer removed "
                    f"{closed_gaps} connection gap(s) with {added_tracks} "
                    f"track segment(s); {remaining} remain"
                ),
            }
        )
    except Exception:  # noqa: BLE001 - the rejected repair is rolled back below
        return artifact
    finally:
        if backup_path.is_file():
            shutil.copy2(backup_path, pcb_path)
            backup_path.unlink(missing_ok=True)


def _resolved_plane_assignments(
    state: PipelineState,
    layer_count: int,
) -> list[dict[str, str]]:
    """Resolve a plane plan to concrete KiCad layers and physical net names."""

    plane_plan = state.artifact(PipelineStep.ROUTE_PLANES)
    pinmap = state.artifact(PipelineStep.SCH_PINMAP)
    if not isinstance(plane_plan, PlanePlan) or not isinstance(pinmap, PinMapPlan):
        return []

    available = {
        net.name: net
        for net in pinmap.nets
        if len(net.pins) >= 2
    }
    ground_names = {
        net.name
        for net in pinmap.nets
        if (
            net.kind.lower() in {"ground", "gnd"}
            or net.name.upper() in {"GND", "AGND", "DGND"}
        )
    }
    power_candidates = sorted(
        (
            net
            for net in pinmap.nets
            if (
                net.name not in ground_names
                and (
                    net.kind.lower() in {"power", "supply"}
                    or any(
                        token in net.name.upper()
                        for token in ("VCC", "VDD", "VBUS", "3V3", "5V")
                    )
                )
                and len(net.pins) >= 2
            )
        ),
        key=lambda net: (-len(net.pins), net.name),
    )

    def layer_name(raw: str) -> str | None:
        text = raw.strip()
        if re.fullmatch(r"(?:F|B|In\d+)\.Cu", text, re.IGNORECASE):
            prefix, _, _ = text.partition(".")
            if prefix.lower() == "f":
                return "F.Cu"
            if prefix.lower() == "b":
                return "B.Cu"
            return f"In{int(prefix[2:])}.Cu"
        match = re.fullmatch(r"L(?:ayer)?\s*(\d+)", text, re.IGNORECASE)
        if match is None:
            return None
        index = int(match.group(1))
        if index < 1 or index > layer_count:
            return None
        if index == 1:
            return "F.Cu"
        if index == layer_count:
            return "B.Cu"
        return f"In{index - 1}.Cu"

    assignments: list[dict[str, str]] = []
    generic_power_index = 0
    for declaration in plane_plan.planes:
        raw_layer, separator, raw_net = str(declaration).partition(":")
        if not separator:
            semantic = raw_layer.strip()
            semantic_upper = semantic.upper()
            if semantic_upper in {"GND", "GROUND"}:
                layer = "In1.Cu" if layer_count >= 4 else "B.Cu"
                net_name = next(
                    (
                        candidate
                        for candidate in available
                        if candidate in ground_names
                    ),
                    None,
                )
            elif semantic_upper in {"PWR", "POWER", "SUPPLY"}:
                if layer_count < 4:
                    continue
                layer = "In2.Cu"
                net_name = (
                    power_candidates[
                        min(
                            generic_power_index,
                            len(power_candidates) - 1,
                        )
                    ].name
                    if power_candidates
                    else None
                )
                generic_power_index += 1
            else:
                net_name = next(
                    (
                        candidate
                        for candidate in available
                        if candidate.casefold() == semantic.casefold()
                    ),
                    None,
                )
                if net_name in ground_names:
                    layer = "In1.Cu" if layer_count >= 4 else "B.Cu"
                elif layer_count >= 4:
                    layer = "In2.Cu"
                else:
                    continue
            if layer is not None and net_name is not None:
                assignment = {"layer": layer, "net": net_name}
                if assignment not in assignments:
                    assignments.append(assignment)
            continue
        layer = layer_name(raw_layer)
        wanted_net = raw_net.strip()
        net_name = next(
            (
                candidate
                for candidate in available
                if candidate.casefold() == wanted_net.casefold()
            ),
            None,
        )
        if (
            net_name is None
            and wanted_net.upper() in {"PWR", "POWER", "SUPPLY"}
            and power_candidates
        ):
            net_name = power_candidates[
                min(generic_power_index, len(power_candidates) - 1)
            ].name
            generic_power_index += 1
        if layer is None or net_name is None:
            continue
        assignment = {"layer": layer, "net": net_name}
        if assignment not in assignments:
            assignments.append(assignment)
    return assignments


def _repair_power_plane_gaps(
    state: PipelineState,
    ctx: PipelineContext,
    artifact: RouteResult,
) -> RouteResult:
    """Materialize planned planes and DRC-monotonically stitch rail islands."""

    from ratsnestpro.eda import routing

    write = state.artifact(PipelineStep.LAYOUT_WRITE)
    if not isinstance(write, PcbWriteResult) or artifact.unconnected <= 0:
        return artifact
    pcb_path = Path(write.pcb_path)
    cli = kicad_cli_available()
    kicad_python = routing.kicad_python()
    assignments = _resolved_plane_assignments(state, artifact.layers)
    if (
        not pcb_path.is_file()
        or not cli
        or not kicad_python
        or not assignments
    ):
        return artifact

    route_plan = state.artifact(PipelineStep.ROUTE_PLAN)
    cap = config.process_capability()
    net_classes = (
        route_plan.net_classes
        if isinstance(route_plan, RoutePlan)
        else []
    )
    clearance = max(
        cap.min_clearance,
        min(
            (net_class.clearance for net_class in net_classes),
            default=cap.min_clearance,
        ),
    )
    track_width = max(
        cap.min_track_width,
        min(
            (net_class.width for net_class in net_classes),
            default=cap.min_track_width,
        ),
    )
    via_diameter = max(
        cap.min_via_diameter,
        min(
            (net_class.via_diameter for net_class in net_classes),
            default=cap.min_via_diameter,
        ),
    )
    via_drill = max(
        cap.min_via_drill,
        min(
            (net_class.via_drill for net_class in net_classes),
            default=cap.min_via_drill,
        ),
    )
    worker = (
        Path(__file__).resolve().parent.parent
        / "eda"
        / "_plane_stitch_worker.py"
    )
    report_path = pcb_path.with_suffix(".ahe-plane.drc.json")
    backup_path = pcb_path.with_suffix(".ahe-plane-backup.kicad_pcb")
    shutil.copy2(pcb_path, backup_path)
    try:
        import subprocess

        process = subprocess.run(
            [
                kicad_python,
                str(worker),
                str(pcb_path),
                cli,
                json.dumps(assignments),
                str(clearance),
                str(track_width),
                str(via_diameter),
                str(via_drill),
                str(report_path),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=600,
            check=False,
        )
        payload: dict[str, Any] = {}
        for line in process.stdout.splitlines():
            if line.startswith("RESULT "):
                try:
                    payload = json.loads(line[len("RESULT "):])
                except json.JSONDecodeError:
                    payload = {}
        remaining = int(payload.get("unconnected", -1))
        closed = int(payload.get("closed_gaps", 0))
        if (
            process.returncode == 0
            and payload.get("ok")
            and closed > 0
            and 0 <= remaining < artifact.unconnected
        ):
            backup_path.unlink(missing_ok=True)
            tracks = max(
                artifact.routed_tracks,
                int(payload.get("routed_tracks", artifact.routed_tracks)),
            )
            return artifact.model_copy(
                update={
                    "routed_nets": (
                        artifact.total_nets
                        if remaining == 0
                        else artifact.routed_nets
                    ),
                    "routed_connections": -1,
                    "total_connections": -1,
                    "metric_basis": "kicad_drc_unconnected_after_repair",
                    "routed_tracks": tracks,
                    "unconnected": remaining,
                    "note": (
                        f"{artifact.note}; AHE materialized "
                        f"{payload.get('added_zones', 0)} planned copper "
                        f"plane(s) and closed {closed} rail gap(s) with "
                        f"{payload.get('added_vias', 0)} DRC-verified "
                        f"stitching via(s); {remaining} remain"
                    ),
                }
            )
    except Exception:  # noqa: BLE001 - rejected patch is rolled back below
        pass
    finally:
        if backup_path.is_file():
            shutil.copy2(backup_path, pcb_path)
            backup_path.unlink(missing_ok=True)
    return artifact


def _rotated_point(
    point: tuple[float, float],
    rotation: float,
) -> tuple[float, float]:
    radians = math.radians(rotation)
    cosine = math.cos(radians)
    sine = math.sin(radians)
    return (
        point[0] * cosine - point[1] * sine,
        point[0] * sine + point[1] * cosine,
    )


def _route_gap_placement_plan(
    state: PipelineState,
    snapshot: _DrcSnapshot,
) -> PcbPlacementPlan | None:
    """Move one small failed endpoint toward the routed frontier.

    The transform is derived entirely from KiCad DRC endpoint coordinates,
    real pad offsets, courtyards, and board rules.  It is deliberately unaware
    of reference names, buses, or board families.
    """

    plan = state.artifact(PipelineStep.LAYOUT_GENERAL)
    if not isinstance(plan, PcbPlacementPlan):
        return None
    roles = _roles(state)
    footprint_ids = _footprints_of(state)
    placements = plan.by_ref()
    cap = config.process_capability()
    edge = cap.min_board_edge_clearance

    movable: list[
        tuple[
            float,
            _DrcEndpoint,
            _DrcEndpoint,
            PcbPlacement,
            str,
        ]
    ] = []
    for gap in snapshot.gaps:
        for endpoint, target in (
            (gap.left, gap.right),
            (gap.right, gap.left),
        ):
            if endpoint.ref is None or endpoint.ref not in placements:
                continue
            role = roles.get(endpoint.ref, "")
            if _is_mcu_role(role) or _is_connector_role(role):
                continue
            footprint_id = footprint_ids.get(endpoint.ref, "")
            shape = _placement_shape(footprint_id)
            area = sum(
                max(0.0, rectangle[2] - rectangle[0])
                * max(0.0, rectangle[3] - rectangle[1])
                for rectangle in shape
            )
            # Routing-aware relocation is for passives and small support ICs;
            # moving a dominant functional block would be an upstream replan.
            if area > 36.0:
                continue
            movable.append((
                area,
                endpoint,
                target,
                placements[endpoint.ref],
                footprint_id,
            ))
    if not movable:
        return None

    _, endpoint, target, current, footprint_id = min(
        movable,
        key=lambda item: (
            item[0],
            math.dist(
                (item[1].x, item[1].y),
                (item[2].x, item[2].y),
            ),
        ),
    )
    pad_offset = (0.0, 0.0)
    if endpoint.pad_number is not None:
        pads = footprints.footprint_pads(footprint_id) or []
        pad = next(
            (
                item
                for item in pads
                if str(item.get("number", "")) == endpoint.pad_number
            ),
            None,
        )
        if pad is not None:
            pad_offset = _rotated_point(
                (float(pad["x"]), float(pad["y"])),
                current.rotation,
            )

    occupied: list[tuple[float, float, float, float]] = []
    for placement in plan.placements:
        if placement.ref == endpoint.ref:
            continue
        occupied.extend(
            _absolute_placement_shape(
                placement,
                footprint_ids.get(placement.ref, ""),
            )
        )

    offsets = [
        (dx, dy)
        for radius in range(0, 25)
        for dx in (value / 2 for value in range(-radius, radius + 1))
        for dy in (value / 2 for value in range(-radius, radius + 1))
        if max(abs(dx), abs(dy)) == radius / 2
    ]
    candidates: list[tuple[tuple[float, float], list[PcbPlacement]]] = []
    for dx, dy in offsets:
        x = _snap(target.x - pad_offset[0] + dx)
        y = _snap(target.y - pad_offset[1] + dy)
        candidate = current.model_copy(update={"x": x, "y": y})
        rectangles = _absolute_placement_shape(candidate, footprint_id)
        if any(
            rectangle[0] < edge
            or rectangle[1] < edge
            or rectangle[2] > plan.board_width - edge
            or rectangle[3] > plan.board_height - edge
            for rectangle in rectangles
        ):
            continue
        if any(
            _boxes_overlap(rectangle, other, cap.min_clearance)
            for rectangle in rectangles
            for other in occupied
        ):
            continue
        candidate_pad = (x + pad_offset[0], y + pad_offset[1])
        candidates.append((
            (
                math.dist(candidate_pad, (target.x, target.y)),
                0.01 * math.dist((x, y), (current.x, current.y)),
            ),
            [
                candidate if item.ref == candidate.ref else item
                for item in plan.placements
            ],
        ))

    # A dense legal packing may have no empty cell near the routed frontier.
    # In that case, swap with another small, non-critical placement and verify
    # both real courtyards.  This preserves density instead of expanding the
    # board or deleting a component.
    if not candidates:
        def freely_relocatable(role: str) -> bool:
            text = role.lower()
            positive = (
                "resistor",
                "series",
                "termination",
                "bias",
                "pull",
                "led",
                "indicator",
                "jumper",
                "link",
                "testpoint",
            )
            protected = (
                "feedback",
                "decoupling",
                "bootstrap",
                "timing",
                "power",
                "crystal",
                "filter",
            )
            return any(token in text for token in positive) and not any(
                token in text for token in protected
            )

        for other in plan.placements:
            if other.ref == current.ref or not freely_relocatable(
                roles.get(other.ref, "")
            ):
                continue
            other_fp = footprint_ids.get(other.ref, "")
            other_area = sum(
                max(0.0, rectangle[2] - rectangle[0])
                * max(0.0, rectangle[3] - rectangle[1])
                for rectangle in _placement_shape(other_fp)
            )
            if other_area > 36.0:
                continue
            moved = current.model_copy(update={"x": other.x, "y": other.y})
            displaced = other.model_copy(update={"x": current.x, "y": current.y})
            pair_shapes = [
                *_absolute_placement_shape(moved, footprint_id),
                *_absolute_placement_shape(displaced, other_fp),
            ]
            occupied_without_pair: list[
                tuple[float, float, float, float]
            ] = []
            for placement in plan.placements:
                if placement.ref in {current.ref, other.ref}:
                    continue
                occupied_without_pair.extend(
                    _absolute_placement_shape(
                        placement,
                        footprint_ids.get(placement.ref, ""),
                    )
                )
            if any(
                rectangle[0] < edge
                or rectangle[1] < edge
                or rectangle[2] > plan.board_width - edge
                or rectangle[3] > plan.board_height - edge
                for rectangle in pair_shapes
            ):
                continue
            if any(
                _boxes_overlap(rectangle, occupied_rectangle, cap.min_clearance)
                for rectangle in pair_shapes
                for occupied_rectangle in occupied_without_pair
            ):
                continue
            moved_shapes = _absolute_placement_shape(moved, footprint_id)
            displaced_shapes = _absolute_placement_shape(displaced, other_fp)
            if any(
                _boxes_overlap(left, right, cap.min_clearance)
                for left in moved_shapes
                for right in displaced_shapes
            ):
                continue
            candidate_pad = (
                moved.x + pad_offset[0],
                moved.y + pad_offset[1],
            )
            candidates.append((
                (
                    math.dist(candidate_pad, (target.x, target.y)),
                    0.01
                    * math.dist(
                        (other.x, other.y),
                        (current.x, current.y),
                    ),
                ),
                [
                    moved
                    if item.ref == moved.ref
                    else displaced
                    if item.ref == displaced.ref
                    else item
                    for item in plan.placements
                ],
            ))
    if not candidates:
        return None
    _, repaired_placements = min(candidates, key=lambda item: item[0])
    return plan.model_copy(
        update={
            "placements": repaired_placements,
            "rationale": (
                f"{plan.rationale}; AHE route-frontier relocation of one "
                "small failed endpoint"
            ),
        }
    )


def _repair_route_endpoint_placement(
    step: RouteSignalsStep,
    state: PipelineState,
    ctx: PipelineContext,
    knowledge: str,
    artifact: RouteResult,
) -> RouteResult:
    """Relocate one DRC-proven endpoint and reroute, with full rollback."""

    write = state.artifact(PipelineStep.LAYOUT_WRITE)
    old_plan = state.artifact(PipelineStep.LAYOUT_GENERAL)
    if not isinstance(write, PcbWriteResult) or not isinstance(
        old_plan,
        PcbPlacementPlan,
    ):
        return artifact
    pcb_path = Path(write.pcb_path)
    report_path = pcb_path.with_suffix(".drc.json")
    snapshot = _read_drc_snapshot(report_path)
    candidate_plan = _route_gap_placement_plan(state, snapshot)
    if candidate_plan is None:
        return artifact

    managed_paths = [
        pcb_path,
        pcb_path.with_name(f"{pcb_path.stem}.unrouted.kicad_pcb"),
        pcb_path.with_suffix(".dsn"),
        pcb_path.with_suffix(".ses"),
        pcb_path.with_suffix(".drc.json"),
        pcb_path.with_suffix(".ahe-route.drc.json"),
    ]
    with tempfile.TemporaryDirectory(prefix="rnp_route_relocate_") as backup_dir:
        backup_root = Path(backup_dir)
        existing: set[Path] = set()
        for path in managed_paths:
            if path.is_file():
                existing.add(path)
                shutil.copy2(path, backup_root / path.name)
        state.artifacts[PipelineStep.LAYOUT_GENERAL] = candidate_plan
        try:
            semantic_checks = LayoutGeneralStep().check(state, candidate_plan)
            if any(
                not check.ok and check.severity == Severity.ERROR
                for check in semantic_checks
            ):
                raise ValueError(
                    "route-aware placement violates persisted semantic constraints"
                )
            candidate_write, _ = LayoutWriteStep().propose(state, ctx, knowledge)
            assert isinstance(candidate_write, PcbWriteResult)
            write_checks = LayoutWriteStep().check(state, candidate_write)
            if any(
                not check.ok and check.severity == Severity.ERROR
                for check in write_checks
            ):
                raise ValueError("route-aware placement is not layout-legal")
            state.artifacts[PipelineStep.LAYOUT_WRITE] = candidate_write
            candidate, _ = step.propose(state, ctx, knowledge)
            assert isinstance(candidate, RouteResult)
            if candidate.unconnected < artifact.unconnected:
                return candidate
        except Exception:  # noqa: BLE001 - rollback retains the verified board
            pass

        state.artifacts[PipelineStep.LAYOUT_GENERAL] = old_plan
        state.artifacts[PipelineStep.LAYOUT_WRITE] = write
        for path in managed_paths:
            backup = backup_root / path.name
            if path in existing and backup.is_file():
                shutil.copy2(backup, path)
            elif path not in existing:
                path.unlink(missing_ok=True)
    return artifact


def _route_completion_metrics(
    outcome: Any,
) -> tuple[int, int, int, str]:
    """Return logical-net and physical-connection progress without mixing grains."""
    routed_nets = (
        outcome.nets
        if outcome.ok and outcome.unconnected == 0
        else 0
    )
    total_connections = outcome.total_connections
    basis = outcome.metric_basis
    if (
        outcome.ok
        and total_connections >= 0
        and 0 <= outcome.unconnected <= total_connections
    ):
        routed_connections = total_connections - outcome.unconnected
    else:
        routed_connections = -1
        if (
            outcome.ok
            and total_connections >= 0
            and outcome.unconnected > total_connections
        ):
            basis = f"{basis}_inconsistent"
    return routed_nets, routed_connections, total_connections, basis


class RouteSignalsStep(PipelineStepBase):
    """Route remaining signals with Freerouting.

    Planning/test contexts may opt into graceful degradation. Deployed
    production contexts set ``require_freerouting`` and fail closed.
    """

    step = PipelineStep.ROUTE_SIGNALS
    repair_is_deterministic = True
    knowledge_role = "routing"
    repair_strategy_id = "route_plane_stitch_and_local_search_v2"

    def propose(
        self, state: PipelineState, ctx: PipelineContext, knowledge: str
    ) -> tuple[BaseModel, bool]:
        from ratsnestpro.eda import routing

        pm = state.artifact(PipelineStep.SCH_PINMAP)
        pcb_path = (
            Path(ctx.out_dir) / f"{state.project_name}.kicad_pcb" if ctx.out_dir else None
        )
        if not isinstance(pm, PinMapPlan) or pcb_path is None or not pcb_path.is_file():
            return RouteResult(
                method="deferred", required=ctx.require_freerouting,
                routed_nets=0, total_nets=0,
                note="no board or pin-map available; signal routing deferred",
            ), False

        # net -> [(ref, pad_number)]; only nets with >=2 pins are routable.
        netmap = {
            n.name: [[p.ref, p.number] for p in n.pins]
            for n in pm.nets if len(n.pins) >= 2
        }
        route_plan = state.artifact(PipelineStep.ROUTE_PLAN)
        planned_layers = (
            route_plan.layers if isinstance(route_plan, RoutePlan) else 2
        )
        net_classes = (
            route_plan.net_classes if isinstance(route_plan, RoutePlan) else []
        )
        cap = config.process_capability()
        route_rules = {
            "clearance_mm": min(
                (net_class.clearance for net_class in net_classes),
                default=cap.min_clearance,
            ),
            "track_width_mm": min(
                (net_class.width for net_class in net_classes),
                default=cap.min_track_width,
            ),
            "via_diameter_mm": min(
                (net_class.via_diameter for net_class in net_classes),
                default=cap.min_via_diameter,
            ),
            "via_drill_mm": min(
                (net_class.via_drill for net_class in net_classes),
                default=cap.min_via_drill,
            ),
        }
        layers = max(
            planned_layers,
            _requested_layer_count(state.requirement_text),
        )
        explicit_layers = _explicit_requested_layer_count(
            state.requirement_text
        )
        allow_layer_escalation = explicit_layers is None
        baseline_path = pcb_path.with_name(f"{pcb_path.stem}.unrouted.kicad_pcb")
        previous_ses = pcb_path.with_suffix(".ses")
        if (
            allow_layer_escalation
            and
            layers < 4
            and previous_ses.is_file()
            and previous_ses.stat().st_size > 0
        ):
            layers = 4
        best_pcb_path = pcb_path.with_suffix(".route-best.kicad_pcb")
        best_dsn_path = pcb_path.with_suffix(".route-best.dsn")
        best_ses_path = pcb_path.with_suffix(".route-best.ses")
        best_outcome: routing.RouteOutcome | None = None
        route_attempt = 0
        route_budget = max(1, int(ctx.max_route_invocations))
        route_budget_exhausted = False

        def route_once(
            *,
            attempt_layers: int,
            max_passes: int,
            rules: dict[str, float],
            profile: str,
            seed_override: int | None = None,
        ) -> routing.RouteOutcome | None:
            nonlocal best_outcome, route_attempt, route_budget_exhausted
            if route_attempt >= route_budget:
                route_budget_exhausted = True
                return None
            route_attempt += 1
            if baseline_path.is_file():
                shutil.copy2(baseline_path, pcb_path)
            seed = (
                seed_override
                if seed_override is not None
                else _routing_seed(
                    state,
                    layer_count=attempt_layers,
                    attempt=route_attempt,
                )
            )
            current = routing.autoroute(
                pcb_path,
                netmap,
                max_passes=max_passes,
                layer_count=attempt_layers,
                random_seed=seed,
                **rules,
            )
            current.note = (
                f"routing_profile={profile}; deterministic_seed={seed}; "
                f"{current.note}"
            )
            current_score = (
                0 if current.ok else 1,
                current.unconnected if current.unconnected >= 0 else 10**9,
                -current.routed_tracks,
            )
            if best_outcome is None:
                best_score = (2, 10**9, 0)
            else:
                best_score = (
                    0 if best_outcome.ok else 1,
                    (
                        best_outcome.unconnected
                        if best_outcome.unconnected >= 0
                        else 10**9
                    ),
                    -best_outcome.routed_tracks,
                )
            if current_score < best_score:
                best_outcome = current
                if pcb_path.is_file():
                    shutil.copy2(pcb_path, best_pcb_path)
                for source, target in (
                    (Path(current.dsn_path), best_dsn_path),
                    (Path(current.ses_path), best_ses_path),
                ):
                    if source.is_file():
                        shutil.copy2(source, target)
            return current

        first_outcome = route_once(
            attempt_layers=layers,
            max_passes=routing.pass_budget(netmap, layers),
            rules=route_rules,
            profile="requested",
        )
        assert first_outcome is not None
        outcome = first_outcome
        # Escalate an incomplete two-layer attempt once. This is a bounded
        # geometric fallback, not a relaxed release gate: SES import and zero
        # remaining connections are still mandatory.
        if (
            allow_layer_escalation
            and outcome.ok
            and outcome.unconnected > 0
            and layers < 4
        ):
            candidate = route_once(
                attempt_layers=4,
                max_passes=routing.pass_budget(netmap, 4),
                rules=route_rules,
                profile="layer_escalation",
            )
            if candidate is not None:
                outcome = candidate
        adaptive_rules = {
            "clearance_mm": cap.min_clearance,
            "track_width_mm": cap.min_track_width,
            "via_diameter_mm": cap.min_via_diameter,
            "via_drill_mm": cap.min_via_drill,
        }
        can_tighten_within_fab = any(
            adaptive_rules[name] < route_rules[name] - 1e-9
            for name in adaptive_rules
        )
        adaptive_allowed = (
            can_tighten_within_fab
            and not _has_explicit_routing_geometry(state.requirement_text)
        )
        if (
            outcome.ok
            and outcome.unconnected > 0
            and not adaptive_allowed
            and (
                outcome.layers >= 4
                or not allow_layer_escalation
            )
        ):
            candidate = route_once(
                attempt_layers=outcome.layers,
                max_passes=100,
                rules=route_rules,
                profile="extended_passes",
            )
            if candidate is not None:
                outcome = candidate
        active_rules = route_rules
        if outcome.ok and outcome.unconnected > 0 and adaptive_allowed:
            candidate = route_once(
                attempt_layers=outcome.layers,
                max_passes=100,
                rules=adaptive_rules,
                profile="fab_min",
            )
            if candidate is not None:
                outcome = candidate
                active_rules = adaptive_rules
        # Freerouting is heuristic: the same legal geometry can leave one
        # connection on one seed and finish on another.  Run a bounded,
        # reproducible seed portfolio before escalating to an upstream layout
        # replan.  Geometry and release gates remain unchanged.
        if outcome.ok and outcome.unconnected > 0:
            for _ in range(3):
                if outcome.unconnected == 0:
                    break
                candidate = route_once(
                    attempt_layers=outcome.layers,
                    max_passes=100,
                    rules=active_rules,
                    profile="seed_portfolio",
                )
                if candidate is None:
                    break
                outcome = candidate
        if best_outcome is not None:
            outcome = best_outcome
            if best_pcb_path.is_file():
                shutil.copy2(best_pcb_path, pcb_path)
            for source, target in (
                (best_dsn_path, Path(outcome.dsn_path)),
                (best_ses_path, Path(outcome.ses_path)),
            ):
                if source.is_file():
                    shutil.copy2(source, target)
        for temporary in (best_pcb_path, best_dsn_path, best_ses_path):
            temporary.unlink(missing_ok=True)
        if route_budget_exhausted:
            outcome.note = (
                f"{outcome.note}; routing invocation budget exhausted "
                f"({route_attempt}/{route_budget}); retained best available "
                "PCB/DSN/SES outcome; routing remains incomplete "
                f"(unconnected={outcome.unconnected})"
            )
        adaptive_rules_used = "routing_profile=fab_min" in outcome.note
        # A KiCad ratsnest item is a physical connection, not a logical net.
        # Never subtract those different grains.  Whole-net completion is only
        # certain when no physical connections remain; partial progress is
        # reported separately using the worker's before/after connectivity
        # counts from the same KiCad API.
        (
            routed,
            routed_connections,
            total_connections,
            metric_basis,
        ) = _route_completion_metrics(outcome)
        return RouteResult(
            method=outcome.method,
            required=ctx.require_freerouting,
            layers=outcome.layers,
            routed_nets=routed,
            total_nets=outcome.nets,
            routed_connections=routed_connections,
            total_connections=total_connections,
            metric_basis=metric_basis,
            assigned_pads=outcome.assigned_pads,
            routed_tracks=outcome.routed_tracks,
            unconnected=outcome.unconnected,
            dsn_path=outcome.dsn_path,
            ses_path=outcome.ses_path,
            note=(
                "adaptive fab-min routing rules used; "
                f"{outcome.note}"
                if adaptive_rules_used
                else outcome.note
            ),
        ), False

    def check(self, state: PipelineState, artifact: BaseModel) -> list[CheckResult]:
        assert isinstance(artifact, RouteResult)
        complete = (
            artifact.method == "freerouting"
            and artifact.total_nets > 0
            and artifact.routed_nets >= artifact.total_nets
            and artifact.unconnected == 0
            and bool(artifact.dsn_path)
            and bool(artifact.ses_path)
        )
        return [
            CheckResult(
                name="signals_routed",
                ok=complete,
                severity=Severity.ERROR if artifact.required else Severity.WARNING,
                message=f"{artifact.routed_nets}/{artifact.total_nets} nets routed "
                        f"({artifact.method}), tracks={artifact.routed_tracks}, "
                        f"connections={artifact.routed_connections}/"
                        f"{artifact.total_connections}, "
                        f"unconnected={artifact.unconnected}, "
                        f"basis={artifact.metric_basis}; {artifact.note}",
            ),
        ]

    def repair(
        self,
        state: PipelineState,
        ctx: PipelineContext,
        knowledge: str,
        artifact: BaseModel,
        checks: list[CheckResult],
    ) -> tuple[BaseModel, bool]:
        assert isinstance(artifact, RouteResult)
        repaired = _repair_drc_connectivity_gaps(state, ctx, artifact)
        if repaired.unconnected < artifact.unconnected:
            return repaired, False
        repaired = _repair_power_plane_gaps(state, ctx, artifact)
        if repaired.unconnected < artifact.unconnected:
            return repaired, False
        return (
            _repair_route_endpoint_placement(
                self,
                state,
                ctx,
                knowledge,
                artifact,
            ),
            False,
        )

    def repair_applicable(
        self,
        state: PipelineState,
        artifact: BaseModel,
        checks: list[CheckResult],
    ) -> bool:
        write = state.artifact(PipelineStep.LAYOUT_WRITE)
        return (
            isinstance(artifact, RouteResult)
            and artifact.unconnected > 0
            and isinstance(write, PcbWriteResult)
            and Path(write.pcb_path).is_file()
            and bool(kicad_cli_available())
        )

    def convergence_score(
        self,
        artifact: BaseModel,
        results: list[CheckResult],
    ) -> tuple[int, int, int]:
        failed = [check for check in results if not check.ok]
        errors = sum(
            not check.ok and check.severity == Severity.ERROR
            for check in results
        )
        remaining = (
            max(0, artifact.unconnected)
            if isinstance(artifact, RouteResult)
            else len(failed)
        )
        return errors, remaining, sum(len(check.message) for check in failed)

    def rollback_target(
        self,
        state: PipelineState,
        artifact: BaseModel,
        checks: list[CheckResult],
    ) -> PipelineStep | None:
        if (
            isinstance(artifact, RouteResult)
            and artifact.required
            and artifact.unconnected > 0
        ):
            return PipelineStep.LAYOUT_PARTITION
        return None

    def summarize(self, artifact: BaseModel) -> str:
        assert isinstance(artifact, RouteResult)
        return (
            f"{artifact.method} ({artifact.layers} layers): "
            f"{artifact.routed_nets}/{artifact.total_nets} nets, "
            f"{artifact.routed_connections}/{artifact.total_connections} connections"
        )


class RouteFabStep(PipelineStepBase):
    """Fabrication bottom-line audit: every planned width/clearance/via meets the
    process minimums. Blocks on any violation (anti-board-burn)."""

    step = PipelineStep.ROUTE_FAB

    def propose(
        self, state: PipelineState, ctx: PipelineContext, knowledge: str
    ) -> tuple[BaseModel, bool]:
        cap = config.process_capability()
        plan = state.artifact(PipelineStep.ROUTE_PLAN)
        violations: list[str] = []
        if isinstance(plan, RoutePlan):
            for c in plan.net_classes:
                if c.width < cap.min_track_width:
                    violations.append(f"{c.name}: width {c.width} < {cap.min_track_width}")
                if c.clearance < cap.min_clearance:
                    violations.append(f"{c.name}: clearance {c.clearance} < {cap.min_clearance}")
                if c.via_diameter < cap.min_via_diameter:
                    violations.append(f"{c.name}: via {c.via_diameter} < {cap.min_via_diameter}")
                if c.via_drill < cap.min_via_drill:
                    violations.append(f"{c.name}: drill {c.via_drill} < {cap.min_via_drill}")
        return FabAudit(violations=violations), False

    def check(self, state: PipelineState, artifact: BaseModel) -> list[CheckResult]:
        assert isinstance(artifact, FabAudit)
        return [
            CheckResult(name="fab_rules_met", ok=not artifact.violations,
                        message=f"process violations: {artifact.violations}"),
        ]

    def summarize(self, artifact: BaseModel) -> str:
        assert isinstance(artifact, FabAudit)
        return f"{len(artifact.violations)} fab violations"


def _run_kicad_drc(
    cli: str,
    pcb_path: Path,
    report_path: Path,
) -> list[str]:
    """Return error-severity findings from the actual final PCB file."""
    return list(
        _run_kicad_drc_snapshot(cli, pcb_path, report_path).findings
    )


def _worker_result(
    command: list[str],
    *,
    timeout: int,
) -> tuple[int, dict[str, Any]]:
    """Run a KiCad-system-Python worker and decode its single result record."""

    import subprocess

    process = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )
    payload: dict[str, Any] = {}
    for line in process.stdout.splitlines():
        if not line.startswith("RESULT "):
            continue
        try:
            payload = json.loads(line[len("RESULT "):])
        except json.JSONDecodeError:
            payload = {}
    return process.returncode, payload


def _repair_manufacture_drills(
    state: PipelineState,
    ctx: PipelineContext,
    artifact: ManufactureResult,
) -> bool:
    """Apply only DRC-proven, annular-safe plated-hole normalization."""

    if not any(
        finding.startswith("kicad_cli:drill_out_of_range:")
        for finding in artifact.drc_violations
    ):
        return False
    from ratsnestpro.eda import routing

    write = state.artifact(PipelineStep.LAYOUT_WRITE)
    cli = kicad_cli_available()
    kicad_python = routing.kicad_python()
    if (
        not isinstance(write, PcbWriteResult)
        or not Path(write.pcb_path).is_file()
        or not cli
        or not kicad_python
    ):
        return False
    cap = config.process_capability()
    worker = (
        Path(__file__).resolve().parent.parent
        / "eda"
        / "_manufacture_repair_worker.py"
    )
    out_dir = (
        Path(ctx.out_dir)
        if ctx.out_dir
        else Path(write.pcb_path).resolve().parent
    )
    report_path = out_dir / f"{state.project_name}.ahe-manufacture.drc.json"
    try:
        returncode, payload = _worker_result(
            [
                kicad_python,
                str(worker),
                write.pcb_path,
                cli,
                str(cap.min_hole_diameter),
                str(cap.min_annular_ring),
                str(report_path),
            ],
            timeout=300,
        )
    except Exception:  # noqa: BLE001 - rejected candidates leave board intact
        return False
    return (
        returncode == 0
        and bool(payload.get("ok"))
        and int(payload.get("after_errors", -1))
        < int(payload.get("before_errors", -1))
        and int(payload.get("unconnected", -1)) == 0
    )


def _drc_refs(
    report_path: Path,
    finding_types: set[str],
) -> set[str]:
    """Extract affected references from authoritative KiCad DRC items."""

    if not report_path.is_file():
        return set()
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    refs: set[str] = set()
    for finding in report.get("violations", []):
        if (
            not isinstance(finding, dict)
            or str(finding.get("severity", "error")) != "error"
            or str(finding.get("type", "")) not in finding_types
        ):
            continue
        for item in finding.get("items", []):
            if not isinstance(item, dict):
                continue
            match = re.search(
                r"\b(?:of|footprint)\s+([A-Za-z]+\d+)\b",
                str(item.get("description", "")),
                re.IGNORECASE,
            )
            if match is not None:
                refs.add(match.group(1).upper())
    return refs


def _prepare_intrinsic_footprint_replan(
    state: PipelineState,
    ctx: PipelineContext,
) -> bool:
    """Select a real pin-compatible footprint after intrinsic DRC evidence.

    A selected catalogue MPN/LCSC is never silently changed.  For an ungrounded
    generic connector/package, the candidate must be from the same installed
    KiCad library and semantic family and expose exactly the same numbered pad
    set.  The system-Python worker evaluates candidates with the project's real
    DRC rules.
    """

    from ratsnestpro.eda import routing

    selection = state.artifact(PipelineStep.SELECTION)
    write = state.artifact(PipelineStep.LAYOUT_WRITE)
    cli = kicad_cli_available()
    kicad_python = routing.kicad_python()
    if (
        not isinstance(selection, SelectionPlan)
        or not isinstance(write, PcbWriteResult)
        or not Path(write.pcb_path).is_file()
        or not cli
        or not kicad_python
    ):
        return False
    out_dir = (
        Path(ctx.out_dir)
        if ctx.out_dir
        else Path(write.pcb_path).resolve().parent
    )
    refs = _drc_refs(
        out_dir / f"{state.project_name}.drc.json",
        {"hole_clearance", "copper_edge_clearance"},
    )
    if not refs:
        return False
    worker = (
        Path(__file__).resolve().parent.parent
        / "eda"
        / "_footprint_repair_worker.py"
    )
    replacements: dict[str, str] = {}
    pending = False
    for part in selection.parts:
        if (
            part.ref.upper() not in refs
            or part.mpn.strip()
            or part.lcsc.strip()
        ):
            continue
        try:
            returncode, payload = _worker_result(
                [
                    kicad_python,
                    str(worker),
                    write.pcb_path,
                    cli,
                    part.ref,
                    part.footprint,
                ],
                timeout=600,
            )
        except Exception:  # noqa: BLE001 - no mutation on failed proof
            continue
        if returncode != 0 or not payload.get("ok"):
            continue
        candidate = str(payload.get("footprint", "")).strip()
        if not candidate:
            continue
        if payload.get("pending"):
            pending = True
        elif (
            candidate != part.footprint
            and int(payload.get("candidate_errors", 1)) == 0
        ):
            replacements[part.ref] = candidate
    if replacements:
        updated_selection = SelectionPlan(
            parts=[
                part.model_copy(
                    update={"footprint": replacements.get(part.ref, part.footprint)}
                )
                for part in selection.parts
            ],
            rationale=(
                f"{selection.rationale}; AHE replaced intrinsically "
                "non-manufacturable ungrounded footprint(s) with installed, "
                "pin-compatible, isolated-DRC-clean equivalents"
            ),
        )
        previous_materialized = state.artifacts.get(
            PipelineStep.SCH_MATERIALIZE
        )
        previous_erc = state.artifacts.get(PipelineStep.ERC)
        state.artifacts[PipelineStep.SELECTION] = updated_selection
        try:
            materialized, _ = SchMaterializeStep().propose(state, ctx, "")
            materialize_checks = SchMaterializeStep().check(
                state,
                materialized,
            )
            if any(
                not check.ok and check.severity == Severity.ERROR
                for check in materialize_checks
            ):
                raise RuntimeError("schematic rematerialization failed")
            state.artifacts[PipelineStep.SCH_MATERIALIZE] = materialized
            erc, _ = ErcStep().propose(state, ctx, "")
            erc_checks = ErcStep().check(state, erc)
            if any(
                not check.ok and check.severity == Severity.ERROR
                for check in erc_checks
            ):
                raise RuntimeError("ERC failed after footprint substitution")
            state.artifacts[PipelineStep.ERC] = erc
        except Exception:
            state.artifacts[PipelineStep.SELECTION] = selection
            if previous_materialized is None:
                state.artifacts.pop(PipelineStep.SCH_MATERIALIZE, None)
            else:
                state.artifacts[PipelineStep.SCH_MATERIALIZE] = (
                    previous_materialized
                )
            if previous_erc is None:
                state.artifacts.pop(PipelineStep.ERC, None)
            else:
                state.artifacts[PipelineStep.ERC] = previous_erc
            return False
    return bool(replacements) or pending


class ManufactureStep(PipelineStepBase):
    """DRC bottom-line + manufacturing outputs (BOM, CPL, optional Gerber).

    DRC aggregates the deterministic layout/routing findings (overlaps,
    out-of-bounds, fab-rule violations) — authoritative and blocking. BOM and
    CPL are always written (pure data). Gerber export runs only when kicad-cli
    is available; its absence is a warning, never a pass.
    """

    step = PipelineStep.MANUFACTURE
    repair_is_deterministic = True
    repair_strategy_id = "drc_monotonic_geometry_v1"

    def propose(
        self, state: PipelineState, ctx: PipelineContext, knowledge: str
    ) -> tuple[BaseModel, bool]:
        import csv

        out_dir = Path(ctx.out_dir) if ctx.out_dir else Path(tempfile.mkdtemp(prefix="rnp_mfg_"))
        out_dir.mkdir(parents=True, exist_ok=True)

        # --- DRC bottom-line: aggregate deterministic findings -------------
        drc: list[str] = []
        write = state.artifact(PipelineStep.LAYOUT_WRITE)
        if isinstance(write, PcbWriteResult):
            drc += [f"overlap:{o}" for o in write.overlaps]
            drc += [f"out_of_bounds:{r}" for r in write.out_of_bounds]
        fab = state.artifact(PipelineStep.ROUTE_FAB)
        if isinstance(fab, FabAudit):
            drc += [f"fab:{v}" for v in fab.violations]
        write_art = state.artifact(PipelineStep.LAYOUT_WRITE)
        pcb_path = Path(write_art.pcb_path) if isinstance(
            write_art, PcbWriteResult
        ) else None
        cli = kicad_cli_available()
        if cli and pcb_path is not None and pcb_path.is_file():
            drc += _run_kicad_drc(
                cli,
                pcb_path,
                out_dir / f"{state.project_name}.drc.json",
            )

        # --- BOM + component release manifest from the selection -----------
        bom_path = out_dir / f"{state.project_name}_bom.csv"
        unresolved_manifest_path = (
            out_dir / f"{state.project_name}_unresolved_components.json"
        )
        sel = state.artifact(PipelineStep.SELECTION)
        component_issues = selection_release_issues(state)
        nonrelease_refs = {issue["ref"] for issue in component_issues}
        if isinstance(sel, SelectionPlan):
            with bom_path.open("w", newline="", encoding="utf-8") as fh:
                wr = csv.writer(fh)
                wr.writerow([
                    "Reference",
                    "Value",
                    "Footprint",
                    "MPN",
                    "LCSC",
                    "DNP",
                    "ReleaseReady",
                    "IdentityMode",
                    "IdentityProvenance",
                    "Resolution",
                    "ResolutionDetail",
                ])
                for part in sel.parts:
                    wr.writerow([
                        part.ref,
                        part.value,
                        part.footprint,
                        part.mpn,
                        part.lcsc,
                        "yes" if part.dnp else "",
                        "no" if part.ref in nonrelease_refs else "yes",
                        part.identity_mode,
                        part.identity_provenance,
                        part.resolution_status,
                        part.resolution_detail,
                    ])

        unresolved_components: list[dict[str, Any]] = []
        release_proofs: list[dict[str, Any]] = []
        if isinstance(sel, SelectionPlan):
            by_ref = {part.ref: part for part in sel.parts}
            release_proofs = [
                {
                    "ref": part.ref,
                    "release_ready": part.ref not in nonrelease_refs,
                    "status": part.resolution_status,
                    "dnp": part.dnp,
                    "unresolved": part.unresolved,
                    "symbol": part.symbol,
                    "footprint": part.footprint,
                    "requested_identity": part.requested_identity,
                    "identity_mode": part.identity_mode,
                    "identity_provenance": part.identity_provenance,
                }
                for part in sel.parts
            ]
            for issue in component_issues:
                part = by_ref.get(issue["ref"])
                if part is None:
                    continue
                try:
                    symbol_properties = symbols.symbol_properties(part.symbol)
                except Exception:  # noqa: BLE001 - audit metadata is best effort
                    symbol_properties = {}
                evidence_ids = [
                    value
                    for value in str(
                        symbol_properties.get("RatsNestEvidenceIds", "")
                    ).split(";")
                    if value
                ]
                unresolved_components.append({
                    "ref": part.ref,
                    "status": issue["status"],
                    "reason": issue["reason"],
                    "evidence": {
                        "kind": symbol_properties.get(
                            "RatsNestPinEvidence",
                            getattr(part, "identity_provenance", ""),
                        ),
                        "ids": evidence_ids,
                    },
                    "symbol": part.symbol,
                    "footprint": part.footprint,
                    "requested_identity": part.requested_identity,
                    "value": part.value,
                    "identity_mode": part.identity_mode,
                    "identity_provenance": part.identity_provenance,
                })
        unresolved_manifest_path.write_text(
            json.dumps(
                {
                    "schema_version": _COMPONENT_RELEASE_MANIFEST_SCHEMA,
                    "release_policy": _COMPONENT_RELEASE_POLICY,
                    "source": "pipeline.selection.component_resolution",
                    "project_name": state.project_name,
                    "release_ready": not component_issues,
                    "selection_component_count": (
                        len(sel.parts)
                        if isinstance(sel, SelectionPlan)
                        else 0
                    ),
                    "release_proven_component_count": sum(
                        proof["release_ready"] for proof in release_proofs
                    ),
                    "component_release_proofs": release_proofs,
                    "unresolved_components": unresolved_components,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        # --- CPL / pick-and-place (DNP/nonrelease refs are never populated) -
        cpl_path = out_dir / f"{state.project_name}_cpl.csv"
        plan = state.artifact(PipelineStep.LAYOUT_GENERAL)
        if isinstance(plan, PcbPlacementPlan):
            with cpl_path.open("w", newline="", encoding="utf-8") as fh:
                wr = csv.writer(fh)
                wr.writerow(["Designator", "Mid X", "Mid Y", "Rotation", "Layer"])
                for pp in plan.placements:
                    if pp.ref in nonrelease_refs:
                        continue
                    layer = "top" if pp.side == "front" else "bottom"
                    wr.writerow([pp.ref, pp.x, pp.y, pp.rotation, layer])

        # --- Gerber + Excellon drill outputs via kicad-cli ----------------
        gerber_dir = out_dir / "gerber"
        manufacturing_export_applicable = bool(
            cli and pcb_path is not None and pcb_path.is_file()
        )
        gerber_ok = False
        drill_paths: list[str] = []
        drill_ok = False
        if manufacturing_export_applicable:
            try:
                import subprocess

                gerber_dir.mkdir(parents=True, exist_ok=True)
                proc = subprocess.run(
                    [
                        cli,
                        "pcb",
                        "export",
                        "gerbers",
                        "--output",
                        str(gerber_dir),
                        str(pcb_path),
                    ],
                    capture_output=True, text=True, encoding="utf-8", errors="replace",
                )
                gerber_ok = proc.returncode == 0 and any(gerber_dir.iterdir())
                drill_proc = subprocess.run(
                    [
                        cli,
                        "pcb",
                        "export",
                        "drill",
                        "--output",
                        str(gerber_dir),
                        "--excellon-separate-th",
                        "--generate-map",
                        "--map-format",
                        "gerberx2",
                        str(pcb_path),
                    ],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )
                drill_paths = sorted(
                    str(path)
                    for path in gerber_dir.glob("*.drl")
                    if path.is_file() and path.stat().st_size > 0
                )
                drill_ok = drill_proc.returncode == 0 and bool(drill_paths)
            except Exception:
                gerber_ok = False
                drill_paths = []
                drill_ok = False

        return (
            ManufactureResult(
                bom_path=str(bom_path) if isinstance(sel, SelectionPlan) else "",
                cpl_path=str(cpl_path) if isinstance(plan, PcbPlacementPlan) else "",
                unresolved_manifest_path=str(unresolved_manifest_path),
                component_release_ready=not component_issues,
                component_release_blockers=[
                    f"{issue['ref']}: {issue['reason']}"
                    for issue in component_issues
                ],
                gerber_dir=str(gerber_dir) if gerber_ok else "",
                manufacturing_export_applicable=manufacturing_export_applicable,
                gerber_exported=gerber_ok,
                drill_paths=drill_paths,
                drill_exported=drill_ok,
                drc_violations=drc,
            ),
            False,
        )

    def check(self, state: PipelineState, artifact: BaseModel) -> list[CheckResult]:
        assert isinstance(artifact, ManufactureResult)
        write = state.artifact(PipelineStep.LAYOUT_WRITE)
        export_applicable = (
            artifact.manufacturing_export_applicable
            or (
                bool(kicad_cli_available())
                and isinstance(write, PcbWriteResult)
                and Path(write.pcb_path).is_file()
            )
        )
        checks = [
            CheckResult(name="drc_clean", ok=not artifact.drc_violations,
                        message=f"DRC violations: {artifact.drc_violations}"),
            CheckResult(
                name="component_release_ready",
                ok=artifact.component_release_ready,
                message=(
                    "DNP/unresolved/nonrelease components: "
                    f"{artifact.component_release_blockers}"
                ),
            ),
            CheckResult(name="bom_written", ok=bool(artifact.bom_path),
                        message="BOM not written"),
            CheckResult(name="cpl_written", ok=bool(artifact.cpl_path),
                        message="CPL not written"),
            CheckResult(
                name="unresolved_manifest_written",
                ok=bool(artifact.unresolved_manifest_path),
                message="unresolved-component manifest not written",
            ),
        ]
        if not artifact.gerber_exported:
            checks.append(CheckResult(
                name="gerber_exported",
                ok=False,
                severity=(
                    Severity.ERROR
                    if export_applicable
                    else Severity.WARNING
                ),
                message=(
                    "Gerber export failed"
                    if export_applicable
                    else "Gerber not exported (kicad-cli unavailable)"
                ),
            ))
        if not artifact.drill_exported:
            checks.append(CheckResult(
                name="drill_exported",
                ok=False,
                severity=(
                    Severity.ERROR
                    if export_applicable
                    else Severity.WARNING
                ),
                message=(
                    "Excellon drill export failed or produced no non-empty "
                    ".drl file"
                    if export_applicable
                    else "Drill files not exported (kicad-cli unavailable)"
                ),
            ))
        return checks

    def repair(
        self,
        state: PipelineState,
        ctx: PipelineContext,
        knowledge: str,
        artifact: BaseModel,
        checks: list[CheckResult],
    ) -> tuple[BaseModel, bool]:
        assert isinstance(artifact, ManufactureResult)
        if any(
            not check.ok
            and check.severity == Severity.ERROR
            and check.name in {"gerber_exported", "drill_exported"}
            for check in checks
        ):
            return self.propose(state, ctx, knowledge)
        if _repair_manufacture_drills(state, ctx, artifact):
            return self.propose(state, ctx, knowledge)
        return artifact, False

    def repair_applicable(
        self,
        state: PipelineState,
        artifact: BaseModel,
        checks: list[CheckResult],
    ) -> bool:
        return (
            isinstance(artifact, ManufactureResult)
            and (
                any(
                    finding.startswith("kicad_cli:drill_out_of_range:")
                    for finding in artifact.drc_violations
                )
                or any(
                    not check.ok
                    and check.severity == Severity.ERROR
                    and check.name
                    in {"gerber_exported", "drill_exported"}
                    for check in checks
                )
            )
        )

    def convergence_score(
        self,
        artifact: BaseModel,
        results: list[CheckResult],
    ) -> tuple[int, int, int]:
        failed = [check for check in results if not check.ok]
        errors = sum(
            not check.ok and check.severity == Severity.ERROR
            for check in results
        )
        violations = (
            len(artifact.drc_violations)
            if isinstance(artifact, ManufactureResult)
            else len(failed)
        )
        return errors, violations, sum(len(check.message) for check in failed)

    def rollback_target(
        self,
        state: PipelineState,
        artifact: BaseModel,
        checks: list[CheckResult],
    ) -> PipelineStep | None:
        if not isinstance(artifact, ManufactureResult):
            return None
        types = {
            finding.split(":", 2)[1]
            for finding in artifact.drc_violations
            if finding.startswith("kicad_cli:") and finding.count(":") >= 2
        }
        if (
            {"hole_clearance", "copper_edge_clearance"} & types
            and _prepare_intrinsic_footprint_replan(state, PipelineContext(
                out_dir=str(
                    Path(
                        state.artifact(PipelineStep.LAYOUT_WRITE).pcb_path
                    ).parent
                )
                if isinstance(
                    state.artifact(PipelineStep.LAYOUT_WRITE),
                    PcbWriteResult,
                )
                else None
            ))
        ):
            return PipelineStep.LAYOUT_WRITE
        if {
            "clearance",
            "shorting_items",
            "tracks_crossing",
            "courtyards_overlap",
        } & types:
            return PipelineStep.LAYOUT_GENERAL
        return None

    def summarize(self, artifact: BaseModel) -> str:
        assert isinstance(artifact, ManufactureResult)
        g = "gerber+" if artifact.gerber_exported else ""
        d = "drill+" if artifact.drill_exported else ""
        return (
            f"{g}{d}BOM+CPL written; "
            f"{len(artifact.drc_violations)} DRC violations; "
            f"{len(artifact.component_release_blockers)} nonrelease component(s)"
        )


# Registered steps, in canonical order.
ALL_STEPS: list[PipelineStepBase] = [
    RequirementsStep(),
    TopologyStep(),
    SelectionStep(),
    SchConnectionsStep(),
    SchPinMapStep(),
    SchLayoutStep(),
    SchMaterializeStep(),
    ErcStep(),
    LayoutPartitionStep(),
    LayoutCriticalStep(),
    LayoutGeneralStep(),
    LayoutWriteStep(),
    RoutePlanStep(),
    RoutePlanesStep(),
    RouteSignalsStep(),
    RouteFabStep(),
    ManufactureStep(),
]

ARTIFACT_MODELS: dict[PipelineStep, type[BaseModel]] = {
    PipelineStep.REQUIREMENTS: RequirementSpec,
    PipelineStep.TOPOLOGY: TopologyPlan,
    PipelineStep.SELECTION: SelectionPlan,
    PipelineStep.SCH_CONNECTIONS: NetlistIntent,
    PipelineStep.SCH_PINMAP: PinMapPlan,
    PipelineStep.SCH_LAYOUT: SchLayoutPlan,
    PipelineStep.SCH_MATERIALIZE: MaterializeResult,
    PipelineStep.ERC: ErcSummary,
    PipelineStep.LAYOUT_PARTITION: BoardPartition,
    PipelineStep.LAYOUT_CRITICAL: PcbPlacementPlan,
    PipelineStep.LAYOUT_GENERAL: PcbPlacementPlan,
    PipelineStep.LAYOUT_WRITE: PcbWriteResult,
    PipelineStep.ROUTE_PLAN: RoutePlan,
    PipelineStep.ROUTE_PLANES: PlanePlan,
    PipelineStep.ROUTE_SIGNALS: RouteResult,
    PipelineStep.ROUTE_FAB: FabAudit,
    PipelineStep.MANUFACTURE: ManufactureResult,
}


def _saved_error_signatures(
    saved: dict[str, Any],
) -> frozenset[tuple[str, str, str, bool]] | None:
    """Return persisted error identities, or ``None`` for legacy checkpoints."""

    raw_checks = saved.get("failed_checks")
    if not isinstance(raw_checks, list):
        return None
    signatures: set[tuple[str, str, str, bool]] = set()
    for raw_check in raw_checks:
        if not isinstance(raw_check, dict):
            return None
        severity = str(raw_check.get("severity", Severity.ERROR.value)).strip().lower()
        if severity != Severity.ERROR.value:
            continue
        signatures.add((
            str(raw_check.get("name", "")).strip(),
            severity,
            str(raw_check.get("message", "")).strip(),
            bool(raw_check.get("blocks_execution", False)),
        ))
    return frozenset(signatures)


def _current_error_signatures(
    checks: list[CheckResult],
) -> frozenset[tuple[str, str, str, bool]]:
    return frozenset(
        (
            check.name.strip(),
            check.severity.value,
            check.message.strip(),
            check.blocks_execution,
        )
        for check in checks
        if not check.ok and check.severity == Severity.ERROR
    )


def restore_pipeline_state(
    *,
    requirement_text: str,
    project_name: str,
    intermediate_artifacts: dict[str, Any],
    steps: list[dict[str, Any]],
    revision: int = 0,
    repair_history: list[dict[str, Any]] | None = None,
    replan_history: list[dict[str, Any]] | None = None,
    capability_gaps: list[dict[str, Any]] | None = None,
    resume_candidates: dict[str, Any] | None = None,
    connection_synthesis_checkpoint: dict[str, Any] | None = None,
    connection_synthesis_report: dict[str, Any] | None = None,
    invalidate_from_step: PipelineStep | None = None,
    artifact_first: bool = False,
) -> PipelineState:
    """Restore the longest contiguous prefix that still passes current gates.

    Checkpoints preserve work, not obsolete validation decisions.  Re-running
    deterministic checks lets a newer harness repair stale artifacts from the
    earliest affected step instead of repeatedly retrying a downstream symptom.
    Contract invalidation discards the changed step and all of its dependants.
    """
    try:
        restored_connection_report = (
            ConnectionSynthesisReport.model_validate(connection_synthesis_report)
            if isinstance(connection_synthesis_report, dict)
            else None
        )
    except Exception:  # noqa: BLE001 - optional audit data is not executable state
        restored_connection_report = None
    state = PipelineState(
        requirement_text=requirement_text,
        project_name=project_name,
        revision=max(0, revision),
        repair_history=[
            RepairRecord.model_validate(item)
            for item in (repair_history or [])
        ],
        replan_history=[
            ReplanRecord.model_validate(item)
            for item in (replan_history or [])
        ],
        capability_gaps=[
            CapabilityGap.model_validate(item)
            for item in (capability_gaps or [])
        ],
        connection_synthesis_report=restored_connection_report,
    )
    connection_contract_invalidated = (
        invalidate_from_step is not None
        and _ORDER_INDEX[invalidate_from_step]
        <= _ORDER_INDEX[PipelineStep.SCH_CONNECTIONS]
    )
    if connection_contract_invalidated:
        state.connection_synthesis_report = None
    if invalidate_from_step is not None:
        invalidated_steps = {
            step.value
            for step in CANONICAL_ORDER[_ORDER_INDEX[invalidate_from_step]:]
        }
        state.repair_history = [
            record
            for record in state.repair_history
            if record.step not in invalidated_steps
        ]
        state.replan_history = [
            record
            for record in state.replan_history
            if record.trigger_step not in invalidated_steps
            and record.rollback_to not in invalidated_steps
        ]
        state.capability_gaps = [
            gap
            for gap in state.capability_gaps
            if gap.step not in invalidated_steps
        ]
    for expected, saved in zip(CANONICAL_ORDER, steps, strict=False):
        if expected == invalidate_from_step:
            break
        if str(saved.get("name", "")) != expected.value:
            break
        if (
            expected == PipelineStep.SCH_CONNECTIONS
            and isinstance(connection_synthesis_checkpoint, dict)
            and (
                restored_connection_report is None
                or restored_connection_report.resumable
            )
        ):
            topology = state.artifact(PipelineStep.TOPOLOGY)
            selection = state.artifact(PipelineStep.SELECTION)
            try:
                inflight = ConnectionSynthesisCheckpoint.model_validate(
                    connection_synthesis_checkpoint
                )
            except Exception:  # noqa: BLE001 - stale partial work is discardable
                inflight = None
            if (
                inflight is not None
                and isinstance(topology, TopologyPlan)
                and isinstance(selection, SelectionPlan)
                and checkpoint_matches_inputs(inflight, topology, selection)
                and any(item.status != "completed" for item in inflight.batches)
            ):
                # A draft may have continued through downstream artifact-first
                # steps after connection synthesis paused. On the next same-input
                # run, restore only the verified upstream prefix and let the
                # connection step resume its pending/failed batches.
                state.connection_synthesis_checkpoint = inflight
                break
        raw_artifact = intermediate_artifacts.get(expected.value)
        if raw_artifact is None:
            break
        artifact = ARTIFACT_MODELS[expected].model_validate(raw_artifact)
        requirement_refreshed = (
            expected == PipelineStep.REQUIREMENTS
            and isinstance(artifact, RequirementSpec)
            and artifact.raw_text != requirement_text
        )
        if requirement_refreshed:
            artifact = artifact.model_copy(update={"raw_text": requirement_text})
        validator = ALL_STEPS[_ORDER_INDEX[expected]]
        saved_fingerprint = _artifact_fingerprint(artifact)
        try:
            artifact = validator.prepare_resumed_artifact(state, artifact)
        except Exception:  # noqa: BLE001 - retry this step through the runner
            state.resume_candidates[expected] = (
                artifact,
                bool(saved.get("used_llm")),
            )
            break
        if _artifact_fingerprint(artifact) != saved_fingerprint:
            state.resume_candidates[expected] = (
                artifact,
                bool(saved.get("used_llm")),
            )
            break
        saved_execution_blocked = saved.get("execution_blocked")
        if bool(saved.get("blocked")) and (
            not artifact_first or saved_execution_blocked is not False
        ):
            state.resume_candidates[expected] = (
                artifact,
                bool(saved.get("used_llm")),
            )
            break
        state.artifacts[expected] = artifact
        current_checks: list[CheckResult] = []
        execution_invalid = False
        try:
            current_checks = validator.check(state, artifact)
            invalid = any(
                not check.ok and check.severity == Severity.ERROR
                for check in current_checks
            )
            execution_invalid = any(
                not check.ok
                and check.severity == Severity.ERROR
                and check.blocks_execution
                for check in current_checks
            )
        except Exception:  # noqa: BLE001 - resume safely from this step
            invalid = True
            execution_invalid = True
        saved_errors = _saved_error_signatures(saved)
        if (
            saved_errors is not None
            and saved_errors != _current_error_signatures(current_checks)
        ):
            state.artifacts.pop(expected, None)
            state.resume_candidates[expected] = (
                artifact,
                bool(saved.get("used_llm")),
            )
            break
        if invalid and (not artifact_first or execution_invalid):
            state.artifacts.pop(expected, None)
            state.resume_candidates[expected] = (
                artifact,
                bool(saved.get("used_llm")),
            )
            break
        state.results.append(StepResult(
            step=expected,
            used_llm=bool(saved.get("used_llm")),
            checks=current_checks,
            blocked=invalid,
            execution_blocked=execution_invalid,
            summary=(
                validator.summarize(artifact)
                if requirement_refreshed
                else str(saved.get("summary", ""))
            ),
        ))
    restored_steps = {result.step.value for result in state.results}
    state.capability_gaps = [
        gap for gap in state.capability_gaps if gap.step not in restored_steps
    ]
    # A scheduled upstream replan removes the rollback artifact from
    # ``state.artifacts`` but deliberately retains it as a resume candidate.
    # Persist and restore that candidate so cancellation or a service restart
    # cannot turn bounded replanning into a full regeneration.
    expected_next_index = len(state.results)
    if expected_next_index < len(CANONICAL_ORDER):
        expected_next = CANONICAL_ORDER[expected_next_index]
        raw_candidate = (resume_candidates or {}).get(expected_next.value)
        contract_invalidated = (
            invalidate_from_step is not None
            and expected_next_index >= _ORDER_INDEX[invalidate_from_step]
        )
        if (
            not contract_invalidated
            and expected_next not in state.resume_candidates
            and isinstance(raw_candidate, dict)
            and isinstance(raw_candidate.get("artifact"), dict)
        ):
            state.resume_candidates[expected_next] = (
                ARTIFACT_MODELS[expected_next].model_validate(
                    raw_candidate["artifact"]
                ),
                bool(raw_candidate.get("used_llm")),
            )
        if (
            expected_next == PipelineStep.SCH_CONNECTIONS
            and not connection_contract_invalidated
            and expected_next not in state.resume_candidates
            and isinstance(connection_synthesis_checkpoint, dict)
            and (
                restored_connection_report is None
                or restored_connection_report.resumable
            )
        ):
            topology = state.artifact(PipelineStep.TOPOLOGY)
            selection = state.artifact(PipelineStep.SELECTION)
            if isinstance(topology, TopologyPlan) and isinstance(
                selection,
                SelectionPlan,
            ):
                try:
                    inflight = ConnectionSynthesisCheckpoint.model_validate(
                        connection_synthesis_checkpoint
                    )
                except Exception:  # noqa: BLE001 - stale partial work is discardable
                    inflight = None
                if (
                    inflight is not None
                    and checkpoint_matches_inputs(
                        inflight,
                        topology,
                        selection,
                    )
                ):
                    state.connection_synthesis_checkpoint = inflight
    return state


# --------------------------------------------------------------------------- #
# The pipeline runner
# --------------------------------------------------------------------------- #


class PipelineOrderError(RuntimeError):
    """Raised when the registered steps are not a valid canonical prefix."""


class Pipeline:
    """Runs a contiguous prefix of the pinned step sequence, in order.

    Steps cannot be skipped or reordered: the registered list must equal the
    first N entries of :data:`CANONICAL_ORDER`. Execution stops at the first
    blocking step (fail closed) or after ``until`` (inclusive) if given.
    """

    def __init__(self, steps: list[PipelineStepBase] | None = None) -> None:
        self.steps = steps if steps is not None else ALL_STEPS
        self._validate_order()

    def _validate_order(self) -> None:
        for i, step in enumerate(self.steps):
            if CANONICAL_ORDER[i] != step.step:
                raise PipelineOrderError(
                    f"step {i} is {step.step!r}, expected {CANONICAL_ORDER[i]!r}; "
                    "steps must follow the fixed pipeline order without gaps"
                )

    def run(
        self,
        state: PipelineState,
        ctx: PipelineContext | None = None,
        until: PipelineStep | None = None,
    ) -> PipelineState:
        ctx = ctx or PipelineContext()
        limit = _ORDER_INDEX[until] if until is not None else len(self.steps) - 1
        if ctx.artifact_first and not ctx.repair_release_issues:
            # Checkpoints created by the former fail-closed runner may contain
            # a scheduled full upstream replan for an ordinary release issue.
            # Replaying it would consume another large LLM call before producing
            # artifacts, contradicting artifact-first execution. Preserve the
            # audit record but explicitly retire that pending action.
            for record in state.replan_history:
                if record.status == "scheduled":
                    record.status = "deferred"
                    record.after_score = record.before_score
        completed = state.completed
        invalid_blocked_prefix = state.blocked and (
            not ctx.artifact_first or state.execution_blocked
        )
        if completed != CANONICAL_ORDER[:len(completed)] or invalid_blocked_prefix:
            raise PipelineOrderError(
                "resumed state must contain a contiguous canonical prefix without "
                "an execution-blocking result"
            )
        completed_set = set(completed)

        def emit_replan(event: str, record: ReplanRecord) -> None:
            if ctx.on_ahe_event is None:
                return
            try:
                ctx.on_ahe_event(
                    ahe_event(
                        event,
                        step=record.trigger_step,
                        revision=state.revision,
                        replan=record,
                    )
                )
            except Exception:  # noqa: BLE001 - observability is best effort
                return

        def score(result: StepResult) -> tuple[int, int, int]:
            failed = [check for check in result.checks if not check.ok]
            return (
                len(result.error_checks),
                len(failed),
                sum(len(check.message) for check in failed),
            )

        def pending_replan(trigger: PipelineStep) -> ReplanRecord | None:
            return next(
                (
                    record
                    for record in reversed(state.replan_history)
                    if record.trigger_step == trigger.value
                    and record.status == "scheduled"
                ),
                None,
            )

        index = 0
        while index < len(self.steps):
            step = self.steps[index]
            if _ORDER_INDEX[step.step] > limit:
                break
            if step.step in completed_set:
                index += 1
                continue
            active_replan = next(
                (
                    record
                    for record in reversed(state.replan_history)
                    if record.status == "scheduled"
                    and (
                        _ORDER_INDEX[PipelineStep(record.rollback_to)]
                        <= _ORDER_INDEX[step.step]
                        <= _ORDER_INDEX[PipelineStep(record.trigger_step)]
                    )
                ),
                None,
            )
            if active_replan is not None:
                ctx.repair_feedback = active_replan.feedback
            execution_attempt = 0
            while True:
                try:
                    result = step.run(state, ctx)
                    break
                except Exception as exc:  # noqa: BLE001 - captured at step boundary
                    if not ctx.capture_step_errors:
                        raise
                    if (
                        not isinstance(exc, StructuredOutputError)
                        and execution_attempt < max(0, ctx.execution_retry_attempts)
                    ):
                        execution_attempt += 1
                        continue
                    check_name = (
                        "structured_output_invalid"
                        if isinstance(exc, StructuredOutputError)
                        else "llm_proposal_failed"
                        if isinstance(exc, LlmError)
                        else "step_execution_failed"
                    )
                    check = CheckResult(
                        name=check_name,
                        ok=False,
                        message=f"{type(exc).__name__}: {exc}",
                        blocks_execution=True,
                    )
                    failure = make_failure(
                        step=step.step.value,
                        check_name=check.name,
                        message=check.message,
                        repair_available=False,
                    )
                    result = StepResult(
                        step=step.step,
                        used_llm=isinstance(exc, LlmError),
                        checks=[check],
                        failures=[failure],
                        blocked=True,
                        execution_blocked=True,
                        summary=f"Step execution failed: {exc}",
                    )
                    state.results.append(result)
                    break
                finally:
                    if active_replan is not None:
                        ctx.repair_feedback = ""

            can_continue = (
                not result.blocked
                or (
                    ctx.artifact_first
                    and not result.execution_blocked
                )
            )
            if can_continue:
                recovered = pending_replan(step.step)
                if recovered is not None and not result.blocked:
                    recovered.status = "recovered"
                    recovered.after_score = (0, 0, 0)
                    emit_replan("replan_recovered", recovered)
                if ctx.on_step_completed is not None:
                    ctx.on_step_completed(state, result)
                completed_set.add(step.step)
                index += 1
                continue

            if result.blocked:
                step_artifact = state.artifacts.get(step.step)
                rollback_to = (
                    step.rollback_target(
                        state,
                        step_artifact,
                        result.checks,
                    )
                    if (
                        ctx.ahe_enabled
                        and step_artifact is not None
                        and (
                            not ctx.artifact_first
                            or ctx.repair_release_issues
                        )
                    )
                    else None
                )
                current_failure_ids = {
                    failure.failure_id for failure in result.failures
                }
                rollback_artifact = (
                    state.artifacts.get(rollback_to)
                    if rollback_to is not None
                    else None
                )
                replan_baseline_fingerprint = _artifact_fingerprint(
                    rollback_artifact
                )
                prior = [
                    record
                    for record in state.replan_history
                    if record.trigger_step == step.step.value
                    and (
                        record.baseline_fingerprint
                        == replan_baseline_fingerprint
                    )
                    and bool(
                        current_failure_ids.intersection(record.failure_ids)
                    )
                ]
                current_score = score(result)
                replan_limit = max(0, ctx.max_replan_attempts)
                if rollback_to is not None and ctx.replan_score is not None:
                    learned_score = ctx.replan_score(
                        step.step.value,
                        rollback_to.value,
                    )
                    if learned_score is not None and learned_score >= 0.8:
                        replan_limit = min(5, replan_limit + 1)
                    elif learned_score is not None and learned_score < 0.2:
                        replan_limit = min(replan_limit, 1)
                can_replan = (
                    rollback_to is not None
                    and _ORDER_INDEX[rollback_to] < _ORDER_INDEX[step.step]
                    and len(prior) < replan_limit
                )
                if can_replan and rollback_to is not None:
                    feedback = (
                        f"Downstream {step.step.value} checks failed. Replan from "
                        f"{rollback_to.value} without weakening any requirement:\n"
                        + "\n".join(
                            f"- {check.name}: {check.message}"
                            for check in result.error_checks
                        )
                    )[:12_000]
                    record = ReplanRecord(
                        trigger_step=step.step.value,
                        rollback_to=rollback_to.value,
                        attempt=len(prior) + 1,
                        failure_ids=[
                            failure.failure_id for failure in result.failures
                        ],
                        status="scheduled",
                        before_score=current_score,
                        feedback=feedback,
                        baseline_fingerprint=replan_baseline_fingerprint,
                    )
                    state.replan_history.append(record)
                    state.revision += 1
                    failed_signatures = {
                        failure.signature for failure in result.failures
                    }
                    resolved_gaps = [
                        gap
                        for gap in state.capability_gaps
                        if gap.signature in failed_signatures
                    ]
                    state.capability_gaps = [
                        gap
                        for gap in state.capability_gaps
                        if gap.signature not in failed_signatures
                    ]
                    if ctx.on_ahe_event is not None:
                        for gap in resolved_gaps:
                            try:
                                ctx.on_ahe_event(
                                    ahe_event(
                                        "capability_gap_resolved",
                                        step=step.step.value,
                                        revision=state.revision,
                                        gap=gap,
                                    )
                                )
                            except Exception:  # noqa: BLE001
                                continue
                    rollback_index = _ORDER_INDEX[rollback_to]
                    rollback_result = next(
                        (
                            existing
                            for existing in reversed(state.results)
                            if existing.step == rollback_to
                        ),
                        None,
                    )
                    if rollback_artifact is not None:
                        state.resume_candidates[rollback_to] = (
                            rollback_artifact,
                            bool(
                                rollback_result.used_llm
                                if rollback_result is not None
                                else False
                            ),
                        )
                    state.resume_candidates = {
                        candidate_step: candidate
                        for candidate_step, candidate in state.resume_candidates.items()
                        if _ORDER_INDEX[candidate_step] <= rollback_index
                    }
                    state.results = [
                        existing
                        for existing in state.results
                        if _ORDER_INDEX[existing.step] < rollback_index
                    ]
                    state.artifacts = {
                        artifact_step: artifact
                        for artifact_step, artifact in state.artifacts.items()
                        if _ORDER_INDEX[artifact_step] < rollback_index
                    }
                    completed_set = set(state.completed)
                    emit_replan("replan_scheduled", record)
                    if ctx.on_step_completed is not None:
                        ctx.on_step_completed(state, result)
                    index = rollback_index
                    continue

                pending = pending_replan(step.step)
                if pending is not None:
                    pending.status = (
                        "exhausted"
                        if len(prior) >= replan_limit
                        else "stagnated"
                    )
                    pending.after_score = current_score
                    emit_replan(f"replan_{pending.status}", pending)
                known_gaps = {
                    gap.signature: gap for gap in state.capability_gaps
                }
                for failure in result.failures:
                    if failure.recoverability == Recoverability.HARD_CONFLICT:
                        continue
                    gap = make_capability_gap(failure)
                    existing = known_gaps.get(gap.signature)
                    if existing is None:
                        state.capability_gaps.append(gap)
                        known_gaps[gap.signature] = gap
                        if ctx.on_ahe_event is not None:
                            try:
                                ctx.on_ahe_event(
                                    ahe_event(
                                        "capability_gap",
                                        step=step.step.value,
                                        revision=state.revision,
                                        failure=failure,
                                    )
                                )
                            except Exception:  # noqa: BLE001
                                pass
                    else:
                        existing.message = gap.message
                        existing.category = gap.category
                        existing.required_capability = gap.required_capability
                if ctx.on_step_completed is not None:
                    ctx.on_step_completed(state, result)
                break  # fail closed after bounded local and upstream repair
        return state
