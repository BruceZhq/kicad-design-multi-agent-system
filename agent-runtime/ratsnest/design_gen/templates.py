"""KiCad 8 S-expression template for the linear-regulator board family.

Deterministic synthesis: pin coordinates are computed with the same transform
kicad-happy's analyzer uses (abs = (cx+dx, cy−dy) at rotation 0), so wires
always land exactly on pins. UUIDs are uuid5-derived from the project name —
regenerating the same spec yields byte-identical files.
"""

from __future__ import annotations

import uuid

FONT = "(effects (font (size 1.27 1.27)))"
FONT_HIDE = "(effects (font (size 1.27 1.27)) (hide yes))"


def rail_name(voltage: float) -> str:
    """5 -> '+5V', 3.3 -> '+3V3', 12 -> '+12V' (KiCad rail naming)."""
    if voltage == int(voltage):
        return f"+{int(voltage)}V"
    whole, frac = f"{voltage:g}".split(".")
    return f"+{whole}V{frac}"


class _Uids:
    def __init__(self, seed: str):
        self.ns = uuid.uuid5(uuid.NAMESPACE_URL, f"ratsnest:{seed}")
        self.n = 0

    def __call__(self) -> str:
        self.n += 1
        return str(uuid.uuid5(self.ns, str(self.n)))


# ---------------------------------------------------------------------------
# Library symbols
# ---------------------------------------------------------------------------

def lib_resistor() -> str:
    return f"""    (symbol "Device:R" (pin_numbers hide) (pin_names (offset 0)) (exclude_from_sim no) (in_bom yes) (on_board yes)
      (property "Reference" "R" (at 2.032 0 90) {FONT})
      (property "Value" "R" (at 0 0 90) {FONT})
      (property "Footprint" "" (at -1.778 0 90) {FONT_HIDE})
      (property "Datasheet" "~" (at 0 0 0) {FONT_HIDE})
      (symbol "R_0_1"
        (rectangle (start -1.016 -2.54) (end 1.016 2.54) (stroke (width 0.254) (type default)) (fill (type none)))
      )
      (symbol "R_1_1"
        (pin passive line (at 0 3.81 270) (length 1.27) (name "~" {FONT}) (number "1" {FONT}))
        (pin passive line (at 0 -3.81 90) (length 1.27) (name "~" {FONT}) (number "2" {FONT}))
      )
    )"""


def lib_led() -> str:
    return f"""    (symbol "Device:LED" (pin_numbers hide) (pin_names (offset 1.016) hide) (exclude_from_sim no) (in_bom yes) (on_board yes)
      (property "Reference" "D" (at 0 2.54 0) {FONT})
      (property "Value" "LED" (at 0 -2.54 0) {FONT})
      (property "Footprint" "" (at 0 0 0) {FONT_HIDE})
      (property "Datasheet" "~" (at 0 0 0) {FONT_HIDE})
      (symbol "LED_0_1"
        (polyline (pts (xy -1.27 -1.27) (xy 1.27 -1.27) (xy 0 1.27) (xy -1.27 -1.27)) (stroke (width 0.254) (type default)) (fill (type none)))
      )
      (symbol "LED_1_1"
        (pin passive line (at 0 -3.81 90) (length 2.54) (name "K" {FONT}) (number "1" {FONT}))
        (pin passive line (at 0 3.81 270) (length 2.54) (name "A" {FONT}) (number "2" {FONT}))
      )
    )"""


def lib_regulator() -> str:
    # Adjustable TLV1117 pinout: VIN(3), VOUT(2), ADJ(1).
    return f"""    (symbol "Regulator_Linear:TLV1117-ADJ" (pin_names (offset 0.254)) (exclude_from_sim no) (in_bom yes) (on_board yes)
      (property "Reference" "U" (at 0 6.35 0) {FONT})
      (property "Value" "TLV1117-ADJ" (at 0 5.08 0) {FONT})
      (property "Footprint" "" (at 0 0 0) {FONT_HIDE})
      (property "Datasheet" "~" (at 0 0 0) {FONT_HIDE})
      (symbol "TLV1117-ADJ_0_1"
        (rectangle (start -7.62 3.81) (end 7.62 -3.81) (stroke (width 0.254) (type default)) (fill (type background)))
      )
      (symbol "TLV1117-ADJ_1_1"
        (pin power_in line (at -7.62 0 0) (length 0) (name "VIN" {FONT}) (number "3" {FONT}))
        (pin power_out line (at 7.62 0 180) (length 0) (name "VOUT" {FONT}) (number "2" {FONT}))
        (pin input line (at 5.08 -5.08 90) (length 0) (name "ADJ" {FONT}) (number "1" {FONT}))
      )
    )"""


