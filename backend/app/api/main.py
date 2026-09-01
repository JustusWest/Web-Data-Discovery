from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.background import BackgroundTask

from app.api.models import (
    FeedbackRequest,
    FeedbackResponse,
    SessionInfoResponse,
    SummaryRequest,
    SummaryResponse,
    StartCrawlRequest,
    StartCrawlResponse,
    StopCrawlResponse,
)
from app.api.session_manager import (
    CrawlSessionManager,
    InvalidSessionStateError,
    SessionConflictError,
    SessionNotFoundError,
)
from app.settings import get_settings

settings = get_settings()
manager = CrawlSessionManager()

app = FastAPI(title="LLM Crawl API", version="0.1.0")

allowed_origins = list(settings.api_allowed_origins)
allow_credentials = "*" not in allowed_origins

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=allow_credentials,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _format_sse_payload(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\\n\\n"


def _cleanup_export_parent(path: str) -> None:
    parent = Path(path).parent
    shutil.rmtree(parent, ignore_errors=True)


@app.get("/")
async def root() -> dict:
    return {
        "name": "LLM Crawl API",
        "status": "ok",
        "health": "/health",
    }


@app.get("/health")
async def health_check() -> dict:
    return {"status": "ok"}


@app.post("/api/crawl/sessions", response_model=StartCrawlResponse)
async def start_crawl_session(request: StartCrawlRequest) -> StartCrawlResponse:
    try:
        session = await manager.start_session(request.model_dump(exclude_none=True))
        return StartCrawlResponse(sessionId=session.session_id, status=session.state)
    except SessionConflictError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.get("/api/crawl/sessions/{session_id}", response_model=SessionInfoResponse)
async def get_session_info(session_id: str) -> SessionInfoResponse:
    try:
        session = await manager.get_session_info(session_id)
        return SessionInfoResponse(
            sessionId=session.session_id,
            status=session.state,
            stats=session.stats,
            startedAt=session.started_at.isoformat() if session.started_at else None,
            completedAt=session.completed_at.isoformat() if session.completed_at else None,
            errorMessage=session.error_message,
        )
    except SessionNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@app.get("/api/crawl/sessions/{session_id}/results")
async def get_session_results(session_id: str) -> dict:
    try:
        results = await manager.get_session_results(session_id)
        return {"results": results}
    except SessionNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@app.post("/api/crawl/sessions/{session_id}/stop", response_model=StopCrawlResponse)
async def stop_crawl_session(session_id: str) -> StopCrawlResponse:
    try:
        session = await manager.stop_session(session_id)
        return StopCrawlResponse(sessionId=session.session_id, status=session.state)
    except SessionNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except InvalidSessionStateError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.post("/api/crawl/sessions/{session_id}/pause", response_model=StopCrawlResponse)
async def pause_crawl_session(session_id: str) -> StopCrawlResponse:
    try:
        session = await manager.pause_session(session_id)
        return StopCrawlResponse(sessionId=session.session_id, status=session.state)
    except SessionNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except InvalidSessionStateError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.post("/api/crawl/sessions/{session_id}/resume", response_model=StopCrawlResponse)
async def resume_crawl_session(session_id: str) -> StopCrawlResponse:
    try:
        session = await manager.resume_session(session_id)
        return StopCrawlResponse(sessionId=session.session_id, status=session.state)
    except SessionNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except InvalidSessionStateError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.post("/api/crawl/sessions/{session_id}/feedback", response_model=FeedbackResponse)
async def submit_feedback(session_id: str, request: FeedbackRequest) -> FeedbackResponse:
    try:
        updated = await manager.submit_feedback(
            session_id=session_id,
            result_id=request.resultId,
            feedback=request.feedback,
            notes=request.notes,
        )
        return FeedbackResponse(updated=updated)
    except SessionNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@app.post("/api/crawl/sessions/{session_id}/summary", response_model=SummaryResponse)
async def summarize_session(session_id: str, request: SummaryRequest) -> SummaryResponse:
    try:
        summary_payload = await manager.summarize_session(
            session_id=session_id,
            sample_size=request.sampleSize,
            include_query_history=request.includeQueryHistory,
        )
        return SummaryResponse(**summary_payload)
    except SessionNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@app.get("/api/crawl/sessions/{session_id}/events")
async def stream_session_events(session_id: str) -> StreamingResponse:
    async def event_generator():
        try:
            queue, backlog = await manager.subscribe(session_id)
        except SessionNotFoundError as error:
            payload = {
                "type": "session.failed",
                "sessionId": session_id,
                "timestamp": "",
                "payload": {"message": str(error)},
            }
            yield _format_sse_payload(payload)
            return

        try:
            for item in backlog:
                yield _format_sse_payload(item)

            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": keep-alive\\n\\n"
                    continue

                yield _format_sse_payload(event)
        finally:
            await manager.unsubscribe(session_id, queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/crawl/sessions/{session_id}/export")
async def export_session_artifacts(session_id: str):
    try:
        archive_path, filename = await manager.build_export_archive(session_id)
    except SessionNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error

    return FileResponse(
        path=str(archive_path),
        media_type="application/zip",
        filename=filename,
        background=BackgroundTask(_cleanup_export_parent, str(archive_path)),
    )


@app.exception_handler(SessionConflictError)
async def session_conflict_handler(_, exc: SessionConflictError):
    return JSONResponse(status_code=409, content={"detail": str(exc)})
