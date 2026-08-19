import { FormEvent, useCallback, useEffect, useMemo, useState } from "react";
import { Activity, AlertTriangle, ArrowRight, CircuitBoard, Sparkles } from "lucide-react";
import {
  createDesignRun,
  createRepairRun,
  getTenantContext,
  getRun,
  getRunEvents,
  listRuns
} from "../lib/api";
import {
  AtdpEvent,
  DesignBackend,
  DesignRun,
  formatDate,
  parseRunRecord,
  shortId,
  TenantContext
} from "../lib/runData";
import { StatusBadge } from "./runShared";
import { RunDetail } from "./RunDetail";

export function ConsoleSection({ user }: { user: string | null }) {
  const [runs, setRuns] = useState<DesignRun[]>([]);
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);
  const [selectedRun, setSelectedRun] = useState<DesignRun | null>(null);
  const [events, setEvents] = useState<AtdpEvent[]>([]);
  const [requirement, setRequirement] = useState("");
  const [backend, setBackend] = useState<DesignBackend>("crew");
  const [projectDir, setProjectDir] = useState("");
  const [tenantContext, setTenantContext] = useState<TenantContext | null>(null);
  const [projectId, setProjectId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [isSubmittingDesign, setIsSubmittingDesign] = useState(false);
  const [isSubmittingRepair, setIsSubmittingRepair] = useState(false);

  const sortedRuns = useMemo(
    () =>
      [...runs].sort((a, b) =>
        (b.createdAt ?? "").localeCompare(a.createdAt ?? "")
      ),
    [runs]
  );

  const selectedRecord = useMemo(
    () => parseRunRecord(selectedRun?.resultJson),
    [selectedRun?.resultJson]
  );

  const selectedIterations = selectedRecord?.iterations ?? [];

  const projectOptions = useMemo(
    () =>
      (tenantContext?.organizations ?? []).flatMap((organization) =>
        organization.workspaces.flatMap((workspace) =>
          workspace.projects.map((project) => ({
            id: project.id,
            label: `${organization.name} / ${workspace.name} / ${project.name}`
          }))
        )
      ),
    [tenantContext]
  );

  useEffect(() => {
    if (!user) {
      setTenantContext(null);
      setProjectId(null);
      return;
    }
    let active = true;
    getTenantContext()
      .then((context) => {
        if (!active) return;
        setTenantContext(context);
        const firstProject = context.organizations
          .flatMap((organization) => organization.workspaces)
          .flatMap((workspace) => workspace.projects)[0];
        setProjectId((current) => current ?? firstProject?.id ?? null);
      })
      .catch((err) => {
        if (active) {
          setError(err instanceof Error ? err.message : "Unable to load projects");
        }
      });
    return () => {
      active = false;
    };
  }, [user]);

  const refreshRuns = useCallback(async () => {
    try {
      const nextRuns = await listRuns();
      setRuns(nextRuns);
      setError(null);
      setSelectedRunId((current) => {
        if (current || nextRuns.length === 0) {
          return current;
        }
        return nextRuns[0].id;
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to load runs");
    }
  }, []);

  useEffect(() => {
    void refreshRuns();
    const timer = window.setInterval(() => {
      void refreshRuns();
    }, 4000);
    return () => window.clearInterval(timer);
  }, [refreshRuns]);

  useEffect(() => {
    if (!selectedRunId) {
      setSelectedRun(null);
      setEvents([]);
      return;
    }

    let active = true;
    async function loadSelected() {
      try {
        const [run, runEvents] = await Promise.all([
          getRun(selectedRunId as string),
          getRunEvents(selectedRunId as string).catch(() => [])
        ]);
        if (!active) {
          return;
        }
        setSelectedRun(run);
        setEvents(runEvents);
      } catch (err) {
        if (active) {
          setError(err instanceof Error ? err.message : "Unable to load run");
        }
      }
    }

    void loadSelected();
    return () => {
      active = false;
    };
  }, [selectedRunId, runs]);

  async function submitDesign(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const trimmed = requirement.trim();
    if (!trimmed) {
      return;
    }

    setIsSubmittingDesign(true);
    try {
      const response = await createDesignRun(trimmed, backend, projectId);
      setSelectedRunId(response.runId);
      setRequirement("");
      await refreshRuns();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Design run failed");
    } finally {
      setIsSubmittingDesign(false);
    }
  }

  async function submitRepair(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const trimmed = projectDir.trim();
    if (!trimmed) {
      return;
    }

    setIsSubmittingRepair(true);
    try {
      const response = await createRepairRun(trimmed, projectId);
      setSelectedRunId(response.runId);
      setProjectDir("");
      await refreshRuns();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Repair run failed");
    } finally {
      setIsSubmittingRepair(false);
    }
  }

  return (
    <section
      id="console"
      className="relative overflow-hidden bg-black px-4 py-20 sm:px-6 md:py-28"
    >
      <div className="bg-noise absolute inset-0 opacity-[0.12]" />
      <div className="relative mx-auto max-w-7xl">
        <div className="flex flex-col gap-4 md:flex-row md:items-end md:justify-between">
          <div>
            <p className="text-[10px] uppercase tracking-[0.35em] text-primary/65 sm:text-xs">
              Live console
            </p>
            <h2 className="mt-3 max-w-3xl text-4xl leading-[0.95] text-[#E1E0CC] sm:text-5xl md:text-6xl">
              Run the loop from the same surface that explains it.
            </h2>
          </div>
          <div className="inline-flex items-center gap-2 rounded-full border border-white/10 bg-[#101010] px-4 py-2 text-sm text-gray-400">
            <Activity size={16} className="text-primary" />
            {runs.length} runs tracked
          </div>
        </div>

        {error ? (
          <div className="mt-6 flex items-start gap-3 rounded-lg border border-red-300/20 bg-red-300/10 p-4 text-sm text-red-100">
            <AlertTriangle className="mt-0.5 shrink-0" size={17} />
            <span>{error}</span>
          </div>
        ) : null}

        <div className="mt-8 grid gap-4 lg:grid-cols-[420px_minmax(0,1fr)]">
          <div className="space-y-4">
            <form
              className="rounded-lg border border-white/10 bg-[#101010] p-5"
              onSubmit={submitDesign}
            >
              <div className="flex items-center gap-2 text-primary">
                <Sparkles size={17} />
                <h3 className="text-sm uppercase tracking-[0.25em]">
                  New design
                </h3>
              </div>
              {projectOptions.length > 0 ? (
                <select
                  className="mt-4 w-full rounded-md border border-white/10 bg-black/60 px-3 py-2.5 text-sm text-primary outline-none transition focus:border-primary/45"
                  onChange={(event) => setProjectId(event.target.value)}
                  value={projectId ?? ""}
                >
                  {projectOptions.map((project) => (
                    <option key={project.id} value={project.id}>
                      {project.label}
                    </option>
                  ))}
                </select>
              ) : null}
              <textarea
                className="mt-4 min-h-28 w-full resize-y rounded-md border border-white/10 bg-black/60 px-3 py-3 text-sm text-primary outline-none transition focus:border-primary/45"
                onChange={(event) => setRequirement(event.target.value)}
                placeholder="a 12V to 3.3V power board with a green LED"
                value={requirement}
              />
              <div className="mt-3">
                <p className="mb-2 text-[10px] uppercase tracking-[0.28em] text-gray-500">
                  Design backend
                </p>
                <div className="grid grid-cols-3 gap-1 rounded-full border border-white/10 bg-black/50 p-1">
                  {(
                    [
                      { id: "template", label: "Template" },
                      { id: "crew", label: "Crew" },
                      { id: "mcp", label: "MCP" }
                    ] as { id: DesignBackend; label: string }[]
                  ).map((option) => (
                    <button
                      className={`rounded-full px-3 py-1.5 text-xs font-bold transition ${
                        backend === option.id
                          ? "bg-primary text-black"
                          : "text-gray-400 hover:text-primary"
                      }`}
                      key={option.id}
                      onClick={() => setBackend(option.id)}
                      type="button"
                    >
                      {option.label}
                    </button>
                  ))}
                </div>
                <p className="mt-2 text-[11px] leading-relaxed text-gray-500">
                  {backend === "template"
                    ? "Deterministic S-expression writer. Fast, schematic-only."
                    : backend === "crew"
                      ? "Autonomous Architect, Schematic, and PCB agents plan validated calls to in-process KiCad tools."
                      : "Deterministic KiCAD-MCP-Server tools over the external MCP stdio transport."}
                </p>
              </div>
              <button
                className="group mt-3 inline-flex w-full items-center justify-between rounded-full bg-primary px-4 py-2 text-sm font-bold text-black disabled:cursor-wait disabled:opacity-60"
                disabled={isSubmittingDesign}
                type="submit"
              >
                {isSubmittingDesign ? "Dispatching" : "Generate and verify"}
                <span className="flex h-8 w-8 items-center justify-center rounded-full bg-black text-primary transition-transform group-hover:scale-110">
                  <ArrowRight size={16} />
                </span>
              </button>
            </form>

            <form
              className="rounded-lg border border-white/10 bg-[#101010] p-5"
              onSubmit={submitRepair}
            >
              <div className="flex items-center gap-2 text-primary">
                <CircuitBoard size={17} />
                <h3 className="text-sm uppercase tracking-[0.25em]">
                  Repair project
                </h3>
              </div>
              <input
                className="mt-4 w-full rounded-md border border-white/10 bg-black/60 px-3 py-3 text-sm text-primary outline-none transition focus:border-primary/45"
                onChange={(event) => setProjectDir(event.target.value)}
                placeholder="absolute path to a KiCad project directory"
                value={projectDir}
              />
              <button
                className="group mt-3 inline-flex w-full items-center justify-between rounded-full border border-primary/20 bg-transparent px-4 py-2 text-sm font-bold text-primary disabled:cursor-wait disabled:opacity-60"
                disabled={isSubmittingRepair}
                type="submit"
              >
                {isSubmittingRepair ? "Starting loop" : "Run auto-fix loop"}
                <span className="flex h-8 w-8 items-center justify-center rounded-full bg-primary text-black transition-transform group-hover:scale-110">
                  <ArrowRight size={16} />
                </span>
              </button>
            </form>

            <div className="rounded-lg border border-white/10 bg-[#101010] p-5">
              <h3 className="text-sm uppercase tracking-[0.25em] text-primary">
                Runs
              </h3>
              <div className="mt-4 space-y-2">
                {sortedRuns.length === 0 ? (
                  <p className="rounded-md border border-white/10 bg-black/35 p-4 text-sm text-gray-500">
                    No runs yet. Dispatch a design or repair loop to populate
                    this list.
                  </p>
                ) : (
                  sortedRuns.map((run) => (
                    <button
                      className={`w-full rounded-md border p-3 text-left transition ${
                        run.id === selectedRunId
                          ? "border-primary/45 bg-primary/10"
                          : "border-white/10 bg-black/35 hover:border-white/25"
                      }`}
                      key={run.id}
                      onClick={() => setSelectedRunId(run.id)}
                      type="button"
                    >
                      <div className="flex items-center justify-between gap-3">
                        <span className="flex items-center gap-2 text-sm text-[#E1E0CC]">
                          {run.kind ?? "fix"} / {shortId(run.id)}
                          {run.backend ? (
                            <span className="rounded-full border border-primary/25 bg-primary/10 px-2 py-0.5 text-[10px] font-bold uppercase tracking-wide text-primary">
                              {run.backend}
                            </span>
                          ) : null}
                        </span>
                        <StatusBadge status={run.status} />
                      </div>
                      <div className="mt-2 flex items-center justify-between text-xs text-gray-500">
                        <span>{formatDate(run.createdAt)}</span>
                        <span>
                          score {run.finalScore ?? run.initialScore ?? "-"}
                        </span>
                      </div>
                    </button>
                  ))
                )}
              </div>
            </div>
          </div>

          <RunDetail
            events={events}
            iterations={selectedIterations}
            recordEscalation={selectedRecord?.escalation}
            run={selectedRun}
            onRunChanged={() => void refreshRuns()}
          />
        </div>
      </div>
    </section>
  );
}
