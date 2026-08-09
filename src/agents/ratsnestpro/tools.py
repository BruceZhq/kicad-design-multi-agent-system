"""Typed tools that expose RatsNestPro workflows to LangGraph agents."""

from __future__ import annotations

import difflib
import fnmatch
import hashlib
import json
import os
import re
import subprocess
import unicodedata
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from time import monotonic
from typing import Any, Literal

from pydantic import ValidationError
from ratsnestpro.agents import Architect, LlmError, LlmMode, parse_mode
from ratsnestpro.eda import footprints, grounding, symbols
from ratsnestpro.eda.adapter import kicad_cli_available, run_erc
from ratsnestpro.eda.local_library import (
    GENERATED_LIBRARY_NICKNAME,
    generate_local_library,
    generate_local_symbol_library,
)
from ratsnestpro.families import Atmega328Params, expectations_for
from ratsnestpro.knowledge import build_default_kb
from ratsnestpro.orchestration import generate_design, review_project, run_repair
from ratsnestpro.orchestration.generate import build_design_plan
from ratsnestpro.orchestration.pipeline import (
    Pipeline,
    PipelineContext,
    PipelineState,
    PipelineStep,
    StructuredOutputError,
    restore_pipeline_state,
    selection_release_issues,
)
from ratsnestpro.orchestration.pipeline_contracts import (
    ErcSummary,
    ManufactureResult,
    MaterializeResult,
    PcbWriteResult,
    RouteResult,
    SelectionPlan,
)
from ratsnestpro.orchestration.placement_constraints import (
    review_pcb_placement_constraints,
)
from ratsnestpro.orchestration.review_project import ReviewProjectError
from ratsnestpro.parts import PartSelector

from agents.ratsnestpro.ehe_memory import EheMemory
from service.llm_output import (
    append_llm_output,
    llm_output_record,
    response_text,
    stream_llm_output_record,
)
from service.llm_output_stream import (
    LlmOutputRedisConfig,
    publish_llm_output_best_effort,
)

LlmModeName = Literal["offline", "auto", "required"]

_SAFE_NAME = re.compile(r"[^a-zA-Z0-9_.-]+")
_PIPELINE_STATE_SCHEMA_VERSION = 5
_ARCHITECT_EVIDENCE_MARKER = "GROUNDED ARCHITECT EVIDENCE"
_CHANGE_REQUEST_MARKERS = (
    "USER CHANGE REQUEST:",
    "INDEPENDENT REVIEW FEEDBACK TO REPAIR:",
)
_EVIDENCE_MARKERS = (
    "The Architect verified that",
    _ARCHITECT_EVIDENCE_MARKER,
)
_CONTINUATION_ONLY = re.compile(
    r"\b(?:continue|resume|retry|rerun|re-run|proceed)\b|"
    r"(?:继续|续跑|重试|重新运行|接着运行|从检查点)",
    re.IGNORECASE,
)
_HARDWARE_CHANGE_SIGNAL = re.compile(
    r"\d|"
    r"\b(?:mcu|soc|fpga|component|part|sensor|connector|interface|"
    r"usb|can|uart|spi|i2c|ethernet|power|supply|rail|voltage|current|"
    r"schematic|pcb|pin|net|trace|route|routing|via|impedance|layer|"
    r"outline|dimension|size|footprint|package|bom|gerber|erc|drc)\b|"
    r"(?:器件|元件|主控|芯片|传感器|连接器|接口|电源|电压|电流|"
    r"原理图|电路板|引脚|网络|走线|布线|过孔|阻抗|层数|板框|尺寸|"
    r"封装|物料|生产文件)",
    re.IGNORECASE,
)
_STRUCTURAL_CHANGE = re.compile(
    r"\b(?:add|remove|delete|include|introduce|enable|disable|change|switch)"
    r"\b.{0,60}\b(?:mcu|controller|sensor|connector|interface|port|"
    r"usb|can|uart|spi|i2c|ethernet|power|supply|rail|regulator)\b|"
    r"\b(?:power|supply|rail|regulator|voltage)\b.{0,60}"
    r"\b(?:add|remove|change|switch|new|different)\b|"
    r"(?:新增|增加|移除|删除|启用|禁用|改为|切换|变更).{0,30}"
    r"(?:主控|控制器|传感器|连接器|接口|端口|电源|电压|电源轨|稳压)",
    re.IGNORECASE,
)
_SELECTION_CHANGE = re.compile(
    r"\b(?:replace|substitute|swap|change)\b.{0,80}"
    r"(?:\b(?:part|component|device|model|mpn|footprint|package|bom)\b|"
    r"\b[A-Z]{1,3}\d+\b)|"
    r"(?:替换|更换|改用|选用).{0,40}(?:器件|元件|型号|料号|封装|"
    r"[A-Z]{1,3}\d+)",
    re.IGNORECASE,
)
_CONNECTION_CHANGE = re.compile(
    r"\b(?:connect|disconnect|wire|rewire|remap|pinout|pin|net)\b|"
    r"(?:连接|断开|改接|重映射|引脚|网络)",
    re.IGNORECASE,
)
_LAYOUT_CHANGE = re.compile(
    r"\b(?:board\s+outline|dimension|board\s+size|placement|keepout|"
    r"mounting\s+hole|layer\s+count|[24]-layer)\b|"
    r"(?:板框|外形|尺寸|布局|摆放|禁布区|安装孔|层数|叠层)",
    re.IGNORECASE,
)
_ROUTING_CHANGE = re.compile(
    r"\b(?:route|routing|trace|track|via|impedance|differential|"
    r"length\s+match|clearance|line\s+width)\b|"
    r"(?:布线|走线|过孔|阻抗|差分|等长|线宽|间距)",
    re.IGNORECASE,
)
_MANUFACTURE_CHANGE = re.compile(
    r"\b(?:gerber|drill|pick.?and.?place|cpl|manufactur(?:e|ing)|"
    r"fabrication\s+output)\b|"
    r"(?:生产文件|制造输出|钻孔文件|贴片坐标)",
    re.IGNORECASE,
)


