"""Generate the demo KiCad board (golden variant) used as benchmark corpus seed.

FROZEN FIXTURE SOURCE — deliberately not refactored onto
ratsnest/design_gen/templates.py (its productized descendant): the benchmark
corpus must stay stable so changes to the evolving generator can never
silently rewrite the exam it is graded against.

Circuit: AP1117-ADJ adjustable LDO, 12V in -> 5V out, feedback divider R1/R2,
LED indicator D1 with series resistor R3.

Golden values: R1=3k, R2=1k  -> Vout = 1.25*(1+3) = 5.00V on the +5V rail.
              R3=330        -> LED current (5-2)/330 = 9mA.
              All parts carry MPNs.

Coordinates are computed so wires terminate exactly on pin positions, using the
same transform as kicad-happy's analyzer: abs=(cx+dx, cy-dy) at rotation 0.

Usage: python make_demo_board.py [out_dir]
"""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

_uuid_counter = 0
NAMESPACE = uuid.UUID("a31982f0-6d1c-4f2e-9d5e-000000000000")


def uid() -> str:
    global _uuid_counter
    _uuid_counter += 1
    return str(uuid.uuid5(NAMESPACE, f"demo-{_uuid_counter}"))


ROOT_UUID = "aaaaaaaa-bbbb-cccc-dddd-eeeeffff0001"
PROJECT = "demo_board"

FONT = "(effects (font (size 1.27 1.27)))"
FONT_HIDE = "(effects (font (size 1.27 1.27)) (hide yes))"


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
    # Vertical LED: pin 2 (A, anode) top at (0,3.81); pin 1 (K, cathode) bottom.
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
    # Adjustable LDO, AP1117-ADJ pinout style. Pin offsets (symbol coords, Y-up):
    #   VIN  pin 3 at (-7.62, 0)      -> left
    #   VOUT pin 2 at ( 7.62, 0)      -> right
    #   ADJ  pin 1 at ( 5.08, -5.08)  -> bottom right (feedback input)
    return f"""    (symbol "Regulator_Linear:AP1117-ADJ" (pin_names (offset 0.254)) (exclude_from_sim no) (in_bom yes) (on_board yes)
      (property "Reference" "U" (at 0 6.35 0) {FONT})
      (property "Value" "AP1117-ADJ" (at 0 5.08 0) {FONT})
      (property "Footprint" "" (at 0 0 0) {FONT_HIDE})
      (property "Datasheet" "~" (at 0 0 0) {FONT_HIDE})
      (symbol "AP1117-ADJ_0_1"
        (rectangle (start -7.62 3.81) (end 7.62 -3.81) (stroke (width 0.254) (type default)) (fill (type background)))
      )
      (symbol "AP1117-ADJ_1_1"
        (pin power_in line (at -7.62 0 0) (length 0) (name "VIN" {FONT}) (number "3" {FONT}))
        (pin power_out line (at 7.62 0 180) (length 0) (name "VOUT" {FONT}) (number "2" {FONT}))
        (pin input line (at 5.08 -5.08 90) (length 0) (name "ADJ" {FONT}) (number "1" {FONT}))
      )
    )"""


def lib_flag() -> str:
    # Proper KiCad ERC source marker. NOTE: kicad-happy deliberately excludes
    # PWR_FLAG from connectivity ("DRC-only marker"), so RS-001 still reports
    # +12V as unsourced — that is expected and suppressed via strategy assets.
    # The flag is kept so KiCad's own ERC passes when the board is opened.
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


