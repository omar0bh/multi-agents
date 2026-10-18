# Project Handoff: DSPy Multi-Agent AI Platform

## Purpose of this document

This file is a handoff brief for Codex. Read it first, inspect the
existing repository, verify the current implementation, then continue
the project carefully. Do not assume the code is correct just because
the files exist.

## User's goal

Build a practical multi-agent AI platform in Python that can handle:

1.  General tasks and routing to the appropriate agent.
2.  Research and web information gathering.
3.  Coding assistance, including code generation and review.
4.  Business workflow planning/automation.

The project is intended to use DSPy and FastAPI, with **Big Pickle
through OpenCode Zen as the selected model**. The user wants the project
to use Big Pickle, rather than accidentally routing requests to other
models configured in an account. Other models do not need to be disabled
at the account level if this project explicitly selects Big Pickle.

The user prefers direct, step-by-step instructions, exact Windows
commands and file paths, and explanations in Moroccan Darija/French
where helpful. Avoid vague instructions. Explain what each test proves
and distinguish a running API from a working LLM connection.

## Environment

-   OS: Windows 11
-   Project directory: `C:\dspy-multi-agents`
-   Shell used recently: PowerShell
-   Python virtual environment: `C:\dspy-multi-agents\.venv`
-   Framework: FastAPI
-   Server command: `uvicorn app.main:app --reload`
-   Local server URL: `http://127.0.0.1:8000`
-   Swagger UI: `http://127.0.0.1:8000/docs`
-   Health route: `http://127.0.0.1:8000/health`

## Current verified status

The user ran:

``` powershell
(.venv) PS C:\dspy-multi-agents> uvicorn app.main:app --reload
```

The output included:

``` text
INFO:     Will watch for changes in these directories: ['C:\\dspy-multi-agents']
INFO:     Uvicorn running on http://127.0.0.1:8000 (Press CTRL+C to quit)
INFO:     Started reloader process [13772] using WatchFiles
INFO:     Started server process [14904]
INFO:     Waiting for application startup.
INFO:     Application startup complete.
INFO:     127.0.0.1:54690 - "GET / HTTP/1.1" 404 Not Found
INFO:     127.0.0.1:54690 - "GET /favicon.ico HTTP/1.1" 404 Not Found
INFO:     127.0.0.1:55843 - "GET / HTTP/1.1" 404 Not Found
```

Interpretation: - Uvicorn and the FastAPI app start successfully. -
`GET / 404 Not Found` only means the root `/` route is not defined. It
does **not** mean the server failed. - This does **not** yet prove that
Big Pickle authentication/model calls work. - The next test is to open
`http://127.0.0.1:8000/docs`, locate `POST /api/tasks`, inspect its
request schema, submit a simple test task, and review the HTTP response
and terminal logs. - `/health` is only a server health check unless its
implementation explicitly tests the LLM. Inspect the code before
interpreting it as an LLM test.

## Current intended architecture / files

A PowerShell script called `populate_project.ps1` was previously used to
populate files in the project. The implementation was described as
containing:

-   `app/main.py`: FastAPI app and route registration.
-   `app/config.py`: configuration/settings.
-   `app/llm.py`: DSPy/LLM configuration.
-   Agents for general tasks, research, analysis, coding, and writing.
-   An orchestrator that routes tasks using keyword logic.
-   A business workflow planner.
-   API routes including:
    -   `POST /api/tasks`
    -   `POST /api/research`
    -   `POST /api/coding`
    -   `POST /api/business-workflow`
    -   `GET /health`
-   Research functionality using `ddgs`.
-   Coding agent that generates/reviews code; there is no verified safe
    code execution sandbox.
-   `.env.example` and `requirements.txt`.
-   `database.py` and `repositories.py` were described as placeholders,
    not a completed database layer.
-   Empty `__init__.py` files are normal and are not errors.

**Important:** This list is based on prior descriptions, not a fresh
repository inspection. Codex should inspect the actual files and tests
before changing anything. Do not overwrite the existing implementation
blindly.

## OpenCode Zen / Big Pickle configuration

The intended `.env` configuration is:

``` dotenv
OPENCODE_ZEN_API_KEY=YOUR_REAL_KEY_HERE
LLM_MODEL=openai/big-pickle
LLM_API_BASE=https://opencode.ai/zen/v1
```

Replace `YOUR_REAL_KEY_HERE` with the user's own key. Never ask the user
to paste the key into chat, and never print or commit it.

The intended DSPy configuration was described as something like:

