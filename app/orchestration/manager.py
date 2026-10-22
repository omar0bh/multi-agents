import logging

from app.config import TASK_TIMEOUT_SECONDS
from app.orchestration.policies import classify_task, routing_reason
from app.orchestration.registry import get_registry

logger = logging.getLogger(__name__)


def _run_agent(workflow: str, task: str) -> dict:
    registry = get_registry()
    agent = registry.get(workflow) or registry["general"]

    try:
        if workflow == "coding":
            return agent.run(task)
        if workflow == "business":
            return agent.run(task)
        return agent.run(task)
    except Exception as exc:
        logger.warning(
            "Agent '%s' failed with %s; falling back to direct LLM completion",
            workflow,
            exc,
        )
        from app.llm import call_raw_llm
        raw_answer = call_raw_llm(task)
        return {"answer": raw_answer}


class Orchestrator:
    def run(self, task: str) -> dict:
        workflow = classify_task(task)
        reason = routing_reason(task)

        logger.info(
            "Orchestrator routing workflow=%s method=%s",
            workflow,
            reason.get("method"),
        )

        result = _run_agent(workflow, task)

        return {
            "task": task,
            "workflow": workflow,
            "routing": reason,
            "agents_run": [{"name": workflow, "steps": 1}],
            "result": result,
            "meta": {
                "timeout_seconds": TASK_TIMEOUT_SECONDS,
            },
        }
