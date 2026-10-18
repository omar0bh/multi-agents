
$ErrorActionPreference = "Stop"

Set-Location "C:\dspy-multi-agents"

function Write-ProjectFile {
    param(
        [string]$Path,
        [string]$Content
    )

    $FullPath = Join-Path (Get-Location).Path $Path
    $Parent = Split-Path $FullPath -Parent

    if (-not (Test-Path $Parent)) {
        New-Item -ItemType Directory -Path $Parent -Force | Out-Null
    }

    Set-Content -LiteralPath $FullPath -Value $Content -Encoding UTF8
    Write-Host "Created: $Path" -ForegroundColor Green
}

# ==========================================
# 1. Configuration
# ==========================================

Write-ProjectFile "app\config.py" @'
import os
from dotenv import load_dotenv

load_dotenv()

APP_NAME = os.getenv("APP_NAME", "DSPy Multi-Agent Platform")
OPENCODE_ZEN_API_KEY = os.getenv("OPENCODE_ZEN_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "openai/big-pickle")
LLM_API_BASE = os.getenv(
    "LLM_API_BASE",
    "https://opencode.ai/zen/v1"
)
SEARCH_MAX_RESULTS = int(os.getenv("SEARCH_MAX_RESULTS", "5"))
'@

Write-ProjectFile "app\llm.py" @'
import dspy
from app.config import (
    OPENCODE_ZEN_API_KEY,
    LLM_MODEL,
    LLM_API_BASE,
)

_configured = False

def configure_dspy():
    global _configured

    if _configured:
        return

    if not OPENCODE_ZEN_API_KEY:
        raise RuntimeError(
            "Missing OPENCODE_ZEN_API_KEY. "
            "Add your key to the .env file."
        )

    lm = dspy.LM(
        model=LLM_MODEL,
        api_key=OPENCODE_ZEN_API_KEY,
        api_base=LLM_API_BASE,
        temperature=0.2,
        max_tokens=2048,
    )

    dspy.configure(lm=lm)
    _configured = True
'@

# ==========================================
# 2. General agent
# ==========================================

Write-ProjectFile "app\agents\base.py" @'
import dspy

class GeneralTask(dspy.Signature):
    """Answer a user task with clear, actionable information."""
    task: str = dspy.InputField()
    answer: str = dspy.OutputField()

class GeneralAgent:
    def run(self, task: str) -> dict:
        from app.llm import configure_dspy
        configure_dspy()

        result = dspy.ChainOfThought(GeneralTask)(task=task)

        return {"answer": result.answer}
'@

# ==========================================
# 3. Research agent
# ==========================================

Write-ProjectFile "app\agents\research.py" @'
import dspy
from ddgs import DDGS
from app.config import SEARCH_MAX_RESULTS

class ResearchSummary(dspy.Signature):
    """Summarize the provided search results without inventing sources."""
    topic: str = dspy.InputField()
    results: str = dspy.InputField()
    summary: str = dspy.OutputField()
    findings: str = dspy.OutputField()
    limitations: str = dspy.OutputField()

class ResearchAgent:
    def search(self, topic: str) -> list:
        try:
            with DDGS() as search_client:
                items = list(
                    search_client.text(
                        topic,
                        max_results=SEARCH_MAX_RESULTS,
                    )
                )

            return [
                {
                    "title": item.get("title", ""),
                    "url": item.get("href", item.get("url", "")),
                    "snippet": item.get("body", ""),
                }
                for item in items
            ]
        except Exception as exc:
            raise RuntimeError(
                f"Web search failed: {exc}"
            ) from exc

    def run(self, topic: str) -> dict:
        from app.llm import configure_dspy
        configure_dspy()

        sources = self.search(topic)

        formatted = "\n\n".join(
            f"Title: {s['title']}\n"
            f"URL: {s['url']}\n"
            f"Snippet: {s['snippet']}"
            for s in sources
        )

        result = dspy.ChainOfThought(ResearchSummary)(
            topic=topic,
            results=formatted or "No search results found.",
        )

        return {
            "topic": topic,
            "summary": result.summary,
            "findings": result.findings,
            "limitations": result.limitations,
            "sources": sources,
        }
'@

# ==========================================
# 4. Analysis agent
# ==========================================

Write-ProjectFile "app\agents\analysis.py" @'
import dspy

class AnalyzeTask(dspy.Signature):
    """Analyze a task and identify risks and next steps."""
    task: str = dspy.InputField()
    analysis: str = dspy.OutputField()
    risks: str = dspy.OutputField()
    next_steps: str = dspy.OutputField()

class AnalysisAgent:
    def run(self, task: str) -> dict:
        from app.llm import configure_dspy
        configure_dspy()

        result = dspy.ChainOfThought(AnalyzeTask)(task=task)

        return {
            "analysis": result.analysis,
            "risks": result.risks,
            "next_steps": result.next_steps,
        }
'@

# ==========================================
# 5. Coding agent
# ==========================================

Write-ProjectFile "app\agents\coding.py" @'
import dspy

class GenerateCode(dspy.Signature):
    """Generate code for the requested task."""
    task: str = dspy.InputField()
    language: str = dspy.InputField()
    code: str = dspy.OutputField()
    explanation: str = dspy.OutputField()

class ReviewCode(dspy.Signature):
    """Review code for bugs, edge cases, and security concerns."""
    code: str = dspy.InputField()
    language: str = dspy.InputField()
    review: str = dspy.OutputField()
    tests: str = dspy.OutputField()

class CodingAgent:
    def run(self, task: str, language: str = "python") -> dict:
        from app.llm import configure_dspy
        configure_dspy()

        generated = dspy.ChainOfThought(GenerateCode)(
            task=task,
            language=language,
        )

        reviewed = dspy.ChainOfThought(ReviewCode)(
            code=generated.code,
            language=language,
        )

        return {
            "language": language,
            "code": generated.code,
            "explanation": generated.explanation,
            "review": reviewed.review,
            "suggested_tests": reviewed.tests,
        }
'@

# ==========================================
# 6. Writing agent
# ==========================================

Write-ProjectFile "app\agents\writing.py" @'
import dspy

class WriteDocument(dspy.Signature):
    """Write the requested document for the specified audience."""
    task: str = dspy.InputField()
    context: str = dspy.InputField()
    document: str = dspy.OutputField()

class WritingAgent:
    def run(self, task: str, context: str = "") -> dict:
        from app.llm import configure_dspy
        configure_dspy()

        result = dspy.ChainOfThought(WriteDocument)(
            task=task,
            context=context,
        )

        return {"document": result.document}
'@

# ==========================================
# 7. Business workflow agent
# ==========================================

Write-ProjectFile "app\workers\workflow_engine.py" @'
import dspy

class PlanWorkflow(dspy.Signature):
    """Design a business workflow. Do not claim actions were executed."""
    request: str = dspy.InputField()
    objective: str = dspy.OutputField()
    steps: str = dspy.OutputField()
    required_inputs: str = dspy.OutputField()
    risks_and_controls: str = dspy.OutputField()

class BusinessWorkflowAgent:
    def run(self, request: str) -> dict:
        from app.llm import configure_dspy
        configure_dspy()

        result = dspy.ChainOfThought(PlanWorkflow)(
            request=request,
        )

        return {
            "objective": result.objective,
            "steps": result.steps,
            "required_inputs": result.required_inputs,
            "risks_and_controls": result.risks_and_controls,
            "status": "plan_only",
        }
'@

# ==========================================
# 8. Task routing
# ==========================================

Write-ProjectFile "app\orchestration\policies.py" @'
def classify_task(task: str) -> str:
    text = task.lower()

    if any(x in text for x in (
        "research", "search the web", "sources",
        "find information", "بحث"
    )):
        return "research"

    if any(x in text for x in (
        "write code", "coding", "debug", "python",
        "javascript", "program", "script", "code"
    )):
        return "coding"

    if any(x in text for x in (
        "business workflow", "business process",
        "automate", "invoice approval", "workflow"
    )):
        return "business"

    if any(x in text for x in (
        "analyze", "analyse", "risks", "compare"
    )):
        return "analysis"

    if any(x in text for x in (
        "write a report", "draft", "email",
        "document", "rewrite"
    )):
        return "writing"

    return "general"
'@

Write-ProjectFile "app\orchestration\manager.py" @'
from app.orchestration.policies import classify_task
from app.agents.base import GeneralAgent
from app.agents.research import ResearchAgent
from app.agents.analysis import AnalysisAgent
from app.agents.coding import CodingAgent
from app.agents.writing import WritingAgent
from app.workers.workflow_engine import BusinessWorkflowAgent

class Orchestrator:
    def run(self, task: str) -> dict:
        workflow = classify_task(task)

        if workflow == "research":
            result = ResearchAgent().run(task)

        elif workflow == "coding":
            result = CodingAgent().run(task)

        elif workflow == "business":
            result = BusinessWorkflowAgent().run(task)

        elif workflow == "analysis":
            result = AnalysisAgent().run(task)

        elif workflow == "writing":
            result = WritingAgent().run(task)

        else:
            result = GeneralAgent().run(task)

        return {
            "task": task,
            "workflow": workflow,
            "result": result,
        }
'@

# ==========================================
# 9. API schemas and routes
# ==========================================

Write-ProjectFile "app\api\schemas.py" @'
from pydantic import BaseModel, Field

class TaskRequest(BaseModel):
    task: str = Field(min_length=1, max_length=12000)

class ResearchRequest(BaseModel):
    topic: str = Field(min_length=1, max_length=3000)

class CodingRequest(BaseModel):
    task: str = Field(min_length=1, max_length=12000)
    language: str = "python"

class BusinessRequest(BaseModel):
    request: str = Field(min_length=1, max_length=12000)
'@

Write-ProjectFile "app\api\routes.py" @'
from fastapi import APIRouter, HTTPException
from app.api.schemas import (
    TaskRequest,
    ResearchRequest,
    CodingRequest,
    BusinessRequest,
)
from app.orchestration.manager import Orchestrator
from app.agents.research import ResearchAgent
from app.agents.coding import CodingAgent
from app.workers.workflow_engine import BusinessWorkflowAgent

router = APIRouter(prefix="/api", tags=["agents"])

def execute(callable_task):
    try:
        return callable_task()
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=str(exc),
        ) from exc

