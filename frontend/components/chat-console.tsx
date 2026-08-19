"use client";

import { FormEvent, KeyboardEvent, useEffect, useMemo, useRef, useState } from "react";

import { AccountMenu } from "@/components/account-menu";
import { MarkdownContent } from "@/components/markdown-content";
import { requestedCapabilityProfile, requiresNewRun } from "@/lib/request-intent";
import { readSseStream } from "@/lib/sse";
import {
  CapabilityProfileMetadata,
  ChatMessage,
  ConversationSummary,
  DisplayMessage,
  HumanInputRequest,
  RunArtifact,
  RunEvent,
  RunSummary,
  ServiceInfo,
  TerminalRunEvent,
  isTerminalRunEvent,
  isTerminalReplayFailure,
  makeMessage,
  parseArtifactList,
  parseChatMessage,
  parseConversationList,
  parseHumanInputRequest,
  parseRunEvent,
  parseRunSummary,
} from "@/types/chat";
import { TeamConfig, TeamRole } from "@/types/team";

const AGENT_ID = "ratsnestpro-multi-agent";
const THREAD_KEY = "ratsnest.thread-id";
const ORGANIZATION_KEY = "ratsnest.organization-id";
const PROJECT_KEY = "ratsnest.project-id";
const MODEL_KEY = "ratsnest.model";
const PROFILE_KEY = "ratsnest.capability-profile";
const MAX_RECONNECTS = 4;

interface ActiveRun {
  idempotencyKey: string;
  runId: string | null;
  controller: AbortController;
  lastEventId: number;
}

interface InteractionState {
  request: HumanInputRequest;
  runId: string;
  lastEventId: number;
  idempotencyKey: string;
  status: "waiting" | "submitting" | "submitted" | "error";
  answer?: string;
  error?: string;
}

interface WorkspaceContext {
  organization: { tenantId: string; name: string };
  project: { projectId: string; name: string };
}

class NonRetryableRequestError extends Error {}
class HumanInputRequestedError extends Error {}

type Channel = "design" | "evidence" | "review";
type ModelLoadState = "waiting" | "loading" | "ready" | "error";

function newId(): string {
  return crypto.randomUUID();
}

function safeIdentity(value: string | null): string | null {
  return value && value.length <= 200 && /^[A-Za-z0-9._:-]+$/.test(value) ? value : null;
}

function safeUuid(value: string | null): string | null {
  return value && /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(value)
    ? value
    : null;
}

function explicitReasoning(message: ChatMessage): string {
  const value = message.custom_data?.reasoning_content;
  return typeof value === "string" ? value : "";
}

function displayMessage(message: ChatMessage, pending = false): DisplayMessage {
  return {
    ...makeMessage(message.type, message.content),
    ...message,
    tool_calls: Array.isArray(message.tool_calls) ? message.tool_calls : [],
    response_metadata: message.response_metadata ?? {},
    custom_data: message.custom_data ?? {},
    reasoning: explicitReasoning(message),
    clientId: newId(),
    pending,
  };
}

function errorText(value: unknown, fallback: string): string {
  if (!value || typeof value !== "object") return fallback;
  const detail = (value as { detail?: unknown }).detail;
  if (typeof detail === "string") return detail;
  if (detail && typeof detail === "object") {
    const message = (detail as { message?: unknown }).message;
    if (typeof message === "string") return message;
  }
  return fallback;
}

function errorCode(value: unknown): string {
  if (!value || typeof value !== "object") return "";
  const code = (value as { code?: unknown }).code;
  return typeof code === "string" ? code : "";
}

function workspaceContext(value: unknown): WorkspaceContext | null {
  if (!value || typeof value !== "object") return null;
  const candidate = value as Partial<WorkspaceContext>;
  if (!candidate.organization || !candidate.project) return null;
  if (
    !safeUuid(candidate.organization.tenantId) ||
    typeof candidate.organization.name !== "string" ||
    !safeUuid(candidate.project.projectId) ||
    typeof candidate.project.name !== "string"
  ) return null;
  return candidate as WorkspaceContext;
}

async function waitForRetry(milliseconds: number, signal: AbortSignal): Promise<void> {
  await new Promise<void>((resolve, reject) => {
    const timer = window.setTimeout(resolve, milliseconds);
    signal.addEventListener(
      "abort",
      () => {
        window.clearTimeout(timer);
        reject(new DOMException("Aborted", "AbortError"));
      },
      { once: true },
    );
  });
}

function phaseForRole(role: TeamRole): string[] {
  if (role.role_id === "supervisor-ratsnestpro") return ["intent-router", "supervisor"];
  if (role.role_id === "sub-agent-ratsnest-architect") return ["architect"];
  if (role.role_id === "sub-agent-ratsnest-parts-specialist") return ["parts-specialist"];
  if (role.role_id === "sub-agent-ratsnest-hardware-engineer") return ["hardware-engineer"];
  if (role.role_id === "sub-agent-ratsnest-reviewer") return ["reviewer"];
  return [`specialist:${role.role_id}`];
}

function roleStatus(role: TeamRole, events: DisplayMessage[], busy: boolean): string {
  const phases = phaseForRole(role);
  const matching = events.filter((message) => {
    const phase = String(message.custom_data.phase ?? "");
    return phases.some((candidate) => phase === candidate || phase.startsWith(`${candidate}:`));
  });
  const latest = matching.at(-1);
  const status = String(latest?.custom_data.status ?? "");
  if (status === "started" || status === "retrying" || status === "waiting") return "执行中";
  if (status === "completed" || status === "ok") return "已完成";
  if (status === "blocked" || status === "failed" || status === "error") return "有问题";
  if (role.role_id === "supervisor-ratsnestpro" && busy) return "正在统筹";
  return "等待中";
}

function messageAgent(message: DisplayMessage): string {
  if (message.type === "human") return "你";
  if (message.type === "tool") return "工具结果";
  const content = message.content.toLowerCase();
  if (content.includes("architect")) return "Architect";
  if (content.includes("parts specialist")) return "Parts Specialist";
  if (content.includes("reviewer")) return "Reviewer";
  if (content.includes("hardware engineer")) return "Hardware Engineer";
  return "Supervisor / 工程团队";
}

function channelIncludes(message: DisplayMessage, channel: Channel): boolean {
  if (channel === "design") return true;
  const phase = String(message.custom_data.phase ?? "");
  const content = message.content.toLowerCase();
  if (channel === "evidence") {
    return message.type === "tool" || phase.includes("architect") || phase.includes("parts") || content.includes("datasheet");
  }
  return phase.includes("review") || content.includes("reviewer") || content.includes("erc") || content.includes("drc");
}

