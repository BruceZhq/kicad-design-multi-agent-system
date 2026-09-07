---
name: "failure-reflection"
description: "Reflect on a failed or stagnant pipeline step, attribute the failure from evidence, and choose a measurable next action without weakening requirements or gates."
mode: "reflect"
applies_to_steps: ["*"]
allowed_capabilities: ["artifact.read", "artifact.diff", "artifact.checkpoint", "artifact.rollback", "knowledge.search", "eda.evidence.read", "eda.check.run", "workflow.replan", "workflow.retry_tool", "workflow.request_human", "workflow.record_harness_gap"]
required_gates: ["immutable_requirements", "failure_evidence", "action_budget"]
write_scope: ["run_state", "audit_log"]
---

# Failure Reflection

Use this skill after a failed check, contradictory evidence, a rejected repair, or a repeated action with no material improvement. Reflection must change the causal model or next action; paraphrasing the failure and replaying the same step is not reflection.

## Evidence first

Establish:

- whether the underlying tool actually ran and produced current, parseable output;
- whether the candidate artifact changed and whether the failed check could be affected by that change;
- which evidence is authoritative for the claim under review;
- whether the previous action produced its expected observation;
- which immutable requirements, budgets, and permissions constrain the next action.

Attribute the failure as design, infrastructure, external evidence, Harness, hard constraint, or unresolved. Do not default an unknown origin to a design defect merely because the check is attached to a design step.

## Reflection decision

Choose exactly one next direction:

- `continue_local`: a bounded project-local action has a causal path to improving the failed check.
- `replan_upstream`: current downstream evidence invalidates an upstream assumption or artifact.
- `retry_infrastructure`: execution evidence shows a transient or incomplete tool run; keep design inputs unchanged.
- `record_harness_gap`: the checker, adapter, normalization, or orchestration contradicts authoritative evidence and a project-local workaround would corrupt intent.
- `request_human`: required evidence, authority, or a material design choice is missing.
- `stop_hard_conflict`: an immutable requirement is demonstrably incompatible with another hard constraint or physical fact.

Return an auditable summary containing failure origin, decisive evidence, status of the previous hypothesis, selected direction, next skill or capability, expected observation, affected gates, and stop condition. Do not expose hidden chain-of-thought; provide only the concise engineering rationale needed to review the action.

## Rules

### Congested PCB routing

- Distinguish a search timeout from an endpoint with no legal escape. More retries cannot free a physically enclosed pad. Inspect exact pad corners, existing tracks/vias, both copper layers, and the connected copper island; a DRC endpoint is only an island representative.
- For adjacent fine-pitch pins, plan their escapes together: stagger vias and reserve each neighbor's exit before routing trunks. A locally successful escape that traps its neighbor is not a complete solution. Use a fingerprint-bound CAD action batch and judge the complete candidate after zone refill and DRC.
- If a passive component is trapped, request a bounded placement-owned patch for that component and reconnect its affected nets. Preserve unaffected copper and the verified schematic; do not restart selection or regenerate the entire placement to fix one endpoint.
- If closing a signal disconnects a ground pour, inspect the actual filled islands and their functional pads. Repair the island as part of the same candidate. A via in the same disconnected island is not a bridge. Never delete a required ground pad or ignore the new ground gap.
- Treat redundant vias and the dead stubs they leave behind as a compound cleanup. For same-net drill overlap, merge the drill and preserve its layer connections. Refill and verify zero new errors, preserved connectivity, and fewer warnings before accepting; do not waive a fabrication defect.

- Never weaken, suppress, rename, or skip a requirement or gate to improve a score.
- Never authorize a capability absent from this skill and the runtime permission set.
- If the artifact fingerprint, failed-check signature, and convergence score are unchanged, the same action is exhausted. Select a different hypothesis or terminal direction.
- A Harness gap is a first-class outcome, not a reason to regenerate the same artifact.
- Use `execution_blocked` only when safe in-scope actions and authorized recovery paths are exhausted or a hard conflict prevents credible execution. Preserve checkpoints and evidence for every non-success outcome.