@router.post("/tasks")
def run_task(payload: TaskRequest):
    return execute(lambda: Orchestrator().run(payload.task))

@router.post("/research")
def research(payload: ResearchRequest):
    return execute(lambda: ResearchAgent().run(payload.topic))

@router.post("/coding")
def coding(payload: CodingRequest):
    return execute(
        lambda: CodingAgent().run(payload.task, payload.language)
    )

@router.post("/business-workflow")
def business_workflow(payload: BusinessRequest):
    return execute(
        lambda: BusinessWorkflowAgent().run(payload.request)
    )
'@

# ==========================================
# 10. FastAPI application
# ==========================================

Write-ProjectFile "app\main.py" @'
from fastapi import FastAPI
from app.config import APP_NAME
from app.api.routes import router

app = FastAPI(
    title=APP_NAME,
    version="0.1.0",
    description="DSPy multi-agent platform",
)

app.include_router(router)

@app.get("/health")
def health():
    return {"status": "ok", "app": APP_NAME}
'@

# ==========================================
# 11. Helper tools
# ==========================================

Write-ProjectFile "app\tools\web_search.py" @'
from app.agents.research import ResearchAgent

def search_web(query: str) -> list:
    return ResearchAgent().search(query)
'@

Write-ProjectFile "app\tools\file_reader.py" @'
from pathlib import Path

