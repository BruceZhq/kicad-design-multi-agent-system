import { Download } from "lucide-react";
import { downloadUrl } from "../lib/api";
import {
  AtdpEvent,
  DesignRun,
  formatScoreDelta,
  parseRunRecord,
  shortId,
  summarizeEvent
} from "../lib/runData";
import { StatusBadge, isTerminal, Metric } from "./runShared";
import { EdaPanel } from "./EdaPanel";
import { PreviewPanel } from "./PreviewPanel";
import { ReportPanel } from "./ReportPanel";
import { TimelinePanel } from "./TimelinePanel";
import { ApprovalPanel } from "./ApprovalPanel";
import { VerificationGates } from "./VerificationGates";

export function RunDetail({
  events,
  iterations,
  recordEscalation,
  run,
  onRunChanged
}: {
  events: AtdpEvent[];
  iterations: NonNullable<ReturnType<typeof parseRunRecord>>["iterations"];
  recordEscalation?: unknown;
  run: DesignRun | null;
  onRunChanged: () => void;
}) {
  if (!run) {
    return (
      <div className="rounded-lg border border-white/10 bg-[#101010] p-8 text-center text-gray-500">
        Select a run to inspect scorecards, repair rationale, and trajectory
        events.
      </div>
    );
  }

  const rationaleRows = (iterations ?? []).flatMap((iteration) =>
    Object.entries(iteration.patch_plan?.rationale ?? {}).map(([finding, why]) => ({
      finding,
      iteration: iteration.iteration,
      why
    }))
  );
  const hasDesignOutput = ["converged", "escalated", "suggested"].includes(
    run.status
  );

  return (
    <div className="space-y-4">
      <div className="rounded-lg border border-white/10 bg-[#101010] p-5">
        <div className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
          <div>
            <p className="text-sm uppercase tracking-[0.25em] text-primary">
              Run {shortId(run.id)}
            </p>
            <h3 className="mt-3 text-3xl text-[#E1E0CC] sm:text-4xl">
              {run.kind ?? "fix"} control record
            </h3>
          </div>
          <StatusBadge status={run.status} />
        </div>
        <div className="mt-6 grid gap-3 sm:grid-cols-3">
          <Metric label="Final score" value={run.finalScore ?? "-"} />
          <Metric label="Initial score" value={run.initialScore ?? "-"} />
          <Metric label="Iterations" value={iterations?.length ?? 0} />
        </div>
        <div className="mt-5 space-y-2 text-xs text-gray-500">
          <p className="break-all">
            {run.requirement ? `requirement: ${run.requirement}` : `project: ${run.projectDir ?? "-"}`}
          </p>
          <p className="break-all">
            backend: {run.backend ?? "-"}
            {run.owner ? ` / owner: ${run.owner}` : ""}
          </p>
          <p className="break-all">
            strategy: {run.strategyVersionId ?? "-"} / python run:{" "}
            {run.pythonRunId ?? "-"}
          </p>
        </div>
        {hasDesignOutput &&
        (!run.releaseStatus || run.releaseStatus === "approved") ? (
          <a
            className="mt-5 inline-flex items-center gap-2 rounded-full border border-primary/25 bg-primary/10 px-4 py-2 text-sm font-bold text-primary transition hover:bg-primary/20"
            href={downloadUrl(run.id)}
          >
            <Download size={15} />
            Download KiCad project (.zip)
          </a>
        ) : null}
        {run.failureMessage ? (
          <p className="mt-4 rounded-md border border-red-300/20 bg-red-300/10 p-3 text-sm text-red-100">
            {run.failureMessage}
          </p>
        ) : null}
      </div>

      <ApprovalPanel run={run} onChanged={onRunChanged} />

      <VerificationGates iterations={iterations ?? []} />

      {run.kind === "design" ? (
        <TimelinePanel live={!isTerminal(run.status)} runId={run.id} />
      ) : null}

      {run.kind === "design" && hasDesignOutput ? (
        <EdaPanel runId={run.id} />
      ) : null}

      {run.kind === "design" && hasDesignOutput ? (
        <PreviewPanel runId={run.id} />
      ) : null}

      <ReportPanel runId={run.id} status={run.status} />

      <div className="rounded-lg border border-white/10 bg-[#101010] p-5">
        <h3 className="text-sm uppercase tracking-[0.25em] text-primary">
          Repair iterations
        </h3>
        {iterations && iterations.length > 0 ? (
          <div className="mt-4 overflow-x-auto">
            <table className="w-full min-w-[520px] text-left text-sm">
              <thead className="text-xs uppercase tracking-[0.18em] text-gray-500">
                <tr>
                  <th className="border-b border-white/10 py-3">#</th>
                  <th className="border-b border-white/10 py-3">Score</th>
                  <th className="border-b border-white/10 py-3">Delta</th>
                  <th className="border-b border-white/10 py-3">Ops</th>
                  <th className="border-b border-white/10 py-3">Resolved</th>
                </tr>
              </thead>
              <tbody className="text-gray-300">
                {iterations.map((iteration) => (
                  <tr key={iteration.iteration}>
                    <td className="border-b border-white/5 py-3">
                      {iteration.iteration}
                    </td>
                    <td className="border-b border-white/5 py-3">
                      {iteration.scorecard.score ?? "-"}
                    </td>
                    <td className="border-b border-white/5 py-3">
                      {formatScoreDelta(iteration.score_delta)}
                    </td>
                    <td className="border-b border-white/5 py-3">
                      {iteration.patch_plan?.ops?.length ?? 0}
                    </td>
                    <td className="border-b border-white/5 py-3">
                      {iteration.resolved_findings?.length ?? 0}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <p className="mt-4 rounded-md border border-white/10 bg-black/35 p-4 text-sm text-gray-500">
            No iteration record has been written for this run yet.
          </p>
        )}
      </div>

      {rationaleRows.length > 0 ? (
        <div className="rounded-lg border border-white/10 bg-[#101010] p-5">
          <h3 className="text-sm uppercase tracking-[0.25em] text-primary">
            Repair rationale
          </h3>
          <div className="mt-4 space-y-3">
            {rationaleRows.map((row) => (
              <div
                className="rounded-md border border-white/10 bg-black/35 p-3 text-sm"
                key={`${row.iteration}-${row.finding}`}
              >
                <p className="text-primary">
                  iter {row.iteration} / {row.finding}
                </p>
                <p className="mt-1 text-gray-400">{row.why}</p>
              </div>
            ))}
          </div>
        </div>
      ) : null}

      {recordEscalation ? (
        <div className="rounded-lg border border-amber-300/20 bg-amber-300/10 p-5">
          <h3 className="text-sm uppercase tracking-[0.25em] text-amber-100">
            Escalation
          </h3>
          <pre className="mt-4 max-h-56 overflow-auto rounded-md bg-black/45 p-4 text-xs text-amber-50">
            {JSON.stringify(recordEscalation, null, 2)}
          </pre>
        </div>
      ) : null}

      <div className="rounded-lg border border-white/10 bg-[#101010] p-5">
        <h3 className="text-sm uppercase tracking-[0.25em] text-primary">
          ATDP trajectory ({events.length})
        </h3>
        <div className="mt-4 max-h-[360px] space-y-2 overflow-y-auto pr-1">
          {events.length === 0 ? (
            <p className="rounded-md border border-white/10 bg-black/35 p-4 text-sm text-gray-500">
              No trajectory events are attached to this run yet.
            </p>
          ) : (
            events.map((event, index) => (
              <div
                className="grid gap-2 rounded-md border border-white/10 bg-black/35 p-3 text-sm md:grid-cols-[120px_70px_minmax(0,1fr)]"
                key={event.eventId ?? `${event.node}-${event.step}-${index}`}
              >
                <div className="text-primary">
                  {event.iteration}.{event.node ?? "node"}
                </div>
                <div
                  className={
                    event.reward === null || event.reward === undefined
                      ? "text-gray-600"
                      : event.reward >= 0
                        ? "text-emerald-200"
                        : "text-red-200"
                  }
                >
                  {event.reward === null || event.reward === undefined
                    ? "-"
                    : formatScoreDelta(event.reward)}
                </div>
                <div className="break-all text-gray-400">
                  {summarizeEvent(event)}
                </div>
              </div>
            ))
          )}
        </div>
      </div>
    </div>
  );
}
