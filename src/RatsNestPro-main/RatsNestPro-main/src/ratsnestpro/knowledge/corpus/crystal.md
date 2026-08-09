---
role: reviewer,repair,architect
title: Crystal load capacitors and speed grade
---

# Crystal oscillator and load capacitors

An external crystal on the ATmega328P uses two load capacitors, one from each
crystal pin (XTAL1, XTAL2) to ground. The load capacitor value is chosen to
match the crystal's specified load capacitance; typical values are 18 pF for a
16 MHz crystal and 22 pF for an 8 MHz crystal on this board template.

Speed grade rule: the ATmega328P can only run at 16 MHz when powered at
4.5-5.5 V. At 3.3 V the maximum reliable clock is lower, so a 16 MHz crystal
requires the 5.0 V supply rail. A request for 16 MHz on a 3.3 V rail is
contradictory and must be resolved before generation.

Layout: keep the crystal and both load capacitors within about 5 mm of the
oscillator pins and route the loop without vias.
