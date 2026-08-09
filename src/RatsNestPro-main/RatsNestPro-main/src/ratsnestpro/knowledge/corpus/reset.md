---
role: reviewer,architect
title: Reset circuit
---

# Reset circuit

The ATmega328P active-low RESET pin needs a pull-up resistor (typically 10 k)
to the logic rail so it idles high, plus a momentary push button to ground for
manual reset. An optional small capacitor can add noise immunity but is not
required on this board template.

Common findings:
- Missing reset pull-up (floating reset, spurious resets).
- Reset button not connected to ground.

The breakout headers expose GPIO signals; header pin 1 is the supply rail and
the last pin is ground by convention on this board.
