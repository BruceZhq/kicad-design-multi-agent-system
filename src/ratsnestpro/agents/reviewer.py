"""Independent Reviewer: turns a deterministic VerificationReport into a
human-readable Markdown review, optionally with an EricAI narrative and finding
triage.

The Reviewer never changes the blocking decision — that stays with the
deterministic gates. The LLM may only add a narrative and *advisory* triage
(suspected false positive / priority). Finding order, IDs, and severities are
read from the original findings, never from the model, so the LLM cannot
downgrade a real error.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from ratsnestpro.agents.llm import (
    LLMClient,
    LlmError,
    LlmMode,
    parse_mode,
    resolve_client,
)
from ratsnestpro.domain.contracts import Finding, Severity, VerificationReport

_SYSTEM = (
    "You are the Independent Reviewer for a PCB design agent. You are given the "
    "deterministic verification findings as JSON. Write a concise engineering "
    "review narrative and triage each finding. You MUST NOT change severities or "
    "invent findings; the deterministic gates are authoritative. Do not state a "
    "verdict, pass/fail status, release readiness, or manufacturing approval. "
    "Only explain the supplied findings and suggest corrective actions. Respond with "
    'STRICT JSON only: {"narrative": str, "triage": [{"finding_id": str, '
    '"suspected_false_positive": bool, "priority": "high"|"medium"|"low", '
    '"note": str}]}'
)

_AUTHORITY_CLAIM_RE = re.compile(
    r"(?im)"
    r"\b(?:verdict|overall\s+status|release\s+status)\b"
    r"|(?:^|\n)\s*(?:#{1,6}\s*)?(?:\*{1,2})?"
    r"conclusion\b[^\n]*(?:pass|fail|blocked|approved|ready)"
    r"|(?:^|\n)\s*(?:\*{1,2})?"
    r"(?:PASS|BLOCKED|FAIL(?:ED)?|APPROVED)"
    r"(?:\*{1,2})?[.!]?\s*(?:$|\n)"
    r"|\b(?:release[- ]ready|ready\s+for\s+manufactur(?:e|ing)|"
    r"ready\s+to\s+manufacture|production[- ]ready|"
    r"approved\s+for\s+manufactur(?:e|ing)|"
    r"safe\s+to\s+(?:fabricate|manufacture)|"
    r"no\s+(?:release\s+)?blockers?)\b"
    r"|\b(?:design|board|project)\s+(?:is|has\s+been)\s+"
    r"(?:blocked|approved|passed|release[- ]ready)\b"
    r"|\b(?:all\s+)?(?:deterministic\s+)?(?:gates|checks)\s+"
    r"(?:have\s+)?pass(?:ed)?\b"
    r"|\b(?:looks?|appears?)\s+(?:fine|good|acceptable|compliant)\b"
    r"|\b(?:fully\s+)?compliant\b"
)


@dataclass
class TriageItem:
    finding_id: str
    rule_id: str
    severity: str
    suspected_false_positive: bool
    priority: str
    note: str = ""


@dataclass
class ReviewResult:
    report: VerificationReport
    narrative: str
    triage: list[TriageItem] = field(default_factory=list)
    source: str = "deterministic"

    @property
    def blocked(self) -> bool:
        return self.report.blocked

    @property
    def markdown(self) -> str:
        return _render_markdown(self)

    @property
    def advisory_markdown(self) -> str:
        return _render_advisory_markdown(self)


def _priority_for(sev: Severity) -> str:
    return {"error": "high", "warning": "medium", "info": "low"}[sev.value]


def _deterministic_triage(findings: list[Finding]) -> list[TriageItem]:
    return [
        TriageItem(
            finding_id=f.finding_id,
            rule_id=f.rule_id,
            severity=f.severity.value,
            suspected_false_positive=False,
            priority=_priority_for(f.severity),
            note="",
        )
        for f in findings
    ]


def _deterministic_narrative(report: VerificationReport) -> str:
    findings = report.findings
    if not findings:
        return "All deterministic gates passed with no findings."
    errors = sum(1 for f in findings if f.severity == Severity.ERROR)
    warns = sum(1 for f in findings if f.severity == Severity.WARNING)
    return (
        f"Deterministic verification produced {errors} error(s) and "
        f"{warns} warning(s) across "
        f"{len(report.gates)} gates."
    )


def _parse_json_block(raw: str) -> dict:
    s = raw.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\n?", "", s)
        s = re.sub(r"\n?```$", "", s).strip()
    start, end = s.find("{"), s.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object in model output")
    return json.loads(s[start : end + 1])


class Reviewer:
    def review(
        self,
        report: VerificationReport,
        mode: str | LlmMode = LlmMode.OFFLINE,
        client: LLMClient | None = None,
        kb: object | None = None,
    ) -> ReviewResult:
        mode = parse_mode(mode)
        findings = report.findings

        # No findings → deterministic empty review; never call the model.
        if not findings:
            return ReviewResult(
                report=report,
                narrative=_deterministic_narrative(report),
                triage=[],
                source="deterministic",
            )

        resolved = resolve_client(mode, client)  # raises in REQUIRED if unavailable
        if resolved is None:
            return ReviewResult(
                report=report,
                narrative=_deterministic_narrative(report),
                triage=_deterministic_triage(findings),
                source="deterministic",
            )

        try:
            return self._live(report, findings, resolved, kb)
        except (LlmError, ValueError, KeyError, TypeError) as exc:
            if mode == LlmMode.REQUIRED:
                raise LlmError(f"required EricAI review failed: {exc}") from exc
            return ReviewResult(
                report=report,
                narrative=_deterministic_narrative(report),
                triage=_deterministic_triage(findings),
                source="deterministic",
            )

    def _live(
        self,
        report: VerificationReport,
        findings: list[Finding],
        client: LLMClient,
        kb: object | None = None,
    ) -> ReviewResult:
        system = _SYSTEM
        if kb is not None and hasattr(kb, "retrieve_text"):
            rule_ids = " ".join(f.rule_id for f in findings)
            context = kb.retrieve_text(f"review findings {rule_ids}", top_k=3, role="reviewer")
            if context:
                system = f"{_SYSTEM}\n\nReference knowledge (advisory):\n{context}"
        payload = json.dumps(
            [
                {
                    "finding_id": f.finding_id,
                    "rule_id": f.rule_id,
                    "severity": f.severity.value,
                    "summary": f.summary,
                }
                for f in findings
            ]
        )
        raw = client.complete(system, payload)
        data = _parse_json_block(raw)
        narrative = str(data.get("narrative", "")).strip() or _deterministic_narrative(report)
        raw_triage = data.get("triage", []) or []
        advisory_text = [
            narrative,
            *[
                str(item.get("note", ""))
                for item in raw_triage
                if isinstance(item, dict)
            ],
        ]
        if any(_AUTHORITY_CLAIM_RE.search(text) for text in advisory_text):
            raise ValueError("LLM advisory attempted to publish a review verdict")

        # Build triage strictly from original findings; overlay only advisory
        # fields the model is allowed to set. Severity is taken from findings.
        by_id = {f.finding_id: f for f in findings}
        llm_triage = {
            str(t.get("finding_id")): t
            for t in raw_triage
            if isinstance(t, dict)
        }
        triage: list[TriageItem] = []
        for f in findings:  # preserve original order
            adv = llm_triage.get(f.finding_id, {})
            priority = str(adv.get("priority", _priority_for(f.severity)))
            if priority not in ("high", "medium", "low"):
                priority = _priority_for(f.severity)
            triage.append(
                TriageItem(
                    finding_id=f.finding_id,
                    rule_id=f.rule_id,
                    severity=f.severity.value,  # authoritative, from the finding
                    suspected_false_positive=bool(adv.get("suspected_false_positive", False)),
                    priority=priority,
                    note=str(adv.get("note", "")),
                )
            )
        assert by_id  # findings non-empty here
        return ReviewResult(report=report, narrative=narrative, triage=triage, source="ericai")


def _render_markdown(result: ReviewResult) -> str:
    report = result.report
    lines: list[str] = []
    verdict = "BLOCKED" if report.blocked else "PASS (deterministic gates)"
    lines.append("# Design Review")
    lines.append("")
    lines.append(f"**Verdict:** {verdict}  ")
    lines.append(f"**Review source:** {result.source}")
    lines.append("")
    lines.append(result.narrative)
    lines.append("")

    # Gate basis
    lines.append("## Gate basis")
    lines.append("")
    lines.append("| Gate | Status | Required |")
    lines.append("| --- | --- | --- |")
    for g in report.gates:
        lines.append(f"| {g.gate} | {g.status.value} | {'yes' if g.required else 'no'} |")
    lines.append("")

    # Blockers
    blockers = [f for f in report.findings if f.severity == Severity.ERROR]
    lines.append("## Blockers")
    lines.append("")
    if blockers:
        for f in blockers:
            refs = ", ".join(f.component_refs) if f.component_refs else "-"
            lines.append(f"- **{f.rule_id}** ({refs}): {f.summary}")
    else:
        lines.append("None.")
    lines.append("")

    # All findings + triage
    lines.append("## Findings & triage")
    lines.append("")
    if result.triage:
        lines.append("| Rule | Severity | Priority | Suspected FP | Note |")
        lines.append("| --- | --- | --- | --- | --- |")
        for t in result.triage:
            fp = "yes" if t.suspected_false_positive else "no"
            lines.append(
                f"| {t.rule_id} | {t.severity} | {t.priority} | {fp} | {t.note or '-'} |"
            )
    else:
        lines.append("No findings.")
    lines.append("")

    # Unavailable / skipped
    unavailable = [g for g in report.gates if g.status.value == "unavailable"]
    lines.append("## Unavailable / skipped")
    lines.append("")
    if unavailable:
        for g in unavailable:
            lines.append(f"- {g.gate}: {g.summary}")
    else:
        lines.append("None.")
    lines.append("")
    return "\n".join(lines)


def _render_advisory_markdown(result: ReviewResult) -> str:
    """Render reviewer explanation without a second release verdict."""

    lines = [
        "## Reviewer advisory analysis",
        "",
        f"**Advisory source:** {result.source}",
        "",
        (
            "This section may explain findings and suggest corrective actions. "
            "It cannot set or override the authoritative verdict above."
        ),
        "",
        result.narrative,
        "",
        "### Deterministic project-audit gates",
        "",
        "| Gate | Status | Required |",
        "| --- | --- | --- |",
    ]
    for gate in result.report.gates:
        lines.append(
            f"| {gate.gate} | {gate.status.value} | "
            f"{'yes' if gate.required else 'no'} |"
        )
    lines.extend(
        [
            "",
            "### Finding triage",
            "",
        ]
    )
    if result.triage:
        lines.extend(
            [
                "| Rule | Severity | Priority | Suspected FP | Note |",
                "| --- | --- | --- | --- | --- |",
            ]
        )
        for item in result.triage:
            suspected = "yes" if item.suspected_false_positive else "no"
            lines.append(
                f"| {item.rule_id} | {item.severity} | {item.priority} | "
                f"{suspected} | {item.note or '-'} |"
            )
    else:
        lines.append("No findings.")
    lines.append("")
    return "\n".join(lines)
