from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from ratsnestpro.eda.materialize import materialize_pinmapped
from ratsnestpro.eda.vendor.footprint import (
    load_footprint_node,
    resolve_footprint,
)
from ratsnestpro.eda.vendor.kicad_cli import KicadCliNotFound, find_kicad_cli
from ratsnestpro.eda.vendor.pcb import PcbBoard
from ratsnestpro.eda.vendor.sexpr import find_all, find_first, loads
from ratsnestpro.eda.vendor.symbol_lib import transform_pin


@pytest.mark.parametrize(
    ("rotation", "expected"),
    [
        (90.0, (60.96, 43.18)),
        (270.0, (60.96, 33.02)),
    ],
)
def test_rotated_symbol_pin_uses_kicad_schematic_coordinates(
    rotation: float,
    expected: tuple[float, float],
) -> None:
    assert transform_pin(
        60.96,
        38.10,
        rotation,
        None,
        -5.08,
        0.0,
    ) == expected


def _kicad_cli_or_skip() -> str:
    try:
        return find_kicad_cli()
    except KicadCliNotFound:
        pytest.skip("kicad-cli is not installed")


def test_embedded_pad_angle_tracks_footprint_instance_rotation() -> None:
    footprint = loads(
        '(footprint "Synthetic_SOIC" (layer "F.Cu") '
        '(pad "1" smd rect (at -1 2) (size 1 2) '
        '(layers "F.Cu" "F.Paste" "F.Mask")))'
    )
    assert isinstance(footprint, list)
    board = PcbBoard.blank()
    board.add_footprint(
        "Synthetic:Synthetic_SOIC",
        "U1",
        "synthetic",
        10,
        10,
        rotation=90,
        embed_node=footprint,
    )
    placed = find_all(board.root, "footprint")[0]
    pad = find_all(placed, "pad")[0]

    assert float(str(find_first(pad, "at")[3])) == 90

    board.rotate_footprint("U1", 180)

    assert float(str(find_first(pad, "at")[3])) == 180


def test_rotated_two_pin_connector_is_electrically_connected(
    tmp_path: Path,
) -> None:
    schematic_path = tmp_path / "rotated-j1.kicad_sch"
    document = materialize_pinmapped(
        components=[{
            "ref": "J1",
            "symbol": "Connector_Generic:Conn_01x02",
            "value": "5V IN",
            "footprint": (
                "Connector_PinHeader_2.54mm:"
                "PinHeader_1x02_P2.54mm_Vertical"
            ),
            "x": 60.96,
            "y": 38.10,
            "rotation": 90,
            "release_ready": True,
            "resolution_status": "installed_exact",
        }],
        nets=[
            {"name": "VCC", "pins": [{"ref": "J1", "number": "1"}]},
            {"name": "GND", "pins": [{"ref": "J1", "number": "2"}]},
        ],
        supply_nets=["VCC"],
        ground_net="GND",
    )
    document.save(schematic_path)
    report_path = tmp_path / "erc.json"

    subprocess.run(
        [
            _kicad_cli_or_skip(),
            "sch",
            "erc",
            "--format",
            "json",
            "--severity-all",
            "--output",
            str(report_path),
            "--exit-code-violations",
            str(schematic_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    errors = [
        violation
        for sheet in report.get("sheets", [])
        for violation in sheet.get("violations", [])
        if violation.get("severity") == "error"
    ]
    assert errors == []


def test_rotated_soic_has_no_fatal_pad_geometry_drc(tmp_path: Path) -> None:
    footprint_path = (
        resolve_footprint("Package_SO:SOIC-8_3.9x4.9mm_P1.27mm")
        or resolve_footprint("Package_SO:SOIC-8")
    )
    if footprint_path is None:
        pytest.skip("installed KiCad SOIC-8 footprint is unavailable")
    board_path = tmp_path / "rotated-soic.kicad_pcb"
    board = PcbBoard.blank()
    board.set_board_outline(0, 0, 30, 20)
    board.add_footprint(
        f"Package_SO:{footprint_path.stem}",
        "U1",
        "SOIC-8",
        15,
        10,
        rotation=90,
        embed_node=load_footprint_node(footprint_path),
    )
    board.save(board_path)
    report_path = tmp_path / "drc.json"

    subprocess.run(
        [
            _kicad_cli_or_skip(),
            "pcb",
            "drc",
            "--format",
            "json",
            "--severity-all",
            "--output",
            str(report_path),
            "--exit-code-violations",
            str(board_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    fatal_geometry = {
        "clearance",
        "shorting_items",
        "solder_mask_bridge",
    }
    errors = [
        violation
        for key in ("violations", "schematic_parity")
        for violation in report.get(key, [])
        if (
            violation.get("severity") == "error"
            and violation.get("type") in fatal_geometry
        )
    ]
    assert errors == []
