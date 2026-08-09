---
role: reviewer,repair
title: Verification finding taxonomy
---

# Finding taxonomy

Deterministic gate findings and their meaning:

- CAT-001: a component has no catalog identifier. Sourcing is incomplete.
- REF-001: a component has no connected pins. Likely a missing net.
- REF-002: a net has only one pin (dangling / single-pin net).
- VLT-002: the supply rail voltage declaration does not match the target.
- DEC-001: wrong number of decoupling capacitors.
- DEC-002: a decoupling capacitor has the wrong value.
- DEC-003: a decoupling capacitor is not connected between rail and ground.
- XTL-002: crystal load capacitor value does not match the crystal frequency.
- LDO-001/LDO-002: missing LDO input/output capacitor.
- GPIO-001: wrong number of breakout signal nets.
- HDR-001/HDR-002: header power/ground pin issues.

Severity error findings block release; warnings are advisory. The
deterministic gates are authoritative — a reviewer narrative can explain or
prioritize findings but cannot downgrade a real error.
