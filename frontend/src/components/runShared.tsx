import { statusClassName } from "../lib/runData";

export function StatusBadge({ status }: { status?: string | null }) {
  return (
    <span
      className={`rounded-full border px-2.5 py-1 text-[11px] font-bold ${statusClassName(status)}`}
    >
      {status ?? "unknown"}
    </span>
  );
}

export function isTerminal(status?: string | null): boolean {
  return (
    status === "converged" ||
    status === "escalated" ||
    status === "suggested" ||
    status === "failed" ||
    status === "plan_rejected"
  );
}

export function stepLabel(name: string): string {
  return name.replace(/^step_\d+_/, "").replace(/_/g, " ");
}

export function Metric({ label, value }: { label: string; value: number | string }) {
  return (
    <div className="rounded-md border border-white/10 bg-black/35 p-4">
      <p className="text-xs uppercase tracking-[0.2em] text-gray-500">{label}</p>
      <p className="mt-2 text-3xl text-[#E1E0CC]">{value}</p>
    </div>
  );
}