def _json(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def _contract_clauses(text: str) -> tuple[str, ...]:
    """Canonicalize formatting noise without erasing semantic word order."""

    normalized = unicodedata.normalize("NFKC", text).casefold()
    normalized = re.sub(r"(?<=\w)[ \t]*-[ \t]*(?=\w)", "-", normalized)
    normalized = re.sub(
        r"(?<=\d)\s+(?=(?:v|a|w|hz|khz|mhz|ohm|ω|mm|mil)\b)",
        "",
        normalized,
    )
    normalized = re.sub(r"(?<!\d)[.!?。！？](?!\d)", "\n", normalized)
    clauses: list[str] = []
    for raw_clause in re.split(r"[\n\r;；]+", normalized):
        tokens = re.findall(
            r"[a-z0-9]+(?:[-_./:+][a-z0-9]+)*|[\u3400-\u9fff]+",
            raw_clause,
        )
        if tokens:
            clauses.append(" ".join(tokens))
    return tuple(sorted(clauses))


def _contract_digest(clauses: tuple[str, ...]) -> str:
    return hashlib.sha256("\n".join(clauses).encode("utf-8")).hexdigest()


def _requirement_contract(
    requirement: str,
) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    evidence_boundary = min(
        (index for marker in _EVIDENCE_MARKERS if (index := requirement.find(marker)) >= 0),
        default=len(requirement),
    )
    effective = requirement[:evidence_boundary]
    marker_pattern = "(" + "|".join(re.escape(marker) for marker in _CHANGE_REQUEST_MARKERS) + ")"
    sections = re.split(marker_pattern, effective)
    root_clauses = _contract_clauses(sections[0])
    amendment_clauses: list[str] = []
    substantive_text: list[str] = []
    for index in range(1, len(sections), 2):
        amendment = sections[index + 1] if index + 1 < len(sections) else ""
        clauses = _contract_clauses(amendment)
        if not clauses:
            continue
        canonical = "\n".join(clauses)
        if _CONTINUATION_ONLY.search(canonical) and not _HARDWARE_CHANGE_SIGNAL.search(canonical):
            continue
        amendment_clauses.extend(clauses)
        substantive_text.append(amendment)
    return (
        _contract_digest(root_clauses),
        tuple(sorted(amendment_clauses)),
        tuple(substantive_text),
    )


def _stable_requirement_identity(requirement: str) -> str:
    """Return a formatting-insensitive digest of the immutable root contract."""

    return _requirement_contract(requirement)[0]


def _requirement_contract_payload(requirement: str) -> dict[str, Any]:
    root_digest, amendment_clauses, _ = _requirement_contract(requirement)
    return {
        "schema_version": 1,
        "root_digest": root_digest,
        "change_digest": _contract_digest(amendment_clauses),
    }


def _requirement_invalidation_step(
    saved_requirement: str,
    current_requirement: str,
) -> PipelineStep | None:
    """Return the earliest pipeline dependency changed by an amendment."""

    _, saved_clauses, saved_changes = _requirement_contract(saved_requirement)
    _, current_clauses, current_changes = _requirement_contract(current_requirement)
    if saved_clauses == current_clauses:
        return None

    common_prefix = 0
    for saved_change, current_change in zip(
        saved_changes,
        current_changes,
        strict=False,
    ):
        if _contract_clauses(saved_change) != _contract_clauses(current_change):
            break
        common_prefix += 1
    changed_sections = current_changes[common_prefix:] or current_changes
    change_text = "\n".join(changed_sections)
    # "Keep the same topology" is a scope limiter for a part substitution, not
    # a request to regenerate topology.
    classification_text = re.sub(
        r"\b(?:keep|preserve)\s+(?:the\s+)?same\s+topology\b|"
        r"(?:保持|保留|沿用)(?:原有|现有|相同)?拓扑",
        "",
        change_text,
        flags=re.IGNORECASE,
    )
    if _STRUCTURAL_CHANGE.search(classification_text):
        return PipelineStep.TOPOLOGY
    if _SELECTION_CHANGE.search(classification_text):
        return PipelineStep.SELECTION
    if _CONNECTION_CHANGE.search(classification_text):
        return PipelineStep.SCH_CONNECTIONS
    if _LAYOUT_CHANGE.search(classification_text):
        return PipelineStep.LAYOUT_PARTITION
    if _ROUTING_CHANGE.search(classification_text):
        return PipelineStep.ROUTE_PLAN
    if _MANUFACTURE_CHANGE.search(classification_text):
        return PipelineStep.MANUFACTURE
    # Unknown amendments fail safe at topology, while still preserving the
    # deterministic requirement-normalization prefix.
    return PipelineStep.TOPOLOGY


def _pipeline_steps(state: PipelineState) -> list[dict[str, Any]]:
    return [
        {
            "name": result.step.value,
            "blocked": result.blocked,
            "execution_blocked": result.execution_blocked,
            "used_llm": result.used_llm,
            "summary": result.summary,
            "failed_checks": [
                {
                    "name": check.name,
                    "severity": check.severity.value,
                    "message": check.message,
                    "blocks_execution": check.blocks_execution,
                }
                for check in result.error_checks
            ],
            "issues": [
                {
                    "name": check.name,
                    "severity": check.severity.value,
                    "message": check.message,
                    "blocks_execution": check.blocks_execution,
                }
                for check in result.checks
                if not check.ok
            ],
            "failures": [failure.model_dump(mode="json") for failure in result.failures],
            "repairs": [repair.model_dump(mode="json") for repair in result.repairs],
        }
        for result in state.results
    ]


def _write_pipeline_state(
    path: Path,
    requirement: str,
    state: PipelineState,
) -> None:
    component_issues = selection_release_issues(state)
    selection = state.artifact(PipelineStep.SELECTION)
    payload = {
        "schema_version": _PIPELINE_STATE_SCHEMA_VERSION,
        "requirement": requirement,
        "requirement_contract": _requirement_contract_payload(requirement),
        "project_name": state.project_name,
        "revision": state.revision,
        "completed_steps": len(state.results),
        "release_readiness": {
            "component_gate_evaluated": isinstance(selection, SelectionPlan),
            "component_release_ready": (
                isinstance(selection, SelectionPlan) and not component_issues
            ),
            "component_release_blockers": component_issues,
        },
        "steps": _pipeline_steps(state),
        "repair_history": [repair.model_dump(mode="json") for repair in state.repair_history],
        "replan_history": [replan.model_dump(mode="json") for replan in state.replan_history],
        "capability_gaps": [gap.model_dump(mode="json") for gap in state.capability_gaps],
        "intermediate_artifacts": {
            step.value: artifact.model_dump(mode="json")
            for step, artifact in state.artifacts.items()
        },
        "resume_candidates": {
            step.value: {
                "artifact": artifact.model_dump(mode="json"),
                "used_llm": used_llm,
            }
            for step, (artifact, used_llm) in state.resume_candidates.items()
        },
        "connection_synthesis_checkpoint": (
            state.connection_synthesis_checkpoint.model_dump(mode="json")
            if state.connection_synthesis_checkpoint is not None
            else None
        ),
        "connection_synthesis_report": (
            state.connection_synthesis_report.model_dump(mode="json")
            if state.connection_synthesis_report is not None
            else None
        ),
    }
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(_json(payload), encoding="utf-8")
    temporary.replace(path)


def _checkpoint_pipeline_step(
    path: Path,
    requirement: str,
    state: PipelineState,
    result: Any,
) -> None:
    _write_pipeline_state(path, requirement, state)
    try:
        from langgraph.config import get_stream_writer

        writer = get_stream_writer()
    except RuntimeError:
        return
    writer(
        {
            "kind": "workflow_event",
            "phase": f"pipeline:{result.step.value}",
            "status": (
                "execution_blocked"
                if result.execution_blocked
                else "delivered_with_issues"
                if result.blocked
                else "completed"
            ),
            "detail": result.summary,
            "completed_steps": len(state.results),
            "total_steps": 17,
        }
    )


def _checkpoint_pipeline_progress(
    path: Path,
    requirement: str,
    state: PipelineState,
) -> None:
    """Persist and stream bounded progress inside a long connection step."""

    _write_pipeline_state(path, requirement, state)
    checkpoint = state.connection_synthesis_checkpoint
    if checkpoint is None:
        return
    completed = sum(item.status == "completed" for item in checkpoint.batches)
    try:
        from langgraph.config import get_stream_writer

        writer = get_stream_writer()
    except RuntimeError:
        return
    writer(
        {
            "kind": "workflow_event",
            "phase": "pipeline:schematic_connections:batch",
            "status": "in_progress",
            "detail": (
                f"connection batches {completed}/{len(checkpoint.batches)}; "
                f"proposal attempts={checkpoint.llm_invocations}"
            ),
            "completed_batches": completed,
            "total_batches": len(checkpoint.batches),
            "completed_steps": len(state.results),
            "total_steps": 17,
        }
    )


def _record_ahe_event(
    memory: EheMemory,
    event: dict[str, Any],
    *,
    run_name: str,
    project_name: str,
    requirement: str,
) -> None:
    memory.record(
        event,
        run_name=run_name,
        project_name=project_name,
        requirement=requirement,
    )
    try:
        from langgraph.config import get_stream_writer

        writer = get_stream_writer()
    except RuntimeError:
        return
    writer(event)


def _load_pipeline_state(
    path: Path,
    requirement: str,
    project_name: str,
) -> PipelineState:
    payload = json.loads(path.read_text(encoding="utf-8"))
    saved_requirement = str(payload.get("requirement", ""))
    saved_project = str(payload.get("project_name", ""))
    saved_contract = _requirement_contract_payload(saved_requirement)
    persisted_contract = payload.get("requirement_contract")
    if isinstance(persisted_contract, dict) and (
        persisted_contract.get("root_digest") != saved_contract["root_digest"]
        or persisted_contract.get("change_digest") != saved_contract["change_digest"]
    ):
        raise ValueError("pipeline checkpoint requirement contract is corrupted")
    if (
        saved_contract["root_digest"] != _stable_requirement_identity(requirement)
        or saved_project != project_name
    ):
        raise ValueError(
            "run_name already has a checkpoint for a different requirement or "
            "project_name; choose a new run_name"
        )
    artifacts = payload.get("intermediate_artifacts")
    steps = payload.get("steps")
    if not isinstance(artifacts, dict) or not isinstance(steps, list):
        raise TypeError("pipeline checkpoint is missing artifacts or step history")
    return restore_pipeline_state(
        requirement_text=requirement,
        project_name=project_name,
        intermediate_artifacts=artifacts,
        steps=steps,
        revision=int(payload.get("revision", 0)),
        repair_history=payload.get("repair_history", []),
        replan_history=payload.get("replan_history", []),
        capability_gaps=payload.get("capability_gaps", []),
        resume_candidates=payload.get("resume_candidates", {}),
        connection_synthesis_checkpoint=payload.get("connection_synthesis_checkpoint"),
        connection_synthesis_report=payload.get("connection_synthesis_report"),
        invalidate_from_step=_requirement_invalidation_step(
            saved_requirement,
            requirement,
        ),
        artifact_first=True,
    )


def _workspace_root() -> Path:
    root = Path(os.getenv("RATSNESTPRO_WORKSPACE_ROOT", "data/ratsnestpro"))
    root = root.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _name(value: str, fallback: str) -> str:
    cleaned = _SAFE_NAME.sub("-", value.strip()).strip(".-")
    return cleaned[:80] or fallback


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        value = default
    return max(minimum, min(value, maximum))


def _transcript_token_usage(path: Path | None) -> int:
    """Recover the run-wide token meter used by isolated Temporal steps."""

    if path is None or not path.is_file():
        return 0
    total = 0
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            if not isinstance(record, dict):
                continue
            metadata = record.get("response_metadata", {})
            usage = metadata.get("usage_metadata", {}) if isinstance(metadata, dict) else {}
            call_tokens = usage.get("total_tokens") if isinstance(usage, dict) else None
            if isinstance(call_tokens, int) and call_tokens >= 0:
                total += call_tokens
            else:
                total += max(
                    1,
                    (len(str(record.get("content", ""))) + len(str(record.get("reasoning", ""))))
                    // 4,
                )
    except (OSError, json.JSONDecodeError):
        return total
    return total


class _ToolkitLlmClient:
    """Adapt the toolkit's configured chat model to RatsNestPro's text client."""

    def __init__(
        self,
        model_name: str | None = None,
        model_type: str | None = None,
        *,
        transcript_path: Path | None = None,
        phase: str = "hardware-engineer",
        max_llm_tokens: int = 120_000,
    ) -> None:
        from core import get_model, get_model_for_plain_call, settings

        selected_model = settings.DEFAULT_MODEL
        if model_name:
            candidates = [
                model
                for model in settings.AVAILABLE_MODELS
                if model.value == model_name
                and (model_type is None or type(model).__name__ == model_type)
            ]
            if not candidates:
                raise ValueError(
                    f"Model '{model_name}' ({model_type or 'unspecified provider'}) "
                    "is not available to the Temporal worker."
                )
            if len(candidates) > 1:
                raise ValueError(
                    f"Model '{model_name}' is ambiguous; preserve its provider type "
                    "when dispatching the hardware workflow."
                )
            selected_model = candidates[0]
        self._model = get_model_for_plain_call(selected_model)
        self._fallback_model = get_model(selected_model)
        self._model_name = getattr(selected_model, "value", str(selected_model))
        self._transcript_path = transcript_path
        self._phase = phase
        self._max_llm_tokens = max_llm_tokens
        self._used_llm_tokens = _transcript_token_usage(transcript_path)
        self._workflow_id = os.getenv("RATSNESTPRO_LLM_TRANSCRIPT_WORKFLOW_ID", "").strip()
        self._stream_config = LlmOutputRedisConfig(
            enabled=settings.RATSNESTPRO_LLM_STREAM_ENABLED,
            url=(settings.REDIS_URL.get_secret_value() if settings.REDIS_URL is not None else None),
            key_prefix=settings.REDIS_KEY_PREFIX,
            maxlen=settings.RATSNESTPRO_LLM_STREAM_MAXLEN,
            ttl_seconds=settings.RATSNESTPRO_LLM_STREAM_TTL_SECONDS,
            socket_timeout_seconds=settings.RATSNESTPRO_LLM_STREAM_SOCKET_TIMEOUT_SECONDS,
        )

    def complete(self, system: str, user: str, temperature: float = 0.2) -> str:
        from langchain_core.messages import HumanMessage, SystemMessage

        messages = [SystemMessage(content=system), HumanMessage(content=user)]
        estimated_input = max(1, (len(system) + len(user)) // 4)
        if self._used_llm_tokens + estimated_input > self._max_llm_tokens:
            raise LlmError("LLM token budget exhausted before the next pipeline call")
        try:
            response = self._model.invoke(messages)
        except Exception:
            if self._model is self._fallback_model:
                raise
            # A provider may not expose thinking on a particular endpoint or
            # model version. Preserve task completion with the established
            # non-thinking configuration instead of failing the pipeline.
            response = self._fallback_model.invoke(messages)
        usage = getattr(response, "usage_metadata", None)
        call_tokens = usage.get("total_tokens") if isinstance(usage, dict) else None
        self._used_llm_tokens += (
            int(call_tokens)
            if isinstance(call_tokens, int) and call_tokens >= 0
            else estimated_input + max(1, len(response_text(response)) // 4)
        )
        record = llm_output_record(
            response,
            phase=self._phase,
            agent="Hardware Engineer" if "hardware" in self._phase else "Reviewer",
            model=self._model_name,
        )
        transcript_path = str(self._transcript_path) if self._transcript_path else None
        if self._transcript_path is not None:
            append_llm_output(self._transcript_path, record)
        publish_llm_output_best_effort(
            self._stream_config,
            workflow_id=self._workflow_id,
            record=record,
            transcript_path=transcript_path,
        )
        try:
            from langgraph.config import get_stream_writer

            get_stream_writer()(stream_llm_output_record(record, transcript_path=transcript_path))
        except RuntimeError:
            # Temporal Activities run outside the LangGraph context. Their
            # waiting node tails the transcript and forwards these records.
            pass
        return response_text(response)


_MCU_MODEL_TOKEN = re.compile(
    r"(?<![a-z0-9])("
    r"atmega\d+[a-z0-9-]*|attiny\d+[a-z0-9-]*|"
    r"stm32[a-z0-9-]+|rp\d{4}[a-z0-9-]*|esp32[a-z0-9-]*|"
    r"nrf\d+[a-z0-9-]*|same?\d+[a-z0-9-]*|"
    r"pic\d+[a-z0-9-]*|ch32[a-z0-9-]+"
    r")(?![a-z0-9])",
    re.IGNORECASE,
)
_NEGATED_MODEL_PREFIX = re.compile(
    r"(?:\bnot\b|\bnever\b|\bwithout\b|\bdo\s+not\b|"
    r"\bdon't\b|\bforbid(?:den)?\b|\binstead\s+of\b|"
    r"不要|不得|禁止|不能|不用|不允许|并非|不是|非)"
    r"[^.;。；\n]{0,48}$",
    re.IGNORECASE,
)


def _positive_mcu_models(requirement: str) -> set[str]:
    """Extract asserted MCU models without loading the full KiCad library."""

    models: set[str] = set()
    for match in _MCU_MODEL_TOKEN.finditer(requirement):
        prefix = requirement[max(0, match.start() - 64) : match.start()]
        if _NEGATED_MODEL_PREFIX.search(prefix):
            continue
        models.add(re.sub(r"[^a-z0-9]", "", match.group(1).lower()))
    return models


def _is_atmega328_only(requirement: str) -> bool:
    models = _positive_mcu_models(requirement)
    return bool(models) and all(model.startswith("atmega328") for model in models)


def _pipeline_mode(requirement: str, requested: LlmMode) -> LlmMode:
    """Use the deterministic template only for the family it actually models."""
    if _is_atmega328_only(requirement):
        return requested
    return LlmMode.REQUIRED


def _run_dir(run_name: str) -> Path:
    return _workspace_root() / "runs" / _name(run_name, "design")


@contextmanager
def _serialize_pipeline_run(run_dir: Path) -> Iterator[None]:
    """Lock one run directory across threads and service processes."""
    lock_dir = run_dir.parent / ".locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / f"{run_dir.name}.lock"
    with lock_path.open("a+b") as handle:
        if os.name == "nt":
            import msvcrt

            if handle.seek(0, os.SEEK_END) == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _workspace_path(value: str) -> Path:
    root = _workspace_root()
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    candidate = candidate.resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"path must stay inside the RatsNestPro workspace: {root}") from exc
    return candidate


def _overrides(
    crystal_mhz: int | None,
    ldo_output_v: float | None,
    decoupling_count: int | None,
    power_led: bool | None,
    breakout_rows: int | None,
    breakout_pins_per_row: int | None,
    mounting_holes: int | None,
) -> dict[str, object]:
    values = {
        "crystal_mhz": crystal_mhz,
        "ldo_output_v": ldo_output_v,
        "decoupling_count": decoupling_count,
        "power_led": power_led,
        "breakout_rows": breakout_rows,
        "breakout_pins_per_row": breakout_pins_per_row,
        "mounting_holes": mounting_holes,
    }
    return {key: value for key, value in values.items() if value is not None}


def _resolve_params(
    requirement: str,
    llm_mode: LlmModeName,
    overrides: dict[str, object],
) -> tuple[Atmega328Params | None, dict[str, Any]]:
    result = Architect().plan(requirement, mode=parse_mode(llm_mode))
    if not result.decision.qualified:
        return None, {
            "status": "needs_clarification",
            "reason": result.decision.rationale,
            "questions": result.decision.clarifying_questions,
        }
    if result.params is None and not overrides:
        return None, {
            "status": "needs_clarification",
            "reason": result.decision.rationale,
            "questions": result.decision.clarifying_questions,
        }
    values = result.params.model_dump() if result.params else {}
    try:
        params = Atmega328Params(**{**values, **overrides})  # type: ignore[arg-type]
    except ValidationError as exc:
        return None, {
            "status": "needs_clarification",
            "reason": "The selected parameters violate the board-family contract.",
            "questions": [error["msg"] for error in exc.errors()],
        }
    return params, {
        "architect_source": result.source,
        "architect_rationale": result.decision.rationale,
    }


def _files(path: Path) -> list[str]:
    root = _workspace_root()
    return [
        str(file.resolve().relative_to(root)) for file in sorted(path.rglob("*")) if file.is_file()
    ]


def _current_pipeline_files(
    state: PipelineState,
    *metadata_paths: Path,
) -> list[str]:
    """List only files proven by artifacts in the current checkpoint.

    A run directory can contain outputs from an older, longer attempt. Scanning
    it as if every file belonged to the current state made a 4/17 checkpoint
    appear to have a current PCB or SES. Artifact provenance is therefore the
    authority; unrelated historical files remain on disk but are not reported
    as current deliverables.
    """

    candidates: list[Path] = list(metadata_paths)
    materialized = state.artifact(PipelineStep.SCH_MATERIALIZE)
    if isinstance(materialized, MaterializeResult):
        candidates.append(Path(materialized.sch_path))
    erc = state.artifact(PipelineStep.ERC)
    if isinstance(erc, ErcSummary) and erc.cli_report_path:
        candidates.append(Path(erc.cli_report_path))
    board = state.artifact(PipelineStep.LAYOUT_WRITE)
    if isinstance(board, PcbWriteResult):
        candidates.extend(
            [
                Path(board.pcb_path),
                Path(board.pcb_path).with_name(f"{Path(board.pcb_path).stem}.unrouted.kicad_pcb"),
            ]
        )
        if board.placement_constraints_path:
            candidates.append(Path(board.placement_constraints_path))
    route = state.artifact(PipelineStep.ROUTE_SIGNALS)
    if isinstance(route, RouteResult):
        if route.dsn_path:
            candidates.append(Path(route.dsn_path))
        if route.ses_path:
            candidates.append(Path(route.ses_path))
    manufacture = state.artifact(PipelineStep.MANUFACTURE)
    if isinstance(manufacture, ManufactureResult):
        candidates.extend(
            Path(value)
            for value in (
                manufacture.bom_path,
                manufacture.cpl_path,
                manufacture.unresolved_manifest_path,
            )
            if value
        )
        candidates.extend(Path(value) for value in manufacture.drill_paths)
        if manufacture.gerber_dir:
            gerber_dir = Path(manufacture.gerber_dir)
            if gerber_dir.is_dir():
                candidates.extend(path for path in gerber_dir.rglob("*") if path.is_file())

    root = _workspace_root()
    current: list[str] = []
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
            relative = resolved.relative_to(root)
        except (OSError, ValueError):
            continue
        if resolved.is_file():
            current.append(str(relative))
    return sorted(dict.fromkeys(current))


def _preferred_project_file(
    directory: Path,
    project_name: str,
    suffix: str,
) -> Path | None:
    """Prefer the named deliverable over diagnostics or temporary artifacts."""

    expected = directory / f"{project_name}{suffix}"
    if expected.is_file():
        return expected
    return next(iter(sorted(directory.glob(f"*{suffix}"))), None)


def _drc_check(pcb_path: Path | None) -> dict[str, Any]:
    if pcb_path is None or not pcb_path.is_file():
        return {
            "applicable": False,
            "available": False,
            "ran": False,
            "errors": None,
            "warnings": None,
            "unconnected": None,
            "report_path": None,
            "by_type": {},
        }
    cli = kicad_cli_available()
    if cli is None:
        return {
            "applicable": True,
            "available": False,
            "ran": False,
            "errors": None,
            "warnings": None,
            "unconnected": None,
            "report_path": None,
            "by_type": {},
        }

    report = pcb_path.with_suffix(".drc.json")
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
                str(report),
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
        data = json.loads(report.read_text(encoding="utf-8"))
    except (OSError, subprocess.SubprocessError, ValueError):
        return {
            "applicable": True,
            "available": True,
            "ran": False,
            "errors": None,
            "warnings": None,
            "unconnected": None,
            "report_path": str(report) if report.is_file() else None,
            "by_type": {},
        }

    violations = data.get("violations", []) if isinstance(data, dict) else []
    parity = data.get("schematic_parity", []) if isinstance(data, dict) else []
    unconnected_items = data.get("unconnected_items", []) if isinstance(data, dict) else []
    findings = [
        item for item in [*violations, *parity, *unconnected_items] if isinstance(item, dict)
    ]
    by_type: dict[str, int] = {}
    for finding in findings:
        finding_type = str(finding.get("type", "unknown"))
        by_type[finding_type] = by_type.get(finding_type, 0) + 1
    return {
        "applicable": True,
        "available": True,
        "ran": True,
        "errors": sum(1 for item in findings if str(item.get("severity", "error")) == "error"),
        "warnings": sum(1 for item in findings if str(item.get("severity", "")) == "warning"),
        "unconnected": len(unconnected_items),
        "report_path": str(report),
        "by_type": by_type,
    }


def _erc_check(sch_path: Path | None) -> dict[str, Any]:
    if sch_path is None or not sch_path.is_file():
        return {
            "applicable": False,
            "available": False,
            "ran": False,
            "errors": None,
            "warnings": None,
            "report_path": None,
            "by_type": {},
        }
    result = run_erc(sch_path)
    by_type: dict[str, int] = {}
    for violation in result.violations:
        by_type[violation.rule_id] = by_type.get(violation.rule_id, 0) + 1
    return {
        "applicable": True,
        "available": result.available,
        "ran": result.ran,
        "errors": result.error_count if result.ran else None,
        "warnings": result.warning_count if result.ran else None,
        "report_path": result.report_path,
        "by_type": by_type,
    }


def _verification(
    sch_path: Path | None,
    pcb_path: Path | None,
) -> dict[str, Any]:
    return {
        "erc": _erc_check(sch_path),
        "drc": _drc_check(pcb_path),
    }


def _paired_project_files(
    project: Path,
    sch_path: Path | None,
    pcb_path: Path | None,
) -> tuple[Path | None, Path | None]:
    schematic = sch_path if sch_path and sch_path.is_file() else None
    board = pcb_path if pcb_path and pcb_path.is_file() else None
    if project.is_dir():
        if schematic is None:
            schematic = next(iter(sorted(project.glob("*.kicad_sch"))), None)
        if board is None:
            board = next(iter(sorted(project.glob("*.kicad_pcb"))), None)
    else:
        if schematic is None:
            candidate = project.with_suffix(".kicad_sch")
            schematic = candidate if candidate.is_file() else None
        if board is None:
            candidate = project.with_suffix(".kicad_pcb")
            board = candidate if candidate.is_file() else None
    return schematic, board


def _verification_blockers(verification: dict[str, Any]) -> list[str]:
    blockers: list[str] = []
    for label in ("erc", "drc"):
        result = verification.get(label, {})
        if not result.get("applicable"):
            continue
        display = label.upper()
        if not result.get("available"):
            blockers.append(f"kicad-cli {display} unavailable")
        elif not result.get("ran"):
            blockers.append(f"kicad-cli {display} did not run")
        elif result.get("errors") != 0:
            blockers.append(f"kicad-cli {display} reported {result.get('errors')} error(s)")
    drc = verification.get("drc", {})
    if drc.get("applicable") and drc.get("unconnected") != 0:
        blockers.append(f"kicad-cli DRC reported {drc.get('unconnected')} unconnected item(s)")
    return blockers


def _verification_markdown(verification: dict[str, Any]) -> str:
    erc = verification["erc"]
    drc = verification["drc"]
    return "\n".join(
        [
            "## Independent kicad-cli verification",
            "",
            (
                f"- ERC: ran={erc['ran']}, errors={erc['errors']}, "
                f"warnings={erc['warnings']}, report=`{erc['report_path']}`"
            ),
            (
                f"- DRC: ran={drc['ran']}, errors={drc['errors']}, "
                f"warnings={drc['warnings']}, unconnected={drc['unconnected']}, "
                f"report=`{drc['report_path']}`"
            ),
        ]
    )


def ratsnest_search_internal_knowledge(
    query: str,
    role: str = "general",
    limit: int = 3,
) -> str:
    """Search bundled design-pattern knowledge before external research."""

    bounded_limit = max(1, min(limit, 8))
    hits = build_default_kb().retrieve(
        query,
        top_k=bounded_limit,
        role=role or None,
    )
    relevant = [hit for hit in hits if hit.score > 0]
    experiences = EheMemory(_workspace_root() / "ehe").search_verified(query, limit=bounded_limit)
    results = [
        {
            "id": hit.doc.id,
            "role": hit.doc.role,
            "source": hit.doc.source,
            "score": hit.score,
            "text": hit.doc.text[:4_000],
        }
        for hit in relevant
    ]
    results.extend(
        {
            "id": f"verified:{item.get('experience_id', '')}",
            "role": "verified_experience",
            "source": "local_ehe_memory",
            "score": item.get("score", 0),
            "text": json.dumps(
                {
                    "selected_roles": item.get("selected_roles", []),
                    "resolved_issues": item.get("resolved_issues", []),
                    "evidence": item.get("evidence", {}),
                },
                ensure_ascii=False,
            ),
        }
        for item in experiences
    )
    return _json(
        {
            "status": "ok" if results else "no_results",
            "query": query,
            "role": role,
            "results": results[:bounded_limit],
        }
    )


def ratsnest_create_design_plan(
    requirement: str,
    run_name: str = "design-plan",
    project_name: str = "atmega328_dev_board",
    llm_mode: LlmModeName = "offline",
    crystal_mhz: int | None = None,
    ldo_output_v: float | None = None,
    decoupling_count: int | None = None,
    power_led: bool | None = None,
    breakout_rows: int | None = None,
    breakout_pins_per_row: int | None = None,
    mounting_holes: int | None = None,
) -> str:
    """Create the immutable ATmega328 DesignPlan without generating KiCad files.

    Use this for design planning or parameter review. Explicit parameters
    override values inferred by the Architect. llm_mode controls only the
    embedded EricAI path; offline is deterministic and needs no EricAI install.
    """
    try:
        if not _is_atmega328_only(requirement):
            return _json(
                {
                    "status": "use_generic_pipeline",
                    "reason": (
                        "This tool is the ATmega328 offline template only; the requested "
                        "family must use the adaptive 17-step pipeline."
                    ),
                    "next_tool": "ratsnest_run_pcb_pipeline",
                    "required_llm_mode": "required",
                }
            )
        params, context = _resolve_params(
            requirement,
            llm_mode,
            _overrides(
                crystal_mhz,
                ldo_output_v,
                decoupling_count,
                power_led,
                breakout_rows,
                breakout_pins_per_row,
                mounting_holes,
            ),
        )
        if params is None:
            return _json(context)
        out = _run_dir(run_name)
        out.mkdir(parents=True, exist_ok=True)
        plan = build_design_plan(requirement, params, _name(project_name, "atmega328_dev_board"))
        plan_path = out / "plan.json"
        plan_path.write_text(plan.model_dump_json(indent=2), encoding="utf-8")
        return _json(
            {
                "status": "ok",
                **context,
                "workspace": str(_workspace_root()),
                "run_directory": str(out),
                "plan_path": str(plan_path),
                "family": plan.circuit.family,
                "components": len(plan.circuit.components),
                "nets": len(plan.circuit.nets),
                "params": params.model_dump(),
            }
        )
    except LlmError as exc:
        return _json({"status": "error", "error": str(exc), "llm_mode": llm_mode})


def ratsnest_generate_schematic(
    requirement: str,
    run_name: str = "schematic",
    project_name: str = "atmega328_dev_board",
    llm_mode: LlmModeName = "offline",
    run_erc: bool = True,
    repair: bool = True,
    max_repair_iterations: int = 5,
    crystal_mhz: int | None = None,
    ldo_output_v: float | None = None,
    decoupling_count: int | None = None,
    power_led: bool | None = None,
    breakout_rows: int | None = None,
    breakout_pins_per_row: int | None = None,
    mounting_holes: int | None = None,
) -> str:
    """Generate and verify an ATmega328 schematic, optionally repairing failures.

    This preserves RatsNestPro pipeline A: Architect planning, typed generation,
    deterministic gates, optional KiCad ERC, and the whitelisted Coder repair
    loop. Generated artifacts stay inside the configured workspace.
    """
    try:
        if not _is_atmega328_only(requirement):
            return _json(
                {
                    "status": "use_generic_pipeline",
                    "reason": (
                        "This tool is the ATmega328 schematic template only; the requested "
                        "family must use the adaptive 17-step pipeline."
                    ),
                    "next_tool": "ratsnest_run_pcb_pipeline",
                    "required_llm_mode": "required",
                }
            )
        params, context = _resolve_params(
            requirement,
            llm_mode,
            _overrides(
                crystal_mhz,
                ldo_output_v,
                decoupling_count,
                power_led,
                breakout_rows,
                breakout_pins_per_row,
                mounting_holes,
            ),
        )
        if params is None:
            return _json(context)
        out = _run_dir(run_name)
        project = _name(project_name, "atmega328_dev_board")
        result = generate_design(
            requirement,
            params=params,
            out_dir=out,
            project_name=project,
            run_erc=run_erc,
        )
        repair_summary: dict[str, Any] | None = None
        if result.blocked and repair:
            repaired = run_repair(
                params,
                expectations_for(params),
                max_iter=max(1, min(max_repair_iterations, 10)),
                mode=parse_mode(llm_mode),
            )
            repair_summary = {
                "success": repaired.success,
                "iterations": repaired.iterations,
                "reason": repaired.reason,
            }
            if repaired.success:
                result = generate_design(
                    requirement,
                    params=repaired.params,
                    out_dir=out,
                    project_name=project,
                    run_erc=run_erc,
                )
        return _json(
            {
                "status": "blocked" if result.blocked else "ok",
                **context,
                "summary": result.summary,
                "workspace": str(_workspace_root()),
                "run_directory": str(out),
                "artifacts": _files(out),
                "gates": [
                    {
                        "name": gate.gate,
                        "status": gate.status.value,
                        "required": gate.required,
                    }
                    for gate in result.report.gates
                ],
                "repair": repair_summary,
            }
        )
    except (LlmError, ValidationError, ValueError) as exc:
        return _json({"status": "error", "error": str(exc)})


def _ehe_result_payload(
    *,
    state: PipelineState,
    memory: EheMemory,
    run_name: str,
    project_name: str,
    verified_experience_path: str | None,
    resolved_issues: list[dict[str, str]],
    selected_roles: list[str],
    human_amendment: bool,
    promotion_eligible: bool,
) -> dict[str, Any]:
    run_local_gaps = [gap.model_dump(mode="json") for gap in state.capability_gaps]
    global_snapshot = memory.candidate_snapshot()
    return {
        "schema_version": 2,
        "run_local_gaps": {
            "schema_version": 1,
            "scope": "current_run",
            "source": "pipeline_state.capability_gaps",
            "run_name": run_name,
            "project_name": project_name,
            "items": run_local_gaps,
        },
        "global_candidate_snapshot": global_snapshot,
        # Compatibility alias for existing readers. New readers must use the
        # scoped snapshot above and never interpret this list as current-run state.
        "candidate_gaps": global_snapshot["candidates"],
        "candidate_gaps_compatibility": (
            "deprecated cross-run alias; use global_candidate_snapshot.candidates"
        ),
        "verified_experience": verified_experience_path,
        "resolved_issues_promoted": (resolved_issues if verified_experience_path else []),
        "promotion": {
            "status": (
                "promoted"
                if verified_experience_path
                else "pending_independent_review"
                if promotion_eligible
                else "not_eligible"
            ),
            "candidate": {
                "eligible": promotion_eligible,
                "resolved_issues": resolved_issues,
                "selected_roles": selected_roles,
                "human_amendment": human_amendment,
            },
        },
    }


def _run_pcb_pipeline_unlocked(
    requirement: str,
    run_name: str = "pcb",
    project_name: str = "board",
    llm_mode: LlmModeName = "auto",
    model_name: str | None = None,
    model_type: str | None = None,
    *,
    until_step: str | None = None,
    external_retry_managed: bool = False,
    ahe_budget: dict[str, int] | None = None,
) -> str:
    """Run RatsNestPro pipeline B, the fixed 17-step schematic-to-manufacture flow.

    The artifact-first flow continues past release-gate issues whenever the
    next mechanical step can still run. It produces editable draft artifacts
    plus an explicit issue ledger. Only an execution failure stops progress;
    release readiness still requires the full verification gates.
    """
    try:
        requested_mode = parse_mode(llm_mode)
        mode = _pipeline_mode(requirement, requested_mode)
        requested_until = PipelineStep(until_step) if until_step else None
        out = _run_dir(run_name)
        out.mkdir(parents=True, exist_ok=True)
        workflow_id = os.getenv("RATSNESTPRO_LLM_TRANSCRIPT_WORKFLOW_ID", "").strip()
        if workflow_id:
            from agents.ratsnestpro.temporal.contracts import llm_transcript_filename

            transcript_path = out / llm_transcript_filename(workflow_id)
        else:
            transcript_path = out / "llm_outputs.jsonl"
        pipeline_step = os.getenv("RATSNESTPRO_PIPELINE_STEP", "").strip()
        budget = ahe_budget or {}
        max_llm_tokens = min(
            _env_int(
                "RATSNESTPRO_AHE_MAX_LLM_TOKENS",
                default=120_000,
                minimum=1_000,
                maximum=200_000,
            ),
            max(1_000, int(budget.get("max_llm_tokens", 120_000))),
        )
        client = (
            None
            if mode == LlmMode.OFFLINE
            else _ToolkitLlmClient(
                model_name=model_name,
                model_type=model_type,
                transcript_path=transcript_path,
                phase=f"hardware-engineer:{pipeline_step or until_step or 'pipeline'}",
                max_llm_tokens=max_llm_tokens,
            )
        )
        state_path = out / "pipeline_state.json"
        project = _name(project_name, "board")
        ehe_memory = EheMemory(_workspace_root() / "ehe")
        saved_gap_payloads: list[dict[str, Any]] = []
        saved_issue_payloads: list[dict[str, str]] = []
        if state_path.is_file():
            checkpoint_payload = json.loads(state_path.read_text(encoding="utf-8"))
            raw_gaps = checkpoint_payload.get("capability_gaps", [])
            if isinstance(raw_gaps, list):
                saved_gap_payloads = [item for item in raw_gaps if isinstance(item, dict)]
            raw_steps = checkpoint_payload.get("steps", [])
            if isinstance(raw_steps, list):
                saved_issue_payloads = [
                    {
                        "step": str(step.get("name", "")),
                        "name": str(issue.get("name", "")),
                    }
                    for step in raw_steps
                    if isinstance(step, dict)
                    for issue in step.get(
                        "issues",
                        step.get("failed_checks", []),
                    )
                    if isinstance(issue, dict) and issue.get("name")
                ]
        state = (
            _load_pipeline_state(state_path, requirement, project)
            if state_path.is_file()
            else PipelineState(
                requirement_text=requirement,
                project_name=project,
            )
        )
        active_gap_signatures = {gap.signature for gap in state.capability_gaps}
        for gap in saved_gap_payloads:
            signature = str(gap.get("signature", ""))
            if signature and signature not in active_gap_signatures:
                _record_ahe_event(
                    ehe_memory,
                    {
                        "kind": "ahe_event",
                        "event": "capability_gap_resolved",
                        "step": str(gap.get("step", "pipeline")),
                        "revision": state.revision,
                        "gap": gap,
                    },
                    run_name=run_name,
                    project_name=project,
                    requirement=requirement,
                )
        resumed_steps = len(state.results)
        require_freerouting = _env_flag("RATSNESTPRO_REQUIRE_FREEROUTING")
        Pipeline().run(
            state,
            PipelineContext(
                mode=mode,
                client=client,
                out_dir=str(out),
                # AHE is limited to failures that prevent mechanical execution.
                # Design/ERC/DRC findings remain visible and do not consume the
                # repair budget in artifact-first mode.
                repair_attempts=_env_int(
                    "RATSNESTPRO_AHE_STAGNATION_LIMIT",
                    default=2,
                    minimum=0,
                    maximum=4,
                ),
                require_freerouting=require_freerouting,
                # Let transient provider/process failures escape to Temporal so
                # RetryPolicy can replay from the last accepted checkpoint.
                # Deterministic bottom-line failures remain ordinary StepResult
                # values and are still captured in the issue ledger.
                capture_step_errors=not external_retry_managed,
                ahe_enabled=_env_flag("RATSNESTPRO_AHE_ENABLED", default=True),
                max_total_repair_attempts=min(
                    _env_int(
                        "RATSNESTPRO_AHE_MAX_REPAIRS",
                        default=6,
                        minimum=0,
                        maximum=12,
                    ),
                    max(0, int(budget.get("max_ahe_repairs", 6))),
                ),
                max_same_failure_retries=min(
                    _env_int(
                        "RATSNESTPRO_AHE_MAX_SAME_FAILURE_RETRIES",
                        default=2,
                        minimum=0,
                        maximum=4,
                    ),
                    max(0, int(budget.get("max_same_failure_retries", 2))),
                ),
                ahe_deadline_monotonic=monotonic()
                + 60
                * min(
                    _env_int(
                        "RATSNESTPRO_AHE_MAX_WALL_CLOCK_MINUTES",
                        default=60,
                        minimum=1,
                        maximum=120,
                    ),
                    max(1, int(budget.get("max_wall_clock_minutes", 60))),
                ),
                max_replan_attempts=_env_int(
                    "RATSNESTPRO_AHE_MAX_REPLANS",
                    default=0,
                    minimum=0,
                    maximum=2,
                ),
                artifact_first=True,
                repair_release_issues=False,
                design_repair_attempts=_env_int(
                    "RATSNESTPRO_DESIGN_REPAIR_STAGNATION_LIMIT",
                    default=1,
                    minimum=0,
                    maximum=2,
                ),
                max_design_repair_attempts_per_step=_env_int(
                    "RATSNESTPRO_DESIGN_REPAIR_MAX_REPAIRS",
                    default=2,
                    minimum=0,
                    maximum=4,
                ),
                max_total_design_repair_attempts=_env_int(
                    "RATSNESTPRO_DESIGN_REPAIR_MAX_TOTAL_REPAIRS",
                    default=8,
                    minimum=0,
                    maximum=16,
                ),
                # Temporal owns transient Activity retry. Keeping the legacy
                # in-process retry as well would multiply attempts, latency,
                # and LLM spend. AHE/design repair remains independently
                # bounded because it addresses domain artifacts, not worker
                # availability.
                execution_retry_attempts=(
                    0
                    if external_retry_managed
                    else _env_int(
                        "RATSNESTPRO_EXECUTION_RETRIES",
                        default=1,
                        minimum=0,
                        maximum=2,
                    )
                ),
                connection_completion_limit=_env_int(
                    "RATSNESTPRO_CONNECTION_COMPLETION_LIMIT",
                    default=8192,
                    minimum=2048,
                    maximum=32768,
                ),
                connection_direct_pin_limit=_env_int(
                    "RATSNESTPRO_CONNECTION_DIRECT_PIN_LIMIT",
                    default=180,
                    minimum=32,
                    maximum=1024,
                ),
                connection_batch_target_pins=_env_int(
                    "RATSNESTPRO_CONNECTION_BATCH_TARGET_PINS",
                    default=96,
                    minimum=16,
                    maximum=512,
                ),
                connection_max_batches=_env_int(
                    "RATSNESTPRO_CONNECTION_MAX_BATCHES",
                    default=8,
                    minimum=1,
                    maximum=12,
                ),
                connection_batch_merge_retries=_env_int(
                    "RATSNESTPRO_CONNECTION_BATCH_RETRIES",
                    default=1,
                    minimum=0,
                    maximum=2,
                ),
                connection_max_llm_invocations=_env_int(
                    "RATSNESTPRO_CONNECTION_MAX_LLM_CALLS",
                    default=16,
                    minimum=1,
                    maximum=36,
                ),
                connection_max_total_llm_invocations=_env_int(
                    "RATSNESTPRO_CONNECTION_MAX_TOTAL_LLM_CALLS",
                    default=32,
                    minimum=1,
                    maximum=144,
                ),
                max_route_invocations=_env_int(
                    "RATSNESTPRO_MAX_ROUTE_INVOCATIONS",
                    default=3,
                    minimum=1,
                    maximum=8,
                ),
                on_ahe_event=lambda event: _record_ahe_event(
                    ehe_memory,
                    event,
                    run_name=run_name,
                    project_name=project,
                    requirement=requirement,
                ),
                strategy_score=ehe_memory.strategy_score,
                replan_score=ehe_memory.replan_score,
                on_step_completed=lambda current, result: _checkpoint_pipeline_step(
                    state_path, requirement, current, result
                ),
                on_progress_checkpoint=lambda current: _checkpoint_pipeline_progress(
                    state_path,
                    requirement,
                    current,
                ),
            ),
            until=requested_until,
        )
        target_reached = requested_until is not None and requested_until in state.completed
        if (
            requested_until is not None
            and requested_until != PipelineStep.MANUFACTURE
            and target_reached
            and not state.execution_blocked
        ):
            # Temporal advances one canonical checkpoint at a time. Re-running
            # full project verification after every successful prefix would
            # execute ERC/DRC repeatedly. Persist the engineering state here;
            # the final step (or an execution stop) builds the full report.
            _write_pipeline_state(state_path, requirement, state)
            current_files = _current_pipeline_files(state, state_path)
            return _json(
                {
                    "status": "checkpointed",
                    "outcome": "in_progress",
                    "workspace": str(_workspace_root()),
                    "run_directory": str(out),
                    "completed_steps": len(state.results),
                    "total_steps": 17,
                    "requested_until_step": requested_until.value,
                    "step_target_reached": True,
                    "execution_blocked": False,
                    "execution_complete": False,
                    "release_ready": False,
                    "resumed_steps": resumed_steps,
                    "requested_llm_mode": requested_mode.value,
                    "effective_llm_mode": mode.value,
                    "pipeline_state_path": str(state_path),
                    "pipeline_result_path": "",
                    "artifacts": current_files,
                }
            )
        route_artifact = state.artifact(PipelineStep.ROUTE_SIGNALS)
        selection_artifact = state.artifact(PipelineStep.SELECTION)
        routing = (
            route_artifact.model_dump()
            if isinstance(route_artifact, RouteResult)
            else {
                "required": require_freerouting,
                "method": "not_reached",
                "note": "pipeline stopped before signal routing",
            }
        )
        mcu_parts = []
        if isinstance(selection_artifact, SelectionPlan):
            mcu_parts = [
                {
                    "ref": part.ref,
                    "value": part.value,
                    "symbol": part.symbol,
                    "footprint": part.footprint,
                }
                for part in selection_artifact.parts
                if part.role.lower() == "mcu" or "mcu_" in part.symbol.lower()
            ]
        materialized_artifact = state.artifact(PipelineStep.SCH_MATERIALIZE)
        schematic_path = (
            Path(materialized_artifact.sch_path)
            if isinstance(materialized_artifact, MaterializeResult)
            and Path(materialized_artifact.sch_path).is_file()
            else None
        )
        board_artifact = state.artifact(PipelineStep.LAYOUT_WRITE)
        pcb_path = (
            Path(board_artifact.pcb_path)
            if isinstance(board_artifact, PcbWriteResult)
            and Path(board_artifact.pcb_path).is_file()
            else None
        )
        verification = _verification(
            schematic_path,
            pcb_path,
        )
        verification_blockers = _verification_blockers(verification)
        steps = _pipeline_steps(state)
        pipeline_blockers = [
            (f"{step['name']}:{check['name']}: {check['message']}")
            for step in steps
            if step["blocked"]
            for check in step["failed_checks"]
        ]
        release_blockers = [*verification_blockers, *pipeline_blockers]
        component_release_issues = selection_release_issues(state)
        release_blockers.extend(
            (
                f"component {issue['ref']} is not release eligible "
                f"({issue['status']}): {issue['reason']}"
            )
            for issue in component_release_issues
        )
        # Persist migrations even when a mocked/aborted runner did not invoke a
        # per-step callback. This also establishes provenance for the state file.
        _write_pipeline_state(state_path, requirement, state)
        current_files = _current_pipeline_files(state, state_path)
        current_absolute = [(_workspace_root() / relative).resolve() for relative in current_files]
        has_schematic = any(path.suffix == ".kicad_sch" for path in current_absolute)
        has_pcb = any(path.suffix == ".kicad_pcb" for path in current_absolute)
        has_dsn = any(path.suffix == ".dsn" for path in current_absolute)
        has_ses = any(path.suffix == ".ses" for path in current_absolute)
        if not has_schematic:
            release_blockers.append("no current .kicad_sch artifact")
        if not has_pcb:
            release_blockers.append("no current .kicad_pcb artifact")
        if not has_dsn:
            release_blockers.append("no current Freerouting .dsn artifact")
        if not has_ses:
            release_blockers.append("no current Freerouting .ses artifact")
        if routing.get("method") != "freerouting":
            release_blockers.append("Freerouting did not complete")
        if routing.get("unconnected") != 0:
            release_blockers.append("routing unconnected count is not zero")
        if len(state.results) != 17:
            release_blockers.append("17-step pipeline did not complete")
        release_blockers = list(dict.fromkeys(release_blockers))
        issue_ledger = [
            {
                "step": step["name"],
                **issue,
            }
            for step in steps
            for issue in step.get("issues", [])
        ]
        execution_complete = len(state.results) == 17 and not state.execution_blocked
        if not execution_complete:
            outcome = "execution_blocked"
        elif release_blockers:
            outcome = "delivered_with_issues"
        else:
            outcome = "release_ready"
        harness_repairs = [repair for repair in state.repair_history if repair.kind == "harness"]
        design_repairs = [repair for repair in state.repair_history if repair.kind == "design"]
        active_issue_keys = {
            (str(issue.get("step", "")), str(issue.get("name", ""))) for issue in issue_ledger
        }
        resolved_issues = [
            issue
            for issue in saved_issue_payloads
            if (issue["step"], issue["name"]) not in active_issue_keys
        ]
        # EHE promotion is a team-level decision. Hardware Engineer can only
        # create a candidate; the independent Reviewer promotes it after PASS.
        verified_experience_path: str | None = None
        selected_roles = (
            [part.role for part in selection_artifact.parts]
            if isinstance(selection_artifact, SelectionPlan)
            else []
        )
        human_amendment = (
            "USER CHANGE REQUEST:" in requirement
            or "INDEPENDENT REVIEW FEEDBACK TO REPAIR:" in requirement
        )
        result_path = out / "pipeline_result.json"
        payload = {
            "status": ("ok" if outcome == "release_ready" else outcome),
            "outcome": outcome,
            "workspace": str(_workspace_root()),
            "run_directory": str(out),
            "completed_steps": len(state.results),
            "total_steps": 17,
            "requested_until_step": (
                requested_until.value if requested_until is not None else None
            ),
            "step_target_reached": (requested_until is None or requested_until in state.completed),
            "resumed_steps": resumed_steps,
            "requested_llm_mode": requested_mode.value,
            "effective_llm_mode": mode.value,
            "design_identity": {"mcu_parts": mcu_parts},
            "routing": routing,
            "verification": verification,
            "verification_blockers": verification_blockers,
            "release_blockers": release_blockers,
            "issue_ledger": issue_ledger,
            "execution_complete": execution_complete,
            "execution_blocked": state.execution_blocked,
            "release_ready": outcome == "release_ready",
            "component_release": {
                "ready": not component_release_issues,
                "blockers": component_release_issues,
            },
            "design_repair": {
                "attempts": len(design_repairs),
                "history": [repair.model_dump(mode="json") for repair in design_repairs],
            },
            "ahe": {
                "enabled": _env_flag("RATSNESTPRO_AHE_ENABLED", default=True),
                "revision": state.revision,
                "repair_attempts": len(harness_repairs),
                "repair_history": [repair.model_dump(mode="json") for repair in harness_repairs],
                "replan_history": [
                    replan.model_dump(mode="json") for replan in state.replan_history
                ],
                "capability_gaps": [gap.model_dump(mode="json") for gap in state.capability_gaps],
            },
            "ehe": _ehe_result_payload(
                state=state,
                memory=ehe_memory,
                run_name=run_name,
                project_name=project,
                verified_experience_path=verified_experience_path,
                resolved_issues=resolved_issues,
                selected_roles=selected_roles,
                human_amendment=human_amendment,
                promotion_eligible=outcome == "release_ready",
            ),
            "steps": steps,
            "pipeline_state_path": str(state_path),
            "pipeline_result_path": str(result_path),
            "artifacts": current_files,
        }
        result_path.write_text(_json(payload), encoding="utf-8")
        return _json(payload)
    except StructuredOutputError as exc:
        return _json(
            {
                "status": "error",
                "error": str(exc),
                "error_type": "structured_output_error",
                "llm_mode": llm_mode,
            }
        )
    except LlmError as exc:
        return _json(
            {
                "status": "error",
                "error": str(exc),
                "error_type": "llm_error",
                "llm_mode": llm_mode,
            }
        )
    except ValidationError as exc:
        return _json(
            {
                "status": "error",
                "error": str(exc),
                "error_type": "validation_error",
                "llm_mode": llm_mode,
            }
        )
    except (TypeError, ValueError) as exc:
        return _json(
            {
                "status": "error",
                "error": str(exc),
                "error_type": "configuration_error",
                "llm_mode": llm_mode,
            }
        )
    except OSError as exc:
        return _json(
            {
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
                "error_type": "transient_io_error",
                "llm_mode": llm_mode,
            }
        )
    except Exception as exc:  # noqa: BLE001 - keep tool/SSE boundary structured
        return _json(
            {
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
                "error_type": "unexpected_pipeline_error",
                "llm_mode": llm_mode,
            }
        )


def ratsnest_run_pcb_pipeline(
    requirement: str,
    run_name: str = "pcb",
    project_name: str = "board",
    llm_mode: LlmModeName = "auto",
    model_name: str | None = None,
    model_type: str | None = None,
    ahe_budget: dict[str, int] | None = None,
) -> str:
    """Run one checkpointed 17-step PCB pipeline without run-directory races."""
    out = _run_dir(run_name)
    try:
        with _serialize_pipeline_run(out):
            return _run_pcb_pipeline_unlocked(
                requirement=requirement,
                run_name=run_name,
                project_name=project_name,
                llm_mode=llm_mode,
                model_name=model_name,
                model_type=model_type,
                ahe_budget=ahe_budget,
            )
    except OSError as exc:
        return _json(
            {
                "status": "error",
                "error": f"could not lock run directory: {exc}",
                "llm_mode": llm_mode,
            }
        )


def ratsnest_run_pcb_pipeline_until(
    requirement: str,
    until_step: str,
    run_name: str = "pcb",
    project_name: str = "board",
    llm_mode: LlmModeName = "auto",
    model_name: str | None = None,
    model_type: str | None = None,
    ahe_budget: dict[str, int] | None = None,
) -> str:
    """Advance one checkpointed run through ``until_step``.

    The operation is idempotent for a stable requirement/run pair: completed
    canonical steps are restored from ``pipeline_state.json`` and skipped.
    Temporal Activities use this boundary so a worker retry never creates a
    second pipeline implementation or repeats already committed steps.
    """

    out = _run_dir(run_name)
    try:
        with _serialize_pipeline_run(out):
            return _run_pcb_pipeline_unlocked(
                requirement=requirement,
                run_name=run_name,
                project_name=project_name,
                llm_mode=llm_mode,
                model_name=model_name,
                model_type=model_type,
                until_step=until_step,
                external_retry_managed=True,
                ahe_budget=ahe_budget,
            )
    except OSError as exc:
        return _json(
            {
                "status": "error",
                "error": f"could not lock run directory: {exc}",
                "error_type": "transient_io_error",
                "llm_mode": llm_mode,
            }
        )


def ratsnest_review_kicad_project(
    project_path: str,
    report_name: str = "design-review.md",
    llm_mode: LlmModeName = "offline",
    model_name: str | None = None,
    model_type: str | None = None,
) -> str:
    """Review a KiCad project located inside the RatsNestPro workspace.

    project_path may be absolute or workspace-relative, but cannot escape the
    workspace. The deterministic findings remain authoritative; EricAI can only
    add advisory narrative and triage.
    """
    try:
        project = _workspace_path(project_path)
        mode = parse_mode(llm_mode)
        client = (
            None
            if mode == LlmMode.OFFLINE
            else _ToolkitLlmClient(
                model_name=model_name,
                model_type=model_type,
                transcript_path=(project if project.is_dir() else project.parent)
                / "llm_outputs-review.jsonl",
                phase="reviewer",
            )
        )
        reviewed = review_project(project, mode=mode, client=client)
        schematic_path, pcb_path = _paired_project_files(
            project,
            reviewed.schematic_path,
            reviewed.pcb_path,
        )
        verification = _verification(
            schematic_path,
            pcb_path,
        )
        verification_blockers = _verification_blockers(verification)
        placement_review = (
            review_pcb_placement_constraints(Path(pcb_path))
            if pcb_path
            else None
        )
        placement_review_blockers = (
            placement_review.violations
            if placement_review is not None and placement_review.manifest_found
            else []
        )
        review_report = getattr(reviewed.result, "report", None)
        component_release_gate = (
            review_report.gate("component_release")
            if review_report is not None and callable(getattr(review_report, "gate", None))
            else None
        )
        component_review_blockers = (
            [finding.summary for finding in component_release_gate.findings]
            if component_release_gate is not None
            else []
        )
        blocked = (
            reviewed.blocked
            or bool(verification_blockers)
            or bool(placement_review_blockers)
        )
        verdict_reasons = [
            *verification_blockers,
            *component_review_blockers,
            *placement_review_blockers,
            *(
                ["deterministic project-review gates failed"]
                if reviewed.blocked and not component_review_blockers
                else []
            ),
        ]
        authoritative_verdict = {
            "schema_version": 1,
            "source": "deterministic_project_and_kicad_cli_gates",
            "verdict": "BLOCKED" if blocked else "PASS",
            "blocked": blocked,
            "reasons": verdict_reasons,
        }
        verdict_markdown = "\n".join(
            [
                "# Authoritative review verdict",
                "",
                f"**Verdict:** **{authoritative_verdict['verdict']}**",
                "",
                *(
                    [f"- {reason}" for reason in verdict_reasons]
                    if verdict_reasons
                    else ["- All required deterministic and kicad-cli gates passed."]
                ),
                "",
                (
                    "Only this deterministic section defines release status. "
                    "The advisory analysis below cannot override it."
                ),
            ]
        )
        advisory_review = {
            "schema_version": 1,
            "source": reviewed.result.source,
            "can_override_verdict": False,
            "markdown": reviewed.advisory_markdown,
        }
        placement_markdown = "\n".join(
            [
                "# Placement constraint review",
                "",
                *(
                    [
                        f"- Manifest: `{placement_review.manifest_path}`",
                        f"- Evaluated against final PCB: `{placement_review.evaluated}`",
                        f"- Digest valid: `{placement_review.digest_valid}`",
                        f"- Final footprint count: `{placement_review.placement_count}`",
                        *(
                            [f"- BLOCKER: {item}" for item in placement_review.violations]
                            if placement_review.violations
                            else ["- No persisted placement-constraint violations found."]
                        ),
                    ]
                    if placement_review is not None
                    and placement_review.manifest_found
                    else [
                        "- Unavailable for this project: no placement-constraint "
                        "manifest was found. Legacy projects are not failed solely "
                        "for this missing sidecar."
                    ]
                ),
            ]
        )
        review_markdown = "\n\n".join(
            [
                verdict_markdown,
                _verification_markdown(verification),
                placement_markdown,
                advisory_review["markdown"],
            ]
        )
        report_dir = _workspace_root() / "reviews"
        report_dir.mkdir(parents=True, exist_ok=True)
        report_path = report_dir / _name(report_name, "design-review.md")
        if report_path.suffix.lower() != ".md":
            report_path = report_path.with_suffix(".md")
        report_path.write_text(review_markdown, encoding="utf-8")
        return _json(
            {
                "status": "blocked" if blocked else "ok",
                "workspace": str(_workspace_root()),
                "project_path": str(project),
                "report_path": str(report_path),
                "schematic_path": str(schematic_path) if schematic_path else None,
                "pcb_path": str(pcb_path) if pcb_path else None,
                "verification": verification,
                "verification_blockers": verification_blockers,
                "placement_constraints": (
                    placement_review.model_dump(mode="json")
                    if placement_review is not None
                    else None
                ),
                "component_release": {
                    "status": (
                        component_release_gate.status.value
                        if component_release_gate is not None
                        else "unavailable"
                    ),
                    "blockers": component_review_blockers,
                },
                "authoritative_verdict": authoritative_verdict,
                "advisory_review": advisory_review,
                "review": review_markdown,
            }
        )
    except (LlmError, ReviewProjectError, ValueError) as exc:
        return _json({"status": "error", "error": str(exc)})


def ratsnest_search_parts(query: str, limit: int = 10) -> str:
    """Search the grounded local JLCPCB SQLite cache without inventing parts."""
    selector = PartSelector()
    if not selector.available():
        return _json(
            {
                "status": "unavailable",
                "error": "No local JLCPCB cache is available.",
                "cache_hint": "Mount jlcpcb.sqlite under KICAD_MCP_HOME.",
            }
        )
    hits = selector.search(query, limit=max(1, min(limit, 50)))
    return _json(
        {
            "status": "ok",
            "query": query,
            "results": [
                {
                    "lcsc": item.lcsc,
                    "mpn": item.mpn,
                    "description": item.description,
                    "package": item.package,
                    "basic": item.basic,
                    "stock": item.stock,
                    "price": item.price,
                }
                for item in hits
            ],
        }
    )


def _symbol_match_score(query: str, lib_id: str) -> float:
    wanted = re.sub(r"[^a-z0-9]", "", query.lower())
    symbol_name = lib_id.partition(":")[2]
    candidate = re.sub(r"[^a-z0-9]", "", symbol_name.lower())
    if not wanted or not candidate:
        return 0.0
    match_kind = grounding.symbol_identity_match_kind(query, symbol_name)
    if match_kind == "exact":
        return 4.0
    if match_kind == "kicad_wildcard":
        return 3.5
    if match_kind == "qualified_base":
        return 3.0
    if wanted in candidate or candidate in wanted:
        overlap = min(len(wanted), len(candidate)) / max(
            len(wanted),
            len(candidate),
        )
        return 2.0 + 0.5 * overlap
    return difflib.SequenceMatcher(None, wanted, candidate).ratio()


def _focused_symbol_ids(query: str) -> list[str]:
    upper = query.upper()
    patterns: list[str] = []
    if upper.startswith("STM32"):
        family = re.match(r"STM32([A-Z]\d)", upper)
        patterns = [f"MCU_ST_STM32{family.group(1)}"] if family else ["MCU_ST_STM32*"]
    elif upper.startswith("RP"):
        patterns = ["MCU_RaspberryPi"]
    elif upper.startswith(("ATMEGA", "ATTINY")):
        patterns = ["MCU_Microchip_ATmega", "MCU_Microchip_ATtiny"]
    elif upper.startswith("SAMD"):
        patterns = ["MCU_Microchip_SAMD"]
    elif upper.startswith("PIC"):
        patterns = ["MCU_Microchip_PIC*"]
    elif upper.startswith("CH32"):
        patterns = ["MCU_WCH_CH32*"]
    elif upper.startswith("ESP32"):
        patterns = ["RF_Module"]
    elif upper.startswith("NRF"):
        patterns = ["MCU_Nordic", "RF_Module"]
    if not patterns:
        return list(grounding.symbol_index())
    patterns.append(GENERATED_LIBRARY_NICKNAME)

    folded_patterns = tuple(pattern.casefold() for pattern in patterns)
    return [
        lib_id
        for lib_id in grounding.symbol_index()
        if any(
            fnmatch.fnmatchcase(
                lib_id.partition(":")[0].casefold(),
                pattern,
            )
            for pattern in folded_patterns
        )
    ]


def _symbol_candidate_match_score(query: str, lib_id: str) -> float:
    """Score the electrical device identity, not its library namespace."""

    if ":" in query and query.strip().casefold() == lib_id.casefold():
        return 4.0
    _library, _separator, symbol_name = lib_id.partition(":")
    return max(
        _symbol_match_score(query, symbol_name),
        _symbol_match_score(query, lib_id),
    )


def _normalized_symbol_name(value: str) -> str:
    return "".join(char.casefold() for char in value if char.isalnum())


def _common_edge_length(left: str, right: str, *, reverse: bool = False) -> int:
    if reverse:
        left = left[::-1]
        right = right[::-1]
    return next(
        (
            index
            for index, (left_char, right_char) in enumerate(zip(left, right, strict=False))
            if left_char != right_char
        ),
        min(len(left), len(right)),
    )


def _symbol_rank_key(query: str, ranked: tuple[float, str]) -> tuple[Any, ...]:
    """Keep fuzzy discovery deterministic without turning it into grounding."""

    score, lib_id = ranked
    wanted = _normalized_symbol_name(query)
    candidate = _normalized_symbol_name(lib_id.partition(":")[2])
    return (
        -score,
        -_common_edge_length(wanted, candidate),
        -_common_edge_length(wanted, candidate, reverse=True),
        abs(len(wanted) - len(candidate)),
        lib_id.casefold(),
        lib_id,
    )


def _expected_symbol_name(query: str) -> str | None:
    """Return a requested identity label only when the query is unambiguous."""

    raw = query.strip()
    if not raw:
        return None
    if ":" in raw:
        _library, _separator, name = raw.partition(":")
        return name if re.fullmatch(r"[A-Za-z0-9_.+\-/]+", name) else None
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.+\-/]*", raw):
        return None
    compact = _normalized_symbol_name(raw)
    if (
        len(compact) >= 5
        and any(char.isalpha() for char in compact)
        and any(char.isdigit() for char in compact)
    ):
        return raw
    return None


def _reusable_footprint_options(
    discovery_matches: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Expose verified installed footprints as options, not target matches."""

    sources_by_footprint: dict[str, set[str]] = {}
    pad_counts: dict[str, int] = {}
    for entry in discovery_matches:
        lib_id = str(entry.get("grounded_footprint") or "").strip()
        if not lib_id or not entry.get("footprint_exists"):
            continue
        pad_numbers = footprints.footprint_pad_numbers(lib_id)
        if pad_numbers is None:
            continue
        sources_by_footprint.setdefault(lib_id, set()).add(str(entry["lib_id"]))
        pad_counts[lib_id] = len(pad_numbers)
    return [
        {
            "lib_id": lib_id,
            "pad_count": pad_counts[lib_id],
            "source_symbol_ids": sorted(sources_by_footprint[lib_id]),
        }
        for lib_id in sorted(sources_by_footprint, key=lambda item: (item.casefold(), item))
    ]


def ratsnest_lookup_kicad_symbol(query: str, limit: int = 3) -> str:
    """Return identity-grounded symbols separately from discovery suggestions."""

    result_limit = max(1, min(limit, 5))
    ranked = sorted(
        (
            (_symbol_candidate_match_score(query, lib_id), lib_id)
            for lib_id in _focused_symbol_ids(query)
        ),
        key=lambda item: _symbol_rank_key(query, item),
    )
    matches: list[dict[str, Any]] = []
    discovery_matches: list[dict[str, Any]] = []
    for score, lib_id in ranked:
        if score < 0.72:
            break
        info = symbols.symbol_info(lib_id)
        if info is None:
            continue
        pins = sorted(
            info["pins"],
            key=lambda pin: (
                (
                    0,
                    int(pin["number"]),
                )
                if str(pin["number"]).isdigit()
                else (1, str(pin["number"]))
            ),
        )
        declared_footprint = str(info["properties"].get("Footprint", "")).strip()
        grounded_footprint = (
            grounding.ground_footprint(declared_footprint) if declared_footprint else None
        )
        footprint_exists = bool(
            grounded_footprint and footprints.footprint_pad_numbers(grounded_footprint) is not None
        )
        symbol_name = lib_id.partition(":")[2]
        library_value = str(info["properties"].get("Value", "")).strip()
        match_kinds = [
            grounding.symbol_identity_match_kind(query, candidate)
            for candidate in (symbol_name, library_value)
            if candidate
        ]
        if ":" in query and query.strip().casefold() == lib_id.casefold():
            match_kinds.insert(0, "exact")
        identity_relation = next(
            (kind for kind in ("exact", "kicad_wildcard", "qualified_base") if kind in match_kinds),
            None,
        )
        grounded = identity_relation in {"exact", "kicad_wildcard"}
        entry = {
            "lib_id": lib_id,
            "origin": (
                "evidence_generated"
                if lib_id.partition(":")[0] == GENERATED_LIBRARY_NICKNAME
                else "installed"
            ),
            "score": round(score, 4),
            "match_kind": identity_relation or "discovery_only",
            "grounded": grounded,
            "symbol_ok": grounded,
            "resolution_eligible": grounded,
            "pin_count": info["pin_count"],
            "pins": [
                {
                    "number": str(pin["number"]),
                    "name": str(pin["name"]),
                    "type": str(pin["type"]),
                }
                for pin in pins
            ],
            "properties": info["properties"],
            "declared_footprint": declared_footprint,
            "grounded_footprint": grounded_footprint,
            "footprint_exists": footprint_exists,
        }
        if grounded:
            matches.append(entry)
        elif len(discovery_matches) < result_limit:
            discovery_matches.append(entry)
        if len(matches) >= result_limit:
            break
    visible_discovery = [] if matches else discovery_matches
    reusable_footprints = _reusable_footprint_options(visible_discovery)
    return _json(
        {
            "status": "ok" if matches else "no_results",
            "query": query,
            "source": "installed KiCad symbol and footprint libraries",
            "symbol_ok": bool(matches),
            "grounded": bool(matches),
            "exact_symbol_missing": not bool(matches),
            "expected_symbol_name": _expected_symbol_name(query),
            "candidates": matches,
            "discovery_candidates": visible_discovery,
            "reusable_footprints": reusable_footprints,
        }
    )


def ratsnest_validate_kicad_binding(
    symbol_lib_id: str,
    footprint_lib_id: str,
) -> str:
    """Validate one explicitly requested symbol/footprint pair without substitution."""

    symbol_id = symbol_lib_id.strip()
    footprint_id = footprint_lib_id.strip()
    info = symbols.symbol_info(symbol_id) if ":" in symbol_id else None
    pad_numbers = (
        footprints.footprint_pad_numbers(footprint_id)
        if ":" in footprint_id
        else None
    )
    pin_numbers = {
        str(pin.get("number", "")).strip()
        for pin in (info or {}).get("pins", [])
        if str(pin.get("number", "")).strip()
    }
    actual_pad_numbers = {
        str(number).strip()
        for number in (pad_numbers or [])
        if str(number).strip()
    }
    symbol_exists = info is not None
    footprint_exists = pad_numbers is not None
    compatible = (
        symbol_exists
        and footprint_exists
        and bool(pin_numbers)
        and pin_numbers == actual_pad_numbers
    )
    blockers: list[str] = []
    if not symbol_exists:
        blockers.append("exact symbol lib_id is not installed")
    if not footprint_exists:
        blockers.append("exact footprint lib_id is not installed")
    if symbol_exists and footprint_exists and not compatible:
        blockers.append("symbol pin numbers do not match footprint pad numbers")
    return _json(
        {
            "status": "ok" if compatible else "blocked",
            "symbol_lib_id": symbol_id,
            "footprint_lib_id": footprint_id,
            "symbol_exists": symbol_exists,
            "footprint_exists": footprint_exists,
            "pin_numbers": sorted(pin_numbers),
            "pad_numbers": sorted(actual_pad_numbers),
            "pin_pad_compatible": compatible,
            "blockers": blockers,
        }
    )


def ratsnest_generate_local_kicad_library(
    spec: dict[str, Any],
    project_dir: str = "",
    allowed_footprint_lib_ids: list[str] | None = None,
) -> str:
    """Generate one exact, officially evidenced workspace-local KiCad part.

    A symbol-only specification may reuse a footprint that deterministic
    library discovery placed in ``allowed_footprint_lib_ids``.  The allowlist
    is deliberately supplied out of band so model output cannot authorize its
    own footprint choice.
    """

    if "footprint_lib_id" in spec:
        result = generate_local_symbol_library(
            spec,
            allowed_footprint_lib_ids=allowed_footprint_lib_ids or (),
            project_dir=project_dir or None,
        )
    else:
        result = generate_local_library(
            spec,
            project_dir=project_dir or None,
        )
    return _json(result.model_dump(mode="json"))
