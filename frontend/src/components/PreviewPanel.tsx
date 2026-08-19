import { useState } from "react";
import { Image as ImageIcon } from "lucide-react";
import { previewUrl } from "../lib/api";

function PreviewImage({ runId, which }: { runId: string; which: "sch" | "pcb" }) {
  const [ok, setOk] = useState(true);
  if (!ok) {
    return null;
  }
  return (
    <div className="overflow-hidden rounded-md border border-white/10 bg-white">
      <div className="border-b border-black/10 bg-black/5 px-3 py-1.5 text-[11px] font-bold uppercase tracking-wide text-black/60">
        {which === "sch" ? "Schematic" : "PCB"}
      </div>
      <img
        alt={`${which} preview`}
        className="max-h-[420px] w-full object-contain p-2"
        onError={() => setOk(false)}
        src={previewUrl(runId, which)}
      />
    </div>
  );
}

export function PreviewPanel({ runId }: { runId: string }) {
  return (
    <div className="rounded-lg border border-white/10 bg-[#101010] p-5">
      <div className="flex items-center gap-2 text-primary">
        <ImageIcon size={16} />
        <h3 className="text-sm uppercase tracking-[0.25em]">
          Read-only preview
        </h3>
      </div>
      <div className="mt-4 grid gap-3 lg:grid-cols-2">
        <PreviewImage runId={runId} which="sch" />
        <PreviewImage runId={runId} which="pcb" />
      </div>
      <p className="mt-3 text-[11px] text-gray-500">
        Rendered headless from the generated KiCad files. Missing panels mean
        that layer was not produced (e.g. template backend is schematic-only)
        or kicad-cli is unavailable on the worker.
      </p>
    </div>
  );
}
