---
role: routing,stackup
title: Layer stackup and controlled impedance
---

# Stackup and impedance

Choose the layer count from complexity and signal integrity needs, not habit.

Common stackups:
- 2-layer: signal/power top, ground-ish bottom. Cheapest; fine for low-speed
  MCU boards. Keep a solid ground area on the bottom for return paths.
- 4-layer: Signal / GND / PWR / Signal. Strongly preferred once there are fast
  edges, many nets, or controlled-impedance requirements. The inner GND plane
  gives every signal a clean return path directly beneath it.

Controlled impedance (IPC-2141/2152 style):
- Single-ended 50 Ω and differential 90/100 Ω are set by trace width, the
  dielectric height to the reference plane, and Er of the material.
- Thinner dielectric to the plane -> narrower trace for the same impedance.
- Keep an unbroken reference plane under impedance-controlled traces.

Assign each net class (power, ground, signal, differential, clock) a width,
clearance, and layer. Every value must be at or above the fab minimums.
