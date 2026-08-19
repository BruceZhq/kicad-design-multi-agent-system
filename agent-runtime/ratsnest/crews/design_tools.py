"""Deterministic KiCad tool services used by autonomous design agents.

These objects are deliberately not Agents.  They own command execution only;
goals, planning, observation, and collaboration live in design_agents.py.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Callable

from ratsnest.config import Config
from ratsnest.crews.blackboard import DesignBlackboard
from ratsnest.crews.contracts import BoardPlan, ToolCall
from ratsnest.design_edit.sexp_edit import apply_property_updates
from ratsnest.freerouting import FreeroutingError, run_freerouting
from ratsnest.kicad_host import KicadHostError, get_host
from ratsnest.schemas import StrategyBundle


class ToolServiceError(RuntimeError):
    pass


class CommandToolService:
    """Capability-scoped facade over the shared in-process KiCad host."""

    commands: tuple[str, ...] = ()

    def __init__(self, config: Config):
        self.config = config

    def call(self, command: str, params: dict) -> dict:
        if command not in self.commands:
            raise ToolServiceError(
                f"{type(self).__name__} does not own command {command!r}")
        try:
            host = get_host(self.config)
        except KicadHostError as exc:
            raise ToolServiceError(str(exc)) from exc
        result = host.handle_command(command, params)
        if isinstance(result, dict) and result.get("success") is False:
            raise ToolServiceError(
                f"{command} failed: {str(result.get('message') or result)[:240]}")
        return result if isinstance(result, dict) else {"value": result}


class ProjectTools(CommandToolService):
    commands = ("create_project", "save_project", "close_project", "open_project")


class SymbolTools(CommandToolService):
    commands = ("add_schematic_component",)


class WiringTools(CommandToolService):
    commands = ("add_schematic_net_label", "connect_to_net",
                "add_schematic_wire")


class LayoutTools(CommandToolService):
    commands = ("sync_schematic_to_board", "set_board_size",
                "add_board_outline", "place_component", "suggest_placement",
                "move_component")


class RoutingTools(CommandToolService):
    commands = ("route_pad_to_pad", "autoroute")


def route_key(net: str, ref_a: str, pin_a: str,
              ref_b: str, pin_b: str) -> str:
    endpoints = sorted((f"{ref_a}:{pin_a}", f"{ref_b}:{pin_b}"))
    return f"{net}:{endpoints[0]}->{endpoints[1]}"


class KiCadDesignToolbox:
    """Translate validated high-level ToolCalls into trusted KiCad commands."""

    def __init__(self, config: Config, plan: BoardPlan,
                 strategy: StrategyBundle, out_dir: Path,
                 blackboard: DesignBlackboard,
                 snapshot: Callable[[str], None] | None = None):
        self.config = config
        self.plan = plan
        self.strategy = strategy
        self.out_dir = Path(out_dir).resolve()
        self.blackboard = blackboard
        self.snapshot = snapshot
        self.name = self.out_dir.name
        self.sch = self.out_dir / f"{self.name}.kicad_sch"
        self.board = self.out_dir / f"{self.name}.kicad_pcb"
        self.project = ProjectTools(config)
        self.symbols = SymbolTools(config)
        self.wiring = WiringTools(config)
        self.layout = LayoutTools(config)
        self.routing = RoutingTools(config)

    def execute(self, agent: str, call: ToolCall) -> dict:
        handler = getattr(self, f"_tool_{call.tool}", None)
        if handler is None:
            error = f"unknown design tool {call.tool!r}"
            self.blackboard.record_tool(agent, call, False, error=error)
            raise ToolServiceError(error)
        try:
            result = handler(call.arguments)
            self.observe()
            self.blackboard.record_tool(agent, call, True, result)
            if self.snapshot is not None and call.tool in {
                    "create_project", "place_component", "sync_board",
                    "place_footprint", "autoroute_board", "save_project"}:
                self.snapshot(f"{agent}_{call.tool}")
            return result
        except Exception as exc:
            error = f"{type(exc).__name__}: {str(exc)[:240]}"
            self.blackboard.record_tool(agent, call, False, error=error)
            raise ToolServiceError(error) from exc

    def observe(self) -> dict:
        """Refresh the blackboard from files, not from the LLM's claims."""
        state = self.blackboard.state
        state.project_created = self.sch.exists()
        state.board_exists = self.board.exists()
        if state.board_exists:
            try:
                host_board = get_host(self.config).board
                if host_board is not None:
                    state.observed_footprints = sorted(
                        footprint.GetReference()
                        for footprint in host_board.GetFootprints())
            except Exception:
                pass
        if self.sch.exists():
            try:
                from ratsnest.eda import get_state
                observed = get_state(self.out_dir, self.config)
                state.observed_components = sorted(
                    component["ref"] for component in observed["components"])
                pin_nets: dict[str, str] = {}
                for component in observed["components"]:
                    for pin in component.get("pins", []):
                        if pin.get("net"):
                            pin_nets[f"{component['ref']}:{pin['pin']}"] = pin["net"]
                state.observed_pin_nets = pin_nets
            except Exception:
                # Incomplete schematics can be temporarily unparsable.  Tool
                # history remains available, and the next observation retries.
                pass
        state.revision += 1
        return state.model_dump(mode="json", exclude={"messages", "tool_history"})

    def close(self) -> None:
        try:
            self.project.call("save_project", {"force": True})
            self.project.call("close_project", {"save": False})
        except Exception:
            pass

    # -- high-level tools -------------------------------------------------

    def _tool_create_project(self, args: dict) -> dict:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        result = self.project.call("create_project", {
            "projectName": self.name, "name": self.name,
            "path": str(self.out_dir)})
        self.blackboard.state.project_created = True
        self.blackboard.state.stage = "schematic"
        return result

    def _tool_place_component(self, args: dict) -> dict:
        ref = str(args["ref"])
        component = self.plan.component(ref)
        library, symbol_type = component.symbol.split(":", 1)
        return self.symbols.call("add_schematic_component", {
            "schematicPath": str(self.sch),
            "component": {
                "library": library, "type": symbol_type,
                "reference": component.ref, "value": component.value,
                "footprint": component.footprint,
                "x": float(args["x"]), "y": float(args["y"]),
                "unit": 1, "angle": 0, "mirrorY": False,
            }})

    def _tool_connect_pin(self, args: dict) -> dict:
        return self.wiring.call("add_schematic_net_label", {
            "schematicPath": str(self.sch),
            "netName": str(args["net"]),
            "componentRef": str(args["ref"]),
            "pinNumber": str(args["pin"]),
        })

    def _tool_save_project(self, args: dict) -> dict:
        self._stamp_component_properties()
        return self.project.call("save_project", {"force": True})

    def _tool_sync_board(self, args: dict) -> dict:
        self._stamp_component_properties()
        self._write_fp_lib_table()
        result = self.layout.call("sync_schematic_to_board", {
            "schematicPath": str(self.sch), "boardPath": str(self.board)})
        host_board = get_host(self.config).board
        observed = ({footprint.GetReference()
                     for footprint in host_board.GetFootprints()}
                    if host_board is not None else set())
        expected = {component.ref for component in self.plan.components
                    if component.on_board}
        missing = sorted(expected - observed)
        if missing:
            skipped = result.get("footprints_skipped", [])
            raise ToolServiceError(
                "schematic-to-PCB sync omitted planned footprints "
                f"{missing}; skipped={skipped[:6]}")
        self._apply_net_classes(host_board)
        self.blackboard.state.board_exists = True
        self.blackboard.state.board_synced = True
        self.blackboard.state.observed_footprints = sorted(observed)
        self.blackboard.state.stage = "pcb"
        return result

    def _tool_set_board_outline(self, args: dict) -> dict:
        result = self.layout.call("set_board_size", {
            "width": float(args["width"]),
            "height": float(args["height"]), "unit": "mm"})
        self.blackboard.state.outline_set = True
        return result

    def _tool_place_footprint(self, args: dict) -> dict:
        ref = str(args["ref"])
        result = self.layout.call("move_component", {
            "reference": ref,
            "position": {"x": float(args["x"]), "y": float(args["y"]),
                         "unit": "mm"}})
        if ref not in self.blackboard.state.placed_footprints:
            self.blackboard.state.placed_footprints.append(ref)
        if ref not in self.blackboard.state.observed_footprints:
            self.blackboard.state.observed_footprints.append(ref)
        return result

    def _tool_route_connection(self, args: dict) -> dict:
        key = route_key(str(args["net"]), str(args["from_ref"]),
                        str(args["from_pin"]), str(args["to_ref"]),
                        str(args["to_pin"]))
        if key not in self.blackboard.state.attempted_routes:
            self.blackboard.state.attempted_routes.append(key)
        result = self.routing.call("route_pad_to_pad", {
            "fromRef": str(args["from_ref"]),
            "fromPad": str(args["from_pin"]),
            "toRef": str(args["to_ref"]),
            "toPad": str(args["to_pin"]),
        })
        if key not in self.blackboard.state.routed_connections:
            self.blackboard.state.routed_connections.append(key)
        return result

    def _tool_autoroute_board(self, args: dict) -> dict:
        if args:
            raise ToolServiceError("autoroute_board takes no arguments")
        if self.config.routing_mode != "freerouting":
            raise ToolServiceError(
                "production PCB routing requires RATSNEST_ROUTING_MODE=freerouting")
        jar = self.config.freerouting_jar
        if jar is None or not Path(jar).is_file():
            raise ToolServiceError(
                "Freerouting JAR is unavailable; set RATSNEST_FREEROUTING_JAR")

        physical = {component.ref for component in self.plan.components
                    if component.on_board}
        target_nets = sorted({connection.net for connection in self.plan.connections
                              if connection.ref in physical})
        host_board = get_host(self.config).board
        if host_board is None:
            raise ToolServiceError("KiCad host has no open board to route")
        try:
            result = run_freerouting(host_board, self.board, self.config)
        except FreeroutingError as exc:
            raise ToolServiceError(str(exc)) from exc

        ses_path = Path(str(result.get("ses_path", "")))
        if not ses_path.is_file():
            raise ToolServiceError("Freerouting returned no inspectable SES result")
        ses_text = ses_path.read_text(encoding="utf-8", errors="replace")
        routed_nets = {
            quoted or bare
            for quoted, bare in re.findall(
                r'\(net\s+(?:"([^"]+)"|([^\s()]+))\s*\r?\n\s*\(wire',
                ses_text)
        }
        missing = sorted(set(target_nets) - routed_nets)
        if missing:
            raise ToolServiceError(
                f"Freerouting left required nets unrouted: {missing}")

        unconnected = None
        host_board.BuildConnectivity()
        unconnected = int(
            host_board.GetConnectivity().GetUnconnectedCount(False))
        if unconnected:
            raise ToolServiceError(
                f"Freerouting import leaves {unconnected} unconnected item(s)")

        self.blackboard.state.autorouted = True
        self.blackboard.state.routing_mode = str(result.get("mode", "freerouting"))
        self.blackboard.state.routing_metrics = {
            "target_nets": target_nets,
            "routed_target_nets": sorted(set(target_nets) & routed_nets),
            "best_attempt": result.get("best_attempt", 1),
            "elapsed_seconds": result.get("elapsed_seconds"),
            "unconnected_items": unconnected,
            **(result.get("board_stats") or {}),
        }
        return result

    # -- trusted preparation ------------------------------------------------

    def _stamp_component_properties(self) -> None:
        updates: dict[str, dict[str, str]] = {}
        for component in self.plan.components:
            props = dict(component.properties)
            if component.footprint:
                props["Footprint"] = component.footprint
            if props:
                updates[component.ref] = props
        if not updates or not self.sch.exists():
            return
        text = self.sch.read_text(encoding="utf-8")
        new_text, log = apply_property_updates(text, updates, self.config)
        if any(entry.get("action") == "error" for entry in log):
            raise ToolServiceError(
                "failed to stamp one or more component properties")
        self.sch.write_text(new_text, encoding="utf-8")

    def _write_fp_lib_table(self) -> None:
        if not self.config.kicad_python:
            return
        fp_root = (Path(self.config.kicad_python).parent.parent
                   / "share" / "kicad" / "footprints")
        libraries = sorted({component.footprint.split(":", 1)[0]
                            for component in self.plan.components
                            if ":" in component.footprint})
        rows = []
        for library in libraries:
            pretty = fp_root / f"{library}.pretty"
            if pretty.exists():
                uri = str(pretty).replace("\\", "/")
                rows.append(f'  (lib (name "{library}")(type "KiCad")'
                            f'(uri "{uri}")(options "")(descr ""))')
        if rows:
            (self.out_dir / "fp-lib-table").write_text(
                "(fp_lib_table\n  (version 7)\n" + "\n".join(rows)
                + "\n)\n", encoding="utf-8")

    def _apply_net_classes(self, board) -> None:
        """Materialize approved per-net copper rules before DSN export."""
        if board is None or not self.plan.net_classes:
            return
        try:
            import pcbnew
            settings = board.GetDesignSettings().m_NetSettings
            for index, (net_name, rule) in enumerate(
                    sorted(self.plan.net_classes.items())):
                class_name = f"RN_{index + 1}_{net_name}".replace("+", "P")
                netclass = pcbnew.NETCLASS(class_name)
                netclass.SetTrackWidth(pcbnew.FromMM(rule.track_width_mm))
                netclass.SetClearance(pcbnew.FromMM(rule.clearance_mm))
                settings.SetNetclass(class_name, netclass)
                labels = pcbnew.STRINGSET()
                labels.add(class_name)
                settings.SetNetclassLabelAssignment(net_name, labels)
            settings.RecomputeEffectiveNetclasses()
            board.Save(str(self.board))
        except Exception as exc:
            raise ToolServiceError(f"failed to apply approved net classes: {exc}") from exc
