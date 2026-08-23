"""Standalone review of an existing KiCad project.

Reuses the vendored kicad-happy-style audits (which operate directly on a
parsed ``.kicad_sch`` / ``.kicad_pcb``) to produce Findings, groups them into
gates, and hands the report to the Reviewer for a Markdown write-up. This is
the review half of the agent — independent of generation — and works on any
KiCad project, not just RatsNestPro output.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path
from time import monotonic

from observability import operation_span, record_release_gate
from ratsnestpro.agents import LLMClient, LlmMode, Reviewer, ReviewResult
from ratsnestpro.domain.contracts import (
    Finding,
    GateResult,
    GateStatus,
    Severity,
    Stage,
    VerificationReport,
)
from ratsnestpro.eda.vendor import review as vreview
from ratsnestpro.eda.vendor.pcb import PcbBoard
from ratsnestpro.eda.vendor.schematic import Schematic


class ReviewProjectError(RuntimeError):
    pass


@dataclass
class ProjectReview:
    project_path: Path
    schematic_path: Path | None
    pcb_path: Path | None
    result: ReviewResult

    @property
    def markdown(self) -> str:
        return self.result.markdown

    @property
    def advisory_markdown(self) -> str:
        return self.result.advisory_markdown

    @property
    def blocked(self) -> bool:
        return self.result.blocked


def _find(path: Path, suffix: str) -> Path | None:
    if path.is_file():
        if path.suffix == suffix:
            return path
        sibling = path.with_suffix(suffix)
        return sibling if sibling.is_file() else None
    if path.is_dir():
        hits = sorted(path.glob(f"*{suffix}"))
        return hits[0] if hits else None
    return None


def _severity(value: str) -> Severity:
    v = value.lower()
    if v == "error":
        return Severity.ERROR
    if v == "info":
        return Severity.INFO
    return Severity.WARNING


def _to_findings(rule_prefix: str, audit: list[dict]) -> list[Finding]:
    out: list[Finding] = []
    for i, item in enumerate(audit):
        refs = []
        if item.get("reference"):
            refs = [str(item["reference"])]
        elif item.get("refs"):
            refs = [str(r) for r in item["refs"]]
        out.append(
            Finding(
                stage=Stage.REVIEW,
                severity=_severity(str(item.get("severity", "warning"))),
                rule_id=f"{rule_prefix}-{i + 1:03d}",
                summary=str(item.get("issue", item.get("summary", rule_prefix))),
                component_refs=refs,
            )
        )
    return out


def _gate(name: str, findings: list[Finding]) -> GateResult:
    status = (
        GateStatus.FAILED
        if any(f.severity == Severity.ERROR for f in findings)
        else GateStatus.PASSED
    )
    # Audits are advisory reviews (not release gates), so they are not required.
    return GateResult(
        gate=name, status=status, required=False,
        summary=f"{len(findings)} finding(s)", findings=findings,
    )


def _sidecar(
    project_path: Path,
    schematic_path: Path | None,
    suffix: str,
) -> Path | None:
    directory = (
        project_path
        if project_path.is_dir()
        else project_path.parent
    )
    stems = [
        path.stem
        for path in (schematic_path, project_path if project_path.is_file() else None)
        if path is not None
    ]
    for stem in dict.fromkeys(stems):
        candidate = directory / f"{stem}{suffix}"
        if candidate.is_file():
            return candidate
    matches = sorted(directory.glob(f"*{suffix}"))
    return matches[0] if len(matches) == 1 else None


def _evidence_strings(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    if isinstance(value, dict):
        evidence: list[str] = []
        kind = str(value.get("kind", "")).strip()
        if kind:
            evidence.append(f"kind={kind}")
        ids = value.get("ids", [])
        if isinstance(ids, list):
            evidence.extend(str(item) for item in ids if str(item).strip())
        return evidence
    text = str(value or "").strip()
    return [text] if text else []


_RELEASE_PROVEN_STATUSES = {
    "installed_exact",
    "installed_qualified_validated",
    "replaceable_grounded",
}
_COMPONENT_RELEASE_MANIFEST_SCHEMA = 2
_COMPONENT_RELEASE_POLICY = "explicit_component_closure_v1"


def _release_proof_problem(
    *,
    release_ready: bool,
    status: str,
    dnp: bool,
    unresolved: bool,
    symbol: str,
) -> str:
    if dnp or unresolved:
        return "component is DNP/unresolved"
    if symbol.startswith("RatsNestPlaceholder:"):
        return "component uses a nonrelease placeholder symbol"
    if not release_ready:
        return "release_ready is not explicitly true"
    if not status:
        return "component closure status is missing"
    if status not in _RELEASE_PROVEN_STATUSES:
        return f"resolution status {status!r} is not release-proven"
    return ""


def _release_finding(
    *,
    rule_id: str,
    ref: str,
    problem: str,
    source: Path | None,
    evidence: list[str] | None = None,
) -> Finding:
    return Finding(
        stage=Stage.REVIEW,
        severity=Severity.ERROR,
        rule_id=rule_id,
        summary=f"{ref} is not manufacturing-release eligible: {problem}",
        component_refs=[] if ref.startswith("<") else [ref],
        evidence=[
            *((str(source),) if source is not None else ()),
            *(evidence or []),
        ],
        repairable=True,
    )


def _component_release_gate(
    project_path: Path,
    schematic_path: Path | None,
    schematic: Schematic | None,
) -> GateResult:
    """Rebuild RatsNest component-release state from durable project files."""

    findings_by_ref: dict[str, Finding] = {}
    manifest_findings: list[Finding] = []
    evidence_available = False
    manifest_refs: set[str] | None = None
    bom_refs: set[str] | None = None
    manifest_path = _sidecar(
        project_path,
        schematic_path,
        "_unresolved_components.json",
    )
    if manifest_path is not None:
        evidence_available = True
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            if (
                payload.get("schema_version")
                != _COMPONENT_RELEASE_MANIFEST_SCHEMA
            ):
                manifest_findings.append(
                    _release_finding(
                        rule_id="RELEASE-MANIFEST-SCHEMA",
                        ref="<manifest>",
                        problem=(
                            "component-release manifest schema is missing or "
                            "obsolete; explicit schema-v2 proof is required"
                        ),
                        source=manifest_path,
                    )
                )
            if payload.get("release_policy") != _COMPONENT_RELEASE_POLICY:
                manifest_findings.append(
                    _release_finding(
                        rule_id="RELEASE-MANIFEST-POLICY",
                        ref="<manifest>",
                        problem=(
                            "component-release policy is missing or "
                            "unrecognized"
                        ),
                        source=manifest_path,
                    )
                )
            proofs = payload.get("component_release_proofs")
            if not isinstance(proofs, list) or not proofs:
                manifest_findings.append(
                    _release_finding(
                        rule_id="RELEASE-MANIFEST-PROOFS",
                        ref="<manifest>",
                        problem=(
                            "manifest contains no per-component positive "
                            "release proofs"
                        ),
                        source=manifest_path,
                    )
                )
                proofs = []
            manifest_refs = set()
            proven_count = 0
            for index, proof in enumerate(proofs, start=1):
                if not isinstance(proof, dict):
                    raise TypeError(
                        "component release proof entries must be objects"
                    )
                ref = str(proof.get("ref", "")).strip()
                if not ref:
                    ref = f"<proof-{index}>"
                if ref in manifest_refs:
                    manifest_findings.append(
                        _release_finding(
                            rule_id=f"RELEASE-PROOF-DUP-{index:03d}",
                            ref=ref,
                            problem="manifest contains a duplicate proof",
                            source=manifest_path,
                        )
                    )
                manifest_refs.add(ref)
                status = str(proof.get("status", "")).strip()
                problem = _release_proof_problem(
                    release_ready=proof.get("release_ready") is True,
                    status=status,
                    dnp=proof.get("dnp") is True,
                    unresolved=proof.get("unresolved") is True,
                    symbol=str(proof.get("symbol", "")).strip(),
                )
                if problem:
                    findings_by_ref.setdefault(
                        ref,
                        _release_finding(
                            rule_id=f"RELEASE-PROOF-{index:03d}",
                            ref=ref,
                            problem=problem,
                            source=manifest_path,
                        ),
                    )
                else:
                    proven_count += 1

            declared_count = payload.get("selection_component_count")
            if (
                not isinstance(declared_count, int)
                or declared_count <= 0
                or declared_count != len(proofs)
            ):
                manifest_findings.append(
                    _release_finding(
                        rule_id="RELEASE-MANIFEST-COUNT",
                        ref="<manifest>",
                        problem=(
                            "selection component count does not match the "
                            "per-component proofs"
                        ),
                        source=manifest_path,
                    )
                )
            if payload.get("release_proven_component_count") != proven_count:
                manifest_findings.append(
                    _release_finding(
                        rule_id="RELEASE-MANIFEST-PROVEN-COUNT",
                        ref="<manifest>",
                        problem=(
                            "release-proven component count is inconsistent "
                            "with the proof records"
                        ),
                        source=manifest_path,
                    )
                )
            components = payload.get("unresolved_components", [])
            if not isinstance(components, list):
                raise TypeError("unresolved_components must be a list")
            for index, item in enumerate(components, start=1):
                if not isinstance(item, dict):
                    raise TypeError("unresolved component entries must be objects")
                ref = str(item.get("ref", "")).strip() or f"entry-{index}"
                status = str(item.get("status", "unresolved")).strip()
                reason = str(item.get("reason", "")).strip()
                findings_by_ref[ref] = Finding(
                    stage=Stage.REVIEW,
                    severity=Severity.ERROR,
                    rule_id=f"RELEASE-COMP-{index:03d}",
                    summary=(
                        f"{ref} is not manufacturing-release eligible "
                        f"({status or 'unresolved'}): "
                        f"{reason or 'component closure is unresolved'}"
                    ),
                    component_refs=[ref],
                    evidence=[
                        str(manifest_path),
                        *_evidence_strings(item.get("evidence")),
                    ],
                    repairable=True,
                )
            manifest_claim_ready = payload.get("release_ready") is True
            manifest_computed_ready = bool(proofs) and proven_count == len(proofs)
            if manifest_claim_ready != manifest_computed_ready:
                manifest_findings.append(
                    _release_finding(
                        rule_id="RELEASE-MANIFEST-001",
                        ref="<manifest>",
                        problem=(
                            "declared release readiness is inconsistent with "
                            "the per-component proofs"
                        ),
                        source=manifest_path,
                    )
                )
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            manifest_findings.append(
                _release_finding(
                    rule_id="RELEASE-MANIFEST-INVALID",
                    ref="<manifest>",
                    problem=(
                        "component-release manifest cannot be validated: "
                        f"{type(exc).__name__}"
                    ),
                    source=manifest_path,
                )
            )

    if schematic is not None:
        for component in schematic.list_components():
            symbol = str(component.get("lib_id", "")).strip()
            ref = str(component.get("reference", "")).strip()
            if not symbol or not ref or ref.startswith("#"):
                continue
            properties = component.get("properties")
            if not isinstance(properties, dict):
                properties = {}
            status = str(properties.get("RatsNestStatus", "")).strip()
            release_value = str(
                properties.get("RatsNestReleaseReady", "")
            ).casefold()
            unresolved = (
                str(
                properties.get("RatsNestUnresolved", "")
                ).casefold() == "yes"
            )
            dnp = bool(component.get("dnp")) or (
                str(properties.get("DNP", "")).casefold() == "yes"
            )
            evidence_available = True
            problem = _release_proof_problem(
                release_ready=release_value == "yes",
                status=status,
                dnp=dnp,
                unresolved=unresolved,
                symbol=symbol,
            )
            if not problem:
                continue
            if ref in findings_by_ref:
                continue
            reason = str(
                properties.get("RatsNestResolutionDetail", "")
            ).strip()
            findings_by_ref[ref] = _release_finding(
                rule_id=f"RELEASE-SCH-{len(findings_by_ref) + 1:03d}",
                ref=ref,
                problem=reason or problem,
                source=schematic_path,
                evidence=_evidence_strings(
                    properties.get("RatsNestEvidenceIds", "")
                ),
            )

    bom_path = _sidecar(project_path, schematic_path, "_bom.csv")
    if bom_path is not None:
        evidence_available = True
        try:
            with bom_path.open(newline="", encoding="utf-8-sig") as handle:
                reader = csv.DictReader(handle)
                required_columns = {
                    "Reference",
                    "ReleaseReady",
                    "DNP",
                    "Resolution",
                }
                if not required_columns.issubset(reader.fieldnames or []):
                    manifest_findings.append(
                        _release_finding(
                            rule_id="RELEASE-BOM-SCHEMA",
                            ref="<bom>",
                            problem=(
                                "BOM lacks explicit component-release columns"
                            ),
                            source=bom_path,
                        )
                    )
                bom_refs = set()
                for row in reader:
                    ref = str(row.get("Reference", "")).strip() or "unknown"
                    bom_refs.add(ref)
                    resolution = str(row.get("Resolution", "")).strip()
                    dnp = str(row.get("DNP", "")).casefold() in {
                        "1",
                        "true",
                        "yes",
                    }
                    release_ready = str(
                        row.get("ReleaseReady", "")
                    ).casefold()
                    problem = _release_proof_problem(
                        release_ready=release_ready == "yes",
                        status=resolution,
                        dnp=dnp,
                        unresolved=False,
                        symbol="",
                    )
                    if not problem:
                        continue
                    if ref in findings_by_ref:
                        continue
                    findings_by_ref[ref] = _release_finding(
                        rule_id=f"RELEASE-BOM-{len(findings_by_ref) + 1:03d}",
                        ref=ref,
                        problem=problem,
                        source=bom_path,
                    )
        except (OSError, csv.Error):
            manifest_findings.append(
                _release_finding(
                    rule_id="RELEASE-BOM-INVALID",
                    ref="<bom>",
                    problem="BOM component-release proof cannot be read",
                    source=bom_path,
                )
            )

    if (
        manifest_refs is not None
        and bom_refs is not None
        and manifest_refs != bom_refs
    ):
        manifest_findings.append(
            _release_finding(
                rule_id="RELEASE-SOURCE-MISMATCH",
                ref="<release-proof>",
                problem=(
                    "BOM references and manifest component proofs do not match"
                ),
                source=manifest_path,
                evidence=[str(bom_path)] if bom_path is not None else [],
            )
        )

    findings = [*manifest_findings, *findings_by_ref.values()]
    if not evidence_available:
        findings.append(
            _release_finding(
                rule_id="RELEASE-PROOF-MISSING",
                ref="<project>",
                problem="no explicit component release proof was found",
                source=project_path,
            )
        )
    if findings:
        status = GateStatus.FAILED
        required = True
        summary = f"{len(findings)} nonrelease component finding(s)"
    else:
        status = GateStatus.PASSED
        required = True
        summary = "all physical components have explicit release proof"
    return GateResult(
        gate="component_release",
        status=status,
        required=required,
        summary=summary,
        findings=findings,
    )


def _review_project(
    project_path: str | Path,
    mode: str | LlmMode = LlmMode.OFFLINE,
    client: LLMClient | None = None,
    kb: object | None = None,
    decoupling_radius: float = 10.0,
) -> ProjectReview:
    path = Path(project_path)
    if not path.exists():
        raise ReviewProjectError(f"path does not exist: {path}")

    sch_path = _find(path, ".kicad_sch")
    pcb_path = _find(path, ".kicad_pcb")
    if sch_path is None and pcb_path is None:
        raise ReviewProjectError(f"no .kicad_sch or .kicad_pcb found under {path}")

    gates: list[GateResult] = []
    board = None
    if pcb_path is not None:
        try:
            board = PcbBoard.load(pcb_path)
        except Exception:
            board = None

    sch: Schematic | None = None
    if sch_path is not None:
        sch = Schematic.load(sch_path)
        conn = _to_findings("CONN", _safe(vreview.audit_connections, sch))
        dec = _to_findings(
            "DEC",
            _safe(vreview.audit_decoupling, sch, decoupling_radius, board),
        )
        pwr = _to_findings("PWR", _safe(vreview.audit_power_rails, sch))
        gates.append(_gate("connectivity", conn))
        gates.append(_gate("decoupling", dec))
        gates.append(_gate("power_rails", pwr))
        bom = _safe_dict(vreview.check_bom_health, sch)
        gates.append(_gate("bom", _bom_to_findings(bom)))

    gates.append(_component_release_gate(path, sch_path, sch))

    if pcb_path is not None:
        if board is not None:
            mfg = _to_findings("MFG", _safe(vreview.audit_manufacturing, board))
            gates.append(_gate("manufacturing", mfg))
        else:  # pragma: no cover - malformed/empty pcb
            gates.append(
                GateResult(
                    gate="manufacturing", status=GateStatus.UNAVAILABLE, required=False,
                    summary="PCB present but could not be analyzed",
                )
            )

    report = VerificationReport(gates=gates)
    result = Reviewer().review(report, mode=mode, client=client, kb=kb)
    return ProjectReview(
        project_path=path, schematic_path=sch_path, pcb_path=pcb_path, result=result
    )


def review_project(
    project_path: str | Path,
    mode: str | LlmMode = LlmMode.OFFLINE,
    client: LLMClient | None = None,
    kb: object | None = None,
    decoupling_radius: float = 10.0,
) -> ProjectReview:
    """Run deterministic review with release-gate telemetry and no project path."""

    started = monotonic()
    decision = "error"
    blocker_count = 0
    try:
        with operation_span("agent.release_gate.evaluate") as span:
            review = _review_project(
                project_path,
                mode=mode,
                client=client,
                kb=kb,
                decoupling_radius=decoupling_radius,
            )
            blocker_count = sum(
                finding.severity == Severity.ERROR
                for finding in review.result.report.findings
            )
            decision = "blocked" if review.blocked else "passed"
            span.set_attribute("agent.release_gate.decision", decision)
            span.set_attribute("agent.release_gate.blocker_count", blocker_count)
            span.set_attribute("agent.release_gate.duration_seconds", monotonic() - started)
            return review
    finally:
        record_release_gate(decision=decision, blocker_count=blocker_count)


def _safe(fn, *args) -> list[dict]:
    try:
        return list(fn(*args) or [])
    except Exception:  # pragma: no cover - defensive against odd inputs
        return []


def _safe_dict(fn, *args) -> dict:
    try:
        return dict(fn(*args) or {})
    except Exception:  # pragma: no cover
        return {}


def _bom_to_findings(bom: dict) -> list[Finding]:
    out: list[Finding] = []
    no_value = bom.get("no_value") or []
    if no_value:
        out.append(
            Finding(
                stage=Stage.REVIEW,
                severity=Severity.WARNING,
                rule_id="BOM-001",
                summary=f"{len(no_value)} component(s) missing a value",
                component_refs=[str(r) for r in no_value],
            )
        )
    return out
