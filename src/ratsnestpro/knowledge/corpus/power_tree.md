---
role: topology,selection
title: Power tree design
---

# Power tree

Design the supply from the connector inward. Identify the input source
(USB-C 5 V, barrel jack, battery) and every rail the design needs
(e.g. 5 V, 3.3 V, 1.8 V). Each regulator is a node; draw the tree so every
load sits under a rail that can supply its current with margin.

Guidelines:
- Size each regulator for the sum of downstream currents plus 30% headroom.
- Prefer one LDO per quiet analog rail; share a rail only for similar loads.
- Put bulk capacitance at the regulator output and local decoupling at each load.
- Keep the input protection (fuse/TVS/reverse) at the very front.
- Note the rail voltage on every net so downstream checks can verify it.

For a small MCU board the tree is usually: USB-C 5 V -> LDO -> 3.3 V/5 V rail
feeding the MCU, decouplers, and headers.
