
$ErrorActionPreference = "Stop"

$dirs = @(
    "app\api",
    "app\orchestration",
    "app\agents",
    "app\tools",
    "app\workers",
    "app\storage",
    "app\observability",
    "tests",
    "evaluations",
    "artifacts"
)

foreach ($dir in $dirs) {
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
}

$files = @(
    "app\__init__.py",
    "app\main.py",
    "app\config.py",
    "app\api\__init__.py",
    "app\api\routes.py",
    "app\api\schemas.py",
    "app\orchestration\__init__.py",
    "app\orchestration\manager.py",
    "app\orchestration\registry.py",
    "app\orchestration\policies.py",
    "app\agents\__init__.py",
    "app\agents\base.py",
    "app\agents\research.py",
    "app\agents\coding.py",
    "app\agents\analysis.py",
    "app\agents\writing.py",
    "app\tools\__init__.py",
    "app\tools\web_search.py",
    "app\tools\file_reader.py",
    "app\tools\code_runner.py",
    "app\workers\__init__.py",
    "app\workers\sqs_worker.py",
    "app\storage\__init__.py",
    "app\storage\database.py",
    "app\storage\repositories.py",
    "app\observability\__init__.py",
    "app\observability\logging.py",
    "app\observability\metrics.py",
    "tests\test_agents.py",
    "tests\test_orchestration.py",
    "tests\test_security.py",
    "evaluations\examples.jsonl",
    "evaluations\evaluate.py",
    "artifacts\.gitkeep",
    "Dockerfile",
    "compose.yaml",
    "requirements.txt",
    ".env.example",
    ".gitignore",
    "README.md"
)

foreach ($file in $files) {
    if (-not (Test-Path $file)) {
        New-Item -ItemType File -Path $file | Out-Null
    }
}

@'
APP_ENV=development
LLM_MODEL=gemini/gemini-2.5-flash
GEMINI_API_KEY=replace_with_your_key
AWS_REGION=eu-west-1
DATABASE_URL=sqlite:///./dev.db
'@ | Set-Content -Encoding utf8 ".env.example"

@'
.env
.venv/
__pycache__/
*.py[cod]
.pytest_cache/
dev.db
'@ | Set-Content -Encoding utf8 ".gitignore"

@'
dspy
fastapi
uvicorn[standard]
pydantic-settings
python-dotenv
boto3
sqlalchemy
psycopg[binary]
httpx
pytest
'@ | Set-Content -Encoding utf8 "requirements.txt"

@'
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
RUN useradd --create-home appuser
USER appuser
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
'@ | Set-Content -Encoding utf8 "Dockerfile"

@'
services:
  api:
    build: .
    ports:
      - "8000:8000"
    env_file:
      - .env
'@ | Set-Content -Encoding utf8 "compose.yaml"

@'
# DSPy Agent Platform

Python multi-agent application using DSPy and FastAPI.

## Local development

1. Create a local .env file based on .env.example.
2. Install dependencies from requirements.txt.
3. Run: uvicorn app.main:app --reload
4. Open http://127.0.0.1:8000/docs

AWS services are added in later implementation stages.
'@ | Set-Content -Encoding utf8 "README.md"

Write-Host "Project structure created successfully!" -ForegroundColor Green
Get-ChildItem