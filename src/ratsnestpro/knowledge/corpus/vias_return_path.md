---
role: routing
title: Vias, return paths and planes
---

# Vias and return paths

Current returns directly beneath its signal trace at high frequency. Protect
that return path.

Guidance:
- Keep a continuous ground plane/pour under signal traces; do not slice it with
  other routing.
- When a signal changes layer, keep its reference plane continuous or add a
  nearby ground stitching via so the return current can follow.
- Minimize via count on fast/critical nets; each via adds inductance and a
  return-path discontinuity.
- Use several vias for power/ground connections to planes to lower resistance
  and inductance.
- Add ground stitching vias around board edges, connectors, and between plane
  regions to tie grounds together.

For the crystal loop specifically: no vias, shortest path, guarded by ground.
