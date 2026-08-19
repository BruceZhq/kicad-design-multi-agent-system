"""KiCad MCP execution backend: sub-agents that create designs in real KiCad.

Where the template backend writes S-expressions itself, this backend drives
the vendored KiCAD-MCP-Server (github.com/mixelpixx/KiCAD-MCP-Server, 122
tools over SWIG/IPC) so components come from KiCad's real symbol libraries.
Electrical values still come from `solve_board_values` — one evolvable
strategy governs both creation paths, and kicad-happy remains the judge of
whatever gets created.

Sub-agent split (design doc roster):
  ProjectAgent    create_project / save_project / close_project
  SchematicAgent  place components + bind pins to nets via pin-snapped labels
"""

from __future__ import annotations

from pathlib import Path

from ratsnest.circuit_math import (
    LDO_TOPOLOGY,
    GenerationError,
    solve_circuit,
)
from ratsnest.config import Config
from ratsnest.data_proxy import Recorder
from ratsnest.design_gen.templates import rail_name
from ratsnest.mcp_exec.client import McpClient
from ratsnest.schemas import DesignSpec, StrategyBundle

# real KiCad 10 library part (verified in Regulator_Linear.kicad_sym):
# adjustable 1.25V regulator already covered by the strategy's Vref table.
# AP1117 pinout: pin 1 = ADJ, pin 2 = VOUT, pin 3 = VIN
MCP_REGULATOR_SYMBOL = "Regulator_Linear:TLV1117-ADJ"


class KiCadMcpBackend:
    def __init__(self, config: Config | None = None,
                 recorder: Recorder | None = None):
        self.config = config or Config.load()
        self.recorder = recorder
        if not self.config.mcp_server_dir:
            raise GenerationError(
                "KiCAD-MCP-Server not found (set RATSNEST_MCP_SERVER)")
        self.dist = self.config.mcp_server_dir / "dist" / "index.js"
        if not self.dist.exists():
            raise GenerationError(
                f"{self.dist} missing — build the server with `npm run build`")

    def _client(self) -> McpClient:
        env = {"KICAD_AUTO_LAUNCH": "false", "NODE_ENV": "production"}
        if self.config.kicad_python:
            env["KICAD_PYTHON"] = str(self.config.kicad_python)
        return McpClient(["node", str(self.dist)],
                         cwd=self.config.mcp_server_dir, env=env,
                         recorder=self.recorder)

    def generate(self, spec: DesignSpec, out_dir: Path,
                 strategy: StrategyBundle) -> Path:
        out_dir = Path(out_dir).resolve()
        solved = solve_circuit(spec, strategy, self.config)
        if solved.topology != LDO_TOPOLOGY:
            raise GenerationError(
                "external MCP compatibility backend is limited to the LDO "
                "development schematic; use crew for Buck production runs")
        values, mpns, include_led = (
            solved.values, solved.mpns, solved.include_led)

        vin, vout = rail_name(spec.input_voltage), rail_name(spec.output_voltage)
        # the server creates <path>/<name>.kicad_sch (no subdirectory)
        name = out_dir.name
        sch_path = out_dir / f"{name}.kicad_sch"
        out_dir.mkdir(parents=True, exist_ok=True)

        with self._client() as mcp:
            # --- ProjectAgent -------------------------------------------------
            mcp.call_tool("create_project", {
                "name": name,
                "path": str(out_dir),
            })

            # --- SchematicAgent: place from real KiCad libraries ---------------
            placements = [
                ("J1", "Connector_Generic:Conn_01x02", "Conn_01x02", 75, 60),
                ("U1", MCP_REGULATOR_SYMBOL, values["U1"], 100, 60),
                ("R1", "Device:R", values["R1"], 130, 55),
                ("R2", "Device:R", values["R2"], 130, 80),
            ]
            if include_led:
                placements += [
                    ("R3", "Device:R", values["R3"], 155, 55),
                    ("D1", "Device:LED", values["D1"], 155, 80),
                ]
            for ref, symbol, value, x, y in placements:
                mcp.call_tool("add_schematic_component", {
                    "schematicPath": str(sch_path),
                    "symbol": symbol, "reference": ref, "value": value,
                    "position": {"x": x, "y": y},
                })

            # --- SchematicAgent: connectivity via pin-snapped net labels -------
            # (componentRef+pinNumber guarantees the electrical connection)
            nets: list[tuple[str, str, str]] = [
                (vin, "J1", "1"), ("GND", "J1", "2"),
                (vin, "U1", "3"),
                (vout, "U1", "2"), (vout, "R1", "1"),
                ("FB", "U1", "1"), ("FB", "R1", "2"), ("FB", "R2", "1"),
                ("GND", "R2", "2"),
            ]
            if include_led:
                nets += [
                    (vout, "R3", "1"),
                    ("LED_A", "R3", "2"), ("LED_A", "D1", "2"),
                    ("GND", "D1", "1"),
                ]
            for net, ref, pin in nets:
                mcp.call_tool("add_schematic_net_label", {
                    "schematicPath": str(sch_path),
                    "netName": net,
                    "componentRef": ref,
                    "pinNumber": pin,
                })

            # --- ProjectAgent: persist and release ------------------------------
            mcp.call_tool("save_project", {"force": True})
            mcp.call_tool("close_project", {"save": False})

        if not sch_path.exists():
            raise GenerationError(
                f"MCP run finished but {sch_path} was not created")
        (out_dir / "designspec.json").write_text(
            spec.model_dump_json(indent=2), encoding="utf-8")
        return out_dir
