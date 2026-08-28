"use client";

import { useEffect, useMemo, useState } from "react";

type Arm = "single_agent" | "multi_agent";
type AgentId = "ratsnestpro-single-agent-eval" | "ratsnestpro-multi-agent";

interface FrozenExecution {
  model: string;
  provider: string;
  environmentDigest: string;
  configDigest: string;
}

interface EvaluationCase {
  caseId: string;
  title?: string;
  tier?: string;
  pairId: string;
  arm: Arm;
  prompt: string;
  profileReference?: string | null;
  timeoutSeconds: number;
  agentConfig: Record<string, unknown>;
  verifiedAssetIds?: string[];
}

interface EvaluationPlan {
  schemaVersion: "1.0";
  planId: string;
  frozenExecution: FrozenExecution;
  cases: EvaluationCase[];
}

interface LoadedPlan {
  plan: EvaluationPlan;
  planDigest: string;
}

interface EvaluationLastRun {
  caseId: string;
  arm: Arm;
  runId: string;
  threadId: string;
  status: string;
}

const PLAN_PATH = "/evals/paired-kicad-golden.v1.json";
const EVALUATION_LAUNCH_KEY = "ratsnest.evaluation-launch";
const EVALUATION_LAST_RUN_KEY = "ratsnest.evaluation-last-run";
const AGENT_BY_ARM: Record<Arm, AgentId> = {
  single_agent: "ratsnestpro-single-agent-eval",
  multi_agent: "ratsnestpro-multi-agent",
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function parsePlan(value: unknown): EvaluationPlan {
  if (!isRecord(value) || value.schemaVersion !== "1.0" || typeof value.planId !== "string") {
    throw new Error("评测计划格式无效");
  }
  if (!isRecord(value.frozenExecution) || !Array.isArray(value.cases)) {
    throw new Error("评测计划缺少冻结配置或用例");
  }
  const frozen = value.frozenExecution;
  for (const key of ["model", "provider", "environmentDigest", "configDigest"] as const) {
    if (typeof frozen[key] !== "string") throw new Error(`冻结配置缺少 ${key}`);
  }

  const cases: EvaluationCase[] = value.cases.map((item) => {
    if (!isRecord(item)) throw new Error("评测用例格式无效");
    const arm = item.arm;
    if (arm !== "single_agent" && arm !== "multi_agent") {
      throw new Error("评测用例包含未允许的 arm");
    }
    if (
      typeof item.caseId !== "string" ||
      typeof item.pairId !== "string" ||
      typeof item.prompt !== "string" ||
      typeof item.timeoutSeconds !== "number" ||
      (item.agentConfig !== undefined && !isRecord(item.agentConfig))
    ) {
      throw new Error("评测用例缺少必填字段");
    }
    return { ...item, arm, agentConfig: item.agentConfig ?? {} } as unknown as EvaluationCase;
  });

  const pairs = new Map<string, EvaluationCase[]>();
  for (const item of cases) pairs.set(item.pairId, [...(pairs.get(item.pairId) ?? []), item]);
  for (const [pairId, pairCases] of pairs) {
    const arms = new Set(pairCases.map((item) => item.arm));
    if (pairCases.length !== 2 || arms.size !== 2 || pairCases[0].prompt !== pairCases[1].prompt) {
      throw new Error(`配对 ${pairId} 未包含共享提示词的 single/multi 两臂`);
    }
  }

  return {
    schemaVersion: "1.0",
    planId: value.planId,
    frozenExecution: frozen as unknown as FrozenExecution,
    cases,
  };
}

async function sha256(value: ArrayBuffer | string): Promise<string> {
  const input = typeof value === "string" ? new TextEncoder().encode(value) : value;
  const digest = await crypto.subtle.digest("SHA-256", input);
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
}

function downloadJson(fileName: string, value: unknown) {
  const blob = new Blob([`${JSON.stringify(value, null, 2)}\n`], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = fileName;
  link.click();
  URL.revokeObjectURL(url);
}

export function EvaluationWorkbench() {
  const [loaded, setLoaded] = useState<LoadedPlan | null>(null);
  const [error, setError] = useState("");
  const [pairId, setPairId] = useState("");
  const [arm, setArm] = useState<Arm>("single_agent");
  const [promptDigest, setPromptDigest] = useState("");
  const [copied, setCopied] = useState(false);
  const [lastRun, setLastRun] = useState<EvaluationLastRun | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    async function load() {
      try {
        const response = await fetch(PLAN_PATH, { cache: "no-store", signal: controller.signal });
        if (!response.ok) throw new Error(`无法加载评测计划（HTTP ${response.status}）`);
        const bytes = await response.arrayBuffer();
        const plan = parsePlan(JSON.parse(new TextDecoder().decode(bytes)));
        const planDigest = await sha256(bytes);
        setLoaded({ plan, planDigest });
        setPairId(plan.cases[0]?.pairId ?? "");
      } catch (reason) {
        if (!controller.signal.aborted) {
          setError(reason instanceof Error ? reason.message : "无法加载评测计划");
        }
      }
    }
    void load();
    return () => controller.abort();
  }, []);

  useEffect(() => {
    try {
      const raw = sessionStorage.getItem(EVALUATION_LAST_RUN_KEY);
      if (!raw) return;
      const value = JSON.parse(raw) as Partial<EvaluationLastRun>;
      if (
        typeof value.caseId === "string" &&
        (value.arm === "single_agent" || value.arm === "multi_agent") &&
        typeof value.runId === "string" &&
        typeof value.threadId === "string" &&
        typeof value.status === "string"
      ) setLastRun(value as EvaluationLastRun);
    } catch {
      sessionStorage.removeItem(EVALUATION_LAST_RUN_KEY);
    }
  }, []);

  const pairOptions = useMemo(() => {
    const seen = new Set<string>();
    return (loaded?.plan.cases ?? []).filter((item) => {
      if (seen.has(item.pairId)) return false;
      seen.add(item.pairId);
      return true;
    });
  }, [loaded]);
  const selectedCase = loaded?.plan.cases.find((item) => item.pairId === pairId && item.arm === arm) ?? null;
  const selectedPairCases = loaded?.plan.cases.filter((item) => item.pairId === pairId) ?? [];
  const agentId = AGENT_BY_ARM[arm];

  useEffect(() => {
    const firstArm = loaded?.plan.cases.find((item) => item.pairId === pairId)?.arm;
    if (firstArm) setArm(firstArm);
  }, [loaded, pairId]);

  useEffect(() => {
    let active = true;
    setPromptDigest("");
    if (selectedCase) {
      void sha256(selectedCase.prompt).then((digest) => { if (active) setPromptDigest(digest); });
    }
    return () => { active = false; };
  }, [selectedCase]);

  if (error) {
    return <section className="evaluation-disabled"><h1>评测计划不可用</h1><p role="alert">{error}</p></section>;
  }
  if (!loaded || !selectedCase) return <div className="app-loading">正在校验冻结评测计划…</div>;
  const evaluationCase = selectedCase;

  const frozenConfig = {
    frozenExecution: loaded.plan.frozenExecution,
    profileReference: evaluationCase.profileReference ?? null,
    timeoutSeconds: evaluationCase.timeoutSeconds,
    agentConfig: evaluationCase.agentConfig,
    verifiedAssetIds: evaluationCase.verifiedAssetIds ?? [],
  };
  const commonRecord = {
    schemaVersion: "1.0",
    planId: loaded.plan.planId,
    planDigest: loaded.planDigest,
    pairId: evaluationCase.pairId,
    caseId: evaluationCase.caseId,
    arm: evaluationCase.arm,
    agentId,
    promptDigest,
    frozenConfig,
  };

  function exportRunRequest() {
    downloadJson(`${evaluationCase.caseId}.run-request.json`, {
      ...commonRecord,
      recordKind: "paired_eval_run_request",
      prompt: evaluationCase.prompt,
      executionOwner: "src.evolution.live_runner",
    });
  }

  function exportResultTemplate() {
    downloadJson(`${evaluationCase.caseId}.manual-result.json`, {
      ...commonRecord,
      recordKind: "paired_eval_manual_result",
      observed: {
        transportCompleted: null,
        protocolCompleted: null,
        pipeline17StepCompleted: null,
        completedSteps: null,
        strictTaskSuccess: null,
        releaseReady: null,
        humanAccepted: null,
        durationSeconds: null,
        hitlRequestCount: null,
        phaseContractErrorCount: null,
        toolContractErrorCount: null,
      },
    });
  }

  function runInWorkspace() {
    if (!loaded) return;
    sessionStorage.setItem(EVALUATION_LAUNCH_KEY, JSON.stringify({
      ...commonRecord,
      prompt: evaluationCase.prompt,
      model: loaded.plan.frozenExecution.model,
      profileReference: evaluationCase.profileReference ?? null,
      timeoutSeconds: evaluationCase.timeoutSeconds,
    }));
    window.location.assign("/?evaluation=1#workspace");
  }

  return (
    <section className="evaluation-shell">
      <div className="evaluation-intro">
        <div>
          <p className="section-kicker">PAIRED GOLDEN EVALUATION</p>
          <h1>同题、冻结配置、顺序执行</h1>
        </div>
        <a href="/#workspace">返回工作区</a>
      </div>

      <aside className="evaluation-safety" role="note">
        执行按钮会把冻结 case 带入工程工作区；请求仍经过现有 OIDC、Java 控制面、SSE 与持久化链路。
        服务端会重新校验计划摘要、提示词、模型、能力范围和固定 Agent allowlist。
      </aside>
      {lastRun && (
        <aside className="evaluation-safety" role="status">
          最近一次：{lastRun.caseId} · {lastRun.arm} · {lastRun.status} · Run {lastRun.runId}
          {" · "}<a href={`/?thread_id=${encodeURIComponent(lastRun.threadId)}#workspace`}>查看真实运行会话</a>
        </aside>
      )}

      <div className="evaluation-grid">
        <div className="evaluation-controls">
          <label>
            <span>黄金任务对</span>
            <select value={pairId} onChange={(event) => setPairId(event.target.value)}>
              {pairOptions.map((item) => (
                <option key={item.pairId} value={item.pairId}>{item.title ?? item.pairId}</option>
              ))}
            </select>
          </label>
          <fieldset>
            <legend>执行臂</legend>
            {(["single_agent", "multi_agent"] as const).map((value) => (
              <label key={value}>
                <input type="radio" name="evaluation-arm" value={value} checked={arm === value} onChange={() => setArm(value)} />
                <span>{value === "single_agent" ? "单智能体对照" : "生产多智能体"}</span>
              </label>
            ))}
          </fieldset>
          <p>
            本 pair 的 counterbalanced 顺序：
            {selectedPairCases.map((item) => item.arm === "single_agent" ? "单智能体" : "多智能体").join(" → ")}
          </p>
          <dl>
            <div><dt>Case</dt><dd>{selectedCase.caseId}</dd></div>
            <div><dt>Agent</dt><dd>{agentId}</dd></div>
            <div><dt>Plan SHA-256</dt><dd>{loaded.planDigest}</dd></div>
            <div><dt>Prompt SHA-256</dt><dd>{promptDigest || "计算中…"}</dd></div>
          </dl>
        </div>

        <div className="evaluation-content">
          <label>
            <span>冻结用户提示词</span>
            <textarea readOnly value={selectedCase.prompt} rows={14} />
          </label>
          <div className="evaluation-actions">
            <button type="button" onClick={runInWorkspace} disabled={!promptDigest}>
              在工作区运行此臂
            </button>
            <button type="button" onClick={() => {
              void navigator.clipboard.writeText(selectedCase.prompt).then(() => {
                setCopied(true);
                window.setTimeout(() => setCopied(false), 1600);
              });
            }}>{copied ? "已复制" : "复制提示词"}</button>
            <button type="button" onClick={exportRunRequest} disabled={!promptDigest}>导出运行请求</button>
            <button type="button" onClick={exportResultTemplate} disabled={!promptDigest}>导出空白结果</button>
          </div>
          <details>
            <summary>查看冻结配置</summary>
            <pre>{JSON.stringify(frozenConfig, null, 2)}</pre>
          </details>
        </div>
      </div>
    </section>
  );
}