def read_text_file(path: str, max_chars: int = 20000) -> str:
    file_path = Path(path).expanduser().resolve()

    if not file_path.is_file():
        raise FileNotFoundError(str(file_path))

    return file_path.read_text(
        encoding="utf-8",
        errors="replace",
    )[:max_chars]
'@

Write-ProjectFile "app\tools\code_runner.py" @'
"""Basic Python runner. This is not a secure sandbox."""
import subprocess
import sys
import tempfile
from pathlib import Path

def run_python(code: str, timeout: int = 5) -> dict:
    with tempfile.TemporaryDirectory() as folder:
        script = Path(folder) / "script.py"
        script.write_text(code, encoding="utf-8")

        try:
            result = subprocess.run(
                [sys.executable, "-I", str(script)],
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=folder,
                check=False,
            )

            return {
                "returncode": result.returncode,
                "stdout": result.stdout[-5000:],
                "stderr": result.stderr[-5000:],
                "timed_out": False,
            }

        except subprocess.TimeoutExpired:
            return {
                "returncode": None,
                "stdout": "",
                "stderr": "Execution timed out.",
                "timed_out": True,
            }
'@

# ==========================================
# 12. Logging and placeholders
# ==========================================

Write-ProjectFile "app\observability\logging.py" @'
import logging