def lib_power(name: str) -> str:
    graphic = (
        '(polyline (pts (xy 0 0) (xy 0 1.27)) (stroke (width 0) (type default)) (fill (type none)))'
        if name != "GND"
        else '(polyline (pts (xy 0 0) (xy 0 -1.27)) (stroke (width 0) (type default)) (fill (type none)))'
    )
    return f"""    (symbol "power:{name}" (power) (pin_names (offset 0)) (exclude_from_sim yes) (in_bom yes) (on_board yes)
      (property "Reference" "#PWR" (at 0 -3.81 0) {FONT_HIDE})
      (property "Value" "{name}" (at 0 3.556 0) {FONT})
      (property "Footprint" "" (at 0 0 0) {FONT_HIDE})
      (property "Datasheet" "" (at 0 0 0) {FONT_HIDE})
      (symbol "{name}_0_1"
        {graphic}
      )
      (symbol "{name}_1_1"
        (pin power_in line (at 0 0 90) (length 0) (name "~" {FONT}) (number "1" {FONT}))
      )
    )"""


def lib_flag() -> str:
    # kicad-happy deliberately excludes PWR_FLAG from connectivity ("DRC-only
    # marker"); RS-001 on externally powered rails is expected and suppressed
    # via strategy. The flag keeps KiCad's own ERC happy.
    return f"""    (symbol "power:PWR_FLAG" (power) (pin_names (offset 0)) (exclude_from_sim yes) (in_bom yes) (on_board yes)
      (property "Reference" "#FLG" (at 0 -2.54 0) {FONT_HIDE})
      (property "Value" "PWR_FLAG" (at 0 2.54 0) {FONT})
      (property "Footprint" "" (at 0 0 0) {FONT_HIDE})
      (property "Datasheet" "" (at 0 0 0) {FONT_HIDE})
      (symbol "PWR_FLAG_0_1"
        (polyline (pts (xy 0 0) (xy 0 1.27) (xy -1.016 1.905) (xy 0 2.54) (xy 1.016 1.905) (xy 0 1.27)) (stroke (width 0) (type default)) (fill (type none)))
      )
      (symbol "PWR_FLAG_1_1"
        (pin power_out line (at 0 0 90) (length 0) (name "pwr" {FONT}) (number "1" {FONT}))
      )
    )"""


# ---------------------------------------------------------------------------
# Board assembly
# ---------------------------------------------------------------------------

