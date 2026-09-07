"""Read-only verification of a saved runtime request's next resume route."""
import asyncio
import json
import os
import sys

import redis

from agents.ratsnestpro.ratsnestpro_agent import (
    ratsnestpro_multi_agent, _after_initialize, _release_repair_resume_step,
)
from agents.ratsnestpro.resume_context import recover_context
from memory import initialize_database
from service.run_coordination import checkpoint_thread_candidates


async def main(request_id):
    r = redis.Redis.from_url(os.environ["REDIS_URL"], decode_responses=True)
    record = next(r.hgetall(k) for k in r.scan_iter("ratsnest:{registry}:run:*")
                  if r.type(k) == "hash" and r.hget(k, "request_id") == request_id)
    async with initialize_database() as saver:
        graph = ratsnestpro_multi_agent
        graph.checkpointer = saver
        for thread in checkpoint_thread_candidates(record["agent_id"], record["user_id"],
                                                   record["thread_id"], allow_legacy=True):
            config = {"configurable": {"thread_id": thread}}
            snapshot = await graph.aget_state(config)
            if not snapshot.values:
                continue
            restored = await recover_context(graph, config, snapshot.values, "继续原任务，从原检查点恢复，不新建工程")
            values = {**snapshot.values, **restored, "incremental_resume": True, "workflow_mode": "build"}
            print(json.dumps({"restored": bool(restored), "workspace": values.get("workspace_run_name"),
                              "retains_exact_mcu": "STM32G070RBT6" in values.get("requirement", ""),
                              "next_node": _after_initialize(values),
                              "resume_step": _release_repair_resume_step(values)}, ensure_ascii=False))
            return
        raise RuntimeError("No owned checkpoint found")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