def configure_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
'@

Write-ProjectFile "app\storage\database.py" @'
"""Database integration can be added when task persistence is required."""
'@

Write-ProjectFile "app\storage\repositories.py" @'
"""Repository layer placeholder for future persistence."""
'@

Write-ProjectFile "app\workers\sqs_worker.py" @'
"""AWS SQS worker placeholder; queue integration is not enabled yet."""
'@

Write-ProjectFile "app\orchestration\registry.py" @'
from app.agents.base import GeneralAgent
from app.agents.research import ResearchAgent
from app.agents.analysis import AnalysisAgent
from app.agents.coding import CodingAgent
from app.agents.writing import WritingAgent
from app.workers.workflow_engine import BusinessWorkflowAgent

def build_registry():
    return {
        "general": GeneralAgent(),
        "research": ResearchAgent(),
        "analysis": AnalysisAgent(),
        "coding": CodingAgent(),
        "writing": WritingAgent(),
        "business": BusinessWorkflowAgent(),
    }
'@

# ==========================================
# 13. Environment and dependencies
# ==========================================

Write-ProjectFile "requirements.txt" @'
dspy
litellm
fastapi
uvicorn[standard]
python-dotenv
pydantic
ddgs
pytest
httpx
'@

Write-ProjectFile ".env.example" @'
APP_NAME=DSPy Multi-Agent Platform
OPENCODE_ZEN_API_KEY=put_your_opencode_zen_api_key_here
LLM_MODEL=openai/big-pickle
LLM_API_BASE=https://opencode.ai/zen/v1
SEARCH_MAX_RESULTS=5
'@

Write-ProjectFile "tests\test_policies.py" @'
from app.orchestration.policies import classify_task

def test_research_route():
    assert classify_task("Research cloud security sources") == "research"

def test_coding_route():
    assert classify_task("Write Python code to parse a file") == "coding"

def test_business_route():
    assert classify_task("Automate an invoice approval workflow") == "business"
'@

Write-ProjectFile "README.md" @'
# DSPy Multi-Agent Platform

## Workflows
- General task routing
- Web research and sourced summaries
- Code generation and review
- Business workflow planning
- Analysis and writing

## Setup
1. Copy `.env.example` to `.env`.
2. Put your OpenCode Zen API key in `.env`.
3. Install packages: `pip install -r requirements.txt`
4. Start: `uvicorn app.main:app --reload`
5. Open http://127.0.0.1:8000/docs

Note: code execution is not sandboxed. Business workflows currently produce plans; they do not execute external actions.
'@

Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "Project files populated successfully!" -ForegroundColor Green
Write-Host "Next: configure .env and start the API." -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan