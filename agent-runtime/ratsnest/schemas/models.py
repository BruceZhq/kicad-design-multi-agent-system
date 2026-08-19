"""Contract models — the load-bearing wall between all RatsNest components.

These are the source of truth; `export.py` emits JSON Schema for the Java
control plane's codegen. Changing anything here requires a CONTRACT_VERSION
bump and a matching schema re-export.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

CONTRACT_VERSION = "0.2.0"

# kicad-happy v1.3 severity vocabulary (finding_schema.VALID_SEVERITIES)
SEVERITIES = ("error", "warning", "info")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


# ---------------------------------------------------------------------------
# Evaluation side
# ---------------------------------------------------------------------------

class Finding(BaseModel):
    """Passthrough envelope for a kicad-happy v1.3 finding.

    extra="allow" — the analyzers own this schema; we never strip fields.
    """

    model_config = ConfigDict(extra="allow")

    detector: str = ""
    rule_id: Optional[str] = None
    severity: str = "warning"
    confidence: Optional[str] = None
    recommendation: Optional[str] = None

    def finding_id(self) -> str:
        """Stable identity for dedupe/traceability: rule/detector + components."""
        comps = self.components_involved()
        key = self.rule_id or self.detector or "unknown"
        return f"{key}:{','.join(comps) if comps else 'global'}"

    def components_involved(self) -> list[str]:
        """Best-effort extraction of reference designators from the envelope."""
        extra = self.model_extra or {}
        refs: list[str] = []
        for field in ("components", "refs", "references"):
            val = extra.get(field)
            if isinstance(val, list):
                for v in val:
                    if isinstance(v, str):
                        refs.append(v)
                    elif isinstance(v, dict) and "ref" in v:
                        refs.append(str(v["ref"]))
        for field in ("component", "ref", "reference"):
            val = extra.get(field)
            if isinstance(val, str):
                refs.append(val)
        # report_context often carries a dict with refs
        rc = extra.get("report_context")
        if isinstance(rc, dict):
            for field in ("component", "ref"):
                if isinstance(rc.get(field), str):
                    refs.append(rc[field])
        seen: set[str] = set()
        out = []
        for r in refs:
            if r not in seen:
                seen.add(r)
                out.append(r)
        return out


class AnalyzerOutput(BaseModel):
    """Harmonized kicad-happy analyzer envelope (v1.3)."""

    model_config = ConfigDict(extra="allow")

    analyzer_type: str = ""
    schema_version: str = ""
    summary: dict[str, Any] = Field(default_factory=dict)
    findings: list[Finding] = Field(default_factory=list)
    trust_summary: Optional[dict[str, Any]] = None


class GateStatus(str, Enum):
    passed = "passed"
    failed = "failed"
    unavailable = "unavailable"
    error = "error"


class VerificationGate(BaseModel):
    """One independently reproducible engineering release gate."""

    name: str
    status: GateStatus
    required: bool = True
    summary: str = ""
    tool: str = ""
    evidence: list[str] = Field(default_factory=list)
    metrics: dict[str, Any] = Field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.status == GateStatus.passed


class Scorecard(BaseModel):
    score: float
    max_score: float = 100.0
    severity_counts: dict[str, int] = Field(default_factory=dict)
    deductions: dict[str, float] = Field(default_factory=dict)
    erc_passed: Optional[bool] = None  # None = ERC not run (kicad-cli absent)
    findings_total: int = 0
    suppressed_total: int = 0
    gate_results: dict[str, VerificationGate] = Field(default_factory=dict)
    required_gates_passed: bool = False
    strategy_version_id: str = ""
    created_at: str = Field(default_factory=_now_iso)


class EvaluationResult(BaseModel):
    project_dir: str
    scorecard: Scorecard
    findings: list[Finding] = Field(default_factory=list)
    analyzer_outputs: dict[str, AnalyzerOutput] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Repair side
# ---------------------------------------------------------------------------

class RepairOpType(str, Enum):
    set_value = "set_value"          # change a component's Value property
    set_property = "set_property"    # set/add any named property (MPN, etc.)
    add_component = "add_component"  # reserved: not implemented in editor v1
    remove_component = "remove_component"  # reserved: not implemented in editor v1


class RepairOp(BaseModel):
    op: RepairOpType
    ref: str
    params: dict[str, str] = Field(default_factory=dict)
    finding_id: str = ""  # traceability back to the originating finding


class RepairHint(BaseModel):
    finding_id: str
    rule_id: Optional[str] = None
    severity: str = "warning"
    repair_type: str = ""
    targets: list[str] = Field(default_factory=list)
    suggested_ops: list[RepairOp] = Field(default_factory=list)
    confidence: str = "heuristic"
    explanation: str = ""


class PatchPlan(BaseModel):
    plan_id: str = Field(default_factory=lambda: _new_id("plan"))
    run_id: str = ""
    iteration: int = 0
    ops: list[RepairOp] = Field(default_factory=list)
    strategy_version_id: str = ""
    rationale: dict[str, str] = Field(default_factory=dict)  # finding_id -> why


class PatchResult(BaseModel):
    plan_id: str
    applied: bool
    changed_files: dict[str, dict[str, str]] = Field(default_factory=dict)
    # {path: {"before": sha256, "after": sha256}}
    change_log: list[dict[str, Any]] = Field(default_factory=list)
    error: Optional[str] = None
    rolled_back: bool = False


# ---------------------------------------------------------------------------
# ATDP trajectory (paper [1] §3: e_t = <o, h, a, y, r, m>)
# ---------------------------------------------------------------------------

class TrajectoryEvent(BaseModel):
    event_id: str = Field(default_factory=lambda: _new_id("evt"))
    run_id: str
    iteration: int = 0
    step: int = 0
    node: str  # orchestrator node that produced this event
    observation: dict[str, Any] = Field(default_factory=dict)   # o_t
    agent_state: dict[str, Any] = Field(default_factory=dict)   # h_t
    action: dict[str, Any] = Field(default_factory=dict)        # a_t
    outcome: dict[str, Any] = Field(default_factory=dict)       # y_t
    reward: Optional[float] = None                              # r_t (late-bound)
    metadata: dict[str, Any] = Field(default_factory=dict)      # m_t
    ts: str = Field(default_factory=_now_iso)


# ---------------------------------------------------------------------------
# Design generation (user requirement -> KiCad project)
# ---------------------------------------------------------------------------

class DesignSpec(BaseModel):
    """Validated requirement for the explicitly supported power-board families."""

    model_config = ConfigDict(extra="forbid")

    project_name: str = "generated_board"
    input_voltage: float = Field(default=12.0, gt=0, le=60)
    output_voltage: float = Field(default=5.0, gt=0, le=60)
    output_current_a: float = Field(default=0.5, gt=0, le=10)
    led: Optional[str] = "red"  # LED color, or None for no indicator
    topology: Literal["auto", "ldo", "buck"] = "auto"
    ambient_temperature_c: float = Field(default=25.0, ge=-40, le=85)
    max_output_ripple_mv: float = Field(default=100.0, gt=0, le=1000)
    unsupported_features: list[str] = Field(default_factory=list, max_length=20)
    requirement_text: str = ""


# ---------------------------------------------------------------------------
# Run lifecycle
# ---------------------------------------------------------------------------

class RunConfig(BaseModel):
    project_dir: str
    max_iterations: int = 4
    fix_policy: str = "auto"  # "auto" | "suggest_only"
    strategy_version_id: Optional[str] = None  # None = active strategy
    run_erc: bool = True  # only honored if kicad-cli is available


class IterationRecord(BaseModel):
    iteration: int
    scorecard: Scorecard
    patch_plan: Optional[PatchPlan] = None
    patch_result: Optional[PatchResult] = None
    new_error_findings: list[str] = Field(default_factory=list)  # finding_ids
    resolved_findings: list[str] = Field(default_factory=list)
    score_delta: float = 0.0


class RunRecord(BaseModel):
    run_id: str = Field(default_factory=lambda: _new_id("run"))
    config: RunConfig
    strategy_version_id: str = ""
    status: str = "created"  # created|running|converged|escalated|failed
    iterations: list[IterationRecord] = Field(default_factory=list)
    escalation: Optional[dict[str, Any]] = None
    started_at: str = Field(default_factory=_now_iso)
    finished_at: Optional[str] = None
    contract_version: str = CONTRACT_VERSION


# ---------------------------------------------------------------------------
# Strategy (the evolvable assets) + experiments
# ---------------------------------------------------------------------------

class RepairMapping(BaseModel):
    """One evolvable finding→repair rule."""

    match_rule_id: Optional[str] = None      # exact rule_id match
    match_detector: Optional[str] = None     # or detector name match
    repair_type: str                          # solver name in repair_planner
    params: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True


class SuppressionRule(BaseModel):
    rule_id: Optional[str] = None
    detector: Optional[str] = None
    ref: Optional[str] = None
    reason: str = ""


class StrategyBundle(BaseModel):
    """A complete versioned strategy. version_id = content hash."""

    name: str = "unnamed"
    scorecard_weights: dict[str, float] = Field(
        default_factory=lambda: {"error": 30.0, "warning": 3.0, "info": 0.0,
                                 "erc_fail": 15.0}
    )
    repair_mappings: list[RepairMapping] = Field(default_factory=list)
    suppressions: list[SuppressionRule] = Field(default_factory=list)
    prompts: dict[str, str] = Field(default_factory=dict)  # LLM mode only
    solver_params: dict[str, Any] = Field(default_factory=dict)

    def version_id(self) -> str:
        canonical = json.dumps(
            self.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        )
        return "strat_" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


class ExperimentReport(BaseModel):
    experiment_id: str = Field(default_factory=lambda: _new_id("exp"))
    candidate_version_id: str
    incumbent_version_id: str
    candidate_name: str = ""
    per_board: list[dict[str, Any]] = Field(default_factory=list)
    # each: {board, incumbent_score, candidate_score, new_errors, converged}
    mean_incumbent_score: float = 0.0
    mean_candidate_score: float = 0.0
    gates: dict[str, bool] = Field(default_factory=dict)
    gate_reasons: dict[str, str] = Field(default_factory=dict)
    promoted: bool = False
    created_at: str = Field(default_factory=_now_iso)
