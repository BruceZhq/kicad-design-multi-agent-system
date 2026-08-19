import { useCallback, useEffect, useRef, useState } from "react";
import { applyEdaOps, getEdaState, previewUrl } from "../lib/api";

const SHEET_W = 297;
const SHEET_H = 210;

export function EdaPanel({ runId }: { runId: string }) {
  const [state, setState] = useState<import("../lib/api").EdaState | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [svgKey, setSvgKey] = useState(0);
  const [valueDraft, setValueDraft] = useState("");
  const [netDraft, setNetDraft] = useState({ pin: "1", net: "" });
  const [addDraft, setAddDraft] = useState({ symbol: "Device:C", value: "100n" });
  const canvasRef = useRef<HTMLDivElement | null>(null);
  const dragRef = useRef<{ ref: string; x: number; y: number } | null>(null);

  const load = useCallback(async () => {
    try {
      setState(await getEdaState(runId));
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "EDA state unavailable");
    }
  }, [runId]);

  useEffect(() => {
    setState(null);
    setSelected(null);
    void load();
  }, [load]);

  async function run(ops: import("../lib/api").EdaOp[]) {
    setBusy(true);
    try {
      const next = await applyEdaOps(runId, ops);
      setState(next);
      setSvgKey((k) => k + 1);
      setError(next.errors && next.errors.length > 0
        ? next.errors.join("; ") : null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "edit failed");
    } finally {
      setBusy(false);
    }
  }

  function toSheet(clientX: number, clientY: number) {
    const rect = canvasRef.current?.getBoundingClientRect();
    if (!rect) {
      return { x: 0, y: 0 };
    }
    return {
      x: Math.min(290, Math.max(5, ((clientX - rect.left) / rect.width) * SHEET_W)),
      y: Math.min(205, Math.max(5, ((clientY - rect.top) / rect.height) * SHEET_H))
    };
  }

  if (!state) {
    return error ? (
      <div className="rounded-lg border border-white/10 bg-[#101010] p-5 text-sm text-gray-500">
        Web EDA unavailable: {error}
      </div>
    ) : null;
  }

  const sel = state.components.find((c) => c.ref === selected) ?? null;

  return (
    <div className="rounded-lg border border-white/10 bg-[#101010] p-5">
      <div className="flex items-center justify-between gap-3">
        <h3 className="text-sm uppercase tracking-[0.25em] text-primary">
          Web EDA — drag, edit, connect
        </h3>
        <span className="text-xs text-gray-500">
          {busy ? "writing KiCad files..." : `${state.components.length} components`}
        </span>
      </div>
      {error ? <p className="mt-2 text-xs text-red-200">{error}</p> : null}

      <div
        className="relative mt-4 select-none overflow-hidden rounded-md border border-white/10 bg-white"
        onPointerMove={(e) => {
          if (!dragRef.current) return;
          const p = toSheet(e.clientX, e.clientY);
          dragRef.current = { ...dragRef.current, ...p };
          const el = document.getElementById(`eda-${dragRef.current.ref}`);
          if (el) {
            el.style.left = `${(p.x / SHEET_W) * 100}%`;
            el.style.top = `${(p.y / SHEET_H) * 100}%`;
          }
        }}
        onPointerUp={() => {
          const d = dragRef.current;
          dragRef.current = null;
          if (d) void run([{ op: "move", ref: d.ref, x: d.x, y: d.y }]);
        }}
        ref={canvasRef}
        style={{ aspectRatio: `${SHEET_W} / ${SHEET_H}` }}
      >
        <img
          alt="schematic"
          className="pointer-events-none absolute inset-0 h-full w-full object-fill"
          key={svgKey}
          src={`${previewUrl(runId, "sch")}?v=${svgKey}`}
        />
        {state.components.map((c) => (
          <button
            className={`absolute z-10 -translate-x-1/2 -translate-y-1/2 rounded border px-1 py-0.5 text-[10px] font-bold ${
              c.ref === selected
                ? "border-black bg-primary text-black"
                : "border-black/30 bg-black/70 text-primary"
            } cursor-grab active:cursor-grabbing`}
            id={`eda-${c.ref}`}
            key={c.ref}
            onPointerDown={(e) => {
              e.preventDefault();
              setSelected(c.ref);
              setValueDraft(c.value);
              dragRef.current = { ref: c.ref, x: c.x, y: c.y };
            }}
            style={{
              left: `${(c.x / SHEET_W) * 100}%`,
              top: `${(c.y / SHEET_H) * 100}%`
            }}
            type="button"
          >
            {c.ref}
          </button>
        ))}
      </div>

      <div className="mt-4 grid gap-3 md:grid-cols-3">
        <div className="rounded-md border border-white/10 bg-black/35 p-3">
          <p className="text-[10px] uppercase tracking-[0.25em] text-gray-500">
            {sel ? `${sel.ref} properties` : "select a component"}
          </p>
          {sel ? (
            <>
              <input
                className="mt-2 w-full rounded border border-white/10 bg-black/60 px-2 py-1.5 text-sm text-primary"
                onChange={(e) => setValueDraft(e.target.value)}
                value={valueDraft}
              />
              <button
                className="mt-2 w-full rounded-full bg-primary px-3 py-1.5 text-xs font-bold text-black disabled:opacity-50"
                disabled={busy || valueDraft === sel.value}
                onClick={() => void run([{ op: "set_value", ref: sel.ref, value: valueDraft }])}
                type="button"
              >
                Set value
              </button>
              <p className="mt-2 break-all text-[10px] text-gray-500">
                pins: {sel.pins.map((p) => `${p.pin}→${p.net ?? "?"}`).join("  ")}
              </p>
            </>
          ) : null}
        </div>
        <div className="rounded-md border border-white/10 bg-black/35 p-3">
          <p className="text-[10px] uppercase tracking-[0.25em] text-gray-500">
            connect pin to net
          </p>
          <div className="mt-2 flex gap-2">
            <input
              className="w-14 rounded border border-white/10 bg-black/60 px-2 py-1.5 text-sm text-primary"
              onChange={(e) => setNetDraft({ ...netDraft, pin: e.target.value })}
              placeholder="pin"
              value={netDraft.pin}
            />
            <input
              className="flex-1 rounded border border-white/10 bg-black/60 px-2 py-1.5 text-sm text-primary"
              list={`nets-${runId}`}
              onChange={(e) => setNetDraft({ ...netDraft, net: e.target.value })}
              placeholder="net (e.g. +5V)"
              value={netDraft.net}
            />
            <datalist id={`nets-${runId}`}>
              {state.nets.map((n) => <option key={n} value={n} />)}
            </datalist>
          </div>
          <button
            className="mt-2 w-full rounded-full border border-primary/25 bg-primary/10 px-3 py-1.5 text-xs font-bold text-primary disabled:opacity-50"
            disabled={busy || !sel || !netDraft.net.trim()}
            onClick={() => sel && void run([{ op: "connect_net", ref: sel.ref,
              pin: netDraft.pin.trim(), net: netDraft.net.trim() }])}
            type="button"
          >
            Connect {sel ? sel.ref : "..."}
          </button>
        </div>
        <div className="rounded-md border border-white/10 bg-black/35 p-3">
          <p className="text-[10px] uppercase tracking-[0.25em] text-gray-500">
            add component
          </p>
          <div className="mt-2 flex gap-2">
            <select
              className="flex-1 rounded border border-white/10 bg-black/60 px-2 py-1.5 text-sm text-primary"
              onChange={(e) => setAddDraft({ ...addDraft, symbol: e.target.value })}
              value={addDraft.symbol}
            >
              {state.palette.map((s) => <option key={s} value={s}>{s}</option>)}
            </select>
            <input
              className="w-20 rounded border border-white/10 bg-black/60 px-2 py-1.5 text-sm text-primary"
              onChange={(e) => setAddDraft({ ...addDraft, value: e.target.value })}
              placeholder="value"
              value={addDraft.value}
            />
          </div>
          <button
            className="mt-2 w-full rounded-full border border-primary/25 bg-primary/10 px-3 py-1.5 text-xs font-bold text-primary disabled:opacity-50"
            disabled={busy}
            onClick={() => {
              const prefix = addDraft.symbol.includes("LED") ? "D"
                : addDraft.symbol.includes(":C") ? "C"
                : addDraft.symbol.includes("Conn") ? "J" : "R";
              const used = state.components.filter((c) =>
                c.ref.startsWith(prefix)).length;
              void run([{ op: "add_component", ref: `${prefix}${used + 1}`,
                symbol: addDraft.symbol, value: addDraft.value,
                x: 200, y: 100 }]);
            }}
            type="button"
          >
            Add at (200,100)
          </button>
        </div>
      </div>
    </div>
  );
}
