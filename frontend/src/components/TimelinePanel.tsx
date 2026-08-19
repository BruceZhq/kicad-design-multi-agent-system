import { useEffect, useState } from "react";
import { listSteps, previewUrl } from "../lib/api";
import { stepLabel } from "./runShared";

export function TimelinePanel({ runId, live }: { runId: string; live: boolean }) {
  const [steps, setSteps] = useState<string[]>([]);
  const [selected, setSelected] = useState<string | null>(null);

  useEffect(() => {
    setSteps([]);
    setSelected(null);
  }, [runId]);

  useEffect(() => {
    let active = true;
    async function load() {
      try {
        const next = await listSteps(runId);
        if (!active) {
          return;
        }
        setSteps(next);
        setSelected((current) =>
          current && next.includes(current)
            ? current
            : next.length > 0
              ? next[next.length - 1]
              : null
        );
      } catch {
        // steps not available (fix runs, template backend...)
      }
    }
    void load();
    if (live) {
      const timer = window.setInterval(() => void load(), 3000);
      return () => {
        active = false;
        window.clearInterval(timer);
      };
    }
    return () => {
      active = false;
    };
  }, [runId, live]);

  if (steps.length === 0) {
    return null;
  }

  return (
    <div className="rounded-lg border border-white/10 bg-[#101010] p-5">
      <div className="flex items-center justify-between gap-3">
        <h3 className="text-sm uppercase tracking-[0.25em] text-primary">
          Agent execution timeline
        </h3>
        <span className="text-xs text-gray-500">
          {steps.length} step{steps.length === 1 ? "" : "s"}
          {live ? " · live" : ""}
        </span>
      </div>
      <div className="mt-4 flex flex-wrap gap-2">
        {steps.map((step, index) => (
          <button
            className={`rounded-full border px-3 py-1.5 text-xs font-bold transition ${
              step === selected
                ? "border-primary/45 bg-primary text-black"
                : "border-white/10 bg-black/40 text-gray-400 hover:text-primary"
            }`}
            key={step}
            onClick={() => setSelected(step)}
            type="button"
          >
            {index + 1}. {stepLabel(step)}
          </button>
        ))}
      </div>
      {selected ? (
        <div className="mt-4 overflow-hidden rounded-md border border-white/10 bg-white">
          <img
            alt={stepLabel(selected)}
            className="max-h-[440px] w-full object-contain p-2"
            src={previewUrl(runId, selected)}
          />
        </div>
      ) : null}
    </div>
  );
}