def lib_power(name: str, pin_type: str = "power_in") -> str:
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
        (pin {pin_type} line (at 0 0 90) (length 0) (name "~" {FONT}) (number "1" {FONT}))
      )
    )"""


# ---------------------------------------------------------------------------
# Placements
# ---------------------------------------------------------------------------

def place_symbol(lib_id: str, ref: str, value: str, x: float, y: float,
                 pins: list[str], props: dict[str, str] | None = None,
                 angle: int = 0) -> str:
    prop_lines = [
        f'      (property "Reference" "{ref}" (at {x} {y - 6} 0) {FONT})',
        f'      (property "Value" "{value}" (at {x} {y + 6} 0) {FONT})',
        f'      (property "Footprint" "" (at {x} {y} 0) {FONT_HIDE})',
        f'      (property "Datasheet" "~" (at {x} {y} 0) {FONT_HIDE})',
    ]
    for k, v in (props or {}).items():
        prop_lines.append(f'      (property "{k}" "{v}" (at {x} {y} 0) {FONT_HIDE})')
    pin_lines = [f'      (pin "{p}" (uuid "{uid()}"))' for p in pins]
    return (
        f'    (symbol\n'
        f'      (lib_id "{lib_id}")\n'
        f'      (at {x} {y} {angle})\n'
        f'      (unit 1)\n'
        f'      (exclude_from_sim no) (in_bom yes) (on_board yes) (dnp no)\n'
        f'      (uuid "{uid()}")\n'
        + "\n".join(prop_lines) + "\n"
        + "\n".join(pin_lines) + "\n"
        f'      (instances (project "{PROJECT}" (path "/{ROOT_UUID}" (reference "{ref}") (unit 1))))\n'
        f'    )'
    )


def wire(x1: float, y1: float, x2: float, y2: float) -> str:
    return (f'    (wire (pts (xy {x1} {y1}) (xy {x2} {y2})) '
            f'(stroke (width 0) (type default)) (uuid "{uid()}"))')


def junction(x: float, y: float) -> str:
    return f'    (junction (at {x} {y}) (diameter 0) (color 0 0 0 0) (uuid "{uid()}"))'


def build_schematic(values: dict[str, str], mpns: dict[str, str]) -> str:
    """values: {ref: value}; mpns: {ref: MPN or omitted}."""

    def props_for(ref: str) -> dict[str, str]:
        return {"MPN": mpns[ref]} if ref in mpns else {}

    # --- coordinates (sheet, Y down). Pin abs = (cx+dx, cy-dy) at rot 0. ---
    # U1 at (100, 60): VIN (92.38,60)  VOUT (107.62,60)  ADJ (105.08,65.08)
    # R1 (fb top) at (130,63.81):  p1 (130,60)     p2 (130,67.62)
    # R2 (fb bot) at (130,71.43):  p1 (130,67.62)  p2 (130,75.24)
    # R3 (led)    at (145,63.81):  p1 (145,60)     p2 (145,67.62)
    # D1 (led)    at (145,71.43):  p2/A (145,67.62)  p1/K (145,75.24)
    placements = [
        place_symbol("Regulator_Linear:AP1117-ADJ", "U1", values["U1"], 100, 60,
                     ["1", "2", "3"], props_for("U1")),
        place_symbol("Device:R", "R1", values["R1"], 130, 63.81, ["1", "2"], props_for("R1")),
        place_symbol("Device:R", "R2", values["R2"], 130, 71.43, ["1", "2"], props_for("R2")),
        place_symbol("Device:R", "R3", values["R3"], 145, 63.81, ["1", "2"], props_for("R3")),
        place_symbol("Device:LED", "D1", values["D1"], 145, 71.43, ["1", "2"], props_for("D1")),
        # power symbols (pin offset (0,0) -> pin sits at placement coord)
        place_symbol("power:+12V", "#PWR01", "+12V", 87.38, 60, ["1"]),
        place_symbol("power:+5V", "#PWR02", "+5V", 115, 55, ["1"]),
        place_symbol("power:GND", "#PWR04", "GND", 130, 78, ["1"]),
        place_symbol("power:GND", "#PWR05", "GND", 145, 78, ["1"]),
        # PWR_FLAG for KiCad ERC (analyzer ignores it; RS-001 suppressed via strategy)
        place_symbol("power:PWR_FLAG", "#FLG01", "PWR_FLAG", 90, 57, ["1"]),
    ]

    wires = [
        # +12V rail -> VIN, split at (90,60) where the PWR_FLAG drops in
        wire(87.38, 60, 90, 60),
        wire(90, 60, 92.38, 60),
        wire(90, 57, 90, 60),
        # VOUT rail with +5V flag and branches to R1 (fb) and R3 (led)
        wire(107.62, 60, 115, 60),
        wire(115, 55, 115, 60),           # +5V power symbol drop
        wire(115, 60, 130, 60),           # to R1.p1
        wire(130, 60, 145, 60),           # to R3.p1
        # feedback node R1.p2 == R2.p1 at (130,67.62) -> U1.ADJ (105.08,65.08)
        wire(130, 67.62, 105.08, 67.62),
        wire(105.08, 67.62, 105.08, 65.08),
        # grounds (AP1117-ADJ has no GND pin; ADJ is the reference)
        wire(130, 75.24, 130, 78),        # R2.p2 -> GND
        wire(145, 75.24, 145, 78),        # D1.K -> GND
    ]
    junctions = [junction(115, 60), junction(130, 60), junction(130, 67.62),
                 junction(90, 60)]

    libs = "\n".join([
        lib_resistor(), lib_led(), lib_regulator(),
        lib_power("+12V"), lib_power("+5V"), lib_power("GND"),
        lib_flag(),
    ])
    body = "\n".join(placements + wires + junctions)

    return f"""(kicad_sch
  (version 20231120)
  (generator "eeschema")
  (generator_version "8.0")
  (uuid "{ROOT_UUID}")
  (paper "A4")
  (title_block (title "RatsNest demo board") (rev "A"))
  (lib_symbols
{libs}
  )
{body}
  (sheet_instances (path "/" (page "1")))
)
"""


GOLDEN_VALUES = {"U1": "AP1117-ADJ", "R1": "3k", "R2": "1k", "R3": "330", "D1": "LED_RED"}
GOLDEN_MPNS = {
    "U1": "AP1117E-ADJ",
    "R1": "RC0805FR-073KL",
    "R2": "RC0805FR-071KL",
    "R3": "RC0805FR-07330RL",
    "D1": "LTST-C170KRKT",
}


def main() -> None:
    out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent / "corpus" / "demo_board"
    out_dir.mkdir(parents=True, exist_ok=True)
    sch = build_schematic(GOLDEN_VALUES, GOLDEN_MPNS)
    (out_dir / "demo_board.kicad_sch").write_text(sch, encoding="utf-8")
    (out_dir / "demo_board.kicad_pro").write_text(
        json.dumps({"meta": {"filename": "demo_board.kicad_pro", "version": 1}}, indent=2),
        encoding="utf-8",
    )
    print(f"wrote {out_dir / 'demo_board.kicad_sch'}")


if __name__ == "__main__":
    main()
