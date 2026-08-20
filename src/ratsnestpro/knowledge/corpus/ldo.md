---
role: reviewer,repair,architect
title: LDO regulator input and output capacitors
---

# LDO regulator

A linear regulator (LDO) converts the 5 V USB-C input to the logic rail
(3.3 V or 5.0 V). It needs an input capacitor (typically 1 uF) close to the IN
pin and an output capacitor (typically 1 uF) close to the OUT pin for
stability. The AP2112K family used on this board is stable with small ceramic
output capacitors.

Common findings:
- Missing input or output capacitor.
- Output capacitor not connected to the regulated rail.
- Supply voltage declared on the rail net does not match the regulator's
  configured output.

Fix strategy: set the LDO output voltage to the target rail voltage and ensure
input/output capacitors are present and correctly connected.
