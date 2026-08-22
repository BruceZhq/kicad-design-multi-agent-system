const events = [
  [1, "RUN_STARTED", "Supervisor 固化需求、租户和 Capability Profile 快照"],
  [2, "STATE_DELTA", "Architect 生成设计依据并请求官方器件证据"],
  [3, "TOOL_CALL", "Parts Specialist 校验符号、封装和引脚—焊盘映射"],
  [3, "HUMAN_INPUT_REQUIRED", "发现候选器件不可用，等待批准兼容替代"],
  [4, "WORKFLOW_ATTACHED", "Hardware Engineer 附着 Temporal Workflow"],
  [4, "ARTIFACT_PROGRESS", "生成原理图、PCB、BOM、Gerber 与 Manifest"],
  [5, "REVIEW_COMPLETED", "Reviewer 完成 ERC、DRC、连通性和交付风险审查"],
  [5, "RUN_FINISHED", "Evidence Gate 通过，交付状态 release_ready"],
];

const runButton = document.querySelector("#run-demo");
const resetButton = document.querySelector("#reset-demo");
const approveButton = document.querySelector("#approve");
const eventStream = document.querySelector("#event-stream");
const approval = document.querySelector("#approval");
const runStatus = document.querySelector("#run-status");
const status = document.querySelector(".status");
const gateStatus = document.querySelector("#gate-status");
const agents = [...document.querySelectorAll("#agent-list li")];
const artifacts = [...document.querySelectorAll("#artifact-list li")];

let runId = 0;
let approvalResolver;

const wait = (milliseconds) => new Promise((resolve) => window.setTimeout(resolve, milliseconds));

function timestamp(index) {
  return `00:${String(index * 3).padStart(2, "0")}`;
}

function appendEvent(index, type, message) {
  eventStream.querySelector(".empty")?.remove();
  const row = document.createElement("div");
  row.className = "event";
  row.innerHTML = `<time>${timestamp(index)}</time><span><b>${type}</b><br>${message}</span>`;
  eventStream.append(row);
  eventStream.scrollTop = eventStream.scrollHeight;
}

function activateAgent(step) {
  for (const agent of agents) {
    const agentStep = Number(agent.dataset.step);
    agent.classList.toggle("active", agentStep === step);
    if (agentStep < step) agent.classList.add("done");
  }
}

function reset() {
  runId += 1;
  approvalResolver?.();
  approvalResolver = undefined;
  eventStream.innerHTML = '<p class="empty">点击“运行流程演示”查看模拟事件。</p>';
  approval.hidden = true;
  runStatus.textContent = "等待执行";
  status.className = "status";
  gateStatus.textContent = "尚未执行";
  runButton.disabled = false;
  agents.forEach((agent) => agent.classList.remove("active", "done"));
  artifacts.forEach((artifact) => {
    artifact.classList.remove("ready");
    artifact.querySelector("em").textContent = "等待";
  });
}

async function requestApproval(currentRun) {
  approval.hidden = false;
  runStatus.textContent = "等待人工确认";
  await new Promise((resolve) => { approvalResolver = resolve; });
  if (currentRun !== runId) return false;
  approval.hidden = true;
  approvalResolver = undefined;
  runStatus.textContent = "执行中";
  return true;
}

async function run() {
  reset();
  const currentRun = runId;
  runButton.disabled = true;
  status.className = "status running";
  runStatus.textContent = "执行中";

  for (let index = 0; index < events.length; index += 1) {
    if (currentRun !== runId) return;
    const [step, type, message] = events[index];
    activateAgent(step);
    appendEvent(index + 1, type, message);

    if (type === "HUMAN_INPUT_REQUIRED") {
      const approved = await requestApproval(currentRun);
      if (!approved) return;
      appendEvent(index + 2, "HUMAN_INPUT_RECEIVED", "批准兼容替代，原始需求与审批记录写入 Revision");
    }

    if (type === "ARTIFACT_PROGRESS") {
      artifacts.forEach((artifact, artifactIndex) => {
        window.setTimeout(() => {
          if (currentRun !== runId) return;
          artifact.classList.add("ready");
          artifact.querySelector("em").textContent = "已生成";
        }, artifactIndex * 130);
      });
    }

    await wait(620);
  }

  agents.forEach((agent) => agent.classList.remove("active"));
  agents.forEach((agent) => agent.classList.add("done"));
  gateStatus.textContent = "通过 · release_ready";
  status.className = "status done";
  runStatus.textContent = "交付完成";
  runButton.disabled = false;
}

runButton.addEventListener("click", run);
resetButton.addEventListener("click", reset);
approveButton.addEventListener("click", () => approvalResolver?.());
