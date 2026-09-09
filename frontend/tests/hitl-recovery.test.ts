import assert from "node:assert/strict";
import test from "node:test";
import { parseHumanInputRequest } from "../types/chat.ts";

test("preserves the exact accepted answer for same-run recovery", () => {
  const answer = "继续当前任务，保留原始约束。";
  const event = { type: "CUSTOM", name: "ratsnest.human-input-required.v1", value: {
    interactionId: "same-question", kind: "clarification", question: "请确认",
    options: [], allowFreeText: true, requestedBy: "hardware-engineer", stateVersion: 4,
    resumeAnswer: answer,
  }};
  const envelope = { eventId: 80, runId: "run-1", createdAt: "2026-09-08T00:00:00Z", type: "ag_ui", data: event };
  assert.equal(parseHumanInputRequest(envelope)?.resumeAnswer, answer);
  assert.equal(parseHumanInputRequest({ ...envelope, data: { ...event, value: {
    ...event.value, resumeAnswer: "x".repeat(10001),
  }}})?.resumeAnswer, undefined);
});
