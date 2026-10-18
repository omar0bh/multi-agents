import logging

from fastapi import APIRouter

from app.api.errors import http_error_from_exception
from app.api.executor import run_with_timeout
from app.api.schemas import (
    AgentResponse,
    BusinessRequest,
    CodingRequest,
    ResearchRequest,
    TaskRequest,
)
from app.agents.coding import CodingAgent
from app.agents.research import ResearchAgent
from app.orchestration.manager import Orchestrator
from app.workers.workflow_engine import BusinessWorkflowAgent

router = APIRouter(prefix="/api", tags=["agents"])
logger = logging.getLogger(__name__)


def execute(workflow: str, callable_task):
    try:
        payload = run_with_timeout(callable_task)
        if isinstance(payload, dict) and "result" in payload:
            result = payload["result"]
            meta = {
                key: value
                for key, value in payload.items()
                if key not in {"result", "task"}
            }
        else:
            result = payload
            meta = {}

        return AgentResponse(
            ok=True,
            workflow=workflow,
            result=result,
            meta=meta,
        )
    except Exception as exc:
        logger.exception("Agent run failed workflow=%s", workflow)
        raise http_error_from_exception(exc) from exc


@router.post("/tasks", response_model=AgentResponse)
def run_task(payload: TaskRequest):
    return execute(
        "orchestrated",
        lambda: Orchestrator().run(payload.task),
    )


@router.post("/research", response_model=AgentResponse)
def research(payload: ResearchRequest):
    return execute(
        "research",
        lambda: ResearchAgent().run(payload.topic),
    )


@router.post("/coding", response_model=AgentResponse)
def coding(payload: CodingRequest):
    return execute(
        "coding",
        lambda: CodingAgent().run(payload.task, payload.language),
    )


@router.post("/business-workflow", response_model=AgentResponse)
def business_workflow(payload: BusinessRequest):
    return execute(
        "business",
        lambda: BusinessWorkflowAgent().run(payload.request),
    )
