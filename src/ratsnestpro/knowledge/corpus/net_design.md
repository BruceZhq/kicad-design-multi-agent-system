---
role: schematic,topology
title: Net and connectivity design
---

# Connectivity (netlist intent) design

Express how components connect before assigning real pin numbers. Work net by
net, giving each a clear name and purpose.

Core nets for an MCU board:
- Power input net (e.g. VBUS) from the connector to the regulator input and
  its enable, plus input capacitor.
- Regulated rail (e.g. 3V3/5V) to every IC supply pin, all decoupling caps
  (pin 1), pull-ups, and header power pin.
- Ground (GND) as the reference: connector ground, all IC ground pins, every
  decoupler pin 2, cap grounds, switch/LED cathode, header ground pin.
- Oscillator loop: MCU XTAL1/XTAL2 to the crystal and its two load caps.
- Reset: MCU reset pin to a pull-up and the reset switch.
- Signal nets: one net per logical connection (e.g. GPIO to header pin).

Rules of thumb:
- Every net must have at least two pins — a single-pin net is a wiring mistake.
- A regulated rail and a ground net must both exist.
- USB-C CC pins each need an independent 5.1 k pull-down for a power sink.
- Keep the crystal loop short and isolated; its caps return to ground.
