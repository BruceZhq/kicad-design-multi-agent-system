---
role: layout
title: Placement partitioning
---

# Placement partitioning

Before moving parts, divide the board into functional zones and keep each
circuit block together. Poor partitioning is the root cause of messy,
error-prone layouts.

Partitioning:
- Separate noisy digital, sensitive analog, high-power, and RF sections.
- Place connectors along the board edge where the cable/plug enters.
- Put the power-input and regulator near the power connector; flow power
  across the board so downstream loads follow the rail.
- Cluster each IC with its own decoupling and support parts.
- Reserve keep-outs for mounting holes and board-edge clearance.

A good partition makes routing short and the board legible. Assign every
component to a zone first, then place within the zone.
