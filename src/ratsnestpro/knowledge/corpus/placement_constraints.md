---
role: layout
title: Critical placement constraints
---

# Critical placement constraints

Some parts must be placed before everything else because their position is
electrically critical.

Strong constraints:
- Decoupling caps: within ~3 mm of the IC supply pin they serve, on the
  same side, with the shortest possible loop to ground.
- Crystal and its two load caps: hugging the MCU oscillator pins; keep
  the loop tiny and away from noisy signals; no vias in the loop.
- Bulk/regulator caps: at the regulator input and output pins.
- Connectors: on the board edge, oriented so the mating plug clears the board.
- High-current paths: short and direct; give them room for wide copper.

Position these constrained parts first and lock them, then arrange the rest
around them. Alignment and neatness come after correctness.
