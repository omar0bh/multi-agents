# DSPy Multi-Agent Platform

General-purpose AI team with research, coding, analysis, writing, business workflow planning, and keyword-based orchestration.

## Workflows

- **POST /api/tasks** — routes to a specialist via keyword rules
- **POST /api/research** — DuckDuckGo search + LLM summary (live web when DDGS works)
- **POST /api/coding** — generate and review code (does not execute code)
- **POST /api/business-workflow** — structured plan only (`status: plan_only`)
- **GET /health** — server health; `?check_llm=1` runs Zen auth + completion test

## Setup (Windows PowerShell)

```powershell
cd C:\dspy-multi-agents
Copy-Item .env.example .env
# Edit .env and set OPENCODE_ZEN_API_KEY (never commit .env)

.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000/docs

## Verify Big Pickle / Zen

```powershell
python scripts\verify_llm.py
```

Step 1 checks `GET /zen/v1/models` (API key). Step 2 runs a minimal DSPy completion.

**Important:** OpenCode Zen **free-tier** models (including Big Pickle) often return HTTP 403 for external apps (FastAPI, DSPy, LiteLLM). This is expected. Your key can still be valid. For this project to receive completions, add **pay-as-you-go balance** at [OpenCode Zen](https://opencode.ai/zen).

Configuration for LiteLLM + DSPy:

```dotenv
LLM_MODEL=openai/big-pickle
LLM_API_BASE=https://opencode.ai/zen/v1
```

Do not use `opencode/big-pickle` in `.env` — that namespace is for the OpenCode CLI, not LiteLLM.

## Example requests

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health

$body = @{ task = "Explain what FastAPI is in two sentences." } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/api/tasks -ContentType "application/json" -Body $body
```

## Tests

```powershell
pytest tests\ -q
pytest tests\ -q -m integration   # optional live LLM (skips if free-tier blocked)
```

## Troubleshooting

| Symptom | Likely cause | Action |
|--------|----------------|--------|
| `Missing OPENCODE_ZEN_API_KEY` | No `.env` or empty key | Copy `.env.example` → `.env` |
| `free tier can only be used from within OpenCode` | Zen policy for free models | Add Zen balance or use OpenCode app |
| `Authentication failed` | Invalid/expired key | Regenerate key in Zen dashboard |
| Research empty `sources` | DDGS/network blocked | Retry; LLM summary may still run |
| HTTP 504 | Task timeout | Raise `TASK_TIMEOUT_SECONDS` or shorten task |

## Not implemented (placeholders)

- Database persistence (`app/storage/`)
- AWS SQS worker (`app/workers/sqs_worker.py`)
- Safe code execution (`app/tools/code_runner.py` is intentionally not exposed)

See [docs/aws-architecture.md](docs/aws-architecture.md) for a recommended AWS layout (documentation only).

## Security

- `.env` is gitignored; never commit API keys
- Business workflows are plans only, not executed actions
- Do not expose code execution without a sandbox