def build_regulator_board(
    project: str,
    vin_rail: str,
    vout_rail: str,
    values: dict[str, str],
    mpns: dict[str, str],
    include_led: bool = True,
    title: str = "",
) -> str:
    """Emit a complete .kicad_sch for the regulator board family.

    values: {"U1": part, "R1": fb top, "R2": fb bottom, "R3": led R, "D1": led}
    """
    uid = _Uids(project)
    root_uuid = uid()

    def place(lib_id: str, ref: str, value: str, x: float, y: float,
              pins: list[str], props: dict[str, str] | None = None) -> str:
        prop_lines = [
            f'      (property "Reference" "{ref}" (at {x} {y - 6} 0) {FONT})',
            f'      (property "Value" "{value}" (at {x} {y + 6} 0) {FONT})',
            f'      (property "Footprint" "" (at {x} {y} 0) {FONT_HIDE})',
            f'      (property "Datasheet" "~" (at {x} {y} 0) {FONT_HIDE})',
        ]
        for k, v in (props or {}).items():
            prop_lines.append(
                f'      (property "{k}" "{v}" (at {x} {y} 0) {FONT_HIDE})')
        pin_lines = [f'      (pin "{p}" (uuid "{uid()}"))' for p in pins]
        return (
            f'    (symbol\n'
            f'      (lib_id "{lib_id}")\n'
            f'      (at {x} {y} 0)\n'
            f'      (unit 1)\n'
            f'      (exclude_from_sim no) (in_bom yes) (on_board yes) (dnp no)\n'
            f'      (uuid "{uid()}")\n'
            + "\n".join(prop_lines) + "\n"
            + "\n".join(pin_lines) + "\n"
            f'      (instances (project "{project}" (path "/{root_uuid}" '
            f'(reference "{ref}") (unit 1))))\n'
            f'    )'
        )

    def wire(x1, y1, x2, y2) -> str:
        return (f'    (wire (pts (xy {x1} {y1}) (xy {x2} {y2})) '
                f'(stroke (width 0) (type default)) (uuid "{uid()}"))')

    def junction(x, y) -> str:
        return (f'    (junction (at {x} {y}) (diameter 0) (color 0 0 0 0) '
                f'(uuid "{uid()}"))')

    def mp(ref: str) -> dict[str, str]:
        return {"MPN": mpns[ref]} if mpns.get(ref) else {}

    # geometry identical to the proven demo board (see design docs):
    # U1@(100,60): VIN(92.38,60) VOUT(107.62,60) ADJ(105.08,65.08)
    placements = [
        place("Regulator_Linear:TLV1117-ADJ", "U1", values["U1"], 100, 60,
              ["1", "2", "3"], mp("U1")),
        place("Device:R", "R1", values["R1"], 130, 63.81, ["1", "2"], mp("R1")),
        place("Device:R", "R2", values["R2"], 130, 71.43, ["1", "2"], mp("R2")),
        place(f"power:{vin_rail}", "#PWR01", vin_rail, 87.38, 60, ["1"]),
        place(f"power:{vout_rail}", "#PWR02", vout_rail, 115, 55, ["1"]),
        place("power:GND", "#PWR04", "GND", 130, 78, ["1"]),
        place("power:PWR_FLAG", "#FLG01", "PWR_FLAG", 90, 57, ["1"]),
        # KiCad ERC also wants GND declared as driven (power_pin_not_driven)
        place("power:PWR_FLAG", "#FLG02", "PWR_FLAG", 126, 78, ["1"]),
    ]
    wires = [
        wire(87.38, 60, 90, 60), wire(90, 60, 92.38, 60), wire(90, 57, 90, 60),
        wire(107.62, 60, 115, 60), wire(115, 55, 115, 60), wire(115, 60, 130, 60),
        wire(130, 67.62, 105.08, 67.62), wire(105.08, 67.62, 105.08, 65.08),
        wire(130, 75.24, 130, 78),
        wire(126, 78, 130, 78),
    ]
    junctions = [junction(115, 60), junction(90, 60), junction(130, 67.62)]

    if include_led:
        placements += [
            place("Device:R", "R3", values["R3"], 145, 63.81, ["1", "2"], mp("R3")),
            place("Device:LED", "D1", values["D1"], 145, 71.43, ["1", "2"], mp("D1")),
            place("power:GND", "#PWR05", "GND", 145, 78, ["1"]),
        ]
        wires += [wire(130, 60, 145, 60), wire(145, 75.24, 145, 78)]
        junctions.append(junction(130, 60))

    libs = [lib_resistor(), lib_regulator(),
            lib_power(vin_rail), lib_power(vout_rail), lib_power("GND"),
            lib_flag()]
    if include_led:
        libs.insert(1, lib_led())

    return f"""(kicad_sch
  (version 20231120)
  (generator "eeschema")
  (generator_version "8.0")
  (uuid "{root_uuid}")
  (paper "A4")
  (title_block (title "{title or project}") (rev "A") (comment 1 "generated by RatsNest"))
  (lib_symbols
{chr(10).join(libs)}
  )
{chr(10).join(placements + wires + junctions)}
  (sheet_instances (path "/" (page "1")))
)
"""
