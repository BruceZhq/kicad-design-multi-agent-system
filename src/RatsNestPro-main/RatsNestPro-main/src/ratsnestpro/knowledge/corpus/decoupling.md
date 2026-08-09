---
role: reviewer,repair,architect
title: Decoupling capacitor placement
---

# Decoupling capacitors

Every IC power pin needs a local decoupling (bypass) capacitor, typically
100 nF (0.1 uF), placed within about 3 mm of the supply pin. The ATmega328P
development board uses several 100 nF ceramics distributed across the MCU
supply pins plus bulk capacitance near the regulator output.

Common findings:
- Too few decoupling capacitors for the number of supply pins.
- Decoupling capacitor not connected directly between the supply rail and
  ground.
- Wrong value (e.g. 1 uF where 100 nF is expected for high-frequency bypass).

Fix strategy: match the decoupling count to the family target and ensure each
cap connects pin 1 to the regulated rail and pin 2 to ground.