export function ChatConsole({ team, onEditTeam }: { team: TeamConfig; onEditTeam: () => void }) {
  const [models, setModels] = useState<string[]>([]);
  const [model, setModel] = useState("");
  const [profiles, setProfiles] = useState<CapabilityProfileMetadata[]>([]);
  const [profileReference, setProfileReference] = useState("");
  const [workspace, setWorkspace] = useState<WorkspaceContext | null>(null);
  const [workspaceSetupRequired, setWorkspaceSetupRequired] = useState(false);
  const [workspaceCreating, setWorkspaceCreating] = useState(false);
  const [authenticationRequired, setAuthenticationRequired] = useState(false);
  const [loginHref, setLoginHref] = useState("/oauth2/start?rd=%2F%23workspace");
  const [threadId, setThreadId] = useState("");
  const [workspaceReady, setWorkspaceReady] = useState(false);
  const [modelLoadState, setModelLoadState] = useState<ModelLoadState>("waiting");
  const [messages, setMessages] = useState<DisplayMessage[]>([]);
  const [conversations, setConversations] = useState<ConversationSummary[]>([]);
  const [conversationsLoading, setConversationsLoading] = useState(false);
  const [deletingConversation, setDeletingConversation] = useState<string | null>(null);
  const [conversationRefresh, setConversationRefresh] = useState(0);
  const [liveMessage, setLiveMessage] = useState<DisplayMessage | null>(null);
  const liveRef = useRef<DisplayMessage | null>(null);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [status, setStatus] = useState("正在连接服务…");
  const [runState, setRunState] = useState<"idle" | "running" | "waiting_for_input" | TerminalRunEvent | "disconnected" | "rejected">("idle");
  const [interaction, setInteraction] = useState<InteractionState | null>(null);
  const [latestRun, setLatestRun] = useState<RunSummary | null>(null);
  const [artifacts, setArtifacts] = useState<RunArtifact[]>([]);
  const [artifactLoading, setArtifactLoading] = useState(false);
  const [artifactError, setArtifactError] = useState("");
  const [channel, setChannel] = useState<Channel>("design");
  const activeRun = useRef<ActiveRun | null>(null);
  const selectedThread = useRef("");
  const skipHistoryThread = useRef<string | null>(null);
  const messageEnd = useRef<HTMLDivElement | null>(null);

  function setLive(next: DisplayMessage | null) {
    liveRef.current = next;
    setLiveMessage(next);
  }

  function updateLive(update: (message: DisplayMessage) => DisplayMessage) {
    const current = liveRef.current ?? displayMessage(makeMessage("ai", ""), true);
    setLive(update(current));
  }

  function commitLive() {
    const current = liveRef.current;
    if (current && (current.content || current.reasoning)) {
      setMessages((items) => [...items, { ...current, pending: false }]);
    }
    setLive(null);
  }

  function applyWorkspace(value: unknown): boolean {
    const selected = workspaceContext(value);
    if (!selected) return false;
    localStorage.setItem(ORGANIZATION_KEY, selected.organization.tenantId);
    localStorage.setItem(PROJECT_KEY, selected.project.projectId);
    setWorkspace(selected);
    setAuthenticationRequired(false);
    setWorkspaceSetupRequired(false);
    setWorkspaceReady(true);
    return true;
  }

  useEffect(() => {
    const controller = new AbortController();
    const returnPath = `${window.location.pathname}${window.location.search}${window.location.hash}`;
    setLoginHref(`/oauth2/start?rd=${encodeURIComponent(returnPath)}`);
    const params = new URLSearchParams(window.location.search);
    const requestedThread =
      safeIdentity(params.get("thread_id")) ??
      safeIdentity(localStorage.getItem(THREAD_KEY)) ??
      newId();
    localStorage.setItem(THREAD_KEY, requestedThread);
    selectedThread.current = requestedThread;
    setThreadId(requestedThread);
    void (async () => {
      try {
        const query = new URLSearchParams();
        const organizationId = safeUuid(localStorage.getItem(ORGANIZATION_KEY));
        const projectId = safeUuid(localStorage.getItem(PROJECT_KEY));
        if (organizationId) query.set("organization_id", organizationId);
        if (projectId) query.set("project_id", projectId);
        let response = await fetch(`/api/session?${query}`, {
          cache: "no-store",
          signal: controller.signal,
        });
        let body: unknown = await response.json().catch(() => null);
        if (response.status === 409 && errorCode(body) === "WORKSPACE_SELECTION_INVALID" && query.size > 0) {
          localStorage.removeItem(ORGANIZATION_KEY);
          localStorage.removeItem(PROJECT_KEY);
          response = await fetch("/api/session", { cache: "no-store", signal: controller.signal });
          body = await response.json().catch(() => null);
        }
        if (response.status === 409 && errorCode(body) === "WORKSPACE_SETUP_REQUIRED") {
          setWorkspaceSetupRequired(true);
          setStatus("需要创建企业工作区");
          return;
        }
        if (response.status === 401) {
          setAuthenticationRequired(true);
          setWorkspaceReady(false);
          setModelLoadState("waiting");
          setStatus("请登录企业账号");
          return;
        }
        if (!response.ok || !applyWorkspace(body)) {
          throw new Error(errorText(body, "无法加载企业工作区"));
        }
      } catch (error) {
        if (!controller.signal.aborted) {
          setStatus(error instanceof Error ? error.message : "无法加载企业工作区");
        }
      }
    })();
    return () => controller.abort();
  }, []);

  useEffect(() => {
    if (!workspaceReady || !workspace) return;
    const controller = new AbortController();
    setModelLoadState("loading");
    void (async () => {
      try {
        const query = new URLSearchParams({
          organization_id: workspace.organization.tenantId,
          project_id: workspace.project.projectId,
        });
        const response = await fetch(`/api/info?${query}`, { cache: "no-store", signal: controller.signal });
        if (response.status === 401) {
          setAuthenticationRequired(true);
          setWorkspaceReady(false);
          setModelLoadState("waiting");
          setStatus("登录已失效，请重新登录");
          return;
        }
        if (!response.ok) throw new Error("metadata unavailable");
        const info = (await response.json()) as ServiceInfo;
        const available = Array.isArray(info.models) ? info.models : [];
        const stored = localStorage.getItem(MODEL_KEY);
        const selected = stored && available.includes(stored) ? stored : info.default_model;
        const availableProfiles = Array.isArray(info.capability_profiles)
          ? info.capability_profiles
          : [];
        const storedProfile = localStorage.getItem(PROFILE_KEY);
        const selectedProfile = availableProfiles.find(
          (profile) => `${profile.id}@${profile.version}` === storedProfile,
        );
        setModels(available);
        setModel(selected);
        setModelLoadState("ready");
        setProfiles(availableProfiles);
        setProfileReference(
          selectedProfile ? `${selectedProfile.id}@${selectedProfile.version}` : "",
        );
        localStorage.setItem(MODEL_KEY, selected);
        if (!selectedProfile) localStorage.removeItem(PROFILE_KEY);
        setStatus(selectedProfile ? "就绪" : "请选择能力范围");
      } catch {
        if (!controller.signal.aborted) {
          setModelLoadState("error");
          setStatus("模型元数据暂不可用");
        }
      }
    })();
    return () => controller.abort();
  }, [workspaceReady, workspace]);

  useEffect(() => {
    if (!workspaceReady || !workspace || !threadId) return;
    if (skipHistoryThread.current === threadId) {
      skipHistoryThread.current = null;
      return;
    }
    const controller = new AbortController();
    setHistoryLoading(true);
    void (async () => {
      try {
        const response = await fetch("/api/history", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            organization_id: workspace.organization.tenantId,
            project_id: workspace.project.projectId,
            thread_id: threadId,
          }),
          signal: controller.signal,
        });
        if (!response.ok) {
          const detail: unknown = await response.json().catch(() => null);
          throw new Error(errorText(detail, "无法加载历史消息"));
        }
        const history = (await response.json()) as { messages?: unknown[] };
        const parsed = (history.messages ?? [])
          .map(parseChatMessage)
          .filter((item): item is ChatMessage => item !== null);
        setMessages(parsed.map((item) => displayMessage(item)));
        setStatus("历史会话已加载");
      } catch (error) {
        if (!controller.signal.aborted) {
          setMessages([]);
          setStatus(error instanceof Error ? error.message : "无法加载历史消息");
        }
      } finally {
        if (!controller.signal.aborted) setHistoryLoading(false);
      }
    })();
    return () => controller.abort();
  }, [workspaceReady, workspace, threadId]);

  useEffect(() => {
    if (!workspaceReady || !workspace) return;
    const controller = new AbortController();
    setConversationsLoading(true);
    void (async () => {
      try {
        const query = new URLSearchParams({
          organization_id: workspace.organization.tenantId,
          project_id: workspace.project.projectId,
        });
        const response = await fetch(`/api/conversations?${query}`, {
          cache: "no-store",
          signal: controller.signal,
        });
        if (!response.ok) throw new Error("conversation list unavailable");
        const parsed = parseConversationList(await response.json());
        if (!parsed) throw new Error("invalid conversation list");
        setConversations(parsed.conversations);
        const selected = parsed.conversations.find((item) => item.threadId === threadId);
        if (selected && latestRun?.runId !== selected.latestRunId) {
          void loadDelivery(selected.latestRunId);
        }
        if (selected?.pendingInteraction) {
          setInteraction({
            request: selected.pendingInteraction,
            runId: selected.latestRunId,
            lastEventId: selected.lastEventId,
            idempotencyKey: newId(),
            status: "waiting",
          });
          setRunState("waiting_for_input");
        }
      } catch {
        if (!controller.signal.aborted) setConversations([]);
      } finally {
        if (!controller.signal.aborted) setConversationsLoading(false);
      }
    })();
    return () => controller.abort();
  }, [workspaceReady, workspace, threadId, conversationRefresh]);

  useEffect(() => {
    messageEnd.current?.scrollIntoView({ behavior: busy ? "smooth" : "auto" });
  }, [busy, liveMessage, messages]);

  async function loadDelivery(runId: string): Promise<void> {
    if (!workspace) return;
    setArtifactLoading(true);
    setArtifactError("");
    const query = `organization_id=${encodeURIComponent(workspace.organization.tenantId)}`;
    try {
      const [runResponse, artifactResponse] = await Promise.all([
        fetch(`/api/runs/${encodeURIComponent(runId)}?${query}`, { cache: "no-store" }),
        fetch(`/api/runs/${encodeURIComponent(runId)}/artifacts?${query}`, { cache: "no-store" }),
      ]);
      if (!runResponse.ok || !artifactResponse.ok) throw new Error("delivery metadata unavailable");
      const run = parseRunSummary(await runResponse.json());
      const manifest = parseArtifactList(await artifactResponse.json());
      if (!run || !manifest || run.runId !== runId || manifest.runId !== runId) {
        throw new Error("invalid delivery metadata");
      }
      setLatestRun(run);
      setArtifacts(manifest.artifacts);
    } catch {
      setLatestRun(null);
      setArtifacts([]);
      setArtifactError("交付清单尚未确认");
    } finally {
      setArtifactLoading(false);
    }
  }

  function handleRunEvent(event: RunEvent): HumanInputRequest | null {
    const humanInput = parseHumanInputRequest(event);
    if (humanInput) {
      commitLive();
      setInteraction((current) => current?.request.interactionId === humanInput.interactionId
        ? { ...current, request: humanInput, runId: event.runId, lastEventId: event.eventId }
        : {
            request: humanInput,
            runId: event.runId,
            lastEventId: event.eventId,
            idempotencyKey: newId(),
            status: "waiting",
          });
      setRunState("waiting_for_input");
      setStatus(`等待你回复 ${humanInput.requestedBy} 的澄清问题`);
      return humanInput;
    }
    const content = typeof event.data.content === "string" ? event.data.content : "";
    if (event.type === "token") {
      updateLive((message) => ({ ...message, content: message.content + content }));
      return null;
    }
    if (event.type === "reasoning") {
      updateLive((message) => ({ ...message, reasoning: `${message.reasoning ?? ""}${content}` }));
      return null;
    }
    if (event.type === "message") {
      const parsed = parseChatMessage(event.data.message);
      if (!parsed) return null;
      const structured = displayMessage(parsed);
      if (structured.type === "ai") {
        const live = liveRef.current;
        const completed = {
          ...structured,
          content: structured.content || live?.content || "",
          reasoning: structured.reasoning || live?.reasoning || "",
        };
        setMessages((items) => [...items, completed]);
        setLive(null);
      } else if (structured.type !== "human") {
        setMessages((items) => {
          const recordId = structured.custom_data?.record_id;
          if (
            typeof recordId === "string" &&
            items.some((item) => item.custom_data?.record_id === recordId)
          ) {
            return items;
          }
          return [...items, structured];
        });
      }
      return null;
    }
    if (event.type === "error") {
      const detail = typeof event.data.error === "string" ? event.data.error : "智能体报告了执行错误";
      const code = typeof event.data.code === "string" ? event.data.code : "";
      updateLive((message) => ({
        ...message,
        content: message.content || `执行风险：${detail}`,
      }));
      setStatus(code ? `执行风险 · ${code}` : "智能体报告执行风险");
      return null;
    }
    if (event.type === "artifact_manifest") {
      void loadDelivery(event.runId);
      return null;
    }
    if (isTerminalRunEvent(event.type)) {
      setRunState(event.type);
      const detail = typeof event.data.error === "string" ? event.data.error : "";
      if (event.type === "completed") {
        setStatus("任务完成");
      } else {
        const label = event.type === "cancelled" ? "任务已取消" : event.type === "timed_out" ? "任务超时" : "任务失败";
        updateLive((message) => ({
          ...message,
          content: message.content || `${label}${detail ? `：${detail}` : ""}`,
          pending: false,
        }));
        setStatus(label);
      }
      void loadDelivery(event.runId);
      setConversationRefresh((value) => value + 1);
    }
    return null;
  }

  async function submit(event?: FormEvent) {
    event?.preventDefault();
    const message = draft.trim();
    const requestedProfile = requestedCapabilityProfile(message);
    const effectiveProfileReference = requestedProfile ?? profileReference;
    const selectedProfile = profiles.find(
      (profile) => `${profile.id}@${profile.version}` === effectiveProfileReference,
    );
    if (requestedProfile && !selectedProfile) {
      setStatus(`\u9700\u6c42\u4e2d\u6307\u5b9a\u7684\u80fd\u529b\u8303\u56f4\u4e0d\u53ef\u7528\uff1a${requestedProfile}`);
      return;
    }
    if (!message || busy || interaction?.status === "waiting" || interaction?.status === "error" || !workspaceReady || !workspace || !model || !selectedProfile) return;

    const idempotencyKey = newId();
    const activeRunProfileReference = latestRun?.capabilityProfile
      ? `${latestRun.capabilityProfile.id}@${latestRun.capabilityProfile.version}`
      : latestRun
        ? null
        : undefined;
    const newProject = requiresNewRun(
      message,
      effectiveProfileReference,
      activeRunProfileReference,
    );
    const submissionThreadId = newProject ? newId() : threadId;
    const baseRunId = newProject ? null : latestRun?.runId ?? null;
    if (newProject) {
      skipHistoryThread.current = submissionThreadId;
      localStorage.setItem(THREAD_KEY, submissionThreadId);
      selectedThread.current = submissionThreadId;
      setThreadId(submissionThreadId);
      setLatestRun(null);
    }
    if (requestedProfile && requestedProfile !== profileReference) {
      localStorage.setItem(PROFILE_KEY, requestedProfile);
      setProfileReference(requestedProfile);
    }
    const controller = new AbortController();
    activeRun.current = { idempotencyKey, runId: null, controller, lastEventId: 0 };
    setMessages((current) => [
      ...(newProject ? [] : current),
      displayMessage(makeMessage("human", message)),
    ]);
    setInteraction(null);
    setLive(displayMessage(makeMessage("ai", ""), true));
    setDraft("");
    setStatus("智能体团队正在工作");
    setRunState("running");
    setLatestRun(null);
    setArtifacts([]);
    setArtifactError("");
    setBusy(true);

    let lastEventId = 0;
    let reconnectDelay = 600;
    let terminal: TerminalRunEvent | null = null;
    let runId: string | null = null;
    let replayRejected = false;
    let awaitingInput = false;

    try {
      for (let attempt = 0; attempt <= MAX_RECONNECTS && terminal === null; attempt += 1) {
        try {
          const response = await fetch("/api/chat", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              message,
              model,
              organization_id: workspace.organization.tenantId,
              project_id: workspace.project.projectId,
              thread_id: submissionThreadId,
              request_id: idempotencyKey,
              base_run_id: baseRunId,
              last_event_id: lastEventId,
              capability_profile: {
                id: selectedProfile.id,
                version: selectedProfile.version,
              },
              team_members: team.roles.map(({ role_id, name, responsibility }) => ({
                role_id,
                name,
                responsibility,
              })),
            }),
            signal: controller.signal,
          });
          if (!response.ok || !response.body) {
            const detail: unknown = await response.json().catch(() => null);
            const reason = errorText(detail, `请求失败（HTTP ${response.status}）`);
            if (errorCode(detail) === "RUNTIME_START_FAILED") {
              updateLive((item) => ({ ...item, content: item.content || `任务启动失败：${reason}`, pending: false }));
              setStatus("任务启动失败");
              setRunState("failed");
              terminal = "failed";
              break;
            }
            if (response.status < 500) throw new NonRetryableRequestError(reason);
            throw new Error(reason);
          }
          const responseRunId = safeIdentity(response.headers.get("x-run-id"));
          if (!responseRunId || (runId !== null && responseRunId !== runId)) {
            throw new Error("运行标识缺失或发生变化");
          }
          runId = responseRunId;
          if (activeRun.current?.idempotencyKey === idempotencyKey) {
            activeRun.current.runId = runId;
          }

          await readSseStream(response.body, (sseEvent) => {
            if (sseEvent.retry !== undefined) reconnectDelay = Math.max(250, sseEvent.retry);
            try {
              const runEvent = parseRunEvent(JSON.parse(sseEvent.data));
              if (!runEvent || runEvent.runId !== runId || runEvent.eventId <= lastEventId) return;
              lastEventId = runEvent.eventId;
              if (activeRun.current?.idempotencyKey === idempotencyKey) {
                activeRun.current.lastEventId = lastEventId;
              }
              if (selectedThread.current !== submissionThreadId) return;
              if (handleRunEvent(runEvent)) {
                awaitingInput = true;
                throw new HumanInputRequestedError();
              }
              if (isTerminalReplayFailure(runEvent)) replayRejected = true;
              if (isTerminalRunEvent(runEvent.type)) terminal = runEvent.type;
            } catch (error) {
              if (error instanceof HumanInputRequestedError) throw error;
              return;
            }
          });

          if (replayRejected) {
            throw new NonRetryableRequestError("事件回放窗口已过期，请重新加载会话历史");
          }
          if (terminal === null) throw new Error("响应流在终态事件前结束");
        } catch (error) {
          if (error instanceof HumanInputRequestedError) {
            controller.abort();
            break;
          }
          if (controller.signal.aborted) throw error;
          if (error instanceof NonRetryableRequestError) throw error;
          if (attempt === MAX_RECONNECTS) throw error;
          setStatus(`连接中断，正在恢复（${attempt + 1}/${MAX_RECONNECTS}）`);
          await waitForRetry(Math.min(reconnectDelay * 2 ** attempt, 5_000), controller.signal);
        }
      }
      if (!awaitingInput) commitLive();
    } catch (error) {
      if (selectedThread.current !== submissionThreadId) {
        // The user opened another conversation. The backend run continues;
        // only this page's event subscription was detached.
      } else if (controller.signal.aborted) {
        updateLive((item) => ({ ...item, content: item.content || "已停止接收运行事件，正在确认后端取消状态。", pending: false }));
        commitLive();
        setStatus("已停止接收，正在确认取消状态");
        setRunState("disconnected");
      } else if (error instanceof NonRetryableRequestError) {
        updateLive((item) => ({ ...item, content: item.content || `请求未被接受：${error.message}`, pending: false }));
        commitLive();
        setStatus("请求未被接受");
        setRunState("rejected");
      } else {
        const reason = error instanceof Error ? error.message : "连接失败";
        updateLive((item) => ({ ...item, content: item.content || `连接中断：${reason}`, pending: false }));
        commitLive();
        setStatus("连接中断");
        setRunState("disconnected");
      }
    } finally {
      if (activeRun.current?.idempotencyKey === idempotencyKey) activeRun.current = null;
      if (selectedThread.current === submissionThreadId) setBusy(false);
    }
  }

  async function respondToInteraction(answer: string, displayAnswer: string = answer): Promise<void> {
    if (!interaction || !workspace || interaction.status === "submitting" || interaction.status === "submitted") return;
    const current = interaction;
    const interactionThreadId = threadId;
    const controller = new AbortController();
    activeRun.current = {
      idempotencyKey: current.idempotencyKey,
      runId: current.runId,
      controller,
      lastEventId: current.lastEventId,
    };
    setInteraction({ ...current, status: "submitting", answer, error: undefined });
    setStatus("正在提交澄清信息…");

    try {
      const response = await fetch(
        `/api/runs/${encodeURIComponent(current.runId)}/interactions/${encodeURIComponent(current.request.interactionId)}/respond`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            organization_id: workspace.organization.tenantId,
            request_id: current.idempotencyKey,
            answer,
            state_version: current.request.stateVersion,
          }),
          signal: controller.signal,
        },
      );
      if (!response.ok) {
        const detail: unknown = await response.json().catch(() => null);
        throw new NonRetryableRequestError(errorText(detail, `提交失败（HTTP ${response.status}）`));
      }

      setMessages((items) => [...items, displayMessage(makeMessage("human", displayAnswer))]);
      setInteraction({ ...current, status: "submitted", answer, error: undefined });
      setLive(displayMessage(makeMessage("ai", ""), true));
      setRunState("running");
      setBusy(true);
      setStatus("已提交，智能体团队继续工作");

      let lastEventId = current.lastEventId;
      let reconnectDelay = 600;
      let terminal: TerminalRunEvent | null = null;
      let replayRejected = false;
      let awaitingInput = false;
      for (let attempt = 0; attempt <= MAX_RECONNECTS && terminal === null; attempt += 1) {
        try {
          const events = await fetch(
            `/api/runs/${encodeURIComponent(current.runId)}/events?organization_id=${encodeURIComponent(workspace.organization.tenantId)}`,
            {
              headers: { Accept: "text/event-stream", "Last-Event-ID": String(lastEventId) },
              signal: controller.signal,
            },
          );
          if (!events.ok || !events.body) {
            const detail: unknown = await events.json().catch(() => null);
            const reason = errorText(detail, `事件订阅失败（HTTP ${events.status}）`);
            if (events.status < 500) throw new NonRetryableRequestError(reason);
            throw new Error(reason);
          }
          await readSseStream(events.body, (sseEvent) => {
            if (sseEvent.retry !== undefined) reconnectDelay = Math.max(250, sseEvent.retry);
            try {
              const runEvent = parseRunEvent(JSON.parse(sseEvent.data));
              if (!runEvent || runEvent.runId !== current.runId || runEvent.eventId <= lastEventId) return;
              lastEventId = runEvent.eventId;
              if (activeRun.current?.idempotencyKey === current.idempotencyKey) {
                activeRun.current.lastEventId = lastEventId;
              }
              if (selectedThread.current !== interactionThreadId) return;
              if (handleRunEvent(runEvent)) {
                awaitingInput = true;
                throw new HumanInputRequestedError();
              }
              if (isTerminalReplayFailure(runEvent)) replayRejected = true;
              if (isTerminalRunEvent(runEvent.type)) terminal = runEvent.type;
            } catch (error) {
              if (error instanceof HumanInputRequestedError) throw error;
            }
          });
          if (replayRejected) throw new NonRetryableRequestError("事件回放窗口已过期，请重新加载会话历史");
          if (terminal === null) throw new Error("响应流在终态事件前结束");
        } catch (error) {
          if (error instanceof HumanInputRequestedError) {
            controller.abort();
            break;
          }
          if (controller.signal.aborted || error instanceof NonRetryableRequestError || attempt === MAX_RECONNECTS) throw error;
          setStatus(`连接中断，正在恢复（${attempt + 1}/${MAX_RECONNECTS}）`);
          await waitForRetry(Math.min(reconnectDelay * 2 ** attempt, 5_000), controller.signal);
        }
      }
      if (!awaitingInput) commitLive();
    } catch (error) {
      if (!controller.signal.aborted && selectedThread.current === interactionThreadId) {
        const reason = error instanceof Error ? error.message : "提交澄清信息失败";
        setInteraction({ ...current, status: "error", answer, error: reason });
        setStatus(reason);
        setRunState("disconnected");
      }
    } finally {
      if (activeRun.current?.idempotencyKey === current.idempotencyKey) activeRun.current = null;
      if (selectedThread.current === interactionThreadId) setBusy(false);
    }
  }

  async function cancelRun() {
    const run = activeRun.current;
    if (!run) return;
    setStatus("正在取消任务…");
    const cancellation = run.runId && workspace
      ? fetch(
          `/api/runs/${encodeURIComponent(run.runId)}?organization_id=${encodeURIComponent(workspace.organization.tenantId)}`,
          { method: "POST" },
        )
      : null;
    run.controller.abort();
    if (!cancellation) {
      setStatus("已停止等待；任务启动状态尚未确认");
      return;
    }
    try {
      const response = await cancellation;
      const result = response.ok
        ? await response.json().catch(() => null) as { state?: unknown } | null
        : null;
      if (result?.state === "CANCELLED") {
        setRunState("cancelled");
        setStatus("任务已取消");
      } else {
        setStatus(response.ok ? "取消请求已提交，等待状态确认" : "本地流已停止，后端取消请求未确认");
      }
    } catch {
      setStatus("本地流已停止，后端取消请求未确认");
    }
  }

  async function createWorkspace() {
    if (workspaceCreating) return;
    setWorkspaceCreating(true);
    setStatus("正在创建企业工作区…");
    try {
      const response = await fetch("/api/session", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ workspace_name: team.name }),
      });
      const body: unknown = await response.json().catch(() => null);
      if (!response.ok || !applyWorkspace(body)) {
        throw new Error(errorText(body, "无法创建企业工作区"));
      }
      setStatus("工作区已创建");
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "无法创建企业工作区");
    } finally {
      setWorkspaceCreating(false);
    }
  }

  function createThread() {
    activeRun.current?.controller.abort();
    activeRun.current = null;
    const nextThread = newId();
    localStorage.setItem(THREAD_KEY, nextThread);
    selectedThread.current = nextThread;
    const url = new URL(window.location.href);
    url.searchParams.set("thread_id", nextThread);
    window.history.replaceState(null, "", url);
    setThreadId(nextThread);
    setMessages([]);
    setLive(null);
    setStatus("新工程会话已创建");
    setRunState("idle");
    setLatestRun(null);
    setArtifacts([]);
    setArtifactError("");
    setInteraction(null);
    setBusy(false);
  }

  async function removeConversation(conversation: ConversationSummary): Promise<void> {
    if (!workspace || deletingConversation || conversation.state === "QUEUED" ||
        conversation.state === "RUNNING" || conversation.state === "WAITING_FOR_INPUT") return;
    const confirmed = window.confirm(
      `删除“${conversation.title}”？\n\n该会话会从你的历史列表移除；工程审计记录和产物仍按组织保留策略保存。`,
    );
    if (!confirmed) return;
    setDeletingConversation(conversation.threadId);
    try {
      const query = new URLSearchParams({
        organization_id: workspace.organization.tenantId,
        project_id: workspace.project.projectId,
      });
      const response = await fetch(
        `/api/conversations/${encodeURIComponent(conversation.threadId)}?${query}`,
        { method: "DELETE" },
      );
      if (!response.ok) {
        const detail: unknown = await response.json().catch(() => null);
        throw new Error(errorText(detail, "无法删除历史会话"));
      }
      setConversations((items) => items.filter((item) => item.threadId !== conversation.threadId));
      if (conversation.threadId === threadId) createThread();
      setStatus("历史会话已删除");
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "无法删除历史会话");
    } finally {
      setDeletingConversation(null);
    }
  }

  function handleComposerKey(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      void submit();
    }
  }

  const allVisible = liveMessage ? [...messages, liveMessage] : messages;
  const visibleMessages = allVisible.filter((message) => channelIncludes(message, channel));
  const eventMessages = messages.filter((message) => message.type === "custom");
  const recentEvents = eventMessages.slice(-6).reverse();
  const reviewerCompleted = eventMessages.some((message) => {
    const phase = String(message.custom_data.phase ?? "").toLowerCase();
    const eventStatus = String(message.custom_data.status ?? "").toLowerCase();
    return phase.includes("review") && (eventStatus === "completed" || eventStatus === "ok");
  });
  const kicadArtifacts = artifacts.filter((artifact) =>
    artifact.kind.includes("kicad") || /\.kicad_(?:pro|sch|pcb)$/i.test(artifact.fileName),
  );
  const manufacturingArtifacts = artifacts.filter((artifact) =>
    /(?:bom|cpl|gerber|drill|manufactur)/i.test(artifact.kind) ||
    /\.(?:csv|pos|gbr|drl|zip)$/i.test(artifact.fileName),
  );
  const deliveryState = artifactLoading
    ? "正在验证产物清单"
    : artifactError
      ? artifactError
      : latestRun?.deliveryStatus ?? (busy ? "执行中" : "等待任务");
  const modelPlaceholder = authenticationRequired
    ? "登录后读取模型"
    : modelLoadState === "loading"
      ? "正在读取模型…"
      : modelLoadState === "error"
        ? "模型暂不可用"
        : modelLoadState === "ready"
          ? "没有可用模型"
          : "等待工作区";

  return (
    <main className="workbench-page">
      <header className="product-header workbench-topbar">
        <span className="window-dots" aria-hidden="true"><i /><i /><i /></span>
        <div className="header-center"><span className="workbench-brand">CF</span> CircuitFoundry · {team.name}</div>
        <AccountMenu />
      </header>

      <div className="workbench-shell">
        <aside className="team-sidebar">
          <div className="sidebar-title">
            <strong>{team.name}</strong>
            <button className="galaxy-tooltip" data-tooltip="调整团队角色" type="button" onClick={onEditTeam}>编辑</button>
          </div>

          <nav className="channel-list" aria-label="工程频道">
            <small>工程频道</small>
            <button className={channel === "design" ? "active" : ""} onClick={() => setChannel("design")} type="button"><span>#</span> 设计执行</button>
            <button className={channel === "evidence" ? "active" : ""} onClick={() => setChannel("evidence")} type="button"><span>#</span> 资料与证据</button>
            <button className={channel === "review" ? "active" : ""} onClick={() => setChannel("review")} type="button"><span>#</span> 审查问题</button>
          </nav>

          <section className="conversation-history" aria-label="历史工程会话">
            <div>
              <small>历史会话</small>
              <span>{conversations.length}</span>
            </div>
            {conversationsLoading ? (
              <p>正在加载…</p>
            ) : conversations.length === 0 ? (
              <p>完成首个任务后，会话会保存在这里。</p>
            ) : (
              <div className="conversation-history-list">
                {conversations.map((conversation) => {
                  const activeConversation = conversation.state === "QUEUED" ||
                    conversation.state === "RUNNING" || conversation.state === "WAITING_FOR_INPUT";
                  return (
                    <div className="conversation-history-item" key={conversation.threadId}>
                      <a
                        className={conversation.threadId === threadId ? "active" : ""}
                        href={`/?thread_id=${encodeURIComponent(conversation.threadId)}#workspace`}
                        title={conversation.title}
                      >
                        <strong>{conversation.title}</strong>
                        <span>
                          Revision {conversation.latestRevisionNumber} · {new Date(conversation.updatedAt).toLocaleDateString("zh-CN")}
                        </span>
                      </a>
                      <button
                        type="button"
                        className="conversation-delete"
                        aria-label={`删除会话：${conversation.title}`}
                        title={activeConversation ? "运行中的会话需先取消或等待结束" : "删除会话"}
                        disabled={activeConversation || deletingConversation !== null}
                        onClick={() => void removeConversation(conversation)}
                      >
                        删
                      </button>
                    </div>
                  );
                })}
              </div>
            )}
          </section>

          <section className="team-roster">
            <small>智能体团队</small>
            {team.roles.map((role) => {
              const currentStatus = roleStatus(role, eventMessages, busy);
              return (
                <div className="roster-row" key={role.role_id}>
                  <span>{role.badge}</span>
                  <strong title={role.responsibility}>{role.name}</strong>
                  <em className={currentStatus === "执行中" || currentStatus === "正在统筹" ? "live" : currentStatus === "有问题" ? "issue" : ""}>{currentStatus}</em>
                </div>
              );
            })}
          </section>

          <div className="sidebar-controls">
            <button className="new-conversation" type="button" onClick={createThread} disabled={!workspaceReady}><span>＋</span> 新建工程会话</button>
            <div className="model-control">
              <div><label htmlFor="model-select">当前模型</label><span>LIVE</span></div>
              <div className="select-shell">
                <i aria-hidden="true">M</i>
                <select
                  id="model-select"
                  value={model}
                  onChange={(event) => {
                    setModel(event.target.value);
                    localStorage.setItem(MODEL_KEY, event.target.value);
                  }}
                  disabled={busy || models.length === 0}
                >
                  {models.length === 0 && <option value="">{modelPlaceholder}</option>}
                  {models.map((item) => <option key={item} value={item}>{item}</option>)}
                </select>
              </div>
            </div>
            <div className="model-control">
              <div><label htmlFor="profile-select">能力范围</label><span>V1</span></div>
              <div className="select-shell">
                <i aria-hidden="true">P</i>
                <select
                  id="profile-select"
                  value={profileReference}
                  onChange={(event) => {
                    setProfileReference(event.target.value);
                    if (event.target.value) {
                      localStorage.setItem(PROFILE_KEY, event.target.value);
                      setStatus("就绪");
                    } else {
                      localStorage.removeItem(PROFILE_KEY);
                      setStatus("请选择能力范围");
                    }
                  }}
                  disabled={busy || profiles.length === 0}
                >
                  <option value="">请选择版本化场景…</option>
                  {profiles.map((profile) => (
                    <option
                      key={`${profile.id}@${profile.version}`}
                      value={`${profile.id}@${profile.version}`}
                    >
                      {profile.title} · {profile.version}
                    </option>
                  ))}
                </select>
              </div>
            </div>
            <span title={workspace ? `${workspace.organization.tenantId} / ${workspace.project.projectId}` : undefined}>
              {workspace ? `${workspace.organization.name} · ${workspace.project.name}` : "工作区未就绪"} · {AGENT_ID}
            </span>
          </div>
        </aside>

        <section className="conversation-column">
          <header className="conversation-header">
            <div><strong># {channel === "design" ? "设计执行" : channel === "evidence" ? "资料与证据" : "审查问题"}</strong><span>{team.roles.length} 位智能体 · Supervisor 在线</span></div>
            <span className={`live-state ${busy ? "running" : ""}`}><i /> {status}</span>
          </header>

          <div className="conversation-scroll" aria-live="polite" aria-busy={busy}>
            {authenticationRequired ? (
              <AuthenticationRequiredState loginHref={loginHref} />
            ) : workspaceSetupRequired ? (
              <WorkspaceSetupState name={team.name} creating={workspaceCreating} onCreate={() => void createWorkspace()} />
            ) : historyLoading ? (
              <div className="empty-chat">正在加载工程上下文…</div>
            ) : visibleMessages.length === 0 ? (
              <WelcomeState setDraft={setDraft} />
            ) : (
              visibleMessages.map((message) => <MessageCard key={message.clientId} message={message} />)
            )}
            {channel === "design" && interaction && (
              <HumanInputCard key={interaction.request.interactionId} interaction={interaction} onRespond={respondToInteraction} />
            )}
            <div ref={messageEnd} />
          </div>

          <form className="chat-composer" onSubmit={submit}>
            <span className="composer-glow" aria-hidden="true" />
            <textarea
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
              onKeyDown={handleComposerKey}
              placeholder="描述需求，或在任务进行中补充约束与反馈…"
              rows={2}
              maxLength={100_000}
              disabled={!workspaceReady || interaction?.status === "waiting" || interaction?.status === "error"}
              aria-label="KiCad 硬件设计需求"
            />
            <div className="composer-footer">
              <span><kbd>Enter</kbd> 发送 · <kbd>Shift</kbd> + <kbd>Enter</kbd> 换行 · {profileReference || "未选择能力范围"}</span>
              {busy ? (
                <button className="stop-button" type="button" onClick={() => void cancelRun()}>停止</button>
              ) : (
                <button className="send-button galaxy-tooltip" data-tooltip="发送给 Supervisor" type="submit" disabled={!draft.trim() || !workspaceReady || !model || !profileReference || interaction?.status === "waiting" || interaction?.status === "error"} aria-label="发送需求"><span>→</span></button>
              )}
            </div>
          </form>
        </section>

        <aside className="delivery-sidebar">
          <small className="panel-label">本次交付</small>
          {latestRun && <p className="revision-label">Revision {latestRun.revisionNumber} · {deliveryState}</p>}
          <DeliveryCard icon="板" title="KiCad 项目" state={kicadArtifacts.length ? `${kicadArtifacts.length} 个已验证文件` : deliveryState} ready={kicadArtifacts.length > 0} />
          <DeliveryCard icon="料" title="BOM / CPL / Gerber" state={manufacturingArtifacts.length ? `${manufacturingArtifacts.length} 个已验证文件` : deliveryState} ready={manufacturingArtifacts.length > 0} />
          <DeliveryCard icon="审" title="Reviewer 执行" state={reviewerCompleted ? "已完成" : busy ? "等待 Reviewer" : "待执行"} ready={reviewerCompleted} />

          {workspace && artifacts.length > 0 && (
            <section className="artifact-list" aria-label="可下载产物">
              {artifacts.map((artifact) => (
                <a
                  key={artifact.artifactId}
                  href={`/api/artifacts/${encodeURIComponent(artifact.artifactId)}/download?organization_id=${encodeURIComponent(workspace.organization.tenantId)}`}
                >
                  <span>{artifact.fileName}</span>
                  <small>{formatBytes(artifact.sizeBytes)} · 下载</small>
                </a>
              ))}
            </section>
          )}

          <section className="activity-panel">
            <small className="panel-label">实时事件</small>
            {recentEvents.length === 0 ? <p>任务开始后，这里显示各智能体的节点状态。</p> : recentEvents.map((message) => (
              <div key={message.clientId}>
                <i />
                <span><strong>{String(message.custom_data.phase ?? message.custom_data.kind ?? "workflow")}</strong><small>{String(message.custom_data.status ?? "event")}</small></span>
              </div>
            ))}
          </section>
          <p className="audit-note">♢ 消息、工具调用、显式模型推理与工作流事件均保留在当前会话中</p>
        </aside>
      </div>
    </main>
  );
}

