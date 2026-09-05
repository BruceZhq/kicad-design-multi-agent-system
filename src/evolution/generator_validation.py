"""Trusted CAD probe for generator patches, executed outside candidate imports.

The candidate child writes real files; the parent never imports candidate
Python. KiCad CLI independently checks the files. In production this parent
comes from the read-only evaluator image, with no credentials or network.
This is a generator regression, not evidence of arbitrary-board reliability.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

GENERATOR_PATHS = frozenset({
    "src/ratsnestpro/eda/materialize.py", "src/ratsnestpro/eda/schematic_wiring.py",
    "src/ratsnestpro/eda/routing_rules.py", "src/ratsnestpro/eda/_route_worker.py",
})

_GENERATE = r'''
import sys
from pathlib import Path
from ratsnestpro.eda.materialize import materialize_pinmapped
from ratsnestpro.eda.vendor.pcb import PcbBoard
from ratsnestpro.eda.vendor.sexpr import loads
from ratsnestpro.eda.footprints import footprint_path
from ratsnestpro.eda.routing import autoroute
out = Path(sys.argv[1])
fpj = "Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical"
fpr = "Resistor_SMD:R_0805_2012Metric"
parts = [dict(ref="J1", symbol="Connector_Generic:Conn_01x02", value="INPUT", footprint=fpj,
              x=30.48, y=30.48, rotation=0, release_ready=True, resolution_status="installed_exact"),
         dict(ref="R1", symbol="Device:R", value="1k", footprint=fpr,
              x=55.88, y=30.48, rotation=0, release_ready=True, resolution_status="installed_exact")]
nets = [{"name": n, "pins": [{"ref": "J1", "number": p}, {"ref": "R1", "number": p}]}
        for n,p in [("VIN","1"),("RETURN","2")]]
sch = materialize_pinmapped(parts, nets, label_nets=[])
sch.save(out / "probe.kicad_sch")
board = PcbBoard.blank()
board.set_board_outline(0,0,40,30)
board.add_footprint(fpj, "J1", "INPUT", 8,10, embed_node=loads(footprint_path(fpj).read_text()))
board.add_footprint(fpr, "R1", "1k", 28,18, embed_node=loads(footprint_path(fpr).read_text()))
board.save(out / "probe.kicad_pcb")
netmap = {n["name"]: [[p["ref"], p["number"]] for p in n["pins"]] for n in nets}
classes = [dict(name="power", nets=["VIN"], width=.5, clearance=.2, via_diameter=.6, via_drill=.3),
           dict(name="signal", nets=["RETURN"], width=.25, clearance=.2, via_diameter=.6, via_drill=.3)]
result = autoroute(out / "probe.kicad_pcb", netmap, net_classes=classes, critical_nets=["VIN"], max_passes=10)
if not result.ok or result.unconnected != 0:
    raise RuntimeError(result.note)
'''


def _run(argv: list[str], *, cwd: Path, env: dict | None = None, timeout: int = 90) -> None:
    result = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise ValueError(f"generator validation command failed: {result.stderr[-2000:]} {result.stdout[-1000:]}")


def _findings(value: object) -> list[dict]:
    if isinstance(value, dict):
        own = [value] if "severity" in value and ("type" in value or "description" in value) else []
        return own + [item for child in value.values() for item in _findings(child)]
    if isinstance(value, list):
        return [item for child in value for item in _findings(child)]
    return []


_VERIFY_BOARD = r'''
import json, sys, pcbnew
board = pcbnew.LoadBoard(sys.argv[1])
pins = {f.GetReference()+":"+p.GetNumber(): p.GetNetname() for f in board.GetFootprints() for p in f.Pads()}
assert pins == {"J1:1":"VIN", "R1:1":"VIN", "J1:2":"RETURN", "R1:2":"RETURN"}, pins
widths = {"VIN": .5, "RETURN": .25}
tracks = [t for t in board.GetTracks() if not isinstance(t, pcbnew.PCB_VIA)]
assert tracks and all(pcbnew.ToMM(t.GetWidth()) + 1e-6 >= widths[t.GetNetname()] for t in tracks)
assert board.GetCopperLayerCount() == 2
print(json.dumps({"pins": pins, "tracks": len(tracks)}))
'''


def validate_generated_files(directory: Path, cli: str) -> dict:
    directory = directory.resolve()
    files = [directory / ("probe" + suffix) for suffix in (".kicad_sch", ".kicad_pcb", ".dsn", ".ses")]
    if any(p.is_symlink() or not p.is_file() or not p.stat().st_size for p in files):
        raise ValueError("generator did not create real, nonempty CAD/routing artifacts")
    kicad_python = os.environ.get("KICAD_PYTHON", "/usr/bin/python3")
    _run([kicad_python, "-I", "-c", _VERIFY_BOARD, str(files[1])], cwd=directory)
    for kind, source in (("sch", files[0]), ("pcb", files[1])):
        report = directory / f"trusted-{kind}.json"
        _run([cli, kind, "erc" if kind == "sch" else "drc", "--format", "json", "--output", str(report),
              "--severity-all", str(source)], cwd=directory)
        data = json.loads(report.read_text(encoding="utf-8"))
        expected = "sheets" if kind == "sch" else "violations"
        if expected not in data or any(item.get("severity") == "error" for item in _findings(data)):
            raise ValueError(f"independent {kind} validation failed")
        if kind == "pcb" and (data.get("unconnected_items") or data.get("schematic_parity")):
            raise ValueError("independent PCB connectivity/parity validation failed")
    exported = directory / "trusted.net"
    _run([cli, "sch", "export", "netlist", "--format", "kicadxml", "--output", str(exported), str(files[0])], cwd=directory)
    tree = ET.parse(exported)
    actual = {node.get("name"): {(p.get("ref"), p.get("pin")) for p in node.findall("node")}
              for node in tree.findall(".//nets/net")}
    if actual.get("VIN") != {("J1", "1"), ("R1", "1")} or actual.get("RETURN") != {("J1", "2"), ("R1", "2")}:
        raise ValueError("exported graphical connectivity differs from the fixed pin-level contract")
    return {"passed": True, "artifact_sha256": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in files},
            "source": "independent_kicad_cli", "erc_errors": 0, "drc_errors": 0, "unconnected": 0}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate-root", default=".")
    args = parser.parse_args()
    root = Path(args.candidate_root).resolve()
    cli = shutil.which("kicad-cli")
    if not cli:
        raise ValueError("KiCad is required for generator-patch isolation checks")
    with tempfile.TemporaryDirectory(prefix="generator-probe-") as temp:
        out = Path(temp)
        env = {key: value for key, value in os.environ.items()
               if key in {"PATH", "SYSTEMROOT", "WINDIR", "KICAD_PYTHON", "FREEROUTING_EXE"}}
        env.update({"PYTHONPATH": str(root / "src"), "PYTHONNOUSERSITE": "1", "HOME": str(out),
                    "PYTHONPYCACHEPREFIX": str(out / "pycache"),
                    "RATSNESTPRO_ROUTER_TIMEOUT_SECONDS": "300"})
        _run([sys.executable, "-m", "compileall", "-q", "src/ratsnestpro", "src/agents/ratsnestpro", "src/evolution"],
             cwd=root, env=env)
        _run([sys.executable, "-c", _GENERATE, str(out)], cwd=root, env=env, timeout=360)
        print(json.dumps(validate_generated_files(out, cli), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
