"""Shared, typed state for collaboration between design agents."""

from __future__ import annotations

from pathlib import Path

from ratsnest.crews.contracts import (
    AgentMessage,
    AgentTask,
    DesignState,
    MessagePayload,
    MessageKind,
    TaskStatus,
    ToolCall,
    ToolExecution,
)
from ratsnest.data_proxy import Recorder


class DesignBlackboard:
    def __init__(self, project_dir: str, recorder: Recorder | None = None,
                 state: DesignState | None = None,
                 checkpoint_path: Path | None = None):
        self.state = state or DesignState(project_dir=project_dir)
        self.recorder = recorder
        self.checkpoint_path = Path(checkpoint_path) if checkpoint_path else None

    @classmethod
    def resume(cls, project_dir: str, recorder: Recorder | None = None,
               checkpoint_path: Path | None = None) -> "DesignBlackboard":
        path = Path(checkpoint_path) if checkpoint_path else None
        state = None
        if path is not None and path.is_file():
            state = DesignState.model_validate_json(
                path.read_text(encoding="utf-8"))
        return cls(project_dir, recorder, state=state, checkpoint_path=path)

    def checkpoint(self) -> None:
        if self.checkpoint_path is None:
            return
        self.checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.checkpoint_path.with_suffix(
            self.checkpoint_path.suffix + ".tmp")
        temporary.write_text(
            self.state.model_dump_json(indent=2), encoding="utf-8")
        temporary.replace(self.checkpoint_path)

    def publish(self, sender: str, recipient: str, kind: MessageKind,
                payload: MessagePayload | dict,
                correlation_id: str | None = None) -> AgentMessage:
        message = AgentMessage(
            sender=sender, recipient=recipient, kind=kind, payload=payload,
            correlation_id=correlation_id)
        self.state.messages.append(message)
        self.state.revision += 1
        if self.recorder is not None:
            self.recorder.emit(
                "blackboard.message", 0,
                observation={"revision": self.state.revision},
                action=message.model_dump(mode="json"),
                outcome={"accepted": True},
                metadata={"agent": sender, "crew": "design"})
        self.checkpoint()
        return message

    def assign(self, task: AgentTask, sender: str = "repair_agent") -> AgentTask:
        self.state.tasks.append(task)
        self.publish(sender, task.assignee, MessageKind.task,
                     task.model_dump(mode="json"), task.task_id)
        return task

    def pending_for(self, agent: str) -> list[AgentTask]:
        return [task for task in self.state.tasks
                if task.assignee == agent and task.status == TaskStatus.pending]

    def set_task_status(self, task: AgentTask, status: TaskStatus) -> None:
        task.status = status
        task.attempts += 1 if status == TaskStatus.running else 0
        self.state.revision += 1
        self.publish(task.assignee, "crew", MessageKind.status,
                     {"task_id": task.task_id, "status": status.value},
                     task.task_id)

    def record_tool(self, agent: str, call: ToolCall, success: bool,
                    outcome: dict | None = None, error: str | None = None) -> None:
        execution = ToolExecution(
            agent=agent, call=call, success=success,
            outcome=outcome or {}, error=error)
        self.state.tool_history.append(execution)
        self.state.revision += 1
        if self.recorder is not None:
            self.recorder.emit(
                f"design.{agent}.tool", 0,
                observation={"stage": self.state.stage,
                             "revision": self.state.revision},
                action=call.model_dump(mode="json"),
                outcome={"ok": success, "error": error, **(outcome or {})},
                metadata={"agent": agent, "crew": "design"})
        self.checkpoint()