function WorkspaceSetupState({ name, creating, onCreate }: { name: string; creating: boolean; onCreate: () => void }) {
  return (
    <div className="welcome-chat">
      <span className="welcome-badge">ID</span>
      <h1>创建企业工作区</h1>
      <p>当前 OIDC 身份还没有可用的组织或项目。确认后将以“{name}”显式创建缺失的工作区资源。</p>
      <div><button type="button" onClick={onCreate} disabled={creating}>{creating ? "正在创建…" : "创建并进入工作区"}</button></div>
    </div>
  );
}

function AuthenticationRequiredState({ loginHref }: { loginHref: string }) {
  return (
    <div className="welcome-chat authentication-required">
      <span className="welcome-badge">ID</span>
      <h1>登录后进入工程团队</h1>
      <p>使用企业账号完成身份验证后，系统会返回当前工作区并加载你有权访问的组织、项目和模型。</p>
      <div><a className="login-button" href={loginHref}>使用企业账号登录</a></div>
    </div>
  );
}

function WelcomeState({ setDraft }: { setDraft: (value: string) => void }) {
  return (
    <div className="welcome-chat">
      <span className="welcome-badge">RN</span>
      <h1>把硬件目标交给你的 KiCad 团队</h1>
      <p>用自然语言描述板卡用途、接口、电源、尺寸或制造要求。Supervisor 会自动判断意图并组织团队推进。</p>
      <div>
        <button type="button" onClick={() => setDraft("设计一块 24 V 供电、带双路模拟采集与 CAN 通信的工业控制板，并输出完整 KiCad 工程。")}>工业控制板</button>
        <button type="button" onClick={() => setDraft("请审查我已有的 KiCad 工程，列出 ERC、DRC、连接性与制造风险。")}>审查现有工程</button>
      </div>
    </div>
  );
}

