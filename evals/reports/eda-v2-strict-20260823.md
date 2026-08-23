# Strict live EDA regrade

> A case passes only when the 17-step pipeline reaches an accepted terminal status and publishes valid core EDA artifacts.

- Plan: `ratsnest-five-case-20260730-20260823-p2v2`
- Strict cases: 0/5
- Raw false passes: 3

| Case | Steps | Delivery | Raw | Strict | Missing check |
|---|---:|---|---|---|---|
| `eda.01-rp2040-env-logger` | 8 | - | FAIL | FAIL | requiredPhases, terminal, edaPipeline |
| `eda.02-stm32g431-bldc` | 6 | - | FAIL | FAIL | requiredPhases, terminal, edaPipeline |
| `eda.03-esp32c3-isolated-gateway` | 3 | execution_blocked | PASS | FAIL | edaPipeline |
| `eda.04-nrf52840-motion-beacon` | 3 | execution_blocked | PASS | FAIL | edaPipeline |
| `eda.05-stm32f072-usb-midi` | 5 | execution_blocked | PASS | FAIL | edaPipeline |
