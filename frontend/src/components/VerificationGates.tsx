import { CheckCircle2, CircleDashed, ShieldCheck, XCircle } from "lucide-react";
import type { RunIteration, VerificationGate } from "../lib/runData";

const ORDER = ["catalog", "bom", "erc", "drc", "spice", "thermal", "emc"];

export function VerificationGates({ iterations }: { iterations: RunIteration[] }) {
  const scorecard = iterations.length > 0
    ? iterations[iterations.length - 1].scorecard
    : undefined;
  const gates = scorecard?.gate_results ?? {};
  const rows = ORDER.map((name) => gates[name]).filter(
    (gate): gate is VerificationGate => Boolean(gate)
  );
  if (rows.length === 0) {
    return null;
  }
  const passed = scorecard?.required_gates_passed === true;

  return (
    <section className="rounded-lg border border-white/10 bg-[#101010] p-5">
      <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
        <div className="flex items-center gap-2 text-primary">
          <ShieldCheck size={17} />
          <h3 className="text-sm uppercase tracking-[0.25em]">
            Production verification
          </h3>
        </div>
        <span className={
          passed
            ? "text-xs font-bold text-emerald-200"
            : "text-xs font-bold text-red-200"
        }>
          {passed ? "release gates passed" : "release blocked"}
        </span>
      </div>

      <div className="mt-4 divide-y divide-white/10 border-y border-white/10">
        {rows.map((gate) => (
          <div
            className="grid gap-2 py-3 text-sm sm:grid-cols-[130px_110px_minmax(0,1fr)] sm:items-start"
            key={gate.name}
          >
            <div className="flex items-center gap-2 font-bold uppercase text-[#E1E0CC]">
              <GateIcon status={gate.status} /> {gate.name}
            </div>
            <span className={statusClass(gate.status)}>{gate.status}</span>
            <div className="min-w-0">
              <p className="text-gray-400">{gate.summary}</p>
              <p className="mt-1 break-all text-[11px] text-gray-600">
                {gate.tool || "checker"}
                {gate.evidence.length > 0
                  ? ` / ${gate.evidence.join(", ")}`
                  : ""}
              </p>
            </div>
          </div>
        ))}
      </div>
    </section>
  );
}

function GateIcon({ status }: { status: VerificationGate["status"] }) {
  if (status === "passed") {
    return <CheckCircle2 className="text-emerald-200" size={15} />;
  }
  if (status === "unavailable") {
    return <CircleDashed className="text-amber-200" size={15} />;
  }
  return <XCircle className="text-red-200" size={15} />;
}

function statusClass(status: VerificationGate["status"]): string {
  if (status === "passed") {
    return "w-fit text-xs font-bold text-emerald-200";
  }
  if (status === "unavailable") {
    return "w-fit text-xs font-bold text-amber-200";
  }
  return "w-fit text-xs font-bold text-red-200";
}
