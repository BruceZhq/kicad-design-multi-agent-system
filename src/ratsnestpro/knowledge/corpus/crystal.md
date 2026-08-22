---
role: reviewer,repair,architect
title: Crystal load capacitors and speed grade
---

# Crystal oscillator and load capacitors

An external MCU crystal commonly uses two load capacitors, one from each
oscillator pin to ground. The value must be derived from the selected crystal,
stray capacitance, and MCU datasheet rather than a board template.

Clock and voltage limits are device-specific. A proposed pair must be checked
against the selected MCU datasheet before generation.

Layout: keep the crystal and both load capacitors within about 5 mm of the
oscillator pins and route the loop without vias.
