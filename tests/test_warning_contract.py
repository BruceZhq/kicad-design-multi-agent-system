from __future__ import annotations

import json
from pathlib import Path

from agents.ratsnestpro.tools import _verification_blockers
from agents.ratsnestpro.warning_contract import (
    WAIVER_SCHEMA_VERSION,
    apply_warning_contract,
    classify_warnings,
    sha256_file,
)


def _verification(classifications: dict[str, object]) -> dict[str, object]:
    return {
        "erc": {"applicable": False},
        "drc": {
            "applicable": True,
            "available": True,
            "ran": True,
            "errors": 0,
            "unconnected": 0,
            "warning_classifications": classifications,
        },
    }


def _warning(rule_id: str, ref: str = "U1") -> dict[str, object]:
    return {
        "severity": "warning",
        "type": rule_id,
        "description": f"Footprint {ref} differs from library",
        "items": [{"description": f"Footprint {ref}"}],
    }


def _write_footprint_pair(
    tmp_path: Path,
    *,
    board_pad_size: str = "1 1.45",
) -> tuple[Path, Path]:
    library = (
        tmp_path
        / ".ratsnest-libs"
        / "footprints"
        / "Package_SO.pretty"
        / "SOIC-8.kicad_mod"
    )
    library.parent.mkdir(parents=True)
    library.write_text(
        """
        (footprint "SOIC-8"
          (version 20240108)
          (generator pcbnew)
          (layer "F.Cu")
          (attr smd)
          (pad "1" smd roundrect (at -0.95 0) (size 1 1.45)
            (layers "F.Cu" "F.Paste" "F.Mask")
            (roundrect_rratio 0.25)
            (uuid "11111111-1111-1111-1111-111111111111")))
        """,
        encoding="utf-8",
    )
    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_text(
        f"""
        (kicad_pcb
          (version 20240108)
          (generator pcbnew)
          (footprint "Package_SO:SOIC-8"
            (layer "F.Cu")
            (at 10 20 90)
            (property "Reference" "U1")
            (property "Value" "IC")
            (uuid "22222222-2222-2222-2222-222222222222")
            (attr smd)
            (pad "1" smd roundrect (at -0.95 0 90) (size {board_pad_size})
              (layers "F.Cu" "F.Paste" "F.Mask")
              (roundrect_rratio 0.25)
              (net 1 "VCC")
              (uuid "33333333-3333-3333-3333-333333333333"))))
        """,
        encoding="utf-8",
    )
    report = tmp_path / "board.drc.json"
    report.write_text("{}", encoding="utf-8")
    return pcb, report


def _write_waiver(
    pcb: Path,
    report: Path,
    *,
    rule_id: str,
    count: int = 1,
    report_sha256: str | None = None,
) -> None:
    pcb.with_suffix(".warning-waivers.json").write_text(
        json.dumps({
            "schema_version": WAIVER_SCHEMA_VERSION,
            "waivers": [{
                "approved": True,
                "approved_by": "manufacturing-reviewer@example.test",
                "rationale": "Reference remains legible in the assembly drawing.",
                "rule_id": rule_id,
                "count": count,
                "pcb_sha256": sha256_file(pcb),
                "report_sha256": report_sha256 or sha256_file(report),
            }],
        }),
        encoding="utf-8",
    )


def test_library_mismatch_is_cleared_only_by_normalized_structure_evidence(
    tmp_path: Path,
) -> None:
    pcb, report = _write_footprint_pair(tmp_path)
    findings = [_warning("lib_footprint_mismatch")]

    resolved = apply_warning_contract(
        classify_warnings(findings),
        findings,
        pcb_path=pcb,
        report_path=report,
    )

    decision = resolved["lib_footprint_mismatch"]["resolution"]
    assert decision["status"] == "auto_equivalent"
    assert decision["evidence"]["equivalent"] is True
    assert len(decision["pcb_sha256"]) == 64
    assert len(decision["report_sha256"]) == 64
    assert _verification_blockers(_verification(resolved)) == []


