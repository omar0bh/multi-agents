from app.agents.analysis import AnalysisAgent
from app.agents.base import GeneralAgent
from app.agents.coding import CodingAgent
from app.agents.research import ResearchAgent
from app.agents.scout import AIScoutAgent
from app.agents.writing import WritingAgent
from app.workers.workflow_engine import BusinessWorkflowAgent

_REGISTRY: dict | None = None


def build_registry() -> dict:
    return {
        "general": GeneralAgent(),
        "research": ResearchAgent(),
        "analysis": AnalysisAgent(),
        "coding": CodingAgent(),
        "writing": WritingAgent(),
        "business": BusinessWorkflowAgent(),
        "scout": AIScoutAgent(),
    }


def get_registry() -> dict:
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = build_registry()
    return _REGISTRY
