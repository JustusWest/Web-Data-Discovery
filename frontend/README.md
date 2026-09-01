# frontend

Frontend for a focused web crawling workflow with human feedback. This is the frontend half of the system described in *A Demo of Interactive Thematic Data Collection on the Live Web* (VLDB 2026 Demo). See the [repository root README](../README.md) for the system overview and paper reference.

This project is intentionally structured so the UI can run against:
- a local mock event stream (default), or
- an external crawler API service (live mode, e.g. the `../backend` service in this repository).

## Quick Start

```bash
nvm use   # .nvmrc pins Node 22; any Node >=20.19 should also work
npm install
npm run dev
```

Open `http://localhost:5173`.

## Runtime Modes

- `mock` mode (default): UI uses an in-browser mock crawler client with stream-like events.
- `live` mode: UI calls an external crawler API and subscribes to session events.

## Environment Variables

Create `.env.local` when needed.

- `VITE_CRAWL_CLIENT_MODE`
  - `mock` (default) or `live`
- `VITE_CRAWL_TRANSPORT`
  - `sse` (default)
- `VITE_CRAWL_API_BASE_URL`
  - Default: empty string (`""`), meaning same-origin calls
  - Example: `https://demo-crawler.example.com`
- `VITE_CRAWL_API_PREFIX`
  - Default: `/api/crawl`

### Example `.env.local` for external service

```bash
VITE_CRAWL_CLIENT_MODE=live
VITE_CRAWL_TRANSPORT=sse
VITE_CRAWL_API_BASE_URL=https://your-crawler-host
VITE_CRAWL_API_PREFIX=/api/crawl
```

## Frontend Integration Contract

The frontend expects a `CrawlerClient` implementation with the following methods:

- `startCrawl(request) -> { sessionId }`
- `stopCrawl(sessionId) -> { ... }`
- `submitFeedback(sessionId, feedbackPayload) -> { ... }`
- `subscribe(sessionId, handlers) -> unsubscribe`

### Session Request Shape

```json
{
  "topic": "Recent practical guides for small open LLMs",
  "maxDepth": 3,
  "minRelevance": 0.75,
  "domainFilter": "arxiv.org, github.com",
  "examples": [
    { "type": "url", "url": "https://example.com/post" },
    { "type": "file", "label": "seed-notes.txt" }
  ]
}
```

### Stream Event Envelope

All stream events should use this envelope:

```json
{
  "type": "result.discovered",
  "sessionId": "session-123",
  "timestamp": "2026-03-12T12:00:00.000Z",
  "payload": {}
}
```

Supported `type` values:
- `session.started`
- `crawl.progress`
- `result.discovered`
- `result.updated`
- `session.completed`
- `session.failed`

### Event Payload Expectations

- `session.started`
  - `{ sessionId, startedAt, request }`
- `crawl.progress`
  - `{ stats: { pagesScanned, relevantFound, tokensUsed }, lastResultId? }`
- `result.discovered`
  - `{ result }` (new result to append)
- `result.updated`
  - `{ result }` (existing result patch by `result.id`)
- `session.completed`
  - `{ stats?, completedAt?, reason? }`
- `session.failed`
  - `{ message }`

## Live API Endpoints Expected by Current Placeholder Client

When `VITE_CRAWL_CLIENT_MODE=live`, the frontend currently calls:

- `POST {API_PREFIX}/sessions`
  - body: session request
  - response: `{ sessionId: string }`
- `POST {API_PREFIX}/sessions/:sessionId/stop`
- `POST {API_PREFIX}/sessions/:sessionId/feedback`
  - body: `{ resultId, feedback, notes }`
- `GET {API_PREFIX}/sessions/:sessionId/events`
  - SSE endpoint returning the event envelope above
- `GET {API_PREFIX}/sessions/:sessionId`
  - session status/stats endpoint (also used by frontend polling fallback)
- `GET {API_PREFIX}/sessions/:sessionId/results`
  - current ordered results snapshot (used by frontend polling fallback)
- `GET {API_PREFIX}/sessions/:sessionId/export`
  - artifact download endpoint (`zip` in live mode)

`API_PREFIX` is resolved under `VITE_CRAWL_API_BASE_URL` if provided; otherwise same-origin.

## Architecture Notes

Main feature module:

- `src/features/crawl/`
  - `hooks/useCrawlSession.js`: run lifecycle, connection lifecycle, event reconciliation
  - `clients/`: mock + live crawler adapters
  - `components/`: presentational UI pieces
  - `CrawlPage.jsx`: feature composition container

`src/App.jsx` is a thin shell that renders the crawl feature.

## Demo Hosting Guidance

For short remote demo sessions, same-origin hosting is the simplest path:
- host UI and crawler API behind one domain
- leave `VITE_CRAWL_API_BASE_URL` empty
- keep `VITE_CRAWL_API_PREFIX=/api/crawl`

This avoids browser CORS complications for non-technical test users.

## Backend Pairing (FastAPI)

From the repository root:

```bash
cd backend
source .venv/bin/activate   # after following backend/README.md setup
python -m app.run_api
```

Then run the frontend in live mode:

```bash
cd frontend
nvm use
cp .env.example .env.local
```

Set `.env.local` values:

```bash
VITE_CRAWL_CLIENT_MODE=live
VITE_CRAWL_TRANSPORT=sse
VITE_CRAWL_API_BASE_URL=http://127.0.0.1:8000
VITE_CRAWL_API_PREFIX=/api/crawl
```
