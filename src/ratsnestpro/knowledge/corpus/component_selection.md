---
role: selection
title: Component selection criteria
---

# Component selection

Pick parts that are electrically correct, available, and manufacturable. Never
invent a manufacturer part number — ground every choice in a real catalog
(e.g. the JLCPCB/LCSC library) and a real symbol/footprint.

Selection checklist:
- Electrical fit: voltage/current/tolerance/temperature ratings with margin.
- Package: smaller passives (0402/0603) save space but cost hand-assembly ease;
  0603 is a good default for a hobby/dev board.
- Availability and cost: prefer in-stock "basic"/preferred parts to avoid
  assembly surcharges and lead time.
- Footprint availability: the chosen package must map to a real KiCad footprint.
- Symbol availability: the part must map to a real schematic symbol whose pins
  are known, so pin mapping downstream is grounded, not guessed.

When two parts are equivalent, prefer the one that is cheaper, in stock, and of
a "basic" library type.
