---
name: "schematic-engineering"
description: "Plan, inspect, edit, materialize, and verify candidate KiCad schematics when a schematic pipeline step needs engineering judgment or evidence-guided recovery."
mode: "execute"
applies_to_steps: ["schematic_connections", "schematic_pinmap", "schematic_layout", "schematic_materialize", "erc"]
allowed_capabilities: ["artifact.read", "artifact.diff", "artifact.write_candidate", "artifact.checkpoint", "artifact.rollback", "knowledge.search", "eda.schematic.inspect", "eda.schematic.edit_candidate", "eda.schematic.upsert_net_pin", "eda.schematic.remove_net_pin", "eda.schematic.set_no_connect", "eda.schematic.materialize", "eda.schematic.render", "eda.erc.run", "workflow.replan", "workflow.request_human", "workflow.record_harness_gap"]
required_gates: ["immutable_requirements", "symbol_pin_evidence", "schematic_structure", "erc"]
write_scope: ["run_workspace", "candidate_artifacts"]
---

# Schematic Engineering

Own the engineering decisions needed to produce a truthful, editable schematic candidate. Treat scripts and EDA tools as a toolbox: choose the smallest useful action from current evidence instead of replaying a fixed pipeline blindly.

## Authority

- Read all run-local requirements, plans, evidence, reports, checkpoints, and schematic artifacts relevant to the failed step.
- Create or edit candidates only inside the declared write scope. Checkpoint the current best artifact before a mutation and retain enough information to roll back.
- Use only capabilities granted by the runtime. This skill describes a maximum capability set; it does not grant filesystem, command, network, or publication authority by itself.
- Never change an immutable requirement, invent pin or footprint evidence, suppress a finding, weaken a gate, or edit the production Harness to obtain a pass.

## Plan–Act–Observe–Reflect

1. Plan from the current artifact and failure evidence. State the suspected cause, the smallest candidate change, the check it should affect, and the stop condition.
2. Act with one evidence-producing inspection or one bounded candidate mutation. Prefer source-level or structured edits followed by normal materialization over direct text surgery on KiCad files.
3. Observe raw tool output, artifact diffs, ERC results, rendered views, and pin/net evidence. A successful command is not evidence that the design is correct.
4. Reflect on whether the expected signal appeared. Keep an improved candidate, roll back a regression, or revise the hypothesis before the next action. Do not repeat the same action when the artifact fingerprint and failed checks are unchanged.

## Diagnosis

Separate at least these causes before choosing a repair:

- Design: the candidate violates topology, pin mapping, connectivity, or an accepted requirement.
- Infrastructure: an EDA tool did not execute reliably or its output is missing or unreadable.
- Evidence: required symbol, footprint, pin, or datasheet grounding is unavailable.
- Harness: independent EDA evidence and a wrapper or comparator disagree, or normalization/tool context changes the interpretation without changing the design.

When independent EDA evidence contradicts a derived comparator, inspect both evidence paths and their normalization rules before modifying the design. Record a Harness gap with reproducible evidence when no project-local design change can legitimately fix the failed check.

## Gates and completion

- Re-run every gate affected by a mutation; never reuse a stale ERC or rendered artifact.
- Treat explicit no-connects, hierarchical names, power semantics, and generated net names according to authoritative KiCad evidence rather than string appearance alone.
- Declare completion only when immutable requirements, grounded pin bindings, schematic structure, and ERC are all supported by current tool output.
- If safe actions are exhausted, preserve the best candidate and return a specific next decision: upstream replan, evidence request, Harness gap, or hard conflict. Generic retry exhaustion is not a diagnosis.
