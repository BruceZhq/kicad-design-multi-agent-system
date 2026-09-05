from __future__ import annotations

import json
from pathlib import Path

import pytest

from ratsnestpro.eda.routing_rules import (
    apply_dsn_classes,
    bind_net_classes,
    persist_project_classes,
)
from ratsnestpro.eda.schematic_wiring import draw_local_nets, intersects, local_path
from ratsnestpro.eda.vendor.sexpr import find_first, loads, tag_of
from ratsnestpro.orchestration.engineering_workspace import (
    EngineeringWorkspace,
    complete_with_observations,
)
from ratsnestpro.orchestration.review_repair import prepare_review_repair, valid_review_resume


def test_real_local_wire_paths_avoid_cross_net_t_junctions():
    path = local_path((0, 0), (10, 0), forbidden_pins=[(5, 0)],
                      forbidden_wires=[((5, -1), (5, 1))], bodies=[])
    assert path and not any(intersects(s, ((5, -1), (5, 1))) for s in path)


def test_no_safe_path_uses_explicit_label_fallback():
    class Doc:
        def add_wire(self, *args):
            raise AssertionError("must not wire through a symbol")

        def add_junction(self, *args):
            raise AssertionError("must not add a fake junction")

    receipt = draw_local_nets(Doc(), {"GND": [(0, 0), (10, 0)], "OUT": [(0, 5), (10, 5)]},
                             label_nets={"GND"}, all_pins=[], bodies=[(-100, -100, 100, 100)])
    assert receipt["label_fallbacks"] == ["OUT"]
    assert receipt["wire_count"] == 0


def _classes():
    return [dict(name="power", nets=["VIN"], width=.5, clearance=.3, via_diameter=.6, via_drill=.3),
            dict(name="signal", nets=["DATA"], width=.25, clearance=.2, via_diameter=.6, via_drill=.3)]


def test_net_membership_is_exact_and_legacy_power_class_is_bound():
    classes = [{**c, "nets": []} for c in _classes()]
    assert bind_net_classes(classes, ["VIN", "DATA"], ["VIN"]) == _classes()
    with pytest.raises(ValueError, match="multiply assigned"):
        bind_net_classes([_classes()[0], {**_classes()[1], "nets": ["VIN"]}], ["VIN"], [])
    with pytest.raises(ValueError, match="unknown"):
        bind_net_classes(_classes(), ["VIN"], [])


@pytest.mark.parametrize("subset", [None, {"VIN"}])
def test_dsn_receives_per_net_rules_and_real_critical_subset(tmp_path, subset):
    dsn = tmp_path / "board.dsn"
    dsn.write_text('''(pcb board (resolution um 10)
      (structure (via "via0"))
      (library (padstack "via0" (shape (circle F.Cu 6000))))
      (network (net "VIN" (pins J1-1 R1-1)) (net "DATA" (pins J1-2 R1-2))
               (class Default VIN DATA (rule (width 2000)))))''')
    apply_dsn_classes(dsn, _classes(), only_nets=subset)
    root = loads(dsn.read_text())
    network = find_first(root, "network")
    classes = {str(node[1]): node for node in network if tag_of(node) == "class"}
    assert str(find_first(find_first(classes["RN:power"], "rule"), "width")[1]) == "5000.0"
    assert ("RN:signal" in classes) == (subset is None)
    names = [str(node[1]) for node in find_first(root, "library") if tag_of(node) == "padstack"]
    assert len(names) == len(set(names))


def test_project_netclasses_preserve_unrelated_and_strict_project_rules(tmp_path):
    pcb = tmp_path / "board.kicad_pcb"
    pro = pcb.with_suffix(".kicad_pro")
    pro.write_text(json.dumps({"board": {"design_settings": {"rules": {"min_clearance": .3}}},
                               "net_settings": {"classes": [{"name": "Default", "clearance": .3}]}}))
    persist_project_classes(pcb, _classes())
    result = json.loads(pro.read_text())
    assert result["board"]["design_settings"]["rules"]["min_clearance"] == .3
    assert result["net_settings"]["netclass_assignments"] == {"VIN": ["RN:power"], "DATA": ["RN:signal"]}


