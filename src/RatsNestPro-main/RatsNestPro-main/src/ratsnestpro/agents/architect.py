"""The Architect: requirement normalization, family judgment, and parameter
selection. The LLM only judges and proposes; the params it returns are
re-validated by the Atmega328Params contract before anything downstream runs.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from pydantic import ValidationError

from ratsnestpro.agents import heuristics
from ratsnestpro.agents.llm import LLMClient, LlmError, LlmMode, parse_mode, resolve_client
from ratsnestpro.domain.contracts import FamilyDecision, RequirementSpec
from ratsnestpro.families import FAMILY_ID, Atmega328Params

_SYSTEM = (
    "You are the Architect gatekeeper for RatsNestPro. Only the "
    f"'{FAMILY_ID}' family is supported: an ATmega328P USB-C development board "
    "with an LDO, crystal, decoupling capacitors, reset, optional power LED, "
    "breakout headers and mounting holes. Decide whether the request belongs "
    "to this family, whether all mandatory features are preserved, and choose "
    "in-family parameters. Respond with STRICT JSON only, no prose, matching:\n"
    '{"qualified": bool, "family": str, "mandatory_features_present": bool, '
    '"missing_features": [str], "clarifying_questions": [str], "rationale": str, '
    '"params": {"crystal_mhz": 8|16, "ldo_output_v": 3.3|5.0, '
    '"decoupling_count": int, "power_led": bool, "breakout_rows": 1|2, '
    '"breakout_pins_per_row": int, "mounting_holes": 0|4}}\n'
    "Remember: 16 MHz requires a 5.0 V supply on the ATmega328P."
)


@dataclass
class ArchitectResult:
    requirement: RequirementSpec
    decision: FamilyDecision
    params: Atmega328Params | None
    source: str  # "deterministic" | "ericai"

    @property
    def ready(self) -> bool:
        """True when a qualified, fully-parameterized design is available."""
        return self.decision.qualified and self.params is not None


def _project_name(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return (slug[:40] or "atmega328_dev_board")


def _parse_json_block(raw: str) -> dict:
    s = raw.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\n?", "", s)
        s = re.sub(r"\n?```$", "", s).strip()
    start, end = s.find("{"), s.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object found in model output")
    return json.loads(s[start : end + 1])


class Architect:
    """Produces reviewed typed intent; it has no tool or filesystem access."""

    def plan(
        self,
        requirement_text: str,
        mode: str | LlmMode = LlmMode.OFFLINE,
        client: LLMClient | None = None,
    ) -> ArchitectResult:
        mode = parse_mode(mode)
        resolved = resolve_client(mode, client)  # raises in REQUIRED if unavailable
        if resolved is None:
            return self._deterministic(requirement_text)
        try:
            return self._live(requirement_text, resolved)
        except (LlmError, ValueError, ValidationError, KeyError) as exc:
            if mode == LlmMode.REQUIRED:
                raise LlmError(f"required EricAI planning failed: {exc}") from exc
            # auto mode: fall back to the deterministic plan.
            return self._deterministic(requirement_text)

    # -- deterministic path ------------------------------------------------ #

    def _deterministic(self, text: str) -> ArchitectResult:
        req = RequirementSpec(raw_text=text, project_name=_project_name(text))
        decision = heuristics.judge_family(text)
        if not decision.qualified:
            return ArchitectResult(req, decision, None, "deterministic")
        try:
            params = Atmega328Params(**heuristics.params_from_requirement(text))  # type: ignore[arg-type]
        except ValidationError as exc:
            decision = decision.model_copy(
                update={
                    "clarifying_questions": [e["msg"] for e in exc.errors()],
                }
            )
            return ArchitectResult(req, decision, None, "deterministic")
        return ArchitectResult(req, decision, params, "deterministic")

    # -- live (EricAI) path ------------------------------------------------ #

    def _live(self, text: str, client: LLMClient) -> ArchitectResult:
        raw = client.complete(_SYSTEM, text)
        data = _parse_json_block(raw)
        req = RequirementSpec(raw_text=text, project_name=_project_name(text))

        params_data = data.pop("params", None)
        decision = FamilyDecision(
            qualified=bool(data.get("qualified", False)),
            family=str(data.get("family", "")),
            mandatory_features_present=bool(data.get("mandatory_features_present", False)),
            missing_features=list(data.get("missing_features", []) or []),
            clarifying_questions=list(data.get("clarifying_questions", []) or []),
            rationale=str(data.get("rationale", "")),
        )
        # Reject a qualified verdict for the wrong family.
        if decision.qualified and decision.family and decision.family != FAMILY_ID:
            decision = decision.model_copy(
                update={"qualified": False, "mandatory_features_present": False}
            )
        if not decision.qualified or not decision.mandatory_features_present:
            return ArchitectResult(req, decision, None, "ericai")
        if not isinstance(params_data, dict):
            raise ValueError("qualified design missing params object")
        # Re-validate the model-proposed params through the contract.
        params = Atmega328Params(**params_data)
        return ArchitectResult(req, decision, params, "ericai")
