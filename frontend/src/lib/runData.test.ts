import { describe, expect, it } from "vitest";
import {
  formatScoreDelta,
  parseRunRecord,
  summarizeEvent
} from "./runData";

describe("run data helpers", () => {
  it("returns null for blank or invalid run record JSON", () => {
    expect(parseRunRecord(null)).toBeNull();
    expect(parseRunRecord("")).toBeNull();
    expect(parseRunRecord("{not-json")).toBeNull();
  });

  it("parses run record iterations from resultJson", () => {
    const record = parseRunRecord(
      JSON.stringify({
        run_id: "py-1",
        status: "converged",
        iterations: [
          {
            iteration: 0,
            score_delta: 12,
            scorecard: { score: 84 },
            patch_plan: { ops: [{ op: "replace" }], rationale: { f1: "fixed" } },
            resolved_findings: ["f1"]
          }
        ]
      })
    );

    expect(record?.iterations).toHaveLength(1);
    expect(record?.iterations[0].scorecard.score).toBe(84);
  });

  it("formats score deltas for compact tables", () => {
    expect(formatScoreDelta(4)).toBe("+4");
    expect(formatScoreDelta(0)).toBe("0");
    expect(formatScoreDelta(-3)).toBe("-3");
    expect(formatScoreDelta(null)).toBe("-");
  });

  it("summarizes MCP tool events", () => {
    expect(
      summarizeEvent({
        id: 1,
        eventId: "evt-1",
        runId: "run-1",
        iteration: 2,
        step: 4,
        node: "mcp_tool",
        reward: 0.2,
        receivedAt: "2026-07-04T00:00:00Z",
        payload: JSON.stringify({
          action: { tool: "place_symbol", arguments: { ref: "U1" } }
        })
      })
    ).toContain("place_symbol");
  });

  it("summarizes regular ATDP events from outcome payload", () => {
    expect(
      summarizeEvent({
        id: 2,
        eventId: "evt-2",
        runId: "run-1",
        iteration: 0,
        step: 1,
        node: "evaluate",
        reward: null,
        receivedAt: "2026-07-04T00:00:00Z",
        payload: JSON.stringify({
          outcome: { status: "scored", score: 72 }
        })
      })
    ).toContain("scored");
  });

  it("summarizes autonomous Agent plans and tool calls", () => {
    const plan = summarizeEvent({
      iteration: 0,
      step: 7,
      node: "design.schematic_designer.plan",
      payload: JSON.stringify({
        action: {
          goal: "materialize schematic",
          actions: [{ tool: "place_component" }, { tool: "connect_pin" }]
        }
      })
    });
    const tool = summarizeEvent({
      iteration: 0,
      step: 8,
      node: "design.schematic_designer.tool",
      payload: JSON.stringify({
        action: { tool: "place_component", arguments: { ref: "U1" } }
      })
    });

    expect(plan).toContain("place_component -> connect_pin");
    expect(tool).toContain("place_component");
  });
});
