"""Design generation pipeline: requirement -> spec -> KiCad project -> clean review."""

import pytest

from ratsnest.agents import synthesize
from ratsnest.circuit_math import GenerationError
from ratsnest.config import Config
from ratsnest.design_gen import generate_project, parse_requirement
from ratsnest.design_gen.templates import rail_name
from ratsnest.evolution import StrategyRegistry
from ratsnest.kh_adapter import KicadHappyAdapter
from ratsnest.schemas import DesignSpec


def _strategy():
    return StrategyRegistry().load_active()[1]


def test_parse_requirement_roles():
    spec = parse_requirement("a board from 12V to 3.3V with a green LED, 500mA")
    assert spec.input_voltage == 12.0
    assert spec.output_voltage == 3.3
    assert spec.led == "green"
    assert spec.output_current_a == 0.5

    spec2 = parse_requirement("9V input, 5V output, no led")
    assert (spec2.input_voltage, spec2.output_voltage) == (9.0, 5.0)
    assert spec2.led is None

    spec3 = parse_requirement("simple 12V 5V regulator")  # bare voltages
    assert (spec3.input_voltage, spec3.output_voltage) == (12.0, 5.0)


def test_rail_name():
    assert rail_name(5) == "+5V"
    assert rail_name(3.3) == "+3V3"
    assert rail_name(12.0) == "+12V"


def test_generated_5v_board_is_clean(tmp_path):
    spec = DesignSpec(project_name="gen5v", input_voltage=7, output_voltage=5,
                      output_current_a=0.05, topology="ldo", led="red")
    out = generate_project(spec, tmp_path / "gen5v", _strategy())
    ev = synthesize(KicadHappyAdapter().analyze_all(out), _strategy(), out)
    assert ev.scorecard.score == 100.0, [
        (f.severity, f.rule_id, (f.model_extra or {}).get("summary"))
        for f in ev.findings if f.severity in ("error", "warning")]
    # The catalog-bound TLV1117 divider must solve the approved 5V target.
    reg = [f for f in ev.findings if f.rule_id == "PR-DET"][0]
    div = (reg.model_extra or {})["feedback_divider"]
    achieved = (1.25 * (1 + div["r_bottom"]["ohms"] / div["r_top"]["ohms"])
                + 80e-6 * div["r_bottom"]["ohms"])
    assert achieved == pytest.approx(5.0, rel=0.02)


def test_generated_3v3_board_within_tolerance(tmp_path):
    spec = DesignSpec(project_name="gen3v3", input_voltage=5,
                      output_voltage=3.3, output_current_a=0.05,
                      topology="ldo", led="green")
    out = generate_project(spec, tmp_path / "gen3v3", _strategy())
    ev = synthesize(KicadHappyAdapter().analyze_all(out), _strategy(), out)
    # no vout-mismatch: chosen E24 pair is within the 2% strategy tolerance
    assert not [f for f in ev.findings if f.rule_id == "RN-VOUT-001"]
    assert ev.scorecard.score == 100.0


def test_generated_board_without_led(tmp_path):
    spec = DesignSpec(project_name="nolen", input_voltage=7, output_voltage=5,
                      output_current_a=0.05, topology="ldo", led=None)
    out = generate_project(spec, tmp_path / "noled", _strategy())
    envelope = KicadHappyAdapter().analyze_schematic(out)
    comps = {c["reference"] for c in (envelope.model_extra or {})["components"]}
    assert comps == {"U1", "R1", "R2"}


def test_generation_rejects_impossible_specs(tmp_path):
    with pytest.raises(GenerationError, match="step-down"):
        generate_project(DesignSpec(input_voltage=5, output_voltage=12),
                         tmp_path / "bad1", _strategy())
    with pytest.raises(GenerationError, match="headroom"):
        generate_project(DesignSpec(input_voltage=5, output_voltage=3.3,
                                    output_current_a=0.05, topology="ldo",
                                    led="blue"),  # Vf 3.1 leaves <0.3V headroom
                         tmp_path / "bad2", _strategy())


def test_generation_is_deterministic(tmp_path):
    spec = DesignSpec(project_name="det", input_voltage=7, output_voltage=5,
                      output_current_a=0.05, topology="ldo")
    a = generate_project(spec, tmp_path / "a", _strategy())
    b = generate_project(spec, tmp_path / "b", _strategy())
    assert (a / "det.kicad_sch").read_text() == (b / "det.kicad_sch").read_text()
