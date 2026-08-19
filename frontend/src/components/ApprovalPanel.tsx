import { useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import {
  Check,
  CircuitBoard,
  Clock3,
  Play,
  ShieldCheck,
  X
} from "lucide-react";
import {
  decideRunApproval,
  getBoardPlan,
  getDesignPlan,
  getRunApprovals
} from "../lib/api";
import type {
  BoardPlan,
  DesignPlan,
  DesignRun,
  RunApproval
} from "../lib/runData";
import { isTerminal } from "./runShared";

type ApprovalType = "board_plan" | "design_release";

export function ApprovalPanel({
  run,
  onChanged
}: {
  run: DesignRun;
  onChanged: () => void;
}) {
  const [approvals, setApprovals] = useState<RunApproval[]>([]);
  const [designPlan, setDesignPlan] = useState<DesignPlan | null>(null);
  const [legacyPlan, setLegacyPlan] = useState<BoardPlan | null>(null);
  const [comment, setComment] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (run.kind !== "design") {
      return;
    }
    let active = true;
    Promise.all([
      getRunApprovals(run.id),
      getDesignPlan(run.id),
      getBoardPlan(run.id)
    ])
      .then(([nextApprovals, nextPlan, nextBoardPlan]) => {
        if (active) {
          setApprovals(nextApprovals);
          setDesignPlan(nextPlan);
          setLegacyPlan(nextBoardPlan);
          setError(null);
        }
      })
      .catch((err) => {
        if (active) {
          setError(err instanceof Error ? err.message : "Review data unavailable");
        }
      });
    return () => {
      active = false;
    };
  }, [run.id, run.planSha256, run.releaseStatus, run.status]);

  const boardApproval = approvals.find((item) => item.type === "board_plan");
  const releaseApproval = approvals.find(
    (item) => item.type === "design_release"
  );
  const pendingApproval = useMemo(
    () => [boardApproval, releaseApproval].find(
      (item) => item?.status === "pending"
    ) ?? null,
    [boardApproval, releaseApproval]
  );
  const boardPlan = designPlan?.boardPlan ?? legacyPlan;

  if (run.kind !== "design") {
    return null;
  }

  async function decide(type: ApprovalType, decision: "approved" | "rejected") {
    setBusy(true);
    setError(null);
    try {
      const next = await decideRunApproval(run.id, type, decision, comment);
      setApprovals((current) => [
        ...current.filter((item) => item.type !== next.type),
        next
      ]);
      setComment("");
      onChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Decision failed");
    } finally {
      setBusy(false);
    }
  }

  const pendingType = pendingApproval?.type as ApprovalType | undefined;
  const isBoardReview = pendingType === "board_plan";

  return (
    <section className="rounded-lg border border-white/10 bg-[#101010] p-5">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <div className="flex items-center gap-2 text-primary">
            <ShieldCheck size={17} />
            <h3 className="text-sm uppercase tracking-[0.25em]">
              Design governance
            </h3>
          </div>
          <p className="mt-2 text-sm text-gray-400">
            {boardPlan
              ? `${boardPlan.topology} / ${boardPlan.components.length} components`
              : run.status === "planning"
                ? "RequirementAgent and CircuitArchitect are preparing the plan"
                : "Waiting for a typed design plan"}
          </p>
        </div>
        <ApprovalStatus approval={pendingApproval ?? releaseApproval ?? boardApproval} />
      </div>

      <div className="mt-5 grid overflow-hidden rounded-md border border-white/10 sm:grid-cols-4">
        <GovernanceStep
          complete={Boolean(designPlan)}
          icon={<CircuitBoard size={14} />}
          label="Plan"
        />
        <GovernanceStep
          complete={boardApproval?.status === "approved"}
          failed={boardApproval?.status === "rejected"}
          icon={<ShieldCheck size={14} />}
          label="Approve"
        />
        <GovernanceStep
          complete={isTerminal(run.status) && run.status !== "plan_rejected"}
          icon={<Play size={14} />}
          label="Execute"
        />
        <GovernanceStep
          complete={releaseApproval?.status === "approved"}
          failed={releaseApproval?.status === "rejected"}
          icon={<Check size={14} />}
          label="Release"
        />
      </div>

      {boardPlan ? (
        <div className="mt-5 overflow-hidden rounded-md border border-white/10">
          <div className="flex items-center justify-between border-b border-white/10 bg-black/35 px-4 py-3 text-xs text-gray-500">
            <span className="flex items-center gap-2 text-primary">
              <CircuitBoard size={15} /> Immutable BoardPlan
            </span>
            <span>
              {boardPlan.outline
                ? `${boardPlan.outline.width} x ${boardPlan.outline.height} mm`
                : "outline pending"}
            </span>
          </div>
          <div className="max-h-56 overflow-auto">
            <table className="w-full min-w-[480px] text-left text-xs">
              <thead className="text-gray-500">
                <tr>
                  <th className="px-4 py-2">Ref</th>
                  <th className="px-4 py-2">Value</th>
                  <th className="px-4 py-2">Symbol</th>
                  <th className="px-4 py-2">Footprint</th>
                </tr>
              </thead>
              <tbody className="text-gray-300">
                {boardPlan.components.map((component) => (
                  <tr className="border-t border-white/5" key={component.ref}>
                    <td className="px-4 py-2 text-primary">{component.ref}</td>
                    <td className="px-4 py-2">{component.value}</td>
                    <td className="px-4 py-2">{component.symbol}</td>
                    <td className="px-4 py-2 text-gray-500">
                      {component.footprint || "-"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {boardPlan.rationale ? (
            <p className="border-t border-white/10 px-4 py-3 text-xs leading-relaxed text-gray-400">
              {boardPlan.rationale}
            </p>
          ) : null}
        </div>
      ) : null}

      {designPlan ? (
        <div className="mt-3 space-y-1 break-all text-[11px] text-gray-600">
          <p>plan sha256: {designPlan.subjectSha256}</p>
          <p>
            strategy: {designPlan.strategyName} / {designPlan.strategyVersionId}
          </p>
        </div>
      ) : null}

      {pendingType ? (
        <div className="mt-4">
          <p className="mb-3 text-xs text-amber-100">
            {isBoardReview
              ? "Approval starts the autonomous KiCad agents. The approved plan cannot change."
              : "Approve the verified artifact before it can be downloaded."}
          </p>
          <textarea
            className="min-h-20 w-full resize-y rounded-md border border-white/10 bg-black/60 px-3 py-3 text-sm text-primary outline-none transition focus:border-primary/45"
            maxLength={1000}
            onChange={(event) => setComment(event.target.value)}
            placeholder="Engineering review comment"
            value={comment}
          />
          <div className="mt-3 flex flex-col gap-2 sm:flex-row">
            <button
              className="inline-flex flex-1 items-center justify-center gap-2 rounded-full bg-primary px-4 py-2 text-sm font-bold text-black disabled:opacity-60"
              disabled={busy}
              onClick={() => void decide(pendingType, "approved")}
              type="button"
            >
              {isBoardReview ? <Play size={16} /> : <Check size={16} />}
              {isBoardReview ? "Approve plan and start agents" : "Approve release"}
            </button>
            <button
              className="inline-flex flex-1 items-center justify-center gap-2 rounded-full border border-red-300/25 px-4 py-2 text-sm font-bold text-red-100 disabled:opacity-60"
              disabled={busy}
              onClick={() => void decide(pendingType, "rejected")}
              type="button"
            >
              <X size={16} /> Reject
            </button>
          </div>
        </div>
      ) : boardApproval?.status === "approved" && !releaseApproval ? (
        <p className="mt-4 flex items-center gap-2 text-xs text-gray-400">
          <Clock3 size={14} /> {run.releaseStatus === "blocked"
            ? "Production verification did not pass; release is blocked."
            : "Plan approved. Agents are executing and verifying it."}
        </p>
      ) : null}

      <DecisionHistory approvals={approvals} />
      {error ? <p className="mt-3 text-xs text-red-200">{error}</p> : null}
    </section>
  );
}

function GovernanceStep({
  complete,
  failed = false,
  icon,
  label
}: {
  complete: boolean;
  failed?: boolean;
  icon: ReactNode;
  label: string;
}) {
  return (
    <div className="flex items-center gap-2 border-b border-white/10 px-3 py-2 text-xs last:border-b-0 sm:border-b-0 sm:border-r sm:last:border-r-0">
      <span className={
        failed ? "text-red-200" : complete ? "text-emerald-200" : "text-gray-600"
      }>
        {icon}
      </span>
      <span className={complete ? "text-gray-300" : "text-gray-600"}>{label}</span>
    </div>
  );
}

function ApprovalStatus({ approval }: { approval?: RunApproval | null }) {
  const status = approval?.status ?? "preparing";
  return (
    <span className={`w-fit rounded-full border px-3 py-1 text-xs font-bold ${
      status === "approved"
        ? "border-emerald-300/25 bg-emerald-300/10 text-emerald-100"
        : status === "rejected"
          ? "border-red-300/25 bg-red-300/10 text-red-100"
          : "border-amber-300/25 bg-amber-300/10 text-amber-100"
    }`}>
      {approval ? `${approval.type}: ${status}` : status}
    </span>
  );
}

function DecisionHistory({ approvals }: { approvals: RunApproval[] }) {
  const decided = approvals.filter((item) => item.status !== "pending");
  if (decided.length === 0) {
    return null;
  }
  return (
    <div className="mt-4 border-t border-white/10 pt-3 text-xs text-gray-500">
      {decided.map((item) => (
        <p className="mt-1" key={item.id}>
          {item.type}: {item.status} by {item.decidedBy ?? "reviewer"}
          {item.comment ? ` / ${item.comment}` : ""}
        </p>
      ))}
    </div>
  );
}
