"""LLM brain seams: typed contracts enforced, deterministic fallback proven.

FakeLlm exercises the full brain path without network — validation is the
product here: the LLM proposes, the contracts dispose.
"""

import pytest

from ratsnest.llm import extract_json
from ratsnest.schemas import (
    DesignSpec,
    Finding,
    RepairHint,
    RepairOp,
    RepairOpType,
    StrategyBundle,
)


class FakeLlm:
    """Injectable brain returning canned JSON per agent name."""

    def __init__(self, responses: dict):
        self.responses = responses
        self.calls: list[str] = []
        self.available = True

    def complete_json(self, agent, system, user, max_tokens=0):
        self.calls.append(agent)
        return self.responses.get(agent)


# -- json extraction ----------------------------------------------------------

def test_extract_json_plain_fenced_and_noise():
    assert extract_json('{"a": 1}') == {"a": 1}
    assert extract_json('```json\n{"a": {"b": 2}}\n```') == {"a": {"b": 2}}
    assert extract_json('Sure! Here is the plan:\n{"x": "y"} done') == {"x": "y"}
    assert extract_json("no json here") is None
    assert extract_json('{"broken": ') is None


# -- seam 1: requirement understanding ----------------------------------------

def test_requirement_agent_llm_valid_and_invalid():
    from ratsnest.design_gen.requirement_agent import parse_requirement_llm

    good = FakeLlm({"requirement_agent": {
        "project_name": "Car Dash Cam supply!",
        "input_voltage": 12, "output_voltage": 3.3,
        "output_current_a": 1.0, "led": "green"}})
    spec = parse_requirement_llm("给行车记录仪做一个12V转3.3V的供电板，绿灯指示", good)
    assert spec is not None
    assert spec.output_voltage == 3.3 and spec.led == "green"
    assert spec.project_name == "car_dash_cam_supply"  # slug normalized

    # contract gate: Vout >= Vin is impossible for this family -> fallback
    bad = FakeLlm({"requirement_agent": {
        "project_name": "x", "input_voltage": 3.3, "output_voltage": 12}})
    assert parse_requirement_llm("boost 3.3 to 12", bad) is None

    # no LLM -> None (caller uses deterministic extractor)
    off = FakeLlm({})
    off.available = False
    assert parse_requirement_llm("12V to 5V", off) is None


def test_requirement_agent_honors_required_brain_mode():
    from ratsnest.config import Config
    from ratsnest.design_gen.requirement_agent import parse_requirement_llm
    from ratsnest.llm import BrainRequiredError, LlmClient

    config = Config.load()
    config.llm_enabled = True
    config.llm_required = True
    config.llm_provider = "deepseek"
    config.llm_api_key = None

    with pytest.raises(BrainRequiredError):
        parse_requirement_llm("12V to 5V", LlmClient(config))


def test_llm_client_routes_models_retries_and_enforces_budget(monkeypatch):
    import ratsnest.llm as llm_module
    from ratsnest.config import Config
    from ratsnest.llm import BrainRequiredError, LlmClient

    class Response:
        def __init__(self, status, payload):
            self.status_code = status
            self._payload = payload
            self.text = "temporary" if status != 200 else ""

        def json(self):
            return self._payload

    class Httpx:
        calls = []
        responses = [
            Response(500, {}),
            Response(200, {
                "choices": [{"message": {"content": '{"ok": true}'}}],
                "usage": {"total_tokens": 17},
            }),
        ]

        @classmethod
        def post(cls, *args, **kwargs):
            cls.calls.append(kwargs)
            return cls.responses.pop(0)

    config = Config.load()
    config.llm_enabled = True
    config.llm_required = False
    config.llm_provider = "deepseek"
    config.llm_api_key = "test-key"
    config.llm_retries = 1
    config.llm_max_calls = 2
    config.llm_max_total_tokens = 100
    config.llm_model_routes = {"circuit_architect": "deepseek-reasoner"}
    monkeypatch.setattr(llm_module, "httpx", Httpx)
    monkeypatch.setattr(llm_module.time, "sleep", lambda _: None)

    client = LlmClient(config)
    assert client.complete_json(
        "circuit_architect", "system", "user") == {"ok": True}
    assert len(Httpx.calls) == 2
    assert Httpx.calls[-1]["json"]["model"] == "deepseek-reasoner"
    assert client.total_tokens_used == 17
    assert client.complete_json("repair_agent", "system", "user") is None

    config.llm_required = True
    required = LlmClient(config)
    required.calls_used = config.llm_max_calls
    with pytest.raises(BrainRequiredError, match="budget"):
        required.complete_json("repair_agent", "system", "user")


# -- seam 2: circuit architect -------------------------------------------------

def _architect_inputs():
    from ratsnest.circuit_math import solve_circuit
    from ratsnest.config import Config
    from ratsnest.evolution import StrategyRegistry

    spec = DesignSpec(
        project_name="architect_test", input_voltage=12,
        output_voltage=5, output_current_a=0.5, led="red",
        requirement_text="12V to 5V board with red LED")
    config = Config.load()
    strategy = StrategyRegistry(config.strategies_dir).load_active()[1]
    solved = solve_circuit(spec, strategy, config)
    return spec, solved, strategy


def _canonical_plan(tmp_path):
    from ratsnest.crews.blackboard import DesignBlackboard
    from ratsnest.crews.design_agents import CircuitArchitect

    spec, solved, strategy = _architect_inputs()
    blackboard = DesignBlackboard(str(tmp_path / "canonical"))
    plan = CircuitArchitect(strategy, blackboard).create_plan(spec, solved)
    return plan


