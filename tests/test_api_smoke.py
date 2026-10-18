from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health_without_llm():
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["llm"] == "skipped"


def test_root():
    response = client.get("/")
    assert response.status_code == 200
    assert response.json()["docs"] == "/docs"


@patch("app.agents.base.dspy.ChainOfThought")
@patch("app.llm.configure_dspy")
def test_tasks_route_mocked(mock_configure, mock_cot):
    mock_cot.return_value.return_value.answer = "FastAPI is a Python web framework."

    response = client.post(
        "/api/tasks",
        json={"task": "Explain what FastAPI is in two sentences."},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["workflow"] == "orchestrated"
    assert "answer" in body["result"]
    assert body["meta"]["workflow"] == "general"
    mock_configure.assert_called()


@patch("app.agents.research.dspy.ChainOfThought")
@patch("app.agents.research.ResearchAgent.search", return_value=[])
@patch("app.llm.configure_dspy")
def test_research_route_mocked(mock_configure, _mock_search, mock_cot):
    mock_result = mock_cot.return_value.return_value
    mock_result.summary = "Summary"
    mock_result.findings = "Findings"
    mock_result.limitations = "None"

    response = client.post(
        "/api/research",
        json={"topic": "OpenCode Zen"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["workflow"] == "research"
    assert body["result"]["summary"] == "Summary"


@pytest.mark.integration
def test_live_llm_verification():
    from app.llm import verify_llm_connection

    try:
        result = verify_llm_connection()
    except RuntimeError as exc:
        exc_str = str(exc).lower()
        if "free tier" in exc_str or "free-tier" in exc_str:
            pytest.skip(str(exc))
        raise

    assert result["ok"] is True
    assert result["sample"]
