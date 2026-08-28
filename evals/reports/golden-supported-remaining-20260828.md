# Live Agent evaluation

> Scope: real HTTP/SSE runs with sanitized workflow evidence. This is not a manufacturing approval.

- Plan: `ratsnest-supported-golden-boards-v1` (`85fdbca5afe0b686c7ed8664fa238a8ebd1e9c9738b299c3d893eeb4c7a3bedc`)
- Source commit: `f66ee1f0e233ff11b2f357de57d4ec07f0808c53`
- Cases: 0/2
- Pass rate: 0.000
- Intent accuracy: 1.000
- Tool-contract accuracy: 0.500
- Gate accuracy: 0.000
- False releases: 0

| Case | Category | Intent | Status | Tools | Duration | Result |
|---|---|---|---|---:|---:|---|
| `golden.02-stm32f030-minimal-board` | eda_pipeline | build | completed | 0 | 0.1s | FAIL |
| `golden.03-stm32g070-controller` | eda_pipeline | build | completed | 0 | 0.1s | FAIL |