def test_missing_global_symbol_library_is_cleared_by_project_binding(
    tmp_path: Path,
) -> None:
    symbols = tmp_path / ".ratsnest-libs" / "symbols"
    symbols.mkdir(parents=True)
    library = symbols / "Timer.kicad_sym"
    library.write_text('(kicad_symbol_lib (symbol "NE555D"))', encoding="utf-8")
    (tmp_path / "sym-lib-table").write_text(
        '(sym_lib_table (lib (name "Timer") (type "KiCad") '
        '(uri "\${KIPRJMOD}/.ratsnest-libs/symbols/Timer.kicad_sym")))',
        encoding="utf-8",
    )
    schematic = tmp_path / "board.kicad_sch"
    schematic.write_text(
        '(kicad_sch (lib_symbols (symbol "Timer:NE555D")) '
        '(symbol (lib_id "Timer:NE555D")))',
        encoding="utf-8",
    )
    report = tmp_path / "board.erc.json"
    report.write_text("{}", encoding="utf-8")
    findings = [{
        "severity": "warning",
        "type": "lib_symbol_issues",
        "description": (
            "The current configuration does not include the symbol library 'Timer'"
        ),
    }]

    resolved = apply_warning_contract(
        classify_warnings(findings),
        findings,
        sch_path=schematic,
        report_path=report,
    )

    decision = resolved["lib_symbol_issues"]["resolution"]
    assert decision["status"] == "auto_equivalent"
    assert decision["evidence"]["equivalent"] is True


def test_library_mismatch_with_changed_padstack_fails_closed(tmp_path: Path) -> None:
    pcb, report = _write_footprint_pair(tmp_path, board_pad_size="1.2 1.45")
    findings = [_warning("lib_footprint_mismatch")]

    resolved = apply_warning_contract(
        classify_warnings(findings),
        findings,
        pcb_path=pcb,
        report_path=report,
    )

    decision = resolved["lib_footprint_mismatch"]["resolution"]
    assert decision["status"] == "blocked"
    assert decision["evidence"]["equivalent"] is False
    assert _verification_blockers(_verification(resolved))


def test_silkscreen_waiver_binds_pcb_report_rule_and_count(tmp_path: Path) -> None:
    pcb, report = _write_footprint_pair(tmp_path)
    findings = [_warning("silk_overlap")]
    _write_waiver(pcb, report, rule_id="silk_overlap")

    resolved = apply_warning_contract(
        classify_warnings(findings),
        findings,
        pcb_path=pcb,
        report_path=report,
    )

    decision = resolved["silk_overlap"]["resolution"]
    assert decision["status"] == "waived"
    assert decision["pcb_sha256"] == sha256_file(pcb)
    assert decision["report_sha256"] == sha256_file(report)
    assert _verification_blockers(_verification(resolved)) == []


def test_stale_report_digest_invalidates_silkscreen_waiver(tmp_path: Path) -> None:
    pcb, report = _write_footprint_pair(tmp_path)
    stale_digest = sha256_file(report)
    _write_waiver(
        pcb,
        report,
        rule_id="silk_overlap",
        report_sha256=stale_digest,
    )
    report.write_text('{"changed": true}', encoding="utf-8")
    findings = [_warning("silk_overlap")]

    resolved = apply_warning_contract(
        classify_warnings(findings),
        findings,
        pcb_path=pcb,
        report_path=report,
    )

    assert resolved["silk_overlap"]["resolution"]["status"] == "blocked"
    assert _verification_blockers(_verification(resolved))


def test_connectivity_warning_cannot_be_waived(tmp_path: Path) -> None:
    pcb, report = _write_footprint_pair(tmp_path)
    findings = [_warning("endpoint_off_grid")]
    _write_waiver(pcb, report, rule_id="endpoint_off_grid")

    resolved = apply_warning_contract(
        classify_warnings(findings),
        findings,
        pcb_path=pcb,
        report_path=report,
    )

    decision = resolved["endpoint_off_grid"]["resolution"]
    assert decision["status"] == "blocked"
    assert decision["reason"] == "connectivity_integrity warnings are non-waiverable"
    assert _verification_blockers(_verification(resolved))