def test_circuit_architect_accepts_bounded_llm_plan(tmp_path):
    from ratsnest.crews.blackboard import DesignBlackboard
    from ratsnest.crews.design_agents import CircuitArchitect

    canonical = _canonical_plan(tmp_path)
    proposal = canonical.model_dump(mode="json")
    proposal["rationale"] = "Power flows left to right with a short FB loop"
    llm = FakeLlm({"circuit_architect": proposal})
    spec, solved, strategy = _architect_inputs()
    blackboard = DesignBlackboard(str(tmp_path / "llm"))

    selected = CircuitArchitect(strategy, blackboard, llm=llm).create_plan(
        spec, solved)

    assert selected.outline == canonical.outline
    assert "short FB loop" in selected.rationale
    assert llm.calls == ["circuit_architect"]
    assert blackboard.state.board_plan == selected


def test_circuit_architect_rejects_catalog_or_topology_mutation(tmp_path):
    from ratsnest.crews.blackboard import DesignBlackboard
    from ratsnest.crews.design_agents import CircuitArchitect

    canonical = _canonical_plan(tmp_path)
    proposal = canonical.model_dump(mode="json")
    proposal["components"][0]["value"] = "LLM invented part"
    proposal["topology"] = "boost_converter"
    llm = FakeLlm({"circuit_architect": proposal})
    spec, solved, strategy = _architect_inputs()
    blackboard = DesignBlackboard(str(tmp_path / "fallback"))

    selected = CircuitArchitect(strategy, blackboard, llm=llm).create_plan(
        spec, solved)

    assert selected.topology == canonical.topology
    assert selected.components == canonical.components
    assert selected.rationale == canonical.rationale


# -- seam 3: repair reasoning ---------------------------------------------------

def _hints():
    op1 = RepairOp(op=RepairOpType.set_value, ref="R1",
                   params={"value": "3k"}, finding_id="RN-VOUT-001:R1")
    op2 = RepairOp(op=RepairOpType.set_value, ref="R3",
                   params={"value": "330"}, finding_id="LR-001:R3")
    return [RepairHint(finding_id="RN-VOUT-001:R1", repair_type="feedback_divider",
                       suggested_ops=[op1], explanation="divider"),
            RepairHint(finding_id="LR-001:R3", repair_type="led_resistor",
                       suggested_ops=[op2], explanation="led")]


def test_repair_reasoner_filters_and_explains():
    from ratsnest.agents.repair_planner import _reason_about_repairs
    llm = FakeLlm({"repair_reasoner": {
        "approve": ["RN-VOUT-001:R1"],
        "reject": [{"finding_id": "LR-001:R3", "reason": "LED brightness ok"}],
        "notes": {"RN-VOUT-001:R1": "restores the 5V rail"}}})
    decision = _reason_about_repairs(_hints(), [], llm)
    assert decision is not None
    assert decision["approve"] == {"RN-VOUT-001:R1"}
    assert "LED brightness" in decision["rejects"]["LR-001:R3"]


def test_repair_reasoner_bogus_ids_fail_open():
    from ratsnest.agents.repair_planner import _reason_about_repairs
    llm = FakeLlm({"repair_reasoner": {
        "approve": ["NOT-A-REAL-ID"], "reject": [], "notes": {}}})
    assert _reason_about_repairs(_hints(), [], llm) is None  # keep everything


# -- seam 5: evolution proposer --------------------------------------------------

def test_evolution_proposer_bounded_diff():
    from ratsnest.evolution.proposer import propose_candidate
    incumbent = StrategyBundle(name="v0",
                               solver_params={"vref_table": {"AP1117": 1.25}})
    llm = FakeLlm({"evolution_agent": {
        "vref_table_add": {"LM1117": 1.25, "EVIL": 99.0},
        "weight_updates": {"warning": 5, "error": 9999},
        "prompt_updates": {
            "circuit_architect": "Prefer compact power flow while preserving every contract.",
            "untrusted_agent": "This policy must never be installed."},
        "tool_policy_updates": {
            "schematic_designer": {
                "max_steps": 6, "max_actions_per_step": 10},
            "pcb_designer": {"max_steps": 99},
            "untrusted_agent": {"max_steps": 2}},
        "name_suffix": "lm1117 vref!!",
        "rationale": "3 runs escalated on LM1117 divider findings"}})
    result = propose_candidate(incumbent, {"runs": 5}, llm)
    assert result is not None
    name, bundle, rationale = result
    assert bundle.solver_params["vref_table"]["LM1117"] == 1.25
    assert "EVIL" not in bundle.solver_params["vref_table"]   # out of bounds
    assert bundle.scorecard_weights["warning"] == 5.0
    assert bundle.scorecard_weights["error"] == 30.0          # 9999 rejected
    assert "circuit_architect" in bundle.prompts
    assert "untrusted_agent" not in bundle.prompts
    assert bundle.solver_params["tool_policies"]["schematic_designer"] == {
        "max_steps": 6, "max_actions_per_step": 10}
    assert "pcb_designer" not in bundle.solver_params["tool_policies"]
    assert "untrusted_agent" not in bundle.solver_params["tool_policies"]
    assert name.startswith("candidate-llm-")
    assert bundle.version_id() != incumbent.version_id()


def test_evolution_proposer_empty_diff_is_none():
    from ratsnest.evolution.proposer import propose_candidate
    incumbent = StrategyBundle(name="v0")
    llm = FakeLlm({"evolution_agent": {"rationale": "no idea"}})
    assert propose_candidate(incumbent, {}, llm) is None
