"""Shared design pipeline — one code path for the CLI and the Kafka worker.

generate_for_backend()  requirement -> DesignSpec -> KiCad project via the
                        selected backend (template | crew | mcp)
finalize_outputs()      after the repair loop: headless SVG previews, the
                        markdown report, and the release zip

Keeping this in one place means `python -m ratsnest design ... --backend X`
and a queued cluster job produce byte-identical deliverables.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from ratsnest.config import Config
from ratsnest.data_proxy import Recorder
from ratsnest.design_gen import parse_requirement
from ratsnest.preview import generate_previews
from ratsnest.protocols import DesignBackend, LlmBrain
from ratsnest.reporting import write_report
from ratsnest.schemas import DesignSpec, EvaluationResult, RunRecord, StrategyBundle


def evaluate_for_release(project_dir: Path, strategy: StrategyBundle,
                         config: Config) -> EvaluationResult:
    """Re-run analyzers and every production gate for final artifacts."""
    from ratsnest.agents import synthesize
    from ratsnest.kh_adapter import KicadHappyAdapter
    from ratsnest.verification import verify_production

    project_dir = Path(project_dir)
    gates, gate_findings = verify_production(project_dir, config)
    erc_gate = gates.get("erc")
    return synthesize(
        KicadHappyAdapter(config).analyze_all(project_dir), strategy,
        project_dir, erc_passed=(erc_gate.passed if erc_gate else None),
        gate_results=gates, additional_findings=gate_findings)


def _template_backend(config, recorder, llm) -> DesignBackend:
    from ratsnest.design_gen.generator import TemplateBackend
    return TemplateBackend(config)


def _crew_backend(config, recorder, llm) -> DesignBackend:
    from ratsnest.crews import CreatorCrew
    return CreatorCrew(config, recorder, llm=llm)


def _mcp_backend(config, recorder, llm) -> DesignBackend:
    from ratsnest.mcp_exec import KiCadMcpBackend
    return KiCadMcpBackend(config, recorder)


# registry: adding a backend = one entry here (imports stay lazy)
BACKEND_FACTORIES = {
    "template": _template_backend,
    "crew": _crew_backend,
    "mcp": _mcp_backend,
}
VALID_BACKENDS = tuple(BACKEND_FACTORIES)


def generate_for_backend(requirement: str, out_dir: Path, backend: str,
                         strategy: StrategyBundle, config: Config,
                         recorder: Recorder | None = None,
                         llm: LlmBrain | None = None) -> DesignSpec:
    """Parse the requirement and build the project with the chosen backend.

    Brain-first: the Requirement Understanding Agent (LLM) interprets the
    text when available; the deterministic extractor is the fallback. Either
    way the result is the same typed DesignSpec contract.
    """
    backend = (backend or "template").lower()
    if backend not in VALID_BACKENDS:
        raise ValueError(f"backend must be one of {VALID_BACKENDS}, got {backend!r}")

    if llm is None:
        from ratsnest.llm import LlmClient
        llm = LlmClient(config, recorder)
    spec = None
    if llm is not None:
        from ratsnest.design_gen.requirement_agent import parse_requirement_llm
        spec = parse_requirement_llm(requirement, llm)
    brain = "llm" if spec is not None else "deterministic"
    if spec is None:
        spec = parse_requirement(requirement)
    if recorder is not None:
        recorder.emit("requirement_agent", 0,
                      observation={"requirement": requirement[:300]},
                      agent_state={"brain": brain},
                      action={"spec": spec.model_dump(mode="json")},
                      outcome={"ok": True},
                      metadata={"agent": "requirement_agent", "crew": "creator"})
    out_dir = Path(out_dir)

    BACKEND_FACTORIES[backend](config, recorder, llm).generate(
        spec, out_dir, strategy)
    return spec


def finalize_outputs(project_dir: Path, evaluation: EvaluationResult,
                     record: RunRecord | None, spec: DesignSpec | None,
                     config: Config) -> dict[str, Path]:
    """Produce the end-of-process deliverables. Returns paths that exist."""
    project_dir = Path(project_dir)
    out: dict[str, Path] = {}

    previews = generate_previews(project_dir, config)   # feature-gated
    out.update({f"preview_{k}": v for k, v in previews.items()})

    report = write_report(project_dir / "ratsnest_report.md",
                          evaluation, record, spec)
    out["report"] = report

    stem = (spec.project_name if spec else project_dir.name)
    release = Path(shutil.make_archive(
        str(project_dir.parent / f"{stem}_release"), "zip", project_dir))
    out["release"] = release
    return out