``` python
dspy.LM(
    model="openai/big-pickle",
    api_base="https://opencode.ai/zen/v1",
    # API key should be read securely from environment/config
)
```

Codex must verify the actual installed DSPy/LiteLLM version and current
OpenCode Zen API requirements. Confirm whether the API key is passed
using the expected argument/environment variable and whether the model
identifier and base URL are correct for the installed stack. Do not
assume configuration works just because Uvicorn starts.

Model ID discussed: `big-pickle`; existing project config may prefix it
as `openai/big-pickle` for the OpenAI-compatible provider. Verify
against the provider's current documentation and test with a minimal
model call.

The user's objective is to keep **this project** configured to use Big
Pickle. Do not disable other account models globally unless there is a
concrete reason and the user explicitly wants that.

## How to run locally

In PowerShell:

``` powershell
cd C:\dspy-multi-agents
.\.venv\Scripts\Activate.ps1
uvicorn app.main:app --reload
```

The user already has the server running in one terminal. To test
endpoints, keep that terminal open and use a browser at:

-   Swagger: `http://127.0.0.1:8000/docs`
-   Health route: `http://127.0.0.1:8000/health`

If PowerShell activation is blocked by policy, do not change execution
policy globally without explaining why; commands can also use the venv's
executable directly.

## Immediate next steps for Codex

1.  Inspect the repository tree and read the actual contents of
    `README`, `requirements.txt`, `.env.example`, `app/main.py`,
    `app/config.py`, `app/llm.py`, every agent module, orchestrator,
    routes, and tests.
2.  Check whether `.env` exists and whether configuration loading is
    implemented. Do not display or expose the real API key. If showing
    `.env` status, redact secrets.
3.  Run a static/import check using the project's virtual environment.
4.  Inspect `/docs` or route declarations to determine the exact JSON
    schema expected by `POST /api/tasks`. Do not invent the request
    body.
5.  Make a minimal, explicit Big Pickle connectivity test using the
    project's configured DSPy model. Ensure errors are logged safely
    without leaking the API key.
6.  Test `POST /api/tasks` using the actual schema. Confirm a response
    comes from the model, not a hard-coded mock.
7.  Test each route individually, documenting which routes are
    functional and which are placeholders.
8.  Fix configuration or runtime problems based on observed errors, not
    guesses. Keep changes small and explain them.
9.  Add or update tests and a concise README with setup, `.env`
    variables, startup, endpoints, and troubleshooting.
10. Do not claim research is live web research unless its tool actually
    fetches current sources. Do not claim business workflows execute
    actions if they only generate plans.
11. Do not implement arbitrary code execution without a sandbox,
    resource limits, and explicit security controls. The current coding
    agent was described as generating/reviewing code only.
12. Avoid committing `.env`, keys, or other secrets.

## Useful first test

With the server running, open:

`http://127.0.0.1:8000/docs`

Find `POST /api/tasks` → `Try it out`. Read the displayed schema and
submit a small task such as:

> Explain what FastAPI is in two sentences.

Use the exact field names and types shown by Swagger. If the response
fails, capture the HTTP status, response body, and terminal traceback,
but redact tokens, keys, and personal data.

## Security and reliability notes

-   Keep the API key only in `.env` or another secure secret store.
-   Make sure `.env` is in `.gitignore`.
-   Do not paste the key into screenshots, logs, issue reports, or chat.
-   Big Pickle's free-period data handling may differ from paid models;
    avoid sending confidential, personal, or proprietary data until the
    provider's current privacy terms have been reviewed.
-   Treat external web pages and research results as untrusted input.
-   Do not execute generated code directly on the host machine.
-   Add request validation, timeouts, useful error messages, and safe
    logging.
-   Ensure the API fails clearly if the key/model is not configured,
    rather than silently returning a fake answer.

## Working style requested by the user

-   Work from the existing project, preserving useful code.
-   Give exact file paths and copy-pasteable Windows commands.
-   Prefer direct, step-by-step explanations.
-   Explain what each command does and what output to expect.
-   Ask the user for logs/screenshots only when they are necessary, and
    remind them to redact secrets.
-   Be honest about what is implemented, what is tested, and what
    remains incomplete.

## Handoff instruction

Start by inspecting the actual repository and report: 1. What already
exists. 2. What is incomplete or broken. 3. Whether the Big Pickle API
connection is truly working. 4. The smallest next steps to get a real
end-to-end task working.

Do not merely generate a new project from scratch. Continue from
`C:\dspy-multi-agents`.
