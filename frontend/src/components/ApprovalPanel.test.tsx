import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { vi } from "vitest";
import { ApprovalPanel } from "./ApprovalPanel";

const api = vi.hoisted(() => ({
  getRunApprovals: vi.fn(),
  getDesignPlan: vi.fn(),
  getBoardPlan: vi.fn(),
  decideRunApproval: vi.fn()
}));

vi.mock("../lib/api", () => api);

describe("ApprovalPanel", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    const boardPlan = {
      topology: "adjustable_linear_regulator",
      components: [{
        ref: "U1",
        value: "AP1117",
        symbol: "Regulator_Linear:AP1117-ADJ"
      }],
      connections: [],
      outline: { width: 50, height: 35 }
    };
    api.getRunApprovals.mockResolvedValue([{
      id: "approval-1",
      runId: "run-1",
      type: "board_plan",
      status: "pending",
      subjectSha256: "a".repeat(64),
      requestedAt: "2026-07-15T00:00:00Z"
    }]);
    api.getDesignPlan.mockResolvedValue({
      contractVersion: "ratsnest.design-plan.v1",
      runId: "run-python",
      requirement: "12V to 5V",
      backend: "crew",
      strategyName: "v0",
      strategyVersionId: "strat_0123456789abcdef",
      subjectSha256: "a".repeat(64),
      createdAt: "2026-07-15T00:00:00Z",
      designSpec: { input_voltage: 12, output_voltage: 5 },
      boardPlan
    });
    api.getBoardPlan.mockResolvedValue(boardPlan);
    api.decideRunApproval.mockResolvedValue({
      id: "approval-1",
      runId: "run-1",
      type: "board_plan",
      status: "approved",
      subjectSha256: "a".repeat(64),
      requestedAt: "2026-07-14T00:00:00Z",
      decidedBy: "reviewer"
    });
  });

  it("approves an immutable BoardPlan before starting agents", async () => {
    const onChanged = vi.fn();
    render(
      <ApprovalPanel
        run={{
          id: "run-1",
          kind: "design",
          status: "awaiting_plan_approval",
          planSha256: "a".repeat(64)
        }}
        onChanged={onChanged}
      />
    );

    expect(await screen.findByText("adjustable_linear_regulator / 1 components"))
      .toBeInTheDocument();
    expect(screen.getByText("AP1117")).toBeInTheDocument();

    fireEvent.change(screen.getByPlaceholderText("Engineering review comment"), {
      target: { value: "checked against requirements" }
    });
    fireEvent.click(screen.getByRole("button", {
      name: /approve plan and start agents/i
    }));

    await waitFor(() => expect(api.decideRunApproval).toHaveBeenCalledWith(
      "run-1", "board_plan", "approved", "checked against requirements"
    ));
    expect(onChanged).toHaveBeenCalledOnce();
  });
});
