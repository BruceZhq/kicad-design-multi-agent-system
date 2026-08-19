"""Web EDA engine: typed edit ops in, KiCad files written, fresh state out.

Stage-3 contract (design docs): the browser NEVER writes S-expressions. It
sends ops; this module executes them through the same trusted write paths the
agents use (ops-only property editor, in-process KiCad host), then re-renders
the SVG and re-extracts state so the canvas always reflects the file truth.

Op vocabulary v1:
  {"op": "move",         "ref": "R1", "x": 120.5, "y": 63.0}
  {"op": "set_value",    "ref": "R1", "value": "4.7k"}
  {"op": "set_property", "ref": "R1", "name": "MPN", "value": "..."}
  {"op": "add_component","ref": "C1", "symbol": "Device:C", "value": "100n",
                         "x": 150, "y": 70}
  {"op": "connect_net",  "ref": "C1", "pin": "1", "net": "+5V"}
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ratsnest.config import Config
from ratsnest.kh_adapter.runner import find_root_schematic
from ratsnest.khlib import load_kh_module

# symbols the palette may add — same whitelist philosophy as the foreman
DEFAULT_PALETTE = ["Device:R", "Device:C", "Device:LED",
                   "Connector_Generic:Conn_01x02"]


def get_state(project_dir: Path, config: Config | None = None) -> dict[str, Any]:
    """Editable state: components with anchors, pins with nets, sheet size."""
    config = config or Config.load()
    sch = find_root_schematic(Path(project_dir))
    module = load_kh_module("analyze_schematic", config.kicad_scripts)
    analysis = module.analyze_schematic(str(sch))
    components = []
    for comp in analysis.get("components", []):
        if comp.get("type") in ("power_symbol", "power_flag", "flag"):
            continue
        pins = [{"pin": pin, "net": net[0] if isinstance(net, (list, tuple))
                 else net}
                for pin, net in (comp.get("pin_nets") or {}).items()]
        components.append({
            "ref": comp["reference"], "value": comp.get("value", ""),
            "lib_id": comp.get("lib_id", ""),
            "x": comp.get("x", 0), "y": comp.get("y", 0),
            "pins": pins,
        })
    nets = sorted(analysis.get("nets", {}).keys())
    return {"schematic": sch.name, "components": components, "nets": nets,
            "palette": DEFAULT_PALETTE, "sheet": {"width": 297, "height": 210}}


def apply_edits(project_dir: Path, ops: list[dict[str, Any]],
                config: Config | None = None) -> dict[str, Any]:
    """Execute ops through the trusted write paths, refresh SVG, return state."""
    config = config or Config.load()
    project_dir = Path(project_dir)
    sch = find_root_schematic(project_dir)
    applied: list[str] = []
    errors: list[str] = []

    from ratsnest.design_edit.sexp_edit import apply_property_updates, move_symbol

    host = None

    def get_host():
        nonlocal host
        if host is None:
            from ratsnest.kicad_host import get_host as _host_factory
            host = _host_factory(config)
        return host

    for op in ops:
        kind = str(op.get("op", ""))
        ref = str(op.get("ref", ""))
        try:
            if kind == "move":
                x, y = float(op["x"]), float(op["y"])
                if not (0 < x < 297 and 0 < y < 210):
                    raise ValueError("position outside sheet")
                text = sch.read_text(encoding="utf-8")
                new_text, moved = move_symbol(text, ref, x, y, config)
                if not moved:
                    raise ValueError(f"reference {ref!r} not found")
                sch.write_text(new_text, encoding="utf-8")
            elif kind in ("set_value", "set_property"):
                name = "Value" if kind == "set_value" else str(op["name"])
                if name.lower() in ("reference",):
                    raise ValueError("renaming references is not allowed")
                text = sch.read_text(encoding="utf-8")
                new_text, log = apply_property_updates(
                    text, {ref: {name: str(op["value"])}}, config)
                if any(entry.get("action") == "error" for entry in log):
                    raise ValueError(f"reference {ref!r} not found")
                sch.write_text(new_text, encoding="utf-8")
            elif kind == "add_component":
                symbol = str(op["symbol"])
                if symbol not in DEFAULT_PALETTE:
                    raise ValueError(f"symbol {symbol!r} not in palette")
                library, sym_type = symbol.split(":", 1)
                result = get_host().handle_command("add_schematic_component", {
                    "schematicPath": str(sch),
                    "component": {"library": library, "type": sym_type,
                                  "reference": ref,
                                  "value": str(op.get("value", "")),
                                  "footprint": "",
                                  "x": float(op.get("x", 150)),
                                  "y": float(op.get("y", 100)),
                                  "unit": 1, "angle": 0, "mirrorY": False}})
                if isinstance(result, dict) and result.get("success") is False:
                    raise ValueError(str(result.get("message"))[:150])
            elif kind == "connect_net":
                result = get_host().handle_command("add_schematic_net_label", {
                    "schematicPath": str(sch),
                    "netName": str(op["net"])[:40],
                    "componentRef": ref, "pinNumber": str(op["pin"])})
                if isinstance(result, dict) and result.get("success") is False:
                    raise ValueError(str(result.get("message"))[:150])
            else:
                raise ValueError(f"unknown op {kind!r}")
            applied.append(f"{kind}:{ref}")
        except Exception as exc:
            errors.append(f"{kind}:{ref}: {exc}")

    if host is not None:
        try:
            host.handle_command("close_project", {"save": False})
        except Exception:
            pass

    if applied:  # re-render the truth the canvas displays
        from ratsnest.preview import generate_previews
        generate_previews(project_dir, config)

    state = get_state(project_dir, config)
    state["applied"] = applied
    state["errors"] = errors
    return state
