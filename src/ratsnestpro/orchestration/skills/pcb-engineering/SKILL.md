---
name: "pcb-engineering"
description: "Plan and execute evidence-guided KiCad PCB placement, routing, verification, and manufacturing-candidate recovery across PCB pipeline steps."
mode: "execute"
applies_to_steps: ["layout_partition", "layout_critical", "layout_general", "layout_write", "route_plan", "route_planes", "route_signals", "route_fab", "manufacture"]
allowed_capabilities: ["artifact.read", "artifact.diff", "artifact.write_candidate", "artifact.checkpoint", "artifact.rollback", "knowledge.search", "eda.pcb.inspect", "eda.pcb.edit_candidate", "eda.pcb.move_footprint", "eda.pcb.rotate_footprint", "eda.pcb.swap_footprint_positions", "eda.pcb.ripup_net", "eda.pcb.add_track", "eda.pcb.add_via", "eda.pcb.resize_track", "eda.pcb.refill_zones", "eda.pcb.move_silkscreen", "eda.pcb.render", "eda.pcb.route_candidate", "eda.pcb.measure", "eda.drc.run", "workflow.replan", "workflow.request_human", "workflow.record_harness_gap"]
required_gates: ["immutable_requirements", "footprint_binding", "placement_constraints", "connectivity", "drc", "manufacturing_outputs"]
write_scope: ["run_workspace", "candidate_artifacts"]
---

# PCB Engineering

Produce the best verifiable PCB candidate by combining model judgment with deterministic EDA observations. Algorithms may create seeds, but placement, routing strategy, and repair direction must respond to the actual circuit, geometry, constraints, and prior outcomes.

## Authority

- Inspect run-local schematics, netlists, footprints, board geometry, routing reports, renderings, DRC output, manufacturing artifacts, and checkpoints.
- Move, rotate, route, or otherwise edit only candidate artifacts within the declared write scope. Preserve the last verified or best-scoring candidate before each material change.
- Use only runtime-granted capabilities. Do not install tools, write outside the run workspace, alter user constraints, suppress DRC findings, edit production Harness code, or publish manufacturing data without separate authority.

## Plan–Act–Observe–Reflect

1. Plan around circuit intent and hard constraints. Identify the affected functional block or nets, the proposed action, the measurement expected to improve, and the checks that must be rerun.
2. Act on a bounded candidate. Prefer local moves, rotations, rule-compliant routing changes, or parameter changes over full regeneration when evidence isolates the problem.
3. Observe real geometry, pad and net coordinates, ratsnest or connectivity, route telemetry, DRC findings, constraint measurements, and rendered views. Record the exact candidate fingerprint and tool parameters.
4. Reflect after every consequential action. Compare the observation with the expected signal, retain only material improvement, roll back regressions, and change the hypothesis before another attempt. Keep the best candidate rather than assuming the latest is best.

## Engineering judgment

- Use deterministic checks as legal and physical gates, not as a substitute for circuit reasoning or as a single objective function.
- Derive orientation from real pad geometry and net roles. Do not infer polarity, connector access, or isolation direction from a footprint name alone.
- Choose routing order, layers, widths, vias, planes, and copper strategy from electrical roles and accepted constraints. A router reporting success is not proof of connectivity or DRC compliance.
- When a repeated route or placement recipe does not change the failed measurements, inspect geometric feasibility, tool context, and upstream constraints before trying another parameter combination.

## Gates and completion

- Rerun all checks invalidated by a candidate change, including connectivity and DRC after final copper and routing state.
- Use renderings to discover problems; use geometry, connectivity, DRC, and explicit constraint measurements to declare a pass.
- Manufacturing output is complete only when it is derived from the verified candidate and its identity is consistent across source, board, and exported artifacts.
- If no safe action remains, preserve the best candidate and return a specific upstream replan, evidence request, Harness gap, or hard conflict with the attempted strategies and remaining failed measurements.