def _review(root: Path):
    (root / "pipeline_state.json").write_text(json.dumps({"requirement": "same original", "steps": []}))
    (root / "board.kicad_pcb").write_text("physical board")
    report = root / "board.drc.json"
    report.write_text(json.dumps({"violations": [{"type": "solder_mask_bridge", "items": [{"uuid": "pad-1", "description": "U1 pin 3"}]}]}))
    return {"status": "blocked", "pcb_path": str(root / "board.kicad_pcb"), "verification": {
        "drc": {"ran": True, "errors": 1, "report_path": str(report), "by_type": {"solder_mask_bridge": 1}}}}


def test_review_handoff_binds_actual_pins_and_original_checkpoint(tmp_path):
    review = _review(tmp_path)
    original = (tmp_path / "pipeline_state.json").read_bytes()
    ticket = prepare_review_repair(tmp_path, review)
    assert ticket["resume_from_step"] == "layout_general"
    assert ticket["evidence"]["drc"]["violations"][0]["items"][0]["uuid"] == "pad-1"
    assert (tmp_path / "pipeline_state.json").read_bytes() == original
    assert valid_review_resume(tmp_path, "layout_general")
    assert not valid_review_resume(tmp_path, "selection")
    (tmp_path / "board.kicad_pcb").write_text("changed")
    assert not valid_review_resume(tmp_path, "layout_general")


def test_review_no_progress_stops_instead_of_repeating_generation(tmp_path):
    review = _review(tmp_path)
    assert prepare_review_repair(tmp_path, review)["status"] == "requested"
    assert prepare_review_repair(tmp_path, review)["status"] == "exhausted"


def test_erc_infrastructure_outage_does_not_replan_circuit(tmp_path):
    review = _review(tmp_path)
    review["verification"] = {"erc": {"ran": False, "available": False}}
    assert prepare_review_repair(tmp_path, review)["status"] == "not_actionable"


def test_renderer_delivers_image_content_to_model_not_only_a_path(tmp_path, monkeypatch):
    from ratsnestpro.eda import engineering_render

    (tmp_path / "board.kicad_pcb").write_text("board")
    preview = tmp_path / "view.png"
    preview.write_bytes(b"actual test raster")
    monkeypatch.setattr(engineering_render, "render_cad", lambda *a, **kw: {
        "image_path": str(preview), "image_sha256": "abc", "source_sha256": "def"})

    class Client:
        def complete(self, system, user):
            return '{"engineering_queries":[{"tool":"render","path":"board.kicad_pcb"}]}'

        def complete_with_images(self, system, user, *, images):
            assert images == ["data:image/png;base64,YWN0dWFsIHRlc3QgcmFzdGVy"]
            assert '"source_sha256": "def"' in user
            return '{"value":"inspected"}'

    result = complete_with_observations(Client(), "system", "repair", workspace=EngineeringWorkspace(
        out_dir=str(tmp_path), artifacts=lambda: {}), extract_json=lambda text: text)
    assert json.loads(result)["value"] == "inspected"


def test_generator_patch_surface_excludes_graders_and_policy():
    from evolution.optimizer import load_governance_policy, validate_optimizer_context_path

    policy = load_governance_policy(Path(__file__).resolve().parents[1] / "config/harness/invariants.v1.json")
    assert validate_optimizer_context_path("src/ratsnestpro/eda/materialize.py", policy)
    for path in ("src/evolution/generator_validation.py", "src/ratsnestpro/orchestration/release_invariants.py",
                 "src/agents/ratsnestpro/tools.py", ".env"):
        with pytest.raises(ValueError):
            validate_optimizer_context_path(path, policy)
