import assert from "node:assert/strict";
import test from "node:test";

import { profileForkRequestBody, runSubmissionMode } from "../lib/request-intent.ts";

const profile10 = {
  id: "site-control-telemetry",
  version: "1.0",
  digest: "a".repeat(64),
};

test("negated new project preserves the current conversation and base run", () => {
  for (const message of [
    "继续原任务，从原检查点恢复，不新建工程，沿用原始需求和已确认参数。",
    "继续原任务，不要新建一个 KiCad 工程。",
    "Resume the PCB task. Do not start a new project.",
    "Continue without a new project.",
  ]) {
    assert.equal(runSubmissionMode(message, profile10, profile10), "revision", message);
  }
  assert.equal(runSubmissionMode("不要继续旧任务。新建一个 KiCad 工程。", profile10, profile10), "explicit-new-project");
});

test("keeps a revision only on the exact immutable profile snapshot", () => {
  assert.equal(runSubmissionMode("重新执行布局步骤", profile10, profile10), "revision");
  assert.equal(runSubmissionMode("开始任务", profile10, undefined), "initial");
});

test("forks when id, version, or digest changes", () => {
  assert.equal(runSubmissionMode("重新执行布局步骤", { ...profile10, version: "1.1" }, profile10), "profile-migration");
  assert.equal(runSubmissionMode("重新执行布局步骤", { ...profile10, digest: "b".repeat(64) }, profile10), "profile-migration");
  assert.equal(runSubmissionMode("重新执行布局步骤", profile10, null), "profile-migration");
});

test("an explicit fresh project never replays the source run", () => {
  assert.equal(runSubmissionMode("请新建一个 KiCad 工程", { ...profile10, version: "1.1" }, profile10), "explicit-new-project");
});

test("builds the governed server replay request without client chat history", () => {
  const body = profileForkRequestBody(
    "重新做 step 11/17",
    { id: profile10.id, version: "1.1" },
    "deepseek-v4-flash",
    [{ roleId: "reviewer", name: "Reviewer", responsibility: "独立审查" }],
  );
  assert.deepEqual(body, {
    capabilityProfile: { id: profile10.id, version: "1.1" },
    replayMode: "THROUGH_SOURCE_REVISION",
    changeRequest: "重新做 step 11/17",
    model: "deepseek-v4-flash",
    teamMembers: [{ roleId: "reviewer", name: "Reviewer", responsibility: "独立审查" }],
  });
  assert.equal("messages" in body, false);
  assert.equal("history" in body, false);
});
