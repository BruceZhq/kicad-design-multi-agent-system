# Governed Evolution and durable bridge matrix

| Event/condition | Java acceptance gate | Durable effect | Candidate effect |
|---|---|---|---|
| `harness_defect_observed` | signed Runtime channel; strict envelope; 64-hex `record_id`; `failure.origin=harness`; `recoverability=harness_observation`; `action=observe_harness`; attribution reason `harness_defect_not_yet_cross_run_reproducible`; deterministic failure reason allowlist; counts ≥1 | Insert observation with `record_id` as tenant-scoped idempotency key | Counts as trusted active recurrence |
| `capability_gap` | same envelope; `recoverability=capability_gap`; `action=capability_gap`; attribution reason `cross_run_reproducible_harness_defect`; deterministic failure reason allowlist; supplied project/run counts ≥2 | Idempotent observation insert | Refreshes aggregate; eligible only when DB-observed distinct projects and runs are both ≥2 |
| `capability_gap_resolved` | `gap_id=gap:<signature>`; step/signature binding; `action=resolve_capability_gap`; reason `verified_harness_capability_gap_resolved`; origin `harness`; positive counts | Idempotent resolution insert | Closes only the same tenant + harness version + manifest + project + signature and only later observations |
| missing/unknown `failure.reason_code` | rejected by Java allowlist | no observation | no count |
| raw/unlisted payload field | rejected before persistence | no raw prompt/message storage | no count |
| duplicate `record_id` on a different Runtime event sequence | DB conflict on `(tenant_id, observation_id)` | no duplicate | no duplicate count |
| first project HDO + second project gap | both event classes included in governed active query | two-project/two-run facts retained | eligible on project 2; previous off-by-one removed |
| same manifest/signature/step under two Harness versions | candidate identity includes `baseHarnessVersionId` in addition to manifest/signature/step/check | distinct tenant-scoped candidate rows; no primary-key collision | versions evolve independently even when manifest bytes are reused |
| browser absent/disconnected/large `Last-Event-ID` | browser cursor is ignored by governance ingestion | V15 worker drains from its own durable cursor | observations still arrive |
| collector/database failure | exception propagates before cursor CAS | lease released with retry delay; cursor unchanged | no silent loss |
| explicit Runtime `replay_gap`/cursor-ahead error | structured error code preserved by HTTP/gRPC adapters and rejected | cursor unchanged; high-priority failure log | fail closed |
| stale `[DONE]` filtered by authoritative Runtime state | sequence holes are allowed when no explicit replay-gap exists | visible next event may advance CAS cursor | no false-positive gap |
| historical terminal run at V15 deployment | migration initializes cursor to existing `newest_event_id` | no expired-buffer retry storm | governance collection explicitly starts at V15; no fake historical reconstruction |
| HITL event on browser stream | event high-water hint and operational interaction are persisted before forwarding; Evolution is not collected and its cursor is not advanced on the UI path | WAITING run remains claimable while backlog exists, then stops polling after cursor catch-up | no browser-controlled governance cursor |

Deterministic Harness failure reasons accepted by Java:

- `generic_capability_closure_contradiction`
- `verified_pin_alias_resolution_lost`

The Evolution bridge persists fingerprints and bounded structured facts, not raw prompts or diagnostic prose.
