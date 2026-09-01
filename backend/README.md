# backend

FastAPI wrapper around the focused crawling BFS engine, designed to stream crawl events to the UI and export run artifacts. This is the backend half of the system described in *A Demo of Interactive Thematic Data Collection on the Live Web* (VLDB 2026 Demo). See the [repository root README](../README.md) for the system overview and paper reference.

## What This Backend Provides

- `POST /api/crawl/sessions` to start one crawl session.
- `GET /api/crawl/sessions/{sessionId}/events` SSE stream for incremental updates.
- `POST /api/crawl/sessions/{sessionId}/stop` for graceful stop.
- `POST /api/crawl/sessions/{sessionId}/feedback` to patch a result with human feedback.
- `GET /api/crawl/sessions/{sessionId}/export` to download session artifacts (`zip`).

One active session is enforced for now.

## Prompt Strategy

The backend uses a single general prompt set, defined in `app/Prompts.json`:

- `default_prompt` — system prompt used by the Relevance Agent.
- `general_query` — initial query-generation prompt (Query Agent).
- `general_feedback` — re-seeding / follow-up query-generation prompt (Query Agent).
- `general_classify` — page relevance classification prompt (Relevance Agent).

Topic-specific prompt profiles are not required for normal API runs; `Prompts.json` is loaded once and any profile name resolves against `{profile}_query`, `{profile}_classify`, `{profile}_feedback` keys, so alternate profiles can be added without code changes.

## Project Layout (Integration-Relevant)

- `app/api/main.py` FastAPI entrypoint and HTTP routes.
- `app/api/session_manager.py` in-memory session lifecycle + subscriptions.
- `app/crawler/bfs_runner.py` BFS crawl runner emitting stream events (Query/Relevance/Navigation agent orchestration).
- `app/crawler/artifacts.py` CSV/JSON artifact writing per session.
- `app/crawler/lightweight_classifier.py` cheap heuristic pre-filter used ahead of LLM classification.
- `app/settings.py` env-driven runtime settings.
- `app/WebAgent.py` / `utils/LLM_lib.py` / `utils/crawl_utils.py` LLM calls, fetch/extract, and search integration.
- `app/run_api.py` process entrypoint (`python -m app.run_api`).

## Install

This was developed and tested against Python 3.10. Newer interpreters may work but are unverified against the pinned dependency set.

```bash
cd backend
python3.10 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install --upgrade pip
pip install -r requirements.txt
```

A Conda environment spec (`app/environment.yml`, originally captured on Linux) is also included for reference if you prefer Conda; it is not guaranteed to resolve cross-platform, so `requirements.txt` is the recommended path.

## Configure

```bash
cp .env.example .env
cp app/API_KEY.json.example app/API_KEY.json
```

Edit `app/API_KEY.json` and fill in at minimum:

- `SEARCH_API_KEY` — a [searchapi.io](https://www.searchapi.io/) key (used for `google` engine search).
- `DeepSeek_Michale` — a [DeepSeek](https://platform.deepseek.com/) API key (the field name is a historical artifact of the original implementation; keep it as-is).

Equivalently, these can be set as environment variables (`SEARCH_API_KEY`, `DEEPSEEK_API_KEY`) instead of editing the JSON file directly — env vars take precedence. See `.env.example` for the full list of supported environment variables.

Important env vars:

- `CRAWL_API_HOST` default `0.0.0.0`
- `CRAWL_API_PORT` default `8000`
- `CRAWL_API_ALLOWED_ORIGINS` default `*`
- `CRAWL_SESSION_ARTIFACTS_ROOT` default `<system temp dir>/llm-crawl-sessions`

## Run API Server

```bash
source .venv/bin/activate
python -m app.run_api
```

Health check:

```bash
curl http://127.0.0.1:8000/health
```

## API Contract

### Start Session

`POST /api/crawl/sessions`

Request JSON:

```json
{
  "topic": "Accredited US Law Schools",
  "maxDepth": 3,
  "minRelevance": 0.75,
  "domainFilter": "",
  "examples": [
    { "type": "url", "url": "https://example.com" },
    { "type": "file", "label": "seed_notes.txt" }
  ]
}
```

Response JSON:

```json
{ "sessionId": "session-abc123", "status": "starting" }
```

### Stream Events (SSE)

`GET /api/crawl/sessions/{sessionId}/events`

Envelope:

```json
{
  "type": "result.discovered",
  "sessionId": "session-abc123",
  "timestamp": "2026-03-12T14:10:00.000000+00:00",
  "payload": {}
}
```

Event `type` values:

- `session.started`
- `crawl.progress`
- `result.discovered`
- `result.updated`
- `session.completed`
- `session.failed`

### Stop Session

`POST /api/crawl/sessions/{sessionId}/stop`

### Submit Feedback

`POST /api/crawl/sessions/{sessionId}/feedback`

Request JSON:

```json
{
  "resultId": "session-abc123-result-12",
  "feedback": "yes",
  "notes": "Official program page"
}
```

### Export Artifacts

`GET /api/crawl/sessions/{sessionId}/export`

Returns a zip containing artifacts such as:

- `results.csv`
- `checkpoint.json`
- `component_metrics.csv`
- `fetch_metrics.csv`

## Frontend Integration Notes

For the frontend (`../frontend`), configure live mode:

- `VITE_CRAWL_CLIENT_MODE=live`
- `VITE_CRAWL_TRANSPORT=sse`
- `VITE_CRAWL_API_BASE_URL=http://127.0.0.1:8000` (or leave empty for same-origin)
- `VITE_CRAWL_API_PREFIX=/api/crawl`

## Demo Hosting Guidance

For short remote demo sessions, same-origin hosting is the simplest path:
- host UI and crawler API behind one domain
- leave `VITE_CRAWL_API_BASE_URL` empty
- keep `VITE_CRAWL_API_PREFIX=/api/crawl`

This avoids browser CORS complications for non-technical test users.
