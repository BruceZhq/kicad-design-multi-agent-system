"""Recover an owned engineering context after a falsely classified fresh turn."""
import json
import re

from agents.ratsnestpro.intent_router import classify_intent, requests_new_context


def has_checkpoint(values):
    from agents.ratsnestpro.ratsnestpro_agent import _workspace_root
    name = str(values.get("workspace_run_name", ""))
    if not name or "/" in name or "\\" in name or name in {".", ".."}:
        return False
    root = (_workspace_root() / "runs").resolve()
    path = (root / name / "pipeline_state.json").resolve()
    if not path.is_relative_to(root):
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return (payload.get("project_name") == values.get("project_name")
                and bool(payload.get("steps")) and bool(payload.get("requirement")))
    except (OSError, ValueError, TypeError):
        return False


async def recover_context(agent, config, current, message):
    decision = classify_intent(message, prior_intent="build", has_active_context=bool(current))
    if decision.context_relation != "resume" or has_checkpoint(current):
        return {}
    # Never cross a genuine new-project request. All snapshots come from the
    # already authorized checkpoint thread, not a filesystem-wide search.
    async for snapshot in agent.aget_state_history(config, limit=100):
        values = snapshot.values
        if has_checkpoint(values):
            return {k: v for k, v in values.items() if k not in {
                "messages", "runtime_scope", "scope", "request_id", "user_id",
            }}
        if requests_new_context(str(values.get("latest_request", ""))):
            break
    if re.search(r"checkpoint|检查点|断点|不重跑", message, re.I):
        raise ValueError("Resume requested but no same-task engineering checkpoint was found; refusing a fresh build")
    return {}
