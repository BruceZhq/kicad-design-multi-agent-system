import { useEffect, useState } from "react";
import { FlaskConical } from "lucide-react";
import { getReport } from "../lib/api";
import { isTerminal } from "./runShared";

export function ReportPanel({ runId, status }: { runId: string; status?: string | null }) {
  const [report, setReport] = useState<string | null>(null);

  useEffect(() => {
    setReport(null);
    if (!isTerminal(status)) {
      return;
    }
    let active = true;
    getReport(runId).then((text) => {
      if (active) {
        setReport(text);
      }
    });
    return () => {
      active = false;
    };
  }, [runId, status]);

  if (!report) {
    return null;
  }

  return (
    <div className="rounded-lg border border-white/10 bg-[#101010] p-5">
      <div className="flex items-center gap-2 text-primary">
        <FlaskConical size={16} />
        <h3 className="text-sm uppercase tracking-[0.25em]">Design report</h3>
      </div>
      <pre className="mt-4 max-h-[420px] overflow-auto whitespace-pre-wrap rounded-md bg-black/45 p-4 text-xs leading-relaxed text-gray-300">
        {report}
      </pre>
    </div>
  );
}
