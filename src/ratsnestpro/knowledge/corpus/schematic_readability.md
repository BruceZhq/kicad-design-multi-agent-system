---
role: schematic
title: Schematic readability and layout
---

# Schematic sheet layout

A readable schematic flows left-to-right, top-to-bottom: inputs on the left,
outputs on the right, power at the top, ground at the bottom.

Practices:
- Group by function: power input, regulator, MCU, oscillator, reset, headers.
- Use power symbols for supply and ground rather than long wires.
- Use net labels for buses and repeated signals; use direct wires for short
  local connections. Prefer labels over wires that would cross the whole sheet.
- Keep decoupling caps drawn next to the IC they serve.
- Never route a wire across a component body; go around it.
- Every net label name must exactly match its net so the netlist round-trips.

Placement of symbols on the sheet has no electrical meaning, but a tidy sheet
prevents wiring mistakes and makes review far easier.