function HumanInputCard({
  interaction,
  onRespond,
}: {
  interaction: InteractionState;
  onRespond: (answer: string, displayAnswer?: string) => Promise<void>;
}) {
  const [answer, setAnswer] = useState(interaction.answer ?? "");
  const [decisionAnswers, setDecisionAnswers] = useState<Record<string, { key: string; text: string }>>({});
  const locked = interaction.status === "submitting" || interaction.status === "submitted";
  const hasDecisionForm = interaction.request.questions.length > 0;
  const decisionComplete = hasDecisionForm && interaction.request.questions.every((question) => {
    const selected = decisionAnswers[question.slot];
    if (!selected) return false;
    const option = question.options.find((candidate) => candidate.key === selected.key);
    return Boolean(option) && (!option?.freeText || selected.text.trim().length > 0);
  });
  const canSubmit = !locked && (hasDecisionForm ? decisionComplete : answer.trim().length > 0);

  function selectDecision(slot: string, key: string): void {
    setDecisionAnswers((current) => ({
      ...current,
      [slot]: { key, text: current[slot]?.key === key ? current[slot].text : "" },
    }));
  }

  function submitAnswer(): void {
    if (!canSubmit) return;
    if (!hasDecisionForm) {
      void onRespond(answer.trim());
      return;
    }
    const wireAnswer = JSON.stringify({
      schemaVersion: "ratsnest.decision-answer.v1",
      answers: interaction.request.questions.map((question) => ({
        slot: question.slot,
        key: decisionAnswers[question.slot].key,
        text: decisionAnswers[question.slot].text.trim(),
      })),
    });
    const displayAnswer = [
      "已确认以下工程参数：",
      ...interaction.request.questions.map((question) => {
        const selected = decisionAnswers[question.slot];
        const option = question.options.find((candidate) => candidate.key === selected.key)!;
        return `- ${question.question}：${option.freeText ? selected.text.trim() : option.label}`;
      }),
    ].join("\n");
    void onRespond(wireAnswer, displayAnswer);
  }

  return (
    <article className="human-input-card" aria-label="智能体需要你的补充信息">
      <header>
        <span>需要你的确认</span>
        <small>{interaction.request.requestedBy}</small>
      </header>
      <MarkdownContent content={interaction.request.question} />
      {hasDecisionForm && (
        <div className="human-decision-list">
          {interaction.request.questions.map((question, index) => {
            const selected = decisionAnswers[question.slot];
            const selectedOption = question.options.find((option) => option.key === selected?.key);
            return (
              <fieldset className="human-decision-question" key={question.slot} disabled={locked}>
                <legend>{index + 1}. {question.question}</legend>
                {question.citation && <small className="human-decision-citation">依据：{question.citation}</small>}
                <div className="human-decision-options">
                  {question.options.map((option) => (
                    <button
                      key={option.key}
                      type="button"
                      className={selected?.key === option.key ? "selected" : ""}
                      onClick={() => selectDecision(question.slot, option.key)}
                    >
                      <span>{option.key}</span>
                      <strong>{option.label}</strong>
                      {option.key === question.recommendedKey && <em>推荐</em>}
                      {option.basis && <small>{option.basis}</small>}
                    </button>
                  ))}
                </div>
                {selectedOption?.freeText && (
                  <textarea
                    value={selected.text}
                    onChange={(event) => setDecisionAnswers((current) => ({
                      ...current,
                      [question.slot]: { key: selected.key, text: event.target.value },
                    }))}
                    disabled={locked}
                    rows={2}
                    maxLength={2_000}
                    placeholder="请输入该参数的自定义值…"
                    aria-label={`${question.question}的自定义值`}
                  />
                )}
              </fieldset>
            );
          })}
        </div>
      )}
      {!hasDecisionForm && interaction.request.options.length > 0 && (
        <div className="human-input-options">
          {interaction.request.options.map((option) => (
            <button
              key={option}
              type="button"
              className={answer === option ? "selected" : ""}
              disabled={locked}
              onClick={() => setAnswer(option)}
            >
              {option}
            </button>
          ))}
        </div>
      )}
      {!hasDecisionForm && interaction.request.allowFreeText && (
        <textarea
          value={answer}
          onChange={(event) => setAnswer(event.target.value)}
          disabled={locked}
          rows={3}
          maxLength={10_000}
          placeholder="补充约束、选择或说明…"
          aria-label="澄清问题回复"
        />
      )}
      <footer>
        <span>
          {interaction.status === "submitted"
            ? "已提交，任务正在从原检查点继续"
            : interaction.status === "submitting"
              ? "正在提交…"
              : interaction.error ?? "回复只用于继续当前任务，不会新建 Run"}
        </span>
        {interaction.status !== "submitted" && (
          <button type="button" disabled={!canSubmit} onClick={submitAnswer}>
            {interaction.status === "submitting" ? "提交中" : interaction.status === "error" ? "重试" : "确认并继续"}
          </button>
        )}
      </footer>
      <JsonDetails label="查看交互 JSON" value={interaction.request} />
    </article>
  );
}

