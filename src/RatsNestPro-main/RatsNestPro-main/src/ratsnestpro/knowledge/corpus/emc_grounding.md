---
role: layout,routing,emc
title: EMC and grounding
---

# EMC and grounding

Most EMC problems are loop-area and return-path problems. Keep current loops
small and grounds solid.

Practices:
- Use a solid ground plane/pour; avoid splitting it under signals.
- Minimize the loop area of high-di/dt paths (regulator switching node,
  decoupling loops, crystal loop).
- Place decoupling right at the pin so the high-frequency loop is tiny.
- Keep noisy nets (clocks, switching) away from board edges and sensitive
  analog; guard with ground if needed.
- Filter and protect I/O at the connector (series resistor/ferrite, TVS).
- Route differential pairs tightly coupled and length-matched.

A compact layout with a continuous ground reference solves most emission and
susceptibility issues before they start.
