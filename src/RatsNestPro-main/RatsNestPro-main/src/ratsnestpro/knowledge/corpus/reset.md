---
role: reviewer,architect
title: Reset circuit
---

# Reset circuit

An active-low MCU reset input commonly needs a pull-up to the logic rail and
may use a momentary push button to ground. Values and optional filtering must
be verified against the selected device datasheet.

Common findings:
- Missing reset pull-up (floating reset, spurious resets).
- Reset button not connected to ground.

Connector pin assignments must follow the explicit design contract; no header
pin convention is assumed.