function MessageCard({ message }: { message: DisplayMessage }) {
  if (message.type === "custom" && message.custom_data.kind === "llm_output") {
    const agent = String(message.custom_data.agent ?? "模型");
    const model = String(message.custom_data.model ?? "unknown");
    const phase = String(message.custom_data.phase ?? "workflow");
    const content = String(message.custom_data.content ?? "");
    const reasoning = String(message.custom_data.reasoning ?? "");
    const metadata = message.custom_data.response_metadata;
    const hasMetadata = Boolean(metadata && typeof metadata === "object" && Object.keys(metadata).length);
    const truncated = message.custom_data.stream_truncated === true || message.custom_data.persisted_truncated === true;
    const transcript = typeof message.custom_data.transcript_ref === "string" ? message.custom_data.transcript_ref : "";
    return (
      <article className="chat-message ai llm-output-message">
        <span className="message-avatar">思</span>
        <div className="message-body">
          <header>
            <strong>{agent} · 完整模型输出</strong>
            <span className="llm-output-meta">{phase} · {model}</span>
          </header>
          {reasoning && (
            <details className="reasoning-block">
              <summary>模型供应商返回的显式推理</summary>
              <MarkdownContent content={reasoning} />
            </details>
          )}
          {content && <MarkdownContent content={content} />}
          {!reasoning && !content && <div className="message-content">该模型调用没有返回可显示文本。</div>}
          {truncated && (
            <small className="output-truncation">
              页面事件已达到安全长度上限{transcript ? `；完整记录：${transcript}` : ""}
            </small>
          )}
          {hasMetadata && (
            <JsonDetails label="查看模型与 Token JSON" value={metadata} />
          )}
        </div>
      </article>
    );
  }

  if (message.type === "custom") {
    const phase = String(message.custom_data.phase ?? message.custom_data.kind ?? "workflow_event");
    const status = String(message.custom_data.status ?? "event");
    const detail = typeof message.custom_data.detail === "string" ? message.custom_data.detail : "";
    return (
      <article className="workflow-event">
        <span className="event-icon">◎</span>
        <div><strong>{phase}</strong>{detail && <p>{detail}</p>}</div>
        <em className={status.toLowerCase()}>{status}</em>
        <JsonDetails label="查看步骤 JSON" value={message.custom_data} />
      </article>
    );
  }

  if (message.type === "tool") {
    return (
      <article className="workflow-event tool-event">
        <span className="event-icon">具</span>
        <div>
          <strong>工具执行结果</strong>
          <p>{message.tool_call_id ? `调用 ${message.tool_call_id} 已返回结构化结果` : "结构化工具结果已返回"}</p>
        </div>
        <em>evidence</em>
        <JsonDetails
          label="查看工具 JSON"
          value={{
            tool_call_id: message.tool_call_id,
            content: parseJsonValue(message.content),
            response_metadata: message.response_metadata,
            custom_data: message.custom_data,
          }}
        />
      </article>
    );
  }

  const label = messageAgent(message);
  const metadata = Object.keys(message.response_metadata ?? {}).length > 0;
  return (
    <article className={`chat-message ${message.type}`}>
      <span className="message-avatar">{message.type === "human" ? "我" : label.slice(0, 1)}</span>
      <div className="message-body">
        <header><strong>{label}</strong>{message.pending && <span className="typing"><i /><i /><i /> 正在输出</span>}</header>
        {message.reasoning && (
          <details className="reasoning-block">
            <summary>模型显式推理过程</summary>
            <MarkdownContent content={message.reasoning} />
          </details>
        )}
        {message.type === "human" ? (
          <div className="message-content plain-text">{message.content}</div>
        ) : message.content ? (
          <MarkdownContent content={message.content} />
        ) : (
          <div className="message-content">{message.pending ? "正在思考并组织团队任务…" : ""}</div>
        )}
        {message.tool_calls.length > 0 && (
          <div className="tool-calls">
            {message.tool_calls.map((tool, index) => (
              <JsonDetails
                key={tool.id ?? `${tool.name}-${index}`}
                label={`查看调用参数 JSON · ${tool.name}`}
                value={tool.args}
              />
            ))}
          </div>
        )}
        {metadata && <JsonDetails label="查看模型响应 JSON" value={message.response_metadata} />}
      </div>
    </article>
  );
}

function JsonDetails({ label, value }: { label: string; value: unknown }) {
  return (
    <details className="json-details">
      <summary>{label}</summary>
      <pre>{JSON.stringify(value, null, 2)}</pre>
    </details>
  );
}

function parseJsonValue(value: string): unknown {
  try {
    return JSON.parse(value) as unknown;
  } catch {
    return value;
  }
}

function DeliveryCard({ icon, title, state, ready }: { icon: string; title: string; state: string; ready: boolean }) {
  return (
    <div className={`delivery-card ${ready ? "ready" : ""}`}>
      <span>{icon}</span><div><strong>{title}</strong><small>{state}</small></div><i className="delivery-indicator" aria-hidden="true" />
    </div>
  );
}

function formatBytes(value: number): string {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KiB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MiB`;
}
