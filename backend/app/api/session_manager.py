from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
import traceback
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.WebAgent import query_generator
from app.crawler.artifacts import SessionArtifacts
from app.crawler.bfs_runner import BFSCrawlRunner
from app.settings import get_settings


class SessionConflictError(RuntimeError):
    pass


class SessionNotFoundError(RuntimeError):
    pass


class InvalidSessionStateError(RuntimeError):
    pass


@dataclass
class CrawlSession:
    session_id: str
    request_payload: dict[str, Any]
    artifact_dir: Path
    state: str = "starting"
    stats: dict[str, Any] = field(
        default_factory=lambda: {
            "pagesScanned": 0,
            "relevantFound": 0,
            "tokensUsed": 0,
            "urlsAttempted": 0,
            "urlErrors": 0,
            "errorRate": 0.0,
        }
    )
    stop_event: asyncio.Event = field(default_factory=asyncio.Event)
    pause_event: asyncio.Event = field(default_factory=asyncio.Event)
    pause_requested: bool = False
    task: asyncio.Task | None = None
    event_log: list[dict] = field(default_factory=list)
    subscribers: set[asyncio.Queue] = field(default_factory=set)
    results_by_id: dict[str, dict] = field(default_factory=dict)
    result_order: list[str] = field(default_factory=list)
    review_gate_required_ids: set[str] = field(default_factory=set)
    review_gate_open: bool = False
    review_gate_event: asyncio.Event = field(default_factory=asyncio.Event)
    error_message: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class CrawlSessionManager:
    def __init__(self):
        self.settings = get_settings()
        self._lock = asyncio.Lock()
        self._sessions: dict[str, CrawlSession] = {}
        self._active_session_id: str | None = None

    async def start_session(self, request_payload: dict[str, Any]) -> CrawlSession:
        async with self._lock:
            await self._cleanup_expired_sessions_locked()

            active_session = self._get_active_session_locked()
            if active_session and active_session.state in {
                "starting",
                "running",
                "paused",
                "awaiting_review",
                "stopping",
            }:
                raise SessionConflictError("A crawl session is already running")

            session_id = f"session-{uuid4().hex[:12]}"
            artifact_dir = self.settings.session_artifacts_root / session_id
            artifact_dir.mkdir(parents=True, exist_ok=True)

            session = CrawlSession(
                session_id=session_id,
                request_payload=request_payload,
                artifact_dir=artifact_dir,
            )

            self._sessions[session_id] = session
            self._active_session_id = session_id
            session.pause_event.set()

            runner = BFSCrawlRunner(
                session_id=session_id,
                request_payload=request_payload,
                event_callback=lambda event: self.publish_event(session_id, event),
                stop_event=session.stop_event,
                artifact_dir=artifact_dir,
                review_gate_handler=lambda result_ids: self.handle_review_gate(
                    session_id=session_id,
                    result_ids=result_ids,
                    stop_event=session.stop_event,
                ),
                wait_if_paused_handler=lambda: self.wait_if_paused(
                    session_id=session_id,
                    stop_event=session.stop_event,
                ),
            )

            session.task = asyncio.create_task(self._run_session(session, runner))
            return session

    async def _run_session(self, session: CrawlSession, runner: BFSCrawlRunner) -> None:
        try:
            await runner.run()
        except Exception as error:
            traceback.print_exc()
            await self.publish_event(
                session.session_id,
                {
                    "type": "session.failed",
                    "sessionId": session.session_id,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "payload": {
                        "message": str(error),
                    },
                },
            )

    async def stop_session(self, session_id: str) -> CrawlSession:
        async with self._lock:
            session = self._sessions.get(session_id)
            if not session:
                raise SessionNotFoundError(f"Session not found: {session_id}")

            if session.state not in {"starting", "running", "paused", "awaiting_review", "stopping"}:
                raise InvalidSessionStateError(
                    f"Cannot stop session in state '{session.state}'"
                )

            session.state = "stopping"
            session.stop_event.set()
            session.pause_requested = False
            session.pause_event.set()
            if session.review_gate_open:
                session.review_gate_open = False
                session.review_gate_required_ids.clear()
                session.review_gate_event.set()
            return session

    async def pause_session(self, session_id: str) -> CrawlSession:
        async with self._lock:
            session = self._sessions.get(session_id)
            if not session:
                raise SessionNotFoundError(f"Session not found: {session_id}")

            if session.state != "running":
                raise InvalidSessionStateError(
                    f"Cannot pause session in state '{session.state}'"
                )

            session.pause_requested = True
            session.pause_event.clear()

            event = {
                "type": "crawl.paused",
                "sessionId": session.session_id,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "payload": {"reason": "manual", "stats": session.stats},
            }
            self._publish_event_locked(session, event)
            return session

    async def resume_session(self, session_id: str) -> CrawlSession:
        async with self._lock:
            session = self._sessions.get(session_id)
            if not session:
                raise SessionNotFoundError(f"Session not found: {session_id}")

            if session.state != "paused":
                raise InvalidSessionStateError(
                    f"Cannot resume session in state '{session.state}'"
                )

            session.pause_requested = False
            session.pause_event.set()

            event = {
                "type": "crawl.resumed",
                "sessionId": session.session_id,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "payload": {"reason": "manual", "stats": session.stats},
            }
            self._publish_event_locked(session, event)
            return session

    async def submit_feedback(
        self,
        session_id: str,
        result_id: str,
        feedback: str | None,
        notes: str,
    ) -> bool:
        async with self._lock:
            session = self._sessions.get(session_id)
            if not session:
                raise SessionNotFoundError(f"Session not found: {session_id}")

            existing = session.results_by_id.get(result_id)
            if not existing:
                return False

            patched = {
                **existing,
                "feedback": feedback,
                "notes": notes,
                "feedbackSubmitted": True,
                "feedbackSubmittedAt": datetime.now().strftime("%H:%M:%S"),
            }
            session.results_by_id[result_id] = patched

            artifacts = SessionArtifacts(session.artifact_dir)
            artifacts.append_feedback(result_id=result_id, feedback=feedback, notes=notes)
            artifacts.update_result_feedback(patched)

            event = {
                "type": "result.updated",
                "sessionId": session_id,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "payload": {"result": patched},
            }
            self._publish_event_locked(session, event)

            if session.review_gate_open and result_id in session.review_gate_required_ids:
                session.review_gate_required_ids.discard(result_id)
                if not session.review_gate_required_ids:
                    session.review_gate_open = False
                    session.review_gate_event.set()
                else:
                    remaining = len(session.review_gate_required_ids)
                    wait_event = {
                        "type": "crawl.awaiting_review",
                        "sessionId": session_id,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "payload": {
                            "requiredCount": remaining,
                            "remainingReviews": remaining,
                            "requiredResultIds": sorted(list(session.review_gate_required_ids)),
                            "stats": session.stats,
                        },
                    }
                    self._publish_event_locked(session, wait_event)
            return True

    async def publish_event(self, session_id: str, event: dict) -> None:
        async with self._lock:
            session = self._sessions.get(session_id)
            if not session:
                return
            self._publish_event_locked(session, event)

    def _publish_event_locked(self, session: CrawlSession, event: dict) -> None:
        event_type = event.get("type")
        payload = event.get("payload", {})

        if event_type == "session.started":
            session.state = "running"
            started_at = payload.get("startedAt")
            if started_at:
                session.started_at = _safe_parse_iso_datetime(started_at)
                if session.started_at and session.started_at.tzinfo is None:
                    session.started_at = session.started_at.replace(tzinfo=timezone.utc)
                elif session.started_at is None:
                    session.started_at = datetime.now(timezone.utc)
            else:
                session.started_at = datetime.now(timezone.utc)
        elif event_type == "crawl.progress":
            stats = payload.get("stats")
            if isinstance(stats, dict):
                session.stats = stats
        elif event_type == "result.discovered":
            result = payload.get("result")
            if isinstance(result, dict) and result.get("id"):
                result_id = result["id"]
                if result_id not in session.results_by_id:
                    session.result_order.append(result_id)
                session.results_by_id[result_id] = result
        elif event_type == "result.updated":
            result = payload.get("result")
            if isinstance(result, dict) and result.get("id"):
                result_id = result["id"]
                if result_id not in session.results_by_id:
                    session.result_order.append(result_id)
                session.results_by_id[result_id] = result
        elif event_type == "session.completed":
            session.state = "completed"
            session.completed_at = datetime.now(timezone.utc)
            stats = payload.get("stats")
            if isinstance(stats, dict):
                session.stats = stats
            session.pause_requested = False
            session.pause_event.set()
            session.review_gate_open = False
            session.review_gate_required_ids.clear()
            session.review_gate_event.set()
            if self._active_session_id == session.session_id:
                self._active_session_id = None
        elif event_type == "session.failed":
            session.state = "failed"
            session.completed_at = datetime.now(timezone.utc)
            session.error_message = payload.get("message", "Session failed")
            session.pause_requested = False
            session.pause_event.set()
            session.review_gate_open = False
            session.review_gate_required_ids.clear()
            session.review_gate_event.set()
            if self._active_session_id == session.session_id:
                self._active_session_id = None
        elif event_type == "crawl.awaiting_review":
            session.state = "awaiting_review"
            stats = payload.get("stats")
            if isinstance(stats, dict):
                session.stats = stats
            required_ids = payload.get("requiredResultIds")
            if isinstance(required_ids, list):
                session.review_gate_required_ids = {item for item in required_ids if item}
                session.review_gate_open = bool(session.review_gate_required_ids)
                if session.review_gate_open:
                    session.review_gate_event.clear()
                else:
                    session.review_gate_event.set()
        elif event_type == "crawl.resumed":
            stats = payload.get("stats")
            if isinstance(stats, dict):
                session.stats = stats
            if session.state in {"awaiting_review", "paused"}:
                session.state = "running"
            if payload.get("reason") == "manual":
                session.pause_requested = False
                session.pause_event.set()
            else:
                session.review_gate_open = False
                session.review_gate_required_ids.clear()
                session.review_gate_event.set()
        elif event_type == "crawl.paused":
            stats = payload.get("stats")
            if isinstance(stats, dict):
                session.stats = stats
            session.state = "paused"
            session.pause_requested = True
            session.pause_event.clear()

        session.event_log.append(event)
        if len(session.event_log) > 5000:
            session.event_log = session.event_log[-2500:]

        dead_subscribers = []
        for queue in session.subscribers:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                dead_subscribers.append(queue)

        for queue in dead_subscribers:
            session.subscribers.discard(queue)

    async def subscribe(self, session_id: str) -> tuple[asyncio.Queue, list[dict]]:
        async with self._lock:
            session = self._sessions.get(session_id)
            if not session:
                raise SessionNotFoundError(f"Session not found: {session_id}")

            queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
            session.subscribers.add(queue)
            backlog = list(session.event_log)
            return queue, backlog

    async def unsubscribe(self, session_id: str, queue: asyncio.Queue) -> None:
        async with self._lock:
            session = self._sessions.get(session_id)
            if not session:
                return
            session.subscribers.discard(queue)

    async def get_session_info(self, session_id: str) -> CrawlSession:
        async with self._lock:
            session = self._sessions.get(session_id)
            if not session:
                raise SessionNotFoundError(f"Session not found: {session_id}")
            return session

    async def handle_review_gate(
        self,
        *,
        session_id: str,
        result_ids: list[str],
        stop_event: asyncio.Event,
    ) -> None:
        await self._open_review_gate(session_id, result_ids)
        await self._wait_for_review_gate(session_id, stop_event)

    async def wait_if_paused(self, *, session_id: str, stop_event: asyncio.Event) -> None:
        while True:
            async with self._lock:
                session = self._sessions.get(session_id)
                if not session:
                    return

                if not session.pause_requested:
                    return
                pause_event = session.pause_event

            wait_pause_task = asyncio.create_task(pause_event.wait())
            wait_stop_task = asyncio.create_task(stop_event.wait())

            done, pending = await asyncio.wait(
                {wait_pause_task, wait_stop_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for pending_task in pending:
                pending_task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

            if stop_event.is_set():
                return

            if wait_pause_task in done and wait_pause_task.result():
                return

    async def _open_review_gate(self, session_id: str, result_ids: list[str]) -> None:
        async with self._lock:
            session = self._sessions.get(session_id)
            if not session:
                return

            required_ids = [item for item in result_ids if item]
            session.review_gate_required_ids = set(required_ids)
            session.review_gate_open = bool(required_ids)
            if session.review_gate_open:
                session.review_gate_event.clear()
            else:
                session.review_gate_event.set()

    async def _wait_for_review_gate(self, session_id: str, stop_event: asyncio.Event) -> None:
        while True:
            async with self._lock:
                session = self._sessions.get(session_id)
                if not session:
                    return

                if not session.review_gate_open or not session.review_gate_required_ids:
                    return
                gate_event = session.review_gate_event

            wait_gate_task = asyncio.create_task(gate_event.wait())
            wait_stop_task = asyncio.create_task(stop_event.wait())

            done, pending = await asyncio.wait(
                {wait_gate_task, wait_stop_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for pending_task in pending:
                pending_task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

            if stop_event.is_set():
                return

            if wait_gate_task in done and wait_gate_task.result():
                return

    async def get_session_results(self, session_id: str) -> list[dict]:
        async with self._lock:
            session = self._sessions.get(session_id)
            if not session:
                raise SessionNotFoundError(f"Session not found: {session_id}")

            ordered = []
            for result_id in session.result_order:
                item = session.results_by_id.get(result_id)
                if item:
                    ordered.append(item)
            return ordered

    async def summarize_session(
        self,
        *,
        session_id: str,
        sample_size: int = 40,
        include_query_history: bool = True,
    ) -> dict[str, Any]:
        try:
            normalized_sample_size = int(sample_size)
        except (TypeError, ValueError):
            normalized_sample_size = 40
        normalized_sample_size = max(5, min(100, normalized_sample_size))

        async with self._lock:
            session = self._sessions.get(session_id)
            if not session:
                raise SessionNotFoundError(f"Session not found: {session_id}")

            ordered_results = [
                dict(session.results_by_id[result_id])
                for result_id in session.result_order
                if result_id in session.results_by_id
            ]
            checkpoint_path = session.artifact_dir / "checkpoint.json"
            event_log = list(session.event_log)
            request_payload = dict(session.request_payload)
            stats = dict(session.stats)
            state = session.state
            started_at_iso = session.started_at.isoformat() if session.started_at else None
            completed_at_iso = session.completed_at.isoformat() if session.completed_at else None

        query_history = self._extract_query_history(
            checkpoint_path=checkpoint_path,
            event_log=event_log,
            include_query_history=include_query_history,
        )
        sampled_results = self._sample_results_for_summary(
            ordered_results,
            normalized_sample_size,
        )
        summary_text = await asyncio.to_thread(
            self._generate_summary_text,
            request_payload=request_payload,
            state=state,
            stats=stats,
            started_at_iso=started_at_iso,
            completed_at_iso=completed_at_iso,
            query_history=query_history,
            sampled_results=sampled_results,
            total_results=len(ordered_results),
        )

        return {
            "sessionId": session_id,
            "status": state,
            "generatedAt": datetime.now(timezone.utc).isoformat(),
            "sampleSize": len(sampled_results),
            "queryHistory": query_history,
            "stats": stats,
            "summaryText": summary_text,
        }

    def _extract_query_history(
        self,
        *,
        checkpoint_path: Path,
        event_log: list[dict],
        include_query_history: bool,
    ) -> list[str]:
        if not include_query_history:
            return []

        collected: list[str] = []
        seen: set[str] = set()

        def maybe_add(raw_query: Any) -> None:
            if not isinstance(raw_query, str):
                return
            cleaned = " ".join(raw_query.split()).strip()
            if not cleaned or cleaned in seen:
                return
            seen.add(cleaned)
            collected.append(cleaned)

        if checkpoint_path.exists():
            try:
                with checkpoint_path.open("r", encoding="utf-8") as file:
                    checkpoint_data = json.load(file)
                for query in checkpoint_data.get("all_queries", []):
                    maybe_add(query)
            except Exception:
                pass

        if collected:
            return collected

        for event in event_log:
            if event.get("type") != "crawl.progress":
                continue
            payload = event.get("payload", {})
            if isinstance(payload, dict):
                maybe_add(payload.get("query"))

        return collected

    @staticmethod
    def _sample_results_for_summary(results: list[dict], sample_size: int) -> list[dict]:
        if len(results) <= sample_size:
            return list(results)

        head_count = sample_size // 2
        tail_count = sample_size - head_count
        sampled = results[:head_count] + results[-tail_count:]

        deduped: list[dict] = []
        seen_ids: set[str] = set()
        for item in sampled:
            item_id = str(item.get("id", ""))
            if item_id and item_id in seen_ids:
                continue
            if item_id:
                seen_ids.add(item_id)
            deduped.append(item)

        return deduped[:sample_size]

    @staticmethod
    def _render_prompt_template(template: str, **values: Any) -> str:
        rendered = str(template).replace("\\n", "\n")
        for key, value in values.items():
            rendered = rendered.replace("{" + key + "}", str(value))
        return rendered

    @staticmethod
    def _truncate_text(value: Any, max_chars: int) -> str:
        if value is None:
            return ""
        text = " ".join(str(value).split()).strip()
        if len(text) <= max_chars:
            return text
        return text[: max_chars - 1].rstrip() + "..."

    def _compact_result_for_summary(self, result: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": result.get("id"),
            "url": result.get("url"),
            "title": self._truncate_text(result.get("title"), 160),
            "snippet": self._truncate_text(result.get("snippet"), 420),
            "prediction": result.get("pred"),
            "reason": self._truncate_text(result.get("reason"), 240),
            "query": self._truncate_text(result.get("query"), 200),
            "relevanceScore": result.get("relevanceScore"),
            "feedback": result.get("feedback"),
            "feedbackSubmitted": bool(result.get("feedbackSubmitted")),
        }

    def _generate_summary_text(
        self,
        *,
        request_payload: dict[str, Any],
        state: str,
        stats: dict[str, Any],
        started_at_iso: str | None,
        completed_at_iso: str | None,
        query_history: list[str],
        sampled_results: list[dict],
        total_results: int,
    ) -> str:
        topic = str(request_payload.get("topic", "")).strip() or "No topic provided"
        prompts = {}

        try:
            with self.settings.prompts_path.open("r", encoding="utf-8") as file:
                prompts = json.load(file)
        except Exception:
            prompts = {}

        default_template = (
            "Topic description:\\n{topic_seed}\\n\\n"
            "Generate a concise narrative summary for this crawl session.\\n"
            "Session status: {crawl_status}\\n"
            "Started at: {started_at}\\n"
            "Completed at: {completed_at}\\n"
            "Stats JSON:\\n{stats_json}\\n\\n"
            "Query history:\\n{query_history_json}\\n\\n"
            "Total discovered results: {total_results}\\n"
            "Sampled results for analysis:\\n{sampled_results_json}\\n\\n"
            "Return ONLY valid JSON: {'summaryText': '<plain text summary>'}"
        )

        summary_template = prompts.get("general_summary", default_template)
        sys_prompt = prompts.get("default_prompt", "You are a helpful assistant")

        compact_results = [self._compact_result_for_summary(item) for item in sampled_results]
        prompt_payload = self._render_prompt_template(
            summary_template,
            topic_seed=topic,
            crawl_status=state,
            started_at=started_at_iso or "n/a",
            completed_at=completed_at_iso or "n/a",
            stats_json=json.dumps(stats, ensure_ascii=False, indent=2),
            query_history_json=json.dumps(query_history[:40], ensure_ascii=False, indent=2),
            total_results=total_results,
            sampled_results_json=json.dumps(compact_results, ensure_ascii=False, indent=2),
        )

        try:
            conversation = [
                {"role": "system", "content": "You are a helpful assistant"},
                {"role": "user", "content": prompt_payload},
            ]
            response = query_generator(
                model_name=self.settings.default_model_name,
                sys_prompt=sys_prompt,
                task_prompt=prompt_payload,
                conversation=conversation,
            )

            if isinstance(response, dict):
                summary_text = response.get("summaryText") or response.get("summary") or response.get("text")
                if isinstance(summary_text, str) and summary_text.strip():
                    return summary_text.strip()

            if isinstance(response, str) and response.strip():
                return response.strip()
        except Exception:
            pass

        return self._build_fallback_summary(
            topic=topic,
            state=state,
            stats=stats,
            total_results=total_results,
            query_history=query_history,
        )

    @staticmethod
    def _build_fallback_summary(
        *,
        topic: str,
        state: str,
        stats: dict[str, Any],
        total_results: int,
        query_history: list[str],
    ) -> str:
        pages_scanned = int(stats.get("pagesScanned", 0) or 0)
        relevant_found = int(stats.get("relevantFound", 0) or 0)
        urls_attempted = int(stats.get("urlsAttempted", 0) or 0)
        url_errors = int(stats.get("urlErrors", 0) or 0)
        error_rate = float(stats.get("errorRate", 0.0) or 0.0) * 100.0

        summary_lines = [
            f"Crawl objective: {topic}",
            f"Session status: {state}.",
            (
                f"The crawler scanned {pages_scanned} pages and marked {relevant_found} as relevant "
                f"out of {total_results} discovered results."
            ),
            f"URL attempts: {urls_attempted}, URL errors: {url_errors} ({error_rate:.1f}%).",
        ]

        if query_history:
            summary_lines.append(
                f"Query strategy evolved across {len(query_history)} queries, including: "
                + "; ".join(query_history[:5])
            )

        return " ".join(summary_lines)

    async def build_export_archive(self, session_id: str) -> tuple[Path, str]:
        async with self._lock:
            session = self._sessions.get(session_id)
            if not session:
                raise SessionNotFoundError(f"Session not found: {session_id}")

            if not session.artifact_dir.exists():
                raise SessionNotFoundError("Session artifacts do not exist")

            temp_dir = Path(tempfile.mkdtemp(prefix="llm-crawl-export-"))
            zip_path = temp_dir / f"{session_id}-artifacts.zip"

            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zip_file:
                for file_path in session.artifact_dir.rglob("*"):
                    if file_path.is_file():
                        zip_file.write(file_path, arcname=file_path.relative_to(session.artifact_dir))

            return zip_path, zip_path.name

    def _get_active_session_locked(self) -> CrawlSession | None:
        if not self._active_session_id:
            return None
        return self._sessions.get(self._active_session_id)

    async def _cleanup_expired_sessions_locked(self) -> None:
        now = datetime.now(timezone.utc)
        ttl = timedelta(seconds=self.settings.session_artifact_ttl_seconds)

        session_ids = list(self._sessions.keys())
        for session_id in session_ids:
            session = self._sessions[session_id]

            if session.state in {"starting", "running", "paused", "stopping"}:
                continue

            if session.completed_at is None:
                continue

            if now - session.completed_at < ttl:
                continue

            shutil.rmtree(session.artifact_dir, ignore_errors=True)
            self._sessions.pop(session_id, None)
            if self._active_session_id == session_id:
                self._active_session_id = None


def _safe_parse_iso_datetime(raw_value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(raw_value)
    except Exception:
        return None
