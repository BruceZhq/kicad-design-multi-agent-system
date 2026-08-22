---
role: reviewer,repair,architect
title: Decoupling capacitor placement
---

# Decoupling capacitors

Every IC power pin needs a local decoupling (bypass) capacitor, typically
100 nF (0.1 uF), placed close to the relevant supply pin. The required count,
dielectric, and bulk capacitance must come from the selected device and regulator evidence.

Common findings:
- Too few decoupling capacitors for the number of supply pins.
- Decoupling capacitor not connected directly between the supply rail and
  ground.
- Wrong value (e.g. 1 uF where 100 nF is expected for high-frequency bypass).

Fix strategy: match the decoupling count to the verified device target and ensure each
cap connects pin 1 to the regulated rail and pin 2 to ground.
