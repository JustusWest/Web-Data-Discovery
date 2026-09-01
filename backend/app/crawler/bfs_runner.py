from __future__ import annotations

import asyncio
from enum import Enum
import heapq
import json
from collections import OrderedDict, defaultdict, deque
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
import time
from typing import Awaitable, Callable
from urllib.parse import urlparse

import aiohttp

from app.WebAgent import (
    async_batch_snippet_relevance_judger,
    async_classic_crawl,
    async_classify_link,
    async_judge_query_urls,
    async_score_crawl,
    async_web_content_judger,
    query_feedback,
    query_generator,
)
from app.settings import get_settings
from utils.crawl_utils import (
    async_extract_links_with_context,
    async_get_content_from_url,
    async_hop_with_context,
)

from .artifacts import SessionArtifacts
from .event_mapper import build_progress_payload, build_result_payload, normalize_prediction
from .lightweight_classifier import LightweightRelevanceClassifier


class _ExtractLinkCache:
    def __init__(self, *, max_entries: int, ttl_s: float):
        self.max_entries = max(1, int(max_entries))
        self.ttl_s = max(0.0, float(ttl_s))
        self._store: OrderedDict[str, tuple[float, dict[str, str]]] = OrderedDict()

    def get(self, key: str) -> dict[str, str] | None:
        item = self._store.get(key)
        if item is None:
            return None
        ts, payload = item
        if self.ttl_s > 0 and (time.monotonic() - ts) > self.ttl_s:
            self._store.pop(key, None)
            return None
        self._store.move_to_end(key)
        return dict(payload)

    def set(self, key: str, payload: dict[str, str]) -> None:
        self._store[key] = (time.monotonic(), dict(payload))
        self._store.move_to_end(key)
        while len(self._store) > self.max_entries:
            self._store.popitem(last=False)

    def stats(self) -> dict:
        return {
            "entries": len(self._store),
            "maxEntries": self.max_entries,
            "ttlS": self.ttl_s,
        }


@dataclass
class _QuerySessionState:
    query: str
    mode: str
    classify_target: int
    fallback_classify_target: int
    min_positive_seeds: int
    extract_cap_multiplier: float
    extract_link_cap: int
    fallback_used: bool = False
    positive_seed_count: int = 0
    expansion_seed_count: int = 0
    search_results_considered: int = 0
    query_pages_classified: int = 0
    frontier_pages_classified: int = 0
    links_extracted_to_frontier: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


class _QueryScoreLaneState(str, Enum):
    READY_TO_SCORE = "READY_TO_SCORE"
    READY_TO_CLASSIFY = "READY_TO_CLASSIFY"
    WAITING_FOR_INPUT = "WAITING_FOR_INPUT"
    DONE = "DONE"


class BFSCrawlRunner:
    def __init__(
        self,
        *,
        session_id: str,
        request_payload: dict,
        event_callback,
        stop_event: asyncio.Event,
        artifact_dir: Path,
        review_gate_handler: Callable[[list[str]], Awaitable[None]] | None = None,
        wait_if_paused_handler: Callable[[], Awaitable[None]] | None = None,
    ):
        self.settings = get_settings()
        self.session_id = session_id
        self.request_payload = request_payload
        self.event_callback = event_callback
        self.stop_event = stop_event
        self.artifacts = SessionArtifacts(artifact_dir)

        self.topic = request_payload.get("topic", "Focused crawl topic")
        self.max_depth = int(request_payload.get("maxDepth", 3))
        self.min_relevance = float(request_payload.get("minRelevance", 0.75))
        self.domain_filter = request_payload.get("domainFilter", "")
        self.examples = request_payload.get("examples", [])
        self.prompt_profile = str(
            request_payload.get("promptProfile", self.settings.default_prompt_profile or "general")
        ).strip() or "general"
        self.prompts_path_override = str(request_payload.get("promptsPath", "") or "").strip()
        self.prompt_overrides = request_payload.get("promptOverrides", {}) or {}
        self.review_before_crawl = bool(request_payload.get("reviewBeforeCrawl", False))
        self.reseed_enabled = bool(request_payload.get("reseedEnabled", True))
        self.query_mode = str(request_payload.get("queryMode", "llm")).strip().lower()
        self.classifier_mode = str(request_payload.get("classifierMode", "llm")).strip().lower()
        self.policy_mode = str(
            request_payload.get("policyMode", self.settings.policy_mode)
        ).strip().lower() or "legacy"
        self.warmup_pages = int(request_payload.get("warmupPages", 200) or 200)
        self.time_budget_sec = int(request_payload.get("timeBudgetSec", 0) or 0)
        self.page_budget = int(request_payload.get("pageBudget", 0) or 0)
        self.static_queries = [
            str(item).strip() for item in request_payload.get("staticQueries", []) if str(item).strip()
        ]
        self.seedfinder_queries = [
            str(item).strip()
            for item in request_payload.get("seedfinderQueries", [])
            if str(item).strip()
        ]
        def _payload_or_default(name: str, default):
            value = request_payload.get(name, None)
            if value is None:
                return default
            return value

        self.max_parallel_targets = int(request_payload.get("maxParallelTargets", 6) or 6)
        self.max_parallel_crawl_targets = int(
            _payload_or_default("maxParallelCrawlTargets", self.settings.max_parallel_crawl_targets)
        )
        self.classify_limit = int(request_payload.get("searchResultClassifyLimit", 10) or 10)
        self.query_classify_target = max(
            1,
            int(_payload_or_default("queryClassifyTarget", self.settings.query_classify_target)),
        )
        self.query_fallback_classify_target = max(
            1,
            int(
                _payload_or_default(
                    "queryFallbackClassifyTarget",
                    self.settings.query_fallback_classify_target,
                )
            ),
        )
        self.query_min_positive_seeds = max(
            1,
            int(_payload_or_default("queryMinPositiveSeeds", self.settings.query_min_positive_seeds)),
        )
        self.query_extract_cap_multiplier = max(
            1.0,
            float(
                _payload_or_default(
                    "queryExtractCapMultiplier",
                    self.settings.query_extract_cap_multiplier,
                )
            ),
        )
        self.classify_batch_size = max(
            1,
            int(_payload_or_default("classifyBatchSize", self.settings.classify_batch_size)),
        )
        self.frontier_select_count = max(
            1,
            int(_payload_or_default("frontierSelectCount", 15)),
        )
        self.frontier_classify_cap = max(
            1,
            int(_payload_or_default("frontierClassifyCap", 10)),
        )
        self.classify_batch_flush_timeout_ms = max(
            50.0,
            float(_payload_or_default("classifyBatchFlushTimeoutMs", 600.0)),
        )
        self.classify_batch_fast_flush_timeout_ms = max(
            50.0,
            float(_payload_or_default("classifyBatchFastFlushTimeoutMs", 300.0)),
        )
        self.classify_batch_min_size = max(
            1,
            int(_payload_or_default("classifyBatchMinSize", 2)),
        )
        self.frontier_fetch_parallel = max(
            1,
            int(_payload_or_default("frontierFetchParallel", 10)),
        )
        self.score_batch_size = max(
            1,
            int(_payload_or_default("scoreBatchSize", self.settings.score_batch_size)),
        )
        self.score_request_timeout_s = max(
            1.0,
            float(_payload_or_default("scoreRequestTimeoutS", self.settings.score_request_timeout_s)),
        )
        self.classify_request_timeout_s = max(
            1.0,
            float(_payload_or_default("classifyRequestTimeoutS", self.settings.classify_request_timeout_s)),
        )
        self.max_frontier_buffer_items = max(
            1,
            int(_payload_or_default("maxFrontierBufferItems", self.settings.max_frontier_buffer_items)),
        )
        self.max_links_per_page = max(
            1,
            int(_payload_or_default("maxLinksPerPage", self.settings.max_links_per_page)),
        )
        hop_link_limits_raw = _payload_or_default("hopLinkLimits", self.settings.hop_link_limits)
        parsed_hop_limits: list[int] = []
        for item in hop_link_limits_raw:
            try:
                parsed = int(item)
            except (TypeError, ValueError):
                continue
            if parsed > 0:
                parsed_hop_limits.append(parsed)
        self.hop_link_limits = tuple(parsed_hop_limits)
        if not self.hop_link_limits:
            self.hop_link_limits = (max(1, int(self.settings.max_links_per_page)),)
        self.extract_parallel_per_hop = max(
            1,
            int(_payload_or_default("extractParallelPerHop", self.settings.extract_parallel_per_hop)),
        )
        self.extract_connect_timeout_s = float(
            _payload_or_default("extractConnectTimeoutS", self.settings.extract_connect_timeout_s)
        )
        self.extract_read_timeout_s = float(
            _payload_or_default("extractReadTimeoutS", self.settings.extract_read_timeout_s)
        )
        self.extract_total_timeout_s = float(
            _payload_or_default("extractTotalTimeoutS", self.settings.extract_total_timeout_s)
        )
        self.extract_max_bytes = int(
            _payload_or_default("extractMaxBytes", self.settings.extract_max_bytes)
        )
        self.extract_max_retries = int(
            _payload_or_default("extractMaxRetries", self.settings.extract_max_retries)
        )
        self.extract_retry_backoff_s = float(
            _payload_or_default("extractRetryBackoffS", self.settings.extract_retry_backoff_s)
        )
        self.extract_allowed_mime_prefixes = tuple(
            str(item).strip().lower()
            for item in _payload_or_default(
                "extractAllowedMimePrefixes",
                self.settings.extract_allowed_mime_prefixes,
            )
            if str(item).strip()
        )
        self.enable_extract_bounds = bool(
            _payload_or_default("enableExtractBounds", self.settings.enable_extract_bounds)
        )
        self.enable_extract_mime_filter = bool(
            _payload_or_default("enableExtractMimeFilter", self.settings.enable_extract_mime_filter)
        )
        self.enable_extract_retry = bool(
            _payload_or_default("enableExtractRetry", self.settings.enable_extract_retry)
        )
        self.extract_cache_enabled = bool(
            _payload_or_default("extractCacheEnabled", self.settings.extract_cache_enabled)
        )
        self.extract_cache_max_entries = max(
            1,
            int(_payload_or_default("extractCacheMaxEntries", self.settings.extract_cache_max_entries)),
        )
        self.extract_cache_ttl_s = max(
            0.0,
            float(_payload_or_default("extractCacheTtlS", self.settings.extract_cache_ttl_s)),
        )
        self.fetch_connect_timeout_s = float(
            _payload_or_default("fetchConnectTimeoutS", self.settings.fetch_connect_timeout_s)
        )
        self.fetch_read_timeout_s = float(
            _payload_or_default("fetchReadTimeoutS", self.settings.fetch_read_timeout_s)
        )
        self.fetch_total_timeout_s = float(
            _payload_or_default("fetchTotalTimeoutS", self.settings.fetch_total_timeout_s)
        )
        self.fetch_max_bytes = int(
            _payload_or_default("fetchMaxBytes", self.settings.fetch_max_bytes)
        )
        self.fetch_max_retries = int(
            _payload_or_default("fetchMaxRetries", self.settings.fetch_max_retries)
        )
        self.fetch_retry_backoff_s = float(
            _payload_or_default("fetchRetryBackoffS", self.settings.fetch_retry_backoff_s)
        )
        self.fetch_allowed_mime_prefixes = tuple(
            str(item).strip().lower()
            for item in _payload_or_default(
                "fetchAllowedMimePrefixes",
                self.settings.fetch_allowed_mime_prefixes,
            )
            if str(item).strip()
        )
        self.host_min_access_interval_s = max(
            0.0,
            float(
                _payload_or_default("hostMinAccessIntervalS", self.settings.host_min_access_interval_s)
            ),
        )
        self.max_pages_per_domain = int(
            _payload_or_default("maxPagesPerDomain", self.settings.max_pages_per_domain)
        )
        self.domain_fail_cooldown_s = max(
            0.0,
            float(
                _payload_or_default("domainFailCooldownS", self.settings.domain_fail_cooldown_s)
            ),
        )
        self.domain_fail_threshold = max(
            1,
            int(
                _payload_or_default("domainFailThreshold", self.settings.domain_fail_threshold)
            ),
        )
        self.enable_fetch_bounds = bool(
            request_payload.get("enableFetchBounds", self.settings.enable_fetch_bounds)
        )
        self.enable_domain_cooldown = bool(
            request_payload.get("enableDomainCooldown", self.settings.enable_domain_cooldown)
        )
        self.enable_mime_filter = bool(
            request_payload.get("enableMimeFilter", self.settings.enable_mime_filter)
        )
        self.enable_fetch_retry = bool(
            request_payload.get("enableFetchRetry", self.settings.enable_fetch_retry)
        )
        requested_review_count = request_payload.get("reviewPageCount", 3)
        try:
            requested_review_count = int(requested_review_count)
        except (TypeError, ValueError):
            requested_review_count = 3

        self.allowed_domains = [
            domain.strip().lower() for domain in self.domain_filter.split(",") if domain.strip()
        ]

        self.result_counter = 0
        self.seen_result_urls: set[str] = set()
        self.stats = {
            "pagesScanned": 0,
            "relevantFound": 0,
            "tokensUsed": 0,
            "urlsAttempted": 0,
            "urlErrors": 0,
            "errorRate": 0.0,
        }
        self.review_gate_handler = review_gate_handler
        self.wait_if_paused_handler = wait_if_paused_handler
        self.review_gate_limit = max(1, min(10, requested_review_count))
        self.review_gate_result_ids: list[str] = []
        self.review_gate_processed = False
        self.component_metrics: list[dict] = []
        self.fetch_metrics: list[dict] = []
        self.classify_skip_counts: dict[str, int] = defaultdict(int)
        self.query_session_summaries: list[dict] = []
        self.query_score_runtime = {
            "scoreTimeoutCount": 0,
            "classifyTimeoutCount": 0,
            "scoreRetrySplitSuccessCount": 0,
            "scoreRetrySplitFailCount": 0,
            "maxInFlightRequestAgeMs": 0.0,
            "stalledLoopWatchdogCount": 0,
        }
        self.total_estimated_cost_usd = 0.0
        self.start_time_utc: datetime | None = None
        self._query_history = set()
        self._warmup_train_rows: deque[tuple[str, int]] = deque(maxlen=2000)
        self.lightweight_classifier = LightweightRelevanceClassifier()
        self.lightweight_ready = False
        self._domain_lock = asyncio.Lock()
        self._domain_last_access_ts: dict[str, float] = {}
        self._domain_fail_streak: dict[str, int] = defaultdict(int)
        self._domain_cooldown_until_ts: dict[str, float] = {}
        self._domain_pages_used: dict[str, int] = defaultdict(int)
        self.extract_link_cache = (
            _ExtractLinkCache(
                max_entries=self.extract_cache_max_entries,
                ttl_s=self.extract_cache_ttl_s,
            )
            if self.extract_cache_enabled
            else None
        )

        self.query_prompt_key = f"{self.prompt_profile}_query"
        self.classify_prompt_key = f"{self.prompt_profile}_classify"
        self.feedback_prompt_key = f"{self.prompt_profile}_feedback"
        self.prompt_context = {
            "topic_seed": self.topic,
            "topic_description": self.topic,
            "domain_filter": self.domain_filter or "none",
            "examples": json.dumps(self.examples, ensure_ascii=False),
            "examples_count": len(self.examples),
            "max_depth": self.max_depth,
            "min_relevance": self.min_relevance,
        }

    def _build_stage_semaphores(
        self,
    ) -> tuple[asyncio.Semaphore, asyncio.Semaphore, asyncio.Semaphore, asyncio.Semaphore]:
        # Keep classify and crawl/frontier work in separate pools to avoid one stage
        # starving the other under bursty loads.
        classify_parallel = max(1, self.max_parallel_targets)
        if self.max_parallel_crawl_targets > 0:
            crawl_parallel = max(1, self.max_parallel_crawl_targets)
        else:
            # Crawl is HTTP-heavy, so allow a wider pool than LLM-bound stages.
            crawl_parallel = max(1, min(16, self.max_parallel_targets * 2))
        # Isolate LLM queues so frontier selection does not block classify calls.
        frontier_llm_parallel = 1
        classify_llm_parallel = max(1, self.max_parallel_targets - frontier_llm_parallel)
        return (
            asyncio.Semaphore(classify_parallel),
            asyncio.Semaphore(crawl_parallel),
            asyncio.Semaphore(classify_llm_parallel),
            asyncio.Semaphore(frontier_llm_parallel),
        )

    async def run(self) -> dict:
        start_time = datetime.utcnow()
        self.start_time_utc = start_time
        prompts = self._load_prompts()

        sys_prompt = prompts.get("default_prompt", "You are a helpful assistant")
        query_prompt_template = prompts.get(self.query_prompt_key)
        classify_prompt_key = self.classify_prompt_key
        feedback_prompt_template = prompts.get(self.feedback_prompt_key)

        if not query_prompt_template:
            raise RuntimeError(f"Missing query prompt key '{self.query_prompt_key}'")
        if not feedback_prompt_template:
            raise RuntimeError(f"Missing feedback prompt key '{self.feedback_prompt_key}'")
        if classify_prompt_key not in prompts:
            raise RuntimeError(f"Missing classify prompt key '{classify_prompt_key}'")
        query_prompt = self._render_prompt_template(
            query_prompt_template,
            **self.prompt_context,
        )

        query_gen_conv = [
            {"role": "system", "content": "You are a helpful assistant"},
            {"role": "user", "content": query_prompt},
        ]

        await self._emit_event(
            "session.started",
            {
                "sessionId": self.session_id,
                "startedAt": start_time.isoformat(),
                "request": self.request_payload,
            },
        )

        init_queries = await self._build_initial_queries(
            sys_prompt=sys_prompt,
            query_prompt=query_prompt,
            query_gen_conv=query_gen_conv,
        )
        first_queries = init_queries.get("queries", []) if isinstance(init_queries, dict) else []
        first_queries = self._dedupe_queries(first_queries)
        query_gen_conv.append({"role": "system", "content": json.dumps(init_queries)})

        visited: set[str] = set()
        all_queries: list[str] = list(first_queries)
        feedback_all = ""
        queries_to_crawl = list(first_queries)

        if self.review_before_crawl and first_queries:
            bootstrap_query = first_queries[0]
            bootstrap_results, seen_urls, feedback_text = await self._query_without_crawl(
                bootstrap_query,
                visited,
                classify_prompt_key,
            )
            visited.update(seen_urls)
            feedback_all += f"{feedback_text}\n"

            selected_bootstrap_results = self._select_bootstrap_results(bootstrap_results)
            await self._record_results(
                bootstrap_query,
                selected_bootstrap_results,
                collect_for_review_gate=True,
            )
            if not self.review_gate_processed and self.review_gate_result_ids:
                await self._apply_initial_review_gate()

            self.artifacts.write_checkpoint(
                visited=visited,
                query_gen_conv=query_gen_conv,
                all_queries=all_queries,
                stats=self.stats,
                start_time=start_time,
                end_time=None,
            )
            queries_to_crawl = first_queries[1:]

        for query in queries_to_crawl:
            if self.stop_event.is_set():
                break
            await self._wait_if_paused()
            if self.stop_event.is_set():
                break

            judge_results, seen_urls, feedback_text = await self._query_and_crawl(
                query,
                visited,
                classify_prompt_key,
            )
            visited.update(seen_urls)
            feedback_all += f"{feedback_text}\n"

            await self._record_results(query, judge_results)
            self.artifacts.write_checkpoint(
                visited=visited,
                query_gen_conv=query_gen_conv,
                all_queries=all_queries,
                stats=self.stats,
                start_time=start_time,
                end_time=None,
            )

        iteration_count = 0
        while (
            iteration_count < self.settings.max_iterations
            and self.stats["pagesScanned"] < self.settings.max_results
            and not self.stop_event.is_set()
        ):
            await self._wait_if_paused()
            if self.stop_event.is_set():
                break
            if self._budget_exhausted():
                break

            next_prompt = self._render_prompt_template(
                feedback_prompt_template,
                query_feedback=feedback_all,
                **self.prompt_context,
            )
            query_gen_conv.append({"role": "user", "content": next_prompt})

            if not self.reseed_enabled:
                break

            new_query_payload = await self._build_followup_queries(
                sys_prompt=sys_prompt,
                next_prompt=next_prompt,
                query_gen_conv=query_gen_conv,
            )

            query_gen_conv.append({"role": "system", "content": json.dumps(new_query_payload)})
            new_queries = new_query_payload.get("queries", []) if isinstance(new_query_payload, dict) else []
            new_queries = self._dedupe_queries(new_queries)
            if not new_queries:
                break
            all_queries.extend(new_queries)
            feedback_all = ""

            for query in new_queries:
                if self.stop_event.is_set():
                    break
                if self._budget_exhausted():
                    break
                await self._wait_if_paused()
                if self.stop_event.is_set():
                    break
                if self._budget_exhausted():
                    break

                judge_results, seen_urls, feedback_text = await self._query_and_crawl(
                    query,
                    visited,
                    classify_prompt_key,
                )
                visited.update(seen_urls)
                feedback_all += f"{feedback_text}\n"

                await self._record_results(query, judge_results)
                self.artifacts.write_checkpoint(
                    visited=visited,
                    query_gen_conv=query_gen_conv,
                    all_queries=all_queries,
                    stats=self.stats,
                    start_time=start_time,
                    end_time=None,
                )

            iteration_count += 1
            if self._budget_exhausted():
                break

        end_time = datetime.utcnow()

        self.artifacts.write_query_log(query_gen_conv)
        self.artifacts.write_component_metrics(self.component_metrics)
        self.artifacts.write_fetch_metrics(self.fetch_metrics)
        cost_summary = self._build_cost_summary(start_time=start_time, end_time=end_time)
        fetch_summary = self._build_fetch_summary()
        self.artifacts.write_checkpoint(
            visited=visited,
            query_gen_conv=query_gen_conv,
            all_queries=all_queries,
            stats=self.stats,
            start_time=start_time,
            end_time=end_time,
        )
        self.artifacts.write_meta(
            {
                "sessionId": self.session_id,
                "request": self.request_payload,
                "stats": self.stats,
                "startTime": start_time.isoformat(),
                "endTime": end_time.isoformat(),
                "stoppedByUser": self.stop_event.is_set(),
                "costSummary": cost_summary,
                "fetchSummary": fetch_summary,
                "queryScoreSummary": self.query_score_runtime,
                "extractCache": self._extract_cache_stats(),
                "querySessionSummaries": self.query_session_summaries,
                "experimentConfig": self._experiment_config_snapshot(),
            }
        )

        await self._emit_event(
            "session.completed",
            {
                "stats": self.stats,
                "completedAt": end_time.isoformat(),
                "reason": "stopped_by_user" if self.stop_event.is_set() else "finished",
            },
        )

        return {
            "stats": self.stats,
            "visitedCount": len(visited),
            "allQueries": all_queries,
            "stoppedByUser": self.stop_event.is_set(),
            "costSummary": cost_summary,
            "fetchSummary": fetch_summary,
            "queryScoreSummary": self.query_score_runtime,
            "extractCache": self._extract_cache_stats(),
            "querySessionSummaries": self.query_session_summaries,
        }

    def _record_component_metric(self, metric: dict) -> None:
        row = {
            "timestamp": metric.get("timestamp", datetime.utcnow().isoformat()),
            "component": metric.get("component", "unknown"),
            "operation": metric.get("operation", "unknown"),
            "provider": metric.get("provider", ""),
            "model": metric.get("model", ""),
            "latencyMs": float(metric.get("latencyMs", 0.0) or 0.0),
            "promptTokens": int(metric.get("promptTokens", 0) or 0),
            "completionTokens": int(metric.get("completionTokens", 0) or 0),
            "totalTokens": int(metric.get("totalTokens", 0) or 0),
            "estimatedCostUsd": float(metric.get("estimatedCostUsd", 0.0) or 0.0),
            "status": metric.get("status", "ok"),
            "error": metric.get("error", ""),
            "meta": metric.get("meta", ""),
        }
        self.component_metrics.append(row)
        self.artifacts.append_component_metric(row)
        self.stats["tokensUsed"] = int(self.stats.get("tokensUsed", 0)) + int(row["totalTokens"])
        self.total_estimated_cost_usd += float(row["estimatedCostUsd"])
        if row["component"] == "classify" and row["operation"] == "relevance_short_circuit":
            skip_reason = str(metric.get("skipReason", "")).strip()
            if not skip_reason and row["meta"]:
                try:
                    meta_payload = json.loads(str(row["meta"]))
                    skip_reason = str(meta_payload.get("skipReason", "")).strip()
                except Exception:
                    skip_reason = ""
            self.classify_skip_counts[skip_reason or "unknown"] += 1

    def _bump_query_score_runtime(self, key: str, delta: int = 1) -> None:
        if key not in self.query_score_runtime:
            self.query_score_runtime[key] = 0
        self.query_score_runtime[key] = int(self.query_score_runtime.get(key, 0) or 0) + int(delta)

    def _set_query_score_max_inflight_age(self, age_ms: float) -> None:
        current = float(self.query_score_runtime.get("maxInFlightRequestAgeMs", 0.0) or 0.0)
        self.query_score_runtime["maxInFlightRequestAgeMs"] = max(current, float(age_ms))

    def _record_fetch_metric(self, metric: dict) -> None:
        row = {
            "timestamp": metric.get("timestamp", datetime.utcnow().isoformat()),
            "url": str(metric.get("url", "")),
            "domain": str(metric.get("domain", "")),
            "fetchMs": float(metric.get("fetchMs", 0.0) or 0.0),
            "statusCode": int(metric.get("statusCode", 0) or 0),
            "bytesRead": int(metric.get("bytesRead", 0) or 0),
            "contentType": str(metric.get("contentType", "")),
            "outcome": str(metric.get("outcome", "")),
            "retryCount": int(metric.get("retryCount", 0) or 0),
            "cooldownApplied": bool(metric.get("cooldownApplied", False)),
        }
        self.fetch_metrics.append(row)
        self.artifacts.append_fetch_metric(row)
        self._record_domain_fetch_outcome(
            url=row["url"],
            outcome=row["outcome"],
            status_code=row["statusCode"],
        )

    def _build_fetch_options(self) -> dict:
        return {
            "fetch_connect_timeout_s": self.fetch_connect_timeout_s,
            "fetch_read_timeout_s": self.fetch_read_timeout_s,
            "fetch_total_timeout_s": self.fetch_total_timeout_s,
            "fetch_max_bytes": self.fetch_max_bytes,
            "fetch_max_retries": self.fetch_max_retries,
            "fetch_retry_backoff_s": self.fetch_retry_backoff_s,
            "fetch_allowed_mime_prefixes": self.fetch_allowed_mime_prefixes,
            "enable_fetch_bounds": self.enable_fetch_bounds,
            "enable_mime_filter": self.enable_mime_filter,
            "enable_fetch_retry": self.enable_fetch_retry,
        }

    def _build_stage4_fetch_options(self) -> dict:
        # Stage-4 selected links are latency-critical; prefer tighter bounds and no retry tail.
        return {
            "fetch_connect_timeout_s": min(self.fetch_connect_timeout_s, 1.5),
            "fetch_read_timeout_s": min(self.fetch_read_timeout_s, 2.5),
            "fetch_total_timeout_s": min(self.fetch_total_timeout_s, 4.0),
            "fetch_max_bytes": self.fetch_max_bytes,
            "fetch_max_retries": 0,
            "fetch_retry_backoff_s": self.fetch_retry_backoff_s,
            "fetch_allowed_mime_prefixes": self.fetch_allowed_mime_prefixes,
            "enable_fetch_bounds": self.enable_fetch_bounds,
            "enable_mime_filter": self.enable_mime_filter,
            "enable_fetch_retry": False,
        }

    def _build_extract_options(self) -> dict:
        return {
            "extract_parallel_per_hop": self.extract_parallel_per_hop,
            "extract_connect_timeout_s": self.extract_connect_timeout_s,
            "extract_read_timeout_s": self.extract_read_timeout_s,
            "extract_total_timeout_s": self.extract_total_timeout_s,
            "extract_max_bytes": self.extract_max_bytes,
            "extract_max_retries": self.extract_max_retries,
            "extract_retry_backoff_s": self.extract_retry_backoff_s,
            "extract_allowed_mime_prefixes": self.extract_allowed_mime_prefixes,
            "enable_extract_bounds": self.enable_extract_bounds,
            "enable_extract_mime_filter": self.enable_extract_mime_filter,
            "enable_extract_retry": self.enable_extract_retry,
            "hop_link_limits": self.hop_link_limits,
        }

    def _extract_cache_get(self, url: str) -> dict[str, str] | None:
        if self.extract_link_cache is None:
            return None
        return self.extract_link_cache.get(str(url or ""))

    def _extract_cache_set(self, url: str, payload: dict[str, str]) -> None:
        if self.extract_link_cache is None:
            return
        self.extract_link_cache.set(str(url or ""), payload)

    def _extract_cache_stats(self) -> dict:
        if self.extract_link_cache is None:
            return {"enabled": False, "entries": 0}
        stats = self.extract_link_cache.stats()
        stats["enabled"] = True
        return stats

    def _should_expand_url_for_hop(self, url: str) -> tuple[bool, str]:
        if not self._is_domain_allowed(url):
            return False, "domain_filter"
        domain_reason = self._domain_block_reason(url)
        if domain_reason:
            return False, domain_reason
        return True, ""

    @staticmethod
    def _extract_domain(url: str) -> str:
        try:
            return urlparse(str(url or "")).netloc.lower()
        except Exception:
            return ""

    @staticmethod
    def _prefetch_skip_reason(url: str) -> str:
        raw = str(url or "").strip()
        lowered = raw.lower()
        if not lowered:
            return "empty_url"
        if not (lowered.startswith("http://") or lowered.startswith("https://")):
            return "non_http_scheme"
        if lowered.startswith("mailto:") or lowered.startswith("javascript:"):
            return "non_http_scheme"

        noisy_patterns = (
            "linkedin.com/sharing/share-offsite",
            "facebook.com/sharer",
            "twitter.com/intent/",
            "/cdn-cgi/l/email-protection",
        )
        for pattern in noisy_patterns:
            if pattern in lowered:
                return "share_or_tracking_url"

        blocked_suffixes = (
            ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico",
            ".mp4", ".mp3", ".avi", ".mov", ".wav",
            ".zip", ".rar", ".7z", ".tar", ".gz",
            ".css", ".js", ".xml",
        )
        for suffix in blocked_suffixes:
            if lowered.endswith(suffix):
                return "non_content_suffix"
        return ""

    async def _acquire_domain_slot(self, url: str) -> tuple[bool, bool, str]:
        domain = self._extract_domain(url)
        if not domain:
            return True, False, ""

        loop = asyncio.get_running_loop()
        while True:
            wait_s = 0.0
            async with self._domain_lock:
                now_ts = loop.time()
                cooldown_until = self._domain_cooldown_until_ts.get(domain, 0.0)
                if self.enable_domain_cooldown and cooldown_until > now_ts:
                    return False, True, "domain_cooldown"

                if self.max_pages_per_domain > 0 and self._domain_pages_used[domain] >= self.max_pages_per_domain:
                    return False, False, "domain_page_cap"

                last_access_ts = self._domain_last_access_ts.get(domain)
                if self.host_min_access_interval_s > 0 and last_access_ts is not None:
                    wait_s = max(0.0, (last_access_ts + self.host_min_access_interval_s) - now_ts)
                    if wait_s > 0.0:
                        pass
                    else:
                        self._domain_last_access_ts[domain] = now_ts
                        self._domain_pages_used[domain] += 1
                        return True, False, ""
                else:
                    self._domain_last_access_ts[domain] = now_ts
                    self._domain_pages_used[domain] += 1
                    return True, False, ""

            if wait_s <= 0.0:
                continue
            await asyncio.sleep(wait_s)
            if self.stop_event.is_set():
                return False, False, "stopped"

    def _record_domain_fetch_outcome(self, *, url: str, outcome: str, status_code: int = 0) -> None:
        if not self.enable_domain_cooldown:
            return
        domain = self._extract_domain(url)
        if not domain:
            return

        normalized = str(outcome or "").strip().lower()
        if normalized == "ok":
            self._domain_fail_streak[domain] = 0
            return

        if normalized in {"mime_filtered", "too_large"}:
            return

        if normalized == "http_error" and status_code in {400, 401, 403, 404, 410}:
            return

        self._domain_fail_streak[domain] += 1
        if self._domain_fail_streak[domain] < self.domain_fail_threshold:
            return

        self._domain_fail_streak[domain] = 0
        self._domain_cooldown_until_ts[domain] = asyncio.get_running_loop().time() + self.domain_fail_cooldown_s

    def _domain_block_reason(self, url: str) -> str:
        domain = self._extract_domain(url)
        if not domain:
            return ""

        now_ts = asyncio.get_running_loop().time()
        if self.enable_domain_cooldown:
            cooldown_until = self._domain_cooldown_until_ts.get(domain, 0.0)
            if cooldown_until > now_ts:
                return "domain_cooldown"
        if self.max_pages_per_domain > 0 and self._domain_pages_used.get(domain, 0) >= self.max_pages_per_domain:
            return "domain_page_cap"
        return ""

    def _record_domain_policy_skip(self, *, url: str, reason: str) -> None:
        skip_reason = f"Skipped by domain policy: {reason}"
        self._record_component_metric(
            {
                "component": "classify",
                "operation": "relevance_short_circuit",
                "provider": "local",
                "model": "rule_based",
                "latencyMs": 0.0,
                "promptTokens": 0,
                "completionTokens": 0,
                "totalTokens": 0,
                "estimatedCostUsd": 0.0,
                "status": "ok",
                "error": "",
                "skipReason": skip_reason,
                "meta": json.dumps(
                    {
                        "skipReason": skip_reason,
                        "url": url,
                    }
                ),
            }
        )

    async def _build_initial_queries(
        self,
        *,
        sys_prompt: str,
        query_prompt: str,
        query_gen_conv: list[dict],
    ) -> dict:
        if self.query_mode == "static":
            return {"queries": list(self.static_queries)}
        if self.query_mode == "seedfinder":
            return {"queries": list(self.seedfinder_queries)}
        return await asyncio.to_thread(
            query_generator,
            model_name=self.settings.default_model_name,
            sys_prompt=sys_prompt,
            task_prompt=query_prompt,
            conversation=query_gen_conv,
            metric_callback=self._record_component_metric,
        )

    async def _build_followup_queries(
        self,
        *,
        sys_prompt: str,
        next_prompt: str,
        query_gen_conv: list[dict],
    ) -> dict:
        if self.query_mode in {"static", "seedfinder"}:
            return {"queries": []}
        return await asyncio.to_thread(
            query_generator,
            model_name=self.settings.default_model_name,
            sys_prompt=sys_prompt,
            task_prompt=next_prompt,
            conversation=query_gen_conv,
            metric_callback=self._record_component_metric,
        )

    def _dedupe_queries(self, queries: list[str]) -> list[str]:
        deduped: list[str] = []
        for raw_query in queries:
            query = str(raw_query or "").strip()
            if not query:
                continue
            if query in self._query_history:
                continue
            self._query_history.add(query)
            deduped.append(query)
        return deduped

    def _budget_exhausted(self) -> bool:
        if self.page_budget > 0 and int(self.stats.get("pagesScanned", 0)) >= self.page_budget:
            return True
        if self.time_budget_sec > 0 and self.start_time_utc is not None:
            elapsed = (datetime.utcnow() - self.start_time_utc).total_seconds()
            if elapsed >= float(self.time_budget_sec):
                return True
        return False

    async def _classify_candidate_link(
        self,
        *,
        link: str,
        session: aiohttp.ClientSession,
        classify_prompt_key: str,
        classify_semaphore: asyncio.Semaphore,
        classify_llm_semaphore: asyncio.Semaphore,
    ) -> dict:
        granted, cooldown_applied, block_reason = await self._acquire_domain_slot(link)
        if not granted:
            reason = f"Skipped by domain policy: {block_reason}"
            self._record_component_metric(
                {
                    "component": "classify",
                    "operation": "relevance_short_circuit",
                    "provider": "local",
                    "model": "rule_based",
                    "latencyMs": 0.0,
                    "promptTokens": 0,
                    "completionTokens": 0,
                    "totalTokens": 0,
                    "estimatedCostUsd": 0.0,
                    "status": "ok",
                    "error": "",
                    "skipReason": reason,
                    "meta": json.dumps(
                        {
                            "skipReason": reason,
                            "cooldownApplied": bool(cooldown_applied),
                        }
                    ),
                }
            )
            return {
                "url": link,
                "__error__": True,
                "errorStage": "domain_policy",
                "errorMessage": block_reason,
                "cooldownApplied": cooldown_applied,
            }

        use_llm = self.classifier_mode == "llm"
        if self.classifier_mode == "hybrid":
            use_llm = not self.lightweight_ready
        if self.classifier_mode == "lightweight":
            use_llm = not self.lightweight_ready

        if use_llm:
            async with classify_semaphore:
                return await async_classify_link(
                    link,
                    self.topic,
                    classify_prompt_key,
                    session=session,
                    metric_callback=self._record_component_metric,
                    llm_semaphore=classify_llm_semaphore,
                    fetch_metric_callback=self._record_fetch_metric,
                    fetch_options=self._build_stage4_fetch_options(),
                    cooldown_applied=cooldown_applied,
                )
        return await self._classify_link_lightweight(
            link=link,
            session=session,
            cooldown_applied=cooldown_applied,
        )

    async def _classify_link_lightweight(
        self,
        *,
        link: str,
        session: aiohttp.ClientSession,
        cooldown_applied: bool = False,
    ) -> dict:
        started = datetime.utcnow()
        start_ts = asyncio.get_running_loop().time()
        try:
            title, body, fetch_meta = await async_get_content_from_url(
                link,
                session=session,
                return_metadata=True,
                **self._build_stage4_fetch_options(),
            )
            fetch_meta = fetch_meta or {}
            self._record_fetch_metric(
                {
                    "timestamp": started.isoformat(),
                    "url": link,
                    "domain": self._extract_domain(link),
                    "fetchMs": float(fetch_meta.get("latencyMs", 0.0) or 0.0),
                    "statusCode": int(fetch_meta.get("statusCode", 0) or 0),
                    "bytesRead": int(fetch_meta.get("bytesRead", 0) or 0),
                    "contentType": str(fetch_meta.get("contentType", "")),
                    "outcome": str(fetch_meta.get("outcome", "")),
                    "retryCount": int(fetch_meta.get("retryCount", 0) or 0),
                    "cooldownApplied": bool(cooldown_applied),
                }
            )
            if str(fetch_meta.get("outcome", "")) != "ok":
                reason = str(body or fetch_meta.get("error") or "No content extracted")[:300]
                self._record_component_metric(
                    {
                        "timestamp": started.isoformat(),
                        "component": "classify",
                        "operation": "relevance_short_circuit",
                        "provider": "local",
                        "model": "rule_based",
                        "latencyMs": float(fetch_meta.get("latencyMs", 0.0) or 0.0),
                        "promptTokens": 0,
                        "completionTokens": 0,
                        "totalTokens": 0,
                        "estimatedCostUsd": 0.0,
                        "status": "ok",
                        "error": "",
                        "skipReason": reason,
                        "meta": json.dumps(
                            {
                                "fetchOutcome": fetch_meta.get("outcome", ""),
                                "fetchStatusCode": fetch_meta.get("statusCode", 0),
                                "fetchBytesRead": fetch_meta.get("bytesRead", 0),
                                "fetchRetryCount": fetch_meta.get("retryCount", 0),
                                "skipReason": reason,
                            }
                        ),
                    }
                )
                return {
                    "url": link,
                    "__error__": True,
                    "errorStage": "fetch",
                    "errorMessage": reason,
                }
            if not str(title or "").strip() and not str(body or "").strip():
                reason = "No content extracted"
                self._record_component_metric(
                    {
                        "timestamp": started.isoformat(),
                        "component": "classify",
                        "operation": "relevance_short_circuit",
                        "provider": "local",
                        "model": "rule_based",
                        "latencyMs": float(fetch_meta.get("latencyMs", 0.0) or 0.0),
                        "promptTokens": 0,
                        "completionTokens": 0,
                        "totalTokens": 0,
                        "estimatedCostUsd": 0.0,
                        "status": "ok",
                        "error": "",
                        "skipReason": reason,
                        "meta": json.dumps(
                            {
                                "fetchOutcome": fetch_meta.get("outcome", ""),
                                "fetchStatusCode": fetch_meta.get("statusCode", 0),
                                "fetchBytesRead": fetch_meta.get("bytesRead", 0),
                                "fetchRetryCount": fetch_meta.get("retryCount", 0),
                                "skipReason": reason,
                            }
                        ),
                    }
                )
                return {
                    "url": link,
                    "__error__": True,
                    "errorStage": "fetch",
                    "errorMessage": reason,
                }
            text = self._compose_classification_text(title, body)
            pred, score = self.lightweight_classifier.predict(text)
            latency_ms = (asyncio.get_running_loop().time() - start_ts) * 1000.0
            self._record_component_metric(
                {
                    "timestamp": started.isoformat(),
                    "component": "classify",
                    "operation": "lightweight_classifier",
                    "provider": "local",
                    "model": self.lightweight_classifier.model_name,
                    "latencyMs": latency_ms,
                    "promptTokens": 0,
                    "completionTokens": 0,
                    "totalTokens": 0,
                    "estimatedCostUsd": 0.0,
                    "status": "ok",
                    "error": "",
                    "meta": json.dumps({"score": round(score, 4)}),
                }
            )
            return {
                "url": link,
                "pred": "Yes" if pred else "No",
                "reason": f"Local lightweight classifier score={score:.4f}",
                "title": title or "",
                "snippet": self._snippet_from_body(body),
            }
        except Exception as error:
            self._record_component_metric(
                {
                    "timestamp": started.isoformat(),
                    "component": "classify",
                    "operation": "lightweight_classifier",
                    "provider": "local",
                    "model": self.lightweight_classifier.model_name,
                    "latencyMs": (asyncio.get_running_loop().time() - start_ts) * 1000.0,
                    "promptTokens": 0,
                    "completionTokens": 0,
                    "totalTokens": 0,
                    "estimatedCostUsd": 0.0,
                    "status": "error",
                    "error": str(error)[:300],
                    "meta": "",
                }
            )
            return {
                "url": link,
                "__error__": True,
                "errorStage": "lightweight_classify",
                "errorMessage": str(error)[:300],
            }

    async def _prepare_batch_candidate(
        self,
        *,
        link: str,
        session: aiohttp.ClientSession,
    ) -> dict:
        started_iso = datetime.utcnow().isoformat()
        run_loop = asyncio.get_running_loop()

        granted, cooldown_applied, block_reason = await self._acquire_domain_slot(link)
        if not granted:
            reason = f"Skipped by domain policy: {block_reason}"
            self._record_component_metric(
                {
                    "timestamp": started_iso,
                    "component": "classify",
                    "operation": "relevance_short_circuit",
                    "provider": "local",
                    "model": "rule_based",
                    "latencyMs": 0.0,
                    "promptTokens": 0,
                    "completionTokens": 0,
                    "totalTokens": 0,
                    "estimatedCostUsd": 0.0,
                    "status": "ok",
                    "error": "",
                    "skipReason": reason,
                    "meta": json.dumps(
                        {
                            "skipReason": reason,
                            "cooldownApplied": bool(cooldown_applied),
                            "mode": "batch_prepare",
                        }
                    ),
                }
            )
            return {
                "kind": "error",
                "result": {
                    "url": link,
                    "__error__": True,
                    "errorStage": "domain_policy",
                    "errorMessage": block_reason,
                    "cooldownApplied": cooldown_applied,
                },
            }

        title, body, fetch_meta = await async_get_content_from_url(
            link,
            session=session,
            return_metadata=True,
            **self._build_stage4_fetch_options(),
        )
        fetch_meta = fetch_meta or {}
        fetch_outcome = str(fetch_meta.get("outcome", "network_error"))
        fetch_status = int(fetch_meta.get("statusCode", 0) or 0)
        fetch_bytes = int(fetch_meta.get("bytesRead", 0) or 0)
        fetch_type = str(fetch_meta.get("contentType", "") or "")
        fetch_retries = int(fetch_meta.get("retryCount", 0) or 0)
        fetch_ms = float(fetch_meta.get("latencyMs", 0.0) or 0.0)
        self._record_fetch_metric(
            {
                "timestamp": started_iso,
                "url": link,
                "domain": self._extract_domain(link),
                "fetchMs": fetch_ms,
                "statusCode": fetch_status,
                "bytesRead": fetch_bytes,
                "contentType": fetch_type,
                "outcome": fetch_outcome,
                "retryCount": fetch_retries,
                "cooldownApplied": bool(cooldown_applied),
            }
        )

        title_txt = str(title or "").strip()
        body_txt = str(body or "").strip()
        if fetch_outcome != "ok":
            reason = str(body or fetch_meta.get("error") or f"Fetch outcome: {fetch_outcome}")[:300]
            self._record_component_metric(
                {
                    "timestamp": started_iso,
                    "component": "classify",
                    "operation": "relevance_short_circuit",
                    "provider": "local",
                    "model": "rule_based",
                    "latencyMs": fetch_ms,
                    "promptTokens": 0,
                    "completionTokens": 0,
                    "totalTokens": 0,
                    "estimatedCostUsd": 0.0,
                    "status": "ok",
                    "error": "",
                    "skipReason": reason,
                    "meta": json.dumps(
                        {
                            "skipReason": reason,
                            "fetchOutcome": fetch_outcome,
                            "fetchStatusCode": fetch_status,
                            "fetchBytesRead": fetch_bytes,
                            "fetchRetryCount": fetch_retries,
                            "mode": "batch_prepare",
                        }
                    ),
                }
            )
            return {
                "kind": "result",
                "result": {
                    "url": link,
                    "pred": "No",
                    "reason": reason,
                    "title": title_txt,
                    "snippet": self._snippet_from_body(body_txt),
                    "fetchOutcome": fetch_outcome,
                    "fetchStatusCode": fetch_status,
                    "fetchBytesRead": fetch_bytes,
                    "fetchRetryCount": fetch_retries,
                    "fetchContentType": fetch_type,
                },
            }

        if not title_txt and not body_txt:
            reason = "No content extracted"
            self._record_component_metric(
                {
                    "timestamp": started_iso,
                    "component": "classify",
                    "operation": "relevance_short_circuit",
                    "provider": "local",
                    "model": "rule_based",
                    "latencyMs": fetch_ms,
                    "promptTokens": 0,
                    "completionTokens": 0,
                    "totalTokens": 0,
                    "estimatedCostUsd": 0.0,
                    "status": "ok",
                    "error": "",
                    "skipReason": reason,
                    "meta": json.dumps(
                        {
                            "skipReason": reason,
                            "fetchOutcome": fetch_outcome,
                            "fetchStatusCode": fetch_status,
                            "fetchBytesRead": fetch_bytes,
                            "fetchRetryCount": fetch_retries,
                            "mode": "batch_prepare",
                        }
                    ),
                }
            )
            return {
                "kind": "result",
                "result": {
                    "url": link,
                    "pred": "No",
                    "reason": reason,
                    "title": title_txt,
                    "snippet": "",
                    "fetchOutcome": fetch_outcome,
                    "fetchStatusCode": fetch_status,
                    "fetchBytesRead": fetch_bytes,
                    "fetchRetryCount": fetch_retries,
                    "fetchContentType": fetch_type,
                },
            }

        snippet = self._snippet_from_body(body_txt)
        if not snippet:
            snippet = body_txt[:280]

        return {
            "kind": "ready",
            "item": {
                "url": link,
                "title": title_txt,
                "snippet": snippet,
                "fetchOutcome": fetch_outcome,
                "fetchStatusCode": fetch_status,
                "fetchBytesRead": fetch_bytes,
                "fetchRetryCount": fetch_retries,
                "fetchContentType": fetch_type,
                "fetchedAtTs": run_loop.time(),
            },
        }

    async def _classify_snippet_individual(
        self,
        *,
        item: dict,
        classify_prompt_key: str,
        classify_llm_semaphore: asyncio.Semaphore,
    ) -> dict:
        run_loop = asyncio.get_running_loop()
        started_ts = run_loop.time()
        wait_started = run_loop.time()
        async with classify_llm_semaphore:
            wait_ms = (run_loop.time() - wait_started) * 1000.0
            llm_started = run_loop.time()
            judge_res, llm_meta = await async_web_content_judger(
                item.get("title", ""),
                item.get("snippet", ""),
                topic_seed=self.topic,
                model_name=self.settings.default_model_name,
                task_prompt_name=classify_prompt_key,
                metric_callback=None,
                return_llm_meta=True,
            )
            llm_ms = (run_loop.time() - llm_started) * 1000.0

        llm_meta = llm_meta or {}
        llm_status = str(llm_meta.get("status", "ok"))
        llm_error = str(llm_meta.get("error", ""))
        pred_raw = judge_res.get("pred") if isinstance(judge_res, dict) else ""
        pred_norm = normalize_prediction(pred_raw)
        pred = "Yes" if pred_norm == "yes" else "No"
        reason = str((judge_res or {}).get("reason", "")).strip() if isinstance(judge_res, dict) else ""
        if not reason:
            reason = "Batch classify fallback produced empty reason."

        self._record_component_metric(
            {
                "component": "classify",
                "operation": "relevance_judge_batch_fallback",
                "status": llm_status,
                "provider": llm_meta.get("provider", ""),
                "model": llm_meta.get("model", self.settings.default_model_name),
                "latencyMs": llm_meta.get("latencyMs", llm_ms),
                "promptTokens": llm_meta.get("promptTokens", 0),
                "completionTokens": llm_meta.get("completionTokens", 0),
                "totalTokens": llm_meta.get("totalTokens", 0),
                "estimatedCostUsd": llm_meta.get("estimatedCostUsd", 0.0),
                "error": llm_error,
                "meta": json.dumps(
                    {
                        "url": item.get("url", ""),
                        "batchFallback": True,
                        "llmWaitMs": round(wait_ms, 3),
                        "llmMs": round(llm_ms, 3),
                        "totalMs": round((run_loop.time() - started_ts) * 1000.0, 3),
                    }
                ),
            }
        )
        return {
            "url": item.get("url", ""),
            "pred": pred,
            "reason": reason[:220],
            "title": item.get("title", ""),
            "snippet": item.get("snippet", ""),
            "fetchOutcome": item.get("fetchOutcome", ""),
            "fetchStatusCode": item.get("fetchStatusCode", 0),
            "fetchBytesRead": item.get("fetchBytesRead", 0),
            "fetchRetryCount": item.get("fetchRetryCount", 0),
            "fetchContentType": item.get("fetchContentType", ""),
        }

    async def _classify_links_batched(
        self,
        *,
        links: list[str],
        session: aiohttp.ClientSession,
        classify_prompt_key: str,
        classify_llm_semaphore: asyncio.Semaphore,
        max_results: int,
    ) -> list[dict]:
        if not links:
            return []

        prepare_semaphore = asyncio.Semaphore(max(1, int(self.frontier_fetch_parallel)))

        async def _prepare_with_limit(link: str):
            async with prepare_semaphore:
                return await self._prepare_batch_candidate(link=link, session=session)

        def _as_result(payload: dict) -> dict | None:
            if not isinstance(payload, dict):
                return None
            value = payload.get("result")
            return value if isinstance(value, dict) else None

        async def _flush_batch(chunk: list[dict]) -> list[dict]:
            if not chunk:
                return []

            flush_ts = asyncio.get_running_loop().time()
            wait_values = []
            for item in chunk:
                wait_ms = max(0.0, (flush_ts - float(item.get("fetchedAtTs", flush_ts) or flush_ts)) * 1000.0)
                wait_values.append(wait_ms)
                self._record_component_metric(
                    {
                        "component": "orchestration",
                        "operation": "classify_batch_wait_for_fetch_item",
                        "provider": "local",
                        "model": "batch_fill",
                        "latencyMs": wait_ms,
                        "promptTokens": 0,
                        "completionTokens": 0,
                        "totalTokens": 0,
                        "estimatedCostUsd": 0.0,
                        "status": "ok",
                        "error": "",
                        "meta": json.dumps({"url": item.get("url", ""), "batchSize": len(chunk)}),
                    }
                )
            self._record_component_metric(
                {
                    "component": "orchestration",
                    "operation": "classify_batch_wait_for_fetch_group",
                    "provider": "local",
                    "model": "batch_fill",
                    "latencyMs": max(wait_values) if wait_values else 0.0,
                    "promptTokens": 0,
                    "completionTokens": 0,
                    "totalTokens": 0,
                    "estimatedCostUsd": 0.0,
                    "status": "ok",
                    "error": "",
                    "meta": json.dumps(
                        {
                            "batchSize": len(chunk),
                            "avgWaitMs": round(sum(wait_values) / max(1, len(wait_values)), 3),
                            "maxWaitMs": round(max(wait_values) if wait_values else 0.0, 3),
                        }
                    ),
                }
            )

            batch_payload = []
            id_to_item: dict[str, dict] = {}
            for idx, item in enumerate(chunk):
                item_id = str(idx)
                id_to_item[item_id] = item
                batch_payload.append({"id": item_id, "title": item.get("title", ""), "snippet": item.get("snippet", "")})

            run_loop = asyncio.get_running_loop()
            llm_wait_started = run_loop.time()
            async with classify_llm_semaphore:
                llm_wait_ms = (run_loop.time() - llm_wait_started) * 1000.0
                llm_started = run_loop.time()
                batch_map, llm_meta = await async_batch_snippet_relevance_judger(
                    items=batch_payload,
                    topic_seed=self.topic,
                    model_name=self.settings.default_model_name,
                    return_llm_meta=True,
                )
                llm_ms = (run_loop.time() - llm_started) * 1000.0

            self._record_component_metric(
                {
                    "component": "orchestration",
                    "operation": "semaphore_wait_classify_llm",
                    "provider": "local",
                    "model": "asyncio_semaphore",
                    "latencyMs": llm_wait_ms,
                    "promptTokens": 0,
                    "completionTokens": 0,
                    "totalTokens": 0,
                    "estimatedCostUsd": 0.0,
                    "status": "ok",
                    "error": "",
                    "meta": json.dumps({"batchSize": len(chunk), "mode": "batch_classify"}),
                }
            )

            llm_meta = llm_meta or {}
            llm_status = str(llm_meta.get("status", "ok"))
            llm_error = str(llm_meta.get("error", ""))
            self._record_component_metric(
                {
                    "component": "classify",
                    "operation": "relevance_judge_batch",
                    "status": llm_status,
                    "provider": llm_meta.get("provider", ""),
                    "model": llm_meta.get("model", self.settings.default_model_name),
                    "latencyMs": llm_meta.get("latencyMs", llm_ms),
                    "promptTokens": llm_meta.get("promptTokens", 0),
                    "completionTokens": llm_meta.get("completionTokens", 0),
                    "totalTokens": llm_meta.get("totalTokens", 0),
                    "estimatedCostUsd": llm_meta.get("estimatedCostUsd", 0.0),
                    "error": llm_error,
                    "meta": json.dumps(
                        {
                            "batchSize": len(chunk),
                            "llmWaitMs": round(llm_wait_ms, 3),
                            "llmMs": round(llm_ms, 3),
                        }
                    ),
                }
            )

            output: list[dict] = []
            mapped_count = 0
            batch_map = batch_map or {}
            for item_id, row in batch_map.items():
                if item_id not in id_to_item:
                    continue
                if not isinstance(row, dict):
                    continue
                pred = "Yes" if normalize_prediction(row.get("pred")) == "yes" else "No"
                reason = str(row.get("reason", "") or "").strip() or "Batch classification returned no reason."
                item = id_to_item[item_id]
                output.append(
                    {
                        "url": item.get("url", ""),
                        "pred": pred,
                        "reason": reason[:220],
                        "title": item.get("title", ""),
                        "snippet": item.get("snippet", ""),
                        "fetchOutcome": item.get("fetchOutcome", ""),
                        "fetchStatusCode": item.get("fetchStatusCode", 0),
                        "fetchBytesRead": item.get("fetchBytesRead", 0),
                        "fetchRetryCount": item.get("fetchRetryCount", 0),
                        "fetchContentType": item.get("fetchContentType", ""),
                    }
                )
                mapped_count += 1

            if mapped_count < len(chunk) or llm_status != "ok":
                missing_ids = [item_id for item_id in id_to_item.keys() if item_id not in batch_map]
                self._record_component_metric(
                    {
                        "component": "classify",
                        "operation": "relevance_judge_batch_partial_fallback",
                        "provider": "local",
                        "model": self.settings.default_model_name,
                        "latencyMs": 0.0,
                        "promptTokens": 0,
                        "completionTokens": 0,
                        "totalTokens": 0,
                        "estimatedCostUsd": 0.0,
                        "status": "ok",
                        "error": "",
                        "meta": json.dumps(
                            {
                                "batchSize": len(chunk),
                                "mappedCount": mapped_count,
                                "fallbackCount": len(missing_ids),
                                "batchStatus": llm_status,
                            }
                        ),
                    }
                )
                for item_id in missing_ids:
                    fallback_result = await self._classify_snippet_individual(
                        item=id_to_item[item_id],
                        classify_prompt_key=classify_prompt_key,
                        classify_llm_semaphore=classify_llm_semaphore,
                    )
                    output.append(fallback_result)
            return output

        selected_links = list(links)[: max(1, int(self.frontier_select_count))]
        prep_tasks = [asyncio.create_task(_prepare_with_limit(link)) for link in selected_links]
        pending = set(prep_tasks)
        if not pending:
            return []

        run_loop = asyncio.get_running_loop()
        base_flush_interval_s = self.classify_batch_flush_timeout_ms / 1000.0
        fast_flush_interval_s = min(
            base_flush_interval_s,
            self.classify_batch_fast_flush_timeout_ms / 1000.0,
        )
        min_batch_size = min(5, max(1, int(self.classify_batch_min_size)))
        next_flush_ts = run_loop.time() + base_flush_interval_s
        batch_size = min(5, max(1, int(self.classify_batch_size)))
        quick_buffer: list[dict] = []
        results: list[dict] = []

        async def _append_result_if_room(result: dict) -> None:
            if len(results) < max_results and isinstance(result, dict):
                results.append(result)

        while (pending or quick_buffer) and len(results) < max_results:
            ready_count = len(quick_buffer)
            active_flush_interval_s = (
                fast_flush_interval_s
                if ready_count >= max(2, min_batch_size)
                else base_flush_interval_s
            )
            next_flush_ts = min(next_flush_ts, run_loop.time() + active_flush_interval_s)

            if pending:
                timeout = max(0.0, next_flush_ts - run_loop.time())
                done, pending = await asyncio.wait(
                    pending,
                    timeout=timeout,
                    return_when=asyncio.FIRST_COMPLETED,
                )
            else:
                done = set()

            for task in done:
                try:
                    payload = await task
                except Exception as error:
                    self._record_runtime_exception(error)
                    continue
                if not isinstance(payload, dict):
                    continue
                kind = payload.get("kind")
                if kind == "ready" and isinstance(payload.get("item"), dict):
                    item = payload["item"]
                    quick_buffer.append(item)
                else:
                    immediate = _as_result(payload)
                    if immediate is not None:
                        await _append_result_if_room(immediate)

            # Flush full batches immediately (do not wait for timer when enough ready).
            while len(quick_buffer) >= batch_size and len(results) < max_results:
                chunk = quick_buffer[:batch_size]
                quick_buffer = quick_buffer[batch_size:]
                for out in await _flush_batch(chunk):
                    await _append_result_if_room(out)

            # Rolling timer: every interval, flush whatever is currently available.
            now_ts = run_loop.time()
            if now_ts >= next_flush_ts:
                # On timeout, flush only when we have enough items to avoid tiny batches,
                # unless all pending fetches are done (then flush remainder).
                if len(quick_buffer) >= min_batch_size or not pending:
                    while quick_buffer and len(results) < max_results:
                        take = min(batch_size, len(quick_buffer))
                        chunk = quick_buffer[:take]
                        quick_buffer = quick_buffer[take:]
                        for out in await _flush_batch(chunk):
                            await _append_result_if_room(out)
                next_interval = (
                    fast_flush_interval_s
                    if len(quick_buffer) >= max(2, min_batch_size)
                    else base_flush_interval_s
                )
                next_flush_ts = now_ts + next_interval

        # Drain any remaining buffered items when all fetches complete.
        while quick_buffer and len(results) < max_results:
            chunk = quick_buffer[:batch_size]
            quick_buffer = quick_buffer[batch_size:]
            for out in await _flush_batch(chunk):
                await _append_result_if_room(out)

        # Ensure pending tasks do not leak.
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

        return results[:max_results]

    def _update_lightweight_model_from_batch(self, judge_results: list[dict]) -> None:
        if self.classifier_mode == "llm":
            return
        if self.lightweight_ready:
            return

        for item in judge_results:
            if not isinstance(item, dict):
                continue
            pred = normalize_prediction(item.get("pred"))
            if pred not in {"yes", "no"}:
                continue
            text = self._compose_classification_text(item.get("title"), item.get("snippet"))
            if not text.strip():
                continue
            label = 1 if pred == "yes" else 0
            self._warmup_train_rows.append((text, label))

        if len(self._warmup_train_rows) < max(10, self.warmup_pages):
            return

        labels = [label for _, label in self._warmup_train_rows]
        if len(set(labels)) < 2:
            return

        texts = [text for text, _ in self._warmup_train_rows]
        fit_start = time.perf_counter()
        self.lightweight_classifier.fit(texts, labels)
        self.lightweight_ready = True
        self._record_component_metric(
            {
                "component": "classify",
                "operation": "lightweight_train",
                "provider": "local",
                "model": self.lightweight_classifier.model_name,
                "latencyMs": (time.perf_counter() - fit_start) * 1000.0,
                "promptTokens": 0,
                "completionTokens": 0,
                "totalTokens": 0,
                "estimatedCostUsd": 0.0,
                "status": "ok",
                "error": "",
                "meta": json.dumps({"trainRows": len(texts)}),
            }
        )

    @staticmethod
    def _compose_classification_text(title: str | None, body: str | None) -> str:
        title_txt = str(title or "").strip()
        body_txt = str(body or "").strip()
        if len(body_txt) > 6000:
            body_txt = body_txt[:6000]
        return f"{title_txt}\n\n{body_txt}"

    @staticmethod
    def _snippet_from_body(body: str | None) -> str:
        raw = " ".join(str(body or "").split())
        if len(raw) <= 280:
            return raw
        return raw[:277].rstrip() + "..."

    @staticmethod
    def _percentile(values: list[float], q: float) -> float:
        if not values:
            return 0.0
        sorted_values = sorted(float(item) for item in values)
        if len(sorted_values) == 1:
            return float(sorted_values[0])
        q = min(1.0, max(0.0, float(q)))
        idx = int(round(q * (len(sorted_values) - 1)))
        idx = max(0, min(idx, len(sorted_values) - 1))
        return float(sorted_values[idx])

    def _build_fetch_summary(self) -> dict:
        fetch_latencies = [float(row.get("fetchMs", 0.0) or 0.0) for row in self.fetch_metrics]
        outcome_counts: dict[str, int] = defaultdict(int)
        for row in self.fetch_metrics:
            outcome_counts[str(row.get("outcome", "unknown"))] += 1
        outlier_threshold_ms = 10_000.0
        outlier_count = sum(1 for value in fetch_latencies if value > outlier_threshold_ms)
        p50 = round(self._percentile(fetch_latencies, 0.50), 3)
        p95 = round(self._percentile(fetch_latencies, 0.95), 3)
        p99 = round(self._percentile(fetch_latencies, 0.99), 3)
        return {
            "fetchCalls": len(self.fetch_metrics),
            "fetchP50Ms": p50,
            "fetchP95Ms": p95,
            "fetchP99Ms": p99,
            "fetch_p50_ms": p50,
            "fetch_p95_ms": p95,
            "fetch_p99_ms": p99,
            "fetchOutlierThresholdMs": outlier_threshold_ms,
            "fetchOutlierCount": int(outlier_count),
            "fetch_outlier_count": int(outlier_count),
            "fetchOutcomeCounts": dict(outcome_counts),
            "classifySkipCounts": dict(self.classify_skip_counts),
        }

    def _build_cost_summary(self, *, start_time: datetime, end_time: datetime) -> dict:
        elapsed_seconds = max(0.001, (end_time - start_time).total_seconds())
        pages_scanned = int(self.stats.get("pagesScanned", 0))
        relevant_found = int(self.stats.get("relevantFound", 0))
        component_rollup: dict[str, dict] = {}
        for row in self.component_metrics:
            key = row.get("component", "unknown")
            agg = component_rollup.setdefault(
                key,
                {"calls": 0, "latencyMs": 0.0, "tokens": 0, "costUsd": 0.0},
            )
            agg["calls"] += 1
            agg["latencyMs"] += float(row.get("latencyMs", 0.0) or 0.0)
            agg["tokens"] += int(row.get("totalTokens", 0) or 0)
            agg["costUsd"] += float(row.get("estimatedCostUsd", 0.0) or 0.0)
        return {
            "elapsedSeconds": round(elapsed_seconds, 3),
            "pagesPerSecond": round(pages_scanned / elapsed_seconds, 6),
            "relevantPagesPerHour": round((relevant_found / elapsed_seconds) * 3600.0, 3),
            "totalTokens": int(self.stats.get("tokensUsed", 0)),
            "estimatedCostUsd": round(float(self.total_estimated_cost_usd), 8),
            "costPerRelevantPageUsd": round(
                float(self.total_estimated_cost_usd) / max(1, relevant_found),
                8,
            ),
            "componentBreakdown": component_rollup,
        }

    def _experiment_config_snapshot(self) -> dict:
        return {
            "reseedEnabled": self.reseed_enabled,
            "queryMode": self.query_mode,
            "classifierMode": self.classifier_mode,
            "policyMode": self.policy_mode,
            "promptProfile": self.prompt_profile,
            "promptsPath": self.prompts_path_override or str(self.settings.prompts_path),
            "promptOverridesCount": len(self.prompt_overrides) if isinstance(self.prompt_overrides, dict) else 0,
            "warmupPages": self.warmup_pages,
            "timeBudgetSec": self.time_budget_sec,
            "pageBudget": self.page_budget,
            "maxParallelTargets": self.max_parallel_targets,
            "maxParallelCrawlTargets": self.max_parallel_crawl_targets,
            "searchResultClassifyLimit": self.classify_limit,
            "queryClassifyTarget": self.query_classify_target,
            "queryFallbackClassifyTarget": self.query_fallback_classify_target,
            "queryMinPositiveSeeds": self.query_min_positive_seeds,
            "queryExtractCapMultiplier": self.query_extract_cap_multiplier,
            "scoreBatchSize": self.score_batch_size,
            "scoreRequestTimeoutS": self.score_request_timeout_s,
            "classifyRequestTimeoutS": self.classify_request_timeout_s,
            "maxFrontierBufferItems": self.max_frontier_buffer_items,
            "classifyBatchSize": self.classify_batch_size,
            "frontierSelectCount": self.frontier_select_count,
            "frontierClassifyCap": self.frontier_classify_cap,
            "classifyBatchFlushTimeoutMs": self.classify_batch_flush_timeout_ms,
            "classifyBatchFastFlushTimeoutMs": self.classify_batch_fast_flush_timeout_ms,
            "classifyBatchMinSize": self.classify_batch_min_size,
            "frontierFetchParallel": self.frontier_fetch_parallel,
            "staticQueriesCount": len(self.static_queries),
            "seedfinderQueriesCount": len(self.seedfinder_queries),
            "hopLinkLimits": list(self.hop_link_limits),
            "extractParallelPerHop": self.extract_parallel_per_hop,
            "extractConnectTimeoutS": self.extract_connect_timeout_s,
            "extractReadTimeoutS": self.extract_read_timeout_s,
            "extractTotalTimeoutS": self.extract_total_timeout_s,
            "extractMaxBytes": self.extract_max_bytes,
            "extractMaxRetries": self.extract_max_retries,
            "extractRetryBackoffS": self.extract_retry_backoff_s,
            "extractAllowedMimePrefixes": list(self.extract_allowed_mime_prefixes),
            "enableExtractBounds": self.enable_extract_bounds,
            "enableExtractMimeFilter": self.enable_extract_mime_filter,
            "enableExtractRetry": self.enable_extract_retry,
            "extractCacheEnabled": self.extract_cache_enabled,
            "extractCacheMaxEntries": self.extract_cache_max_entries,
            "extractCacheTtlS": self.extract_cache_ttl_s,
            "fetchConnectTimeoutS": self.fetch_connect_timeout_s,
            "fetchReadTimeoutS": self.fetch_read_timeout_s,
            "fetchTotalTimeoutS": self.fetch_total_timeout_s,
            "fetchMaxBytes": self.fetch_max_bytes,
            "fetchMaxRetries": self.fetch_max_retries,
            "fetchRetryBackoffS": self.fetch_retry_backoff_s,
            "fetchAllowedMimePrefixes": list(self.fetch_allowed_mime_prefixes),
            "hostMinAccessIntervalS": self.host_min_access_interval_s,
            "maxPagesPerDomain": self.max_pages_per_domain,
            "domainFailCooldownS": self.domain_fail_cooldown_s,
            "domainFailThreshold": self.domain_fail_threshold,
            "enableFetchBounds": self.enable_fetch_bounds,
            "enableDomainCooldown": self.enable_domain_cooldown,
            "enableMimeFilter": self.enable_mime_filter,
            "enableFetchRetry": self.enable_fetch_retry,
        }

    def _load_prompts(self) -> dict:
        prompts_path = Path(self.prompts_path_override) if self.prompts_path_override else self.settings.prompts_path
        with prompts_path.open("r", encoding="utf-8") as file:
            payload = json.load(file)
        if not isinstance(payload, dict):
            raise RuntimeError(f"Prompt file must be a JSON object: {prompts_path}")
        if isinstance(self.prompt_overrides, dict):
            payload.update(self.prompt_overrides)
        return payload

    @staticmethod
    def _render_prompt_template(template: str, **values) -> str:
        rendered = str(template)

        # Some templates may carry escaped newlines from JSON editing.
        rendered = rendered.replace("\\n", "\n")

        # Do literal token replacement instead of str.format to avoid conflicts
        # with JSON braces that appear in prompt instructions.
        for key, value in values.items():
            rendered = rendered.replace("{" + key + "}", str(value))

        return rendered

    def _is_domain_allowed(self, url: str) -> bool:
        if not self.allowed_domains:
            return True

        lowered = url.lower()
        return any(domain in lowered for domain in self.allowed_domains)

    def _select_bootstrap_results(self, judge_results: list[dict]) -> list[dict]:
        selected: list[dict] = []
        seen_urls: set[str] = set()

        for result in judge_results:
            if len(selected) >= self.review_gate_limit:
                break

            url = result.get("url") if isinstance(result, dict) else None
            if not url:
                continue
            if url in seen_urls or url in self.seen_result_urls:
                continue
            if not self._is_domain_allowed(url):
                continue

            seen_urls.add(url)
            selected.append(result)

        return selected

    async def _query_without_crawl(self, query: str, visited: set[str], classify_prompt_key: str):
        if self.stop_event.is_set():
            return [], set(), ""
        await self._wait_if_paused()
        if self.stop_event.is_set():
            return [], set(), ""

        judge_results_all: list[dict] = []
        visited_all: set[str] = set()

        async with aiohttp.ClientSession() as session:
            classify_semaphore, _crawl_semaphore, classify_llm_semaphore, _frontier_llm_semaphore = self._build_stage_semaphores()

            async def classify_fn(link: str, active_session: aiohttp.ClientSession):
                return await self._classify_candidate_link(
                    link=link,
                    session=active_session,
                    classify_prompt_key=classify_prompt_key,
                    classify_semaphore=classify_semaphore,
                    classify_llm_semaphore=classify_llm_semaphore,
                )

            query_results, search_urls = await async_judge_query_urls(
                query,
                visited,
                task_prompt_name=classify_prompt_key,
                topic=self.topic,
                session=session,
                fail_fast=True,
                classify_limit=self.classify_limit,
                metric_callback=self._record_component_metric,
                classify_fn=classify_fn,
            )

        for result in query_results:
            await self._wait_if_paused()
            if self.stop_event.is_set():
                return judge_results_all, visited_all, ""

            if not isinstance(result, dict):
                self._record_runtime_exception(
                    RuntimeError(f"Unexpected query classification payload type: {type(result)}")
                )
                continue

            if result.get("__error__"):
                if str(result.get("errorStage", "")) == "domain_policy":
                    self._record_url_attempt()
                else:
                    self._record_url_error(result)
                continue

            if self._is_domain_allowed(result.get("url", "")):
                self._record_url_attempt()
                judge_results_all.append(result)

        for url in search_urls:
            if self._is_domain_allowed(url):
                visited_all.add(url)

        feedback = await asyncio.to_thread(
            query_feedback,
            query,
            judge_results_all,
            "query",
        )
        self._update_lightweight_model_from_batch(judge_results_all)
        return judge_results_all, visited_all, feedback

    async def _query_and_crawl(self, query: str, visited: set[str], classify_prompt_key: str):
        if self.stop_event.is_set():
            return [], set(), ""
        await self._wait_if_paused()
        if self.stop_event.is_set():
            return [], set(), ""
        if self.policy_mode == "query_score":
            return await self._query_and_crawl_query_score(
                query=query,
                visited=visited,
                classify_prompt_key=classify_prompt_key,
            )

        judge_results_all = []
        visited_all = set()

        async with aiohttp.ClientSession() as session:
            classify_semaphore, crawl_semaphore, classify_llm_semaphore, frontier_llm_semaphore = self._build_stage_semaphores()

            async def classify_fn(link: str, active_session: aiohttp.ClientSession):
                return await self._classify_candidate_link(
                    link=link,
                    session=active_session,
                    classify_prompt_key=classify_prompt_key,
                    classify_semaphore=classify_semaphore,
                    classify_llm_semaphore=classify_llm_semaphore,
                )

            query_results, search_urls = await async_judge_query_urls(
                query,
                visited,
                task_prompt_name=classify_prompt_key,
                topic=self.topic,
                session=session,
                fail_fast=True,
                classify_limit=self.classify_limit,
                metric_callback=self._record_component_metric,
                classify_fn=classify_fn,
            )

            for result in query_results:
                await self._wait_if_paused()
                if self.stop_event.is_set():
                    return judge_results_all, visited_all, ""

                if not isinstance(result, dict):
                    self._record_runtime_exception(
                        RuntimeError(f"Unexpected query classification payload type: {type(result)}")
                    )
                    continue

                if result.get("__error__"):
                    if str(result.get("errorStage", "")) == "domain_policy":
                        self._record_url_attempt()
                    else:
                        self._record_url_error(result)
                    continue

                if self._is_domain_allowed(result.get("url", "")):
                    self._record_url_attempt()
                    judge_results_all.append(result)

            crawl_targets: list[str] = []
            for url in search_urls:
                if not self._is_domain_allowed(url):
                    continue
                block_reason = self._domain_block_reason(url)
                if block_reason:
                    self._record_domain_policy_skip(url=url, reason=block_reason)
                    self._record_url_attempt()
                    continue
                crawl_targets.append(url)

            tasks = []
            for url in crawl_targets:
                if self.stop_event.is_set():
                    break
                await self._wait_if_paused()
                if self.stop_event.is_set():
                    break
                if self._budget_exhausted():
                    break

                task = asyncio.create_task(
                    self._query_page(
                        url,
                        visited=visited,
                        classify_prompt_key=classify_prompt_key,
                        session=session,
                        classify_semaphore=classify_semaphore,
                        crawl_semaphore=crawl_semaphore,
                        classify_llm_semaphore=classify_llm_semaphore,
                        frontier_llm_semaphore=frontier_llm_semaphore,
                    )
                )
                tasks.append(task)

            responses = await asyncio.gather(*tasks, return_exceptions=True)

            for response in responses:
                if isinstance(response, Exception):
                    self._record_runtime_exception(response)
                    continue
                if not response:
                    continue

                judge_results, links = response
                judge_results_all.extend(judge_results)
                visited_all.update(links)

        feedback = await asyncio.to_thread(
            query_feedback,
            query,
            judge_results_all,
            "query",
        )
        self._update_lightweight_model_from_batch(judge_results_all)
        return judge_results_all, visited_all, feedback

    @staticmethod
    def _is_positive_result(result: dict) -> bool:
        if not isinstance(result, dict):
            return False
        return normalize_prediction(result.get("pred")) == "yes"

    async def _query_and_crawl_query_score(
        self,
        *,
        query: str,
        visited: set[str],
        classify_prompt_key: str,
    ):
        judge_results_all: list[dict] = []
        visited_all: set[str] = set()
        query_state = _QuerySessionState(
            query=query,
            mode=self.policy_mode,
            classify_target=max(1, int(self.query_classify_target)),
            fallback_classify_target=max(1, int(self.query_fallback_classify_target)),
            min_positive_seeds=max(1, int(self.query_min_positive_seeds)),
            extract_cap_multiplier=float(self.query_extract_cap_multiplier),
            extract_link_cap=max(
                1,
                int(round(max(1, int(self.query_classify_target)) * float(self.query_extract_cap_multiplier))),
            ),
        )

        async with aiohttp.ClientSession() as session:
            classify_semaphore, crawl_semaphore, classify_llm_semaphore, frontier_llm_semaphore = self._build_stage_semaphores()

            async def classify_fn(link: str, active_session: aiohttp.ClientSession):
                return await self._classify_candidate_link(
                    link=link,
                    session=active_session,
                    classify_prompt_key=classify_prompt_key,
                    classify_semaphore=classify_semaphore,
                    classify_llm_semaphore=classify_llm_semaphore,
                )

            query_results, search_urls = await async_judge_query_urls(
                query,
                visited,
                task_prompt_name=classify_prompt_key,
                topic=self.topic,
                session=session,
                fail_fast=True,
                classify_limit=self.classify_limit,
                metric_callback=self._record_component_metric,
                classify_fn=classify_fn,
            )
            query_state.search_results_considered = len(search_urls)

            for result in query_results:
                await self._wait_if_paused()
                if self.stop_event.is_set():
                    return judge_results_all, visited_all, ""

                if not isinstance(result, dict):
                    self._record_runtime_exception(
                        RuntimeError(f"Unexpected query classification payload type: {type(result)}")
                    )
                    continue

                if result.get("__error__"):
                    if str(result.get("errorStage", "")) == "domain_policy":
                        self._record_url_attempt()
                    else:
                        self._record_url_error(result)
                    continue

                if self._is_domain_allowed(result.get("url", "")):
                    self._record_url_attempt()
                    judge_results_all.append(result)

            positive_seed_urls: list[str] = []
            for item in judge_results_all:
                url = str(item.get("url", "")).strip()
                if not url:
                    continue
                if self._is_positive_result(item):
                    positive_seed_urls.append(url)
            query_state.positive_seed_count = len(positive_seed_urls)

            if query_state.positive_seed_count >= query_state.min_positive_seeds:
                target_budget = query_state.classify_target
                crawl_targets = positive_seed_urls
            else:
                query_state.fallback_used = True
                target_budget = min(query_state.classify_target, query_state.fallback_classify_target)
                crawl_targets = []
                for url in search_urls:
                    if not self._is_domain_allowed(url):
                        continue
                    crawl_targets.append(url)
                    if len(crawl_targets) >= query_state.min_positive_seeds:
                        break

            query_state.extract_link_cap = max(
                1,
                int(round(int(target_budget) * float(query_state.extract_cap_multiplier))),
            )

            filtered_targets: list[str] = []
            for url in crawl_targets:
                block_reason = self._domain_block_reason(url)
                if block_reason:
                    self._record_domain_policy_skip(url=url, reason=block_reason)
                    self._record_url_attempt()
                    continue
                filtered_targets.append(url)
            crawl_targets = filtered_targets
            query_state.expansion_seed_count = len(crawl_targets)

            target_budget = max(1, int(target_budget))
            remaining_classify_budget = max(0, target_budget - len(judge_results_all))
            classify_batch_target = max(1, int(self.frontier_classify_cap))
            score_batch_target = max(1, int(self.score_batch_size))
            max_frontier_buffer_items = max(1, int(self.max_frontier_buffer_items))
            extract_dispatch_limit = max(1, min(16, int(self.extract_parallel_per_hop)))

            extract_queue: deque[tuple[str, int]] = deque()
            extract_queued_urls: set[str] = set()
            extract_expanded_urls: set[str] = set()
            frontier_buffer: OrderedDict[str, str] = OrderedDict()
            classified_or_attempted: set[str] = set()
            scored_heap: list[tuple[float, str, str, float]] = []
            pending_extract_tasks: dict[asyncio.Task, tuple[str, int, float]] = {}
            extracted_count = 0
            lane_state: _QueryScoreLaneState | None = None
            last_progress_ts = asyncio.get_running_loop().time()

            def _enqueue_extract_url(candidate_url: str, depth: int) -> bool:
                link = str(candidate_url or "").strip()
                if not link:
                    return False
                if self.max_depth > 0 and int(depth) >= int(self.max_depth):
                    return False
                if link in extract_queued_urls or link in extract_expanded_urls:
                    return False
                prefetch_reason = self._prefetch_skip_reason(link)
                if prefetch_reason:
                    return False
                block_reason = self._domain_block_reason(link)
                if block_reason:
                    self._record_domain_policy_skip(url=link, reason=block_reason)
                    return False
                extract_queue.append((link, int(depth)))
                extract_queued_urls.add(link)
                return True

            for seed_url in crawl_targets:
                _enqueue_extract_url(seed_url, 0)

            async def _classify_links_async(classify_links: list[str]):
                use_batch_classify = self.classifier_mode == "llm" and self.classify_batch_size > 1
                if use_batch_classify:
                    return await self._classify_links_batched(
                        links=classify_links,
                        session=session,
                        classify_prompt_key=classify_prompt_key,
                        classify_llm_semaphore=classify_llm_semaphore,
                        max_results=len(classify_links),
                    )
                tasks = [
                    asyncio.create_task(
                        self._classify_candidate_link(
                            link=link,
                            session=session,
                            classify_prompt_key=classify_prompt_key,
                            classify_semaphore=classify_semaphore,
                            classify_llm_semaphore=classify_llm_semaphore,
                        )
                    )
                    for link in classify_links
                ]
                return await asyncio.gather(*tasks, return_exceptions=True)

            def _set_lane_state(next_state: _QueryScoreLaneState, reason: str) -> None:
                nonlocal lane_state
                if lane_state == next_state:
                    return
                self._record_component_metric(
                    {
                        "component": "orchestration",
                        "operation": "query_score_llm_lane_state_change",
                        "provider": "local",
                        "model": "state_machine",
                        "latencyMs": 0.0,
                        "promptTokens": 0,
                        "completionTokens": 0,
                        "totalTokens": 0,
                        "estimatedCostUsd": 0.0,
                        "status": "ok",
                        "error": "",
                        "meta": json.dumps(
                            {
                                "from": lane_state.value if lane_state else "",
                                "to": next_state.value,
                                "reason": reason,
                                "extractQueueSize": len(extract_queue),
                                "pendingExtractTasks": len(pending_extract_tasks),
                                "frontierBufferSize": len(frontier_buffer),
                                "scoredHeapSize": len(scored_heap),
                                "remainingClassifyBudget": remaining_classify_budget,
                            }
                        ),
                    }
                )
                lane_state = next_state

            async def _score_batch_with_split_retry(
                score_items: list[tuple[str, str]],
            ) -> dict[str, float]:
                run_loop = asyncio.get_running_loop()

                async def _score_once(items: list[tuple[str, str]]) -> tuple[dict[str, float], bool]:
                    if not items:
                        return {}, True
                    frontier_chunk = {url_key: str(ctx or "")[:150] for url_key, ctx in items}
                    started = run_loop.time()
                    try:
                        raw_map = await asyncio.wait_for(
                            async_score_crawl(
                                frontier_chunk,
                                self.topic,
                                sys_prompt="You are a helpful assistant",
                                model_name=self.settings.default_model_name,
                            ),
                            timeout=self.score_request_timeout_s,
                        )
                    except asyncio.TimeoutError:
                        age_ms = (run_loop.time() - started) * 1000.0
                        self._set_query_score_max_inflight_age(age_ms)
                        self._bump_query_score_runtime("scoreTimeoutCount")
                        self._record_component_metric(
                            {
                                "component": "frontier_score",
                                "operation": "query_score_timeout",
                                "provider": "local",
                                "model": self.settings.default_model_name,
                                "latencyMs": age_ms,
                                "promptTokens": 0,
                                "completionTokens": 0,
                                "totalTokens": 0,
                                "estimatedCostUsd": 0.0,
                                "status": "error",
                                "error": f"Score batch timed out after {self.score_request_timeout_s:.1f}s",
                                "meta": json.dumps(
                                    {
                                        "phase": "score",
                                        "batchSize": len(items),
                                        "urlSample": [url for url, _ in items[:3]],
                                    }
                                ),
                            }
                        )
                        return {}, False
                    except Exception as error:
                        age_ms = (run_loop.time() - started) * 1000.0
                        self._set_query_score_max_inflight_age(age_ms)
                        self._record_component_metric(
                            {
                                "component": "frontier_score",
                                "operation": "query_score_timeout",
                                "provider": "local",
                                "model": self.settings.default_model_name,
                                "latencyMs": age_ms,
                                "promptTokens": 0,
                                "completionTokens": 0,
                                "totalTokens": 0,
                                "estimatedCostUsd": 0.0,
                                "status": "error",
                                "error": f"Score batch failed: {type(error).__name__}: {str(error)[:160]}",
                                "meta": json.dumps(
                                    {
                                        "phase": "score",
                                        "batchSize": len(items),
                                        "urlSample": [url for url, _ in items[:3]],
                                    }
                                ),
                            }
                        )
                        return {}, False

                    age_ms = (run_loop.time() - started) * 1000.0
                    self._set_query_score_max_inflight_age(age_ms)
                    if not isinstance(raw_map, dict):
                        return {}, False
                    mapped: dict[str, float] = {}
                    for raw_url, raw_score in raw_map.items():
                        try:
                            score_value = float(raw_score)
                        except (TypeError, ValueError):
                            continue
                        mapped[str(raw_url)] = max(0.0, min(1000.0, score_value))
                    return mapped, True

                full_map, ok = await _score_once(score_items)
                if ok:
                    return full_map
                if len(score_items) <= 1:
                    self._bump_query_score_runtime("scoreRetrySplitFailCount")
                    return {}

                mid = max(1, len(score_items) // 2)
                left_items = score_items[:mid]
                right_items = score_items[mid:]
                self._record_component_metric(
                    {
                        "component": "frontier_score",
                        "operation": "query_score_retry_split_batch",
                        "provider": "local",
                        "model": self.settings.default_model_name,
                        "latencyMs": 0.0,
                        "promptTokens": 0,
                        "completionTokens": 0,
                        "totalTokens": 0,
                        "estimatedCostUsd": 0.0,
                        "status": "ok",
                        "error": "",
                        "meta": json.dumps(
                            {
                                "originalBatchSize": len(score_items),
                                "leftBatchSize": len(left_items),
                                "rightBatchSize": len(right_items),
                            }
                        ),
                    }
                )
                left_map, left_ok = await _score_once(left_items)
                right_map, right_ok = await _score_once(right_items)
                if left_ok or right_ok:
                    self._bump_query_score_runtime("scoreRetrySplitSuccessCount")
                else:
                    self._bump_query_score_runtime("scoreRetrySplitFailCount")
                merged = {}
                merged.update(left_map)
                merged.update(right_map)
                return merged

            async def _classify_links_with_timeout(classify_links: list[str]) -> list:
                if not classify_links:
                    return []
                run_loop = asyncio.get_running_loop()
                started = run_loop.time()
                try:
                    return await asyncio.wait_for(
                        _classify_links_async(classify_links),
                        timeout=self.classify_request_timeout_s,
                    )
                except asyncio.TimeoutError:
                    age_ms = (run_loop.time() - started) * 1000.0
                    self._set_query_score_max_inflight_age(age_ms)
                    self._bump_query_score_runtime("classifyTimeoutCount")
                    self._record_component_metric(
                        {
                            "component": "classify",
                            "operation": "query_score_timeout",
                            "provider": "local",
                            "model": self.settings.default_model_name,
                            "latencyMs": age_ms,
                            "promptTokens": 0,
                            "completionTokens": 0,
                            "totalTokens": 0,
                            "estimatedCostUsd": 0.0,
                            "status": "error",
                            "error": f"Classify batch timed out after {self.classify_request_timeout_s:.1f}s",
                            "meta": json.dumps(
                                {
                                    "phase": "classify",
                                    "batchSize": len(classify_links),
                                    "urlSample": classify_links[:3],
                                    "fallback": "per_url",
                                }
                            ),
                        }
                    )
                except Exception as error:
                    age_ms = (run_loop.time() - started) * 1000.0
                    self._set_query_score_max_inflight_age(age_ms)
                    self._record_runtime_exception(error)
                    self._record_component_metric(
                        {
                            "component": "classify",
                            "operation": "query_score_timeout",
                            "provider": "local",
                            "model": self.settings.default_model_name,
                            "latencyMs": age_ms,
                            "promptTokens": 0,
                            "completionTokens": 0,
                            "totalTokens": 0,
                            "estimatedCostUsd": 0.0,
                            "status": "error",
                            "error": f"Classify batch failed: {type(error).__name__}: {str(error)[:160]}",
                            "meta": json.dumps(
                                {
                                    "phase": "classify",
                                    "batchSize": len(classify_links),
                                    "urlSample": classify_links[:3],
                                    "fallback": "per_url",
                                }
                            ),
                        }
                    )

                fallback_outputs = []
                for link in classify_links:
                    try:
                        fallback_item = await asyncio.wait_for(
                            self._classify_candidate_link(
                                link=link,
                                session=session,
                                classify_prompt_key=classify_prompt_key,
                                classify_semaphore=classify_semaphore,
                                classify_llm_semaphore=classify_llm_semaphore,
                            ),
                            timeout=self.classify_request_timeout_s,
                        )
                    except asyncio.TimeoutError:
                        self._bump_query_score_runtime("classifyTimeoutCount")
                        self._record_component_metric(
                            {
                                "component": "classify",
                                "operation": "query_score_timeout",
                                "provider": "local",
                                "model": self.settings.default_model_name,
                                "latencyMs": self.classify_request_timeout_s * 1000.0,
                                "promptTokens": 0,
                                "completionTokens": 0,
                                "totalTokens": 0,
                                "estimatedCostUsd": 0.0,
                                "status": "error",
                                "error": f"Per-url classify timeout after {self.classify_request_timeout_s:.1f}s",
                                "meta": json.dumps(
                                    {"phase": "classify_individual", "batchSize": 1, "urlSample": [link]}
                                ),
                            }
                        )
                        continue
                    except Exception as error:
                        self._record_runtime_exception(error)
                        continue
                    fallback_outputs.append(fallback_item)
                return fallback_outputs

            async def _extract_worker(url: str, depth: int) -> tuple[str, int, dict[str, str], set[str]]:
                frontier_dict, seen_links = await self._extract_frontier_from_seed(
                    url,
                    depth=depth,
                    visited=visited,
                    session=session,
                    crawl_semaphore=crawl_semaphore,
                )
                return url, depth, frontier_dict, seen_links

            while (
                not self.stop_event.is_set()
                and not self._budget_exhausted()
                and remaining_classify_budget > 0
            ):
                await self._wait_if_paused()
                if self.stop_event.is_set() or self._budget_exhausted():
                    break

                made_progress = False
                loop_now = asyncio.get_running_loop().time()

                while (
                    len(pending_extract_tasks) < extract_dispatch_limit
                    and extract_queue
                    and extracted_count < int(query_state.extract_link_cap)
                    and len(frontier_buffer) < max_frontier_buffer_items
                ):
                    parent_url, parent_depth = extract_queue.popleft()
                    extract_queued_urls.discard(parent_url)
                    if parent_url in extract_expanded_urls:
                        continue
                    extract_expanded_urls.add(parent_url)
                    task = asyncio.create_task(_extract_worker(parent_url, parent_depth))
                    pending_extract_tasks[task] = (parent_url, parent_depth, asyncio.get_running_loop().time())
                    made_progress = True

                if pending_extract_tasks:
                    done, _ = await asyncio.wait(
                        list(pending_extract_tasks.keys()),
                        timeout=0.0,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                else:
                    done = set()

                for task in done:
                    parent_url, parent_depth, started_ts = pending_extract_tasks.pop(task, ("", 0, loop_now))
                    try:
                        _url, _depth, frontier_dict, seen_links = await task
                    except Exception as error:
                        self._record_runtime_exception(error)
                        continue
                    self._set_query_score_max_inflight_age((asyncio.get_running_loop().time() - started_ts) * 1000.0)
                    visited_all.update(seen_links or set())
                    for link, context in (frontier_dict or {}).items():
                        if extracted_count >= int(query_state.extract_link_cap):
                            break
                        if len(frontier_buffer) >= max_frontier_buffer_items:
                            break
                        if (
                            link in visited
                            or link in classified_or_attempted
                            or link in frontier_buffer
                        ):
                            continue
                        frontier_buffer[link] = str(context or "")
                        extracted_count += 1
                        _enqueue_extract_url(link, int(parent_depth) + 1)
                    query_state.links_extracted_to_frontier = extracted_count
                    made_progress = True
                    last_progress_ts = asyncio.get_running_loop().time()

                if remaining_classify_budget <= 0:
                    _set_lane_state(_QueryScoreLaneState.DONE, "classify_budget_reached")
                    break

                if scored_heap and remaining_classify_budget > 0:
                    _set_lane_state(_QueryScoreLaneState.READY_TO_CLASSIFY, "scored_heap_has_items")
                    made_progress = True
                    classify_started = asyncio.get_running_loop().time()
                    self._record_component_metric(
                        {
                            "component": "orchestration",
                            "operation": "query_score_classify_batch_start",
                            "provider": "local",
                            "model": "llm_lane",
                            "latencyMs": 0.0,
                            "promptTokens": 0,
                            "completionTokens": 0,
                            "totalTokens": 0,
                            "estimatedCostUsd": 0.0,
                            "status": "ok",
                            "error": "",
                            "meta": json.dumps(
                                {
                                    "batchCap": classify_batch_target,
                                    "remainingClassifyBudget": remaining_classify_budget,
                                    "scoredHeapSize": len(scored_heap),
                                }
                            ),
                        }
                    )
                    take_n = max(1, min(classify_batch_target, remaining_classify_budget))
                    classify_links: list[str] = []
                    while scored_heap and len(classify_links) < take_n:
                        _neg_score, link, _context, _scored_at = heapq.heappop(scored_heap)
                        if link in visited or link in classified_or_attempted:
                            continue
                        classify_links.append(link)
                        classified_or_attempted.add(link)

                    classifications = await _classify_links_with_timeout(classify_links)
                    successful_classifications = 0
                    for item in classifications:
                        if isinstance(item, Exception):
                            self._record_runtime_exception(item)
                            continue
                        if isinstance(item, dict) and item.get("__error__"):
                            if str(item.get("errorStage", "")) == "domain_policy":
                                self._record_url_attempt()
                            else:
                                self._record_url_error(item)
                            continue
                        if item:
                            self._record_url_attempt()
                            judge_results_all.append(item)
                            successful_classifications += 1

                    query_state.frontier_pages_classified += int(successful_classifications)
                    remaining_classify_budget = max(0, target_budget - len(judge_results_all))
                    classify_ms = (asyncio.get_running_loop().time() - classify_started) * 1000.0
                    self._set_query_score_max_inflight_age(classify_ms)
                    self._record_component_metric(
                        {
                            "component": "orchestration",
                            "operation": "query_score_classify_batch_end",
                            "provider": "local",
                            "model": "llm_lane",
                            "latencyMs": classify_ms,
                            "promptTokens": 0,
                            "completionTokens": 0,
                            "totalTokens": 0,
                            "estimatedCostUsd": 0.0,
                            "status": "ok",
                            "error": "",
                            "meta": json.dumps(
                                {
                                    "requestedCount": len(classify_links),
                                    "successfulCount": successful_classifications,
                                    "remainingClassifyBudget": remaining_classify_budget,
                                    "scoredHeapSize": len(scored_heap),
                                }
                            ),
                        }
                    )
                    last_progress_ts = asyncio.get_running_loop().time()

                elif frontier_buffer and remaining_classify_budget > 0:
                    _set_lane_state(_QueryScoreLaneState.READY_TO_SCORE, "frontier_buffer_has_items")
                    made_progress = True
                    score_items = list(frontier_buffer.items())[:score_batch_target]
                    for url_key, _ in score_items:
                        frontier_buffer.pop(url_key, None)
                    self._record_component_metric(
                        {
                            "component": "orchestration",
                            "operation": "query_score_score_batch_start",
                            "provider": "local",
                            "model": "llm_lane",
                            "latencyMs": 0.0,
                            "promptTokens": 0,
                            "completionTokens": 0,
                            "totalTokens": 0,
                            "estimatedCostUsd": 0.0,
                            "status": "ok",
                            "error": "",
                            "meta": json.dumps(
                                {
                                    "batchSize": len(score_items),
                                    "frontierBufferRemaining": len(frontier_buffer),
                                }
                            ),
                        }
                    )

                    score_started = asyncio.get_running_loop().time()
                    score_map = await _score_batch_with_split_retry(score_items)
                    score_ms = (asyncio.get_running_loop().time() - score_started) * 1000.0
                    self._set_query_score_max_inflight_age(score_ms)
                    self._record_component_metric(
                        {
                            "component": "orchestration",
                            "operation": "query_score_score_batch_end",
                            "provider": "local",
                            "model": "llm_lane",
                            "latencyMs": score_ms,
                            "promptTokens": 0,
                            "completionTokens": 0,
                            "totalTokens": 0,
                            "estimatedCostUsd": 0.0,
                            "status": "ok",
                            "error": "",
                            "meta": json.dumps(
                                {
                                    "batchSize": len(score_items),
                                    "returnedItems": int(len(score_map) if isinstance(score_map, dict) else 0),
                                    "frontierBufferRemaining": len(frontier_buffer),
                                }
                            ),
                        }
                    )

                    scored_at_ts = asyncio.get_running_loop().time()
                    for scored_url, context in score_items:
                        if scored_url in visited or scored_url in classified_or_attempted:
                            continue
                        raw_score = score_map.get(scored_url)
                        if raw_score is None:
                            continue
                        try:
                            score_value = float(raw_score)
                        except (TypeError, ValueError):
                            continue
                        score_value = max(0.0, min(1000.0, score_value))
                        heapq.heappush(
                            scored_heap,
                            (-score_value, scored_url, str(context or ""), scored_at_ts),
                        )
                    last_progress_ts = asyncio.get_running_loop().time()

                elif (
                    pending_extract_tasks
                    or (
                        extract_queue
                        and extracted_count < int(query_state.extract_link_cap)
                    )
                ):
                    _set_lane_state(_QueryScoreLaneState.WAITING_FOR_INPUT, "waiting_for_extract_output")
                    await asyncio.sleep(0.05)
                else:
                    _set_lane_state(_QueryScoreLaneState.DONE, "no_more_extract_or_llm_work")
                    break

                if (
                    not extract_queue
                    and not pending_extract_tasks
                    and not frontier_buffer
                    and not scored_heap
                ):
                    _set_lane_state(_QueryScoreLaneState.DONE, "all_queues_empty")
                    break

                if not made_progress:
                    no_progress_ms = (asyncio.get_running_loop().time() - last_progress_ts) * 1000.0
                    if no_progress_ms > max(self.score_request_timeout_s, self.classify_request_timeout_s) * 2000.0:
                        self._bump_query_score_runtime("stalledLoopWatchdogCount")
                        self._record_component_metric(
                            {
                                "component": "orchestration",
                                "operation": "query_score_stalled_watchdog",
                                "provider": "local",
                                "model": "llm_lane",
                                "latencyMs": no_progress_ms,
                                "promptTokens": 0,
                                "completionTokens": 0,
                                "totalTokens": 0,
                                "estimatedCostUsd": 0.0,
                                "status": "ok",
                                "error": "",
                                "meta": json.dumps(
                                    {
                                        "extractQueueSize": len(extract_queue),
                                        "pendingExtractTasks": len(pending_extract_tasks),
                                        "frontierBufferSize": len(frontier_buffer),
                                        "scoredHeapSize": len(scored_heap),
                                        "remainingClassifyBudget": remaining_classify_budget,
                                    }
                                ),
                            }
                        )
                        last_progress_ts = asyncio.get_running_loop().time()
                    await asyncio.sleep(0.01)

            for task in pending_extract_tasks:
                task.cancel()
            if pending_extract_tasks:
                await asyncio.gather(*pending_extract_tasks.keys(), return_exceptions=True)

        query_state.query_pages_classified = max(
            0,
            len(judge_results_all) - query_state.frontier_pages_classified,
        )
        self.query_session_summaries.append(query_state.to_dict())

        feedback = await asyncio.to_thread(
            query_feedback,
            query,
            judge_results_all,
            "query",
        )
        self._update_lightweight_model_from_batch(judge_results_all)
        return judge_results_all, visited_all, feedback

    async def _extract_frontier_from_seed(
        self,
        url: str,
        *,
        depth: int,
        visited: set[str],
        session: aiohttp.ClientSession,
        crawl_semaphore: asyncio.Semaphore,
    ) -> tuple[dict[str, str], set[str]]:
        run_loop = asyncio.get_running_loop()
        wait_started = run_loop.time()
        async with crawl_semaphore:
            wait_ms = (run_loop.time() - wait_started) * 1000.0
            self._record_component_metric(
                {
                    "component": "orchestration",
                    "operation": "semaphore_wait_crawl",
                    "provider": "local",
                    "model": "asyncio_semaphore",
                    "latencyMs": wait_ms,
                    "promptTokens": 0,
                    "completionTokens": 0,
                    "totalTokens": 0,
                    "estimatedCostUsd": 0.0,
                    "status": "ok",
                    "error": "",
                    "meta": json.dumps(
                        {
                            "url": url,
                            "mode": "query_score_stream_extract",
                            "depth": int(depth),
                        }
                    ),
                }
            )
            extract_started = run_loop.time()
            extracted = await async_extract_links_with_context(
                url=url,
                max_links=self.max_links_per_page,
                visited=visited,
                session=session,
                metric_callback=self._record_component_metric,
                hop_level=int(depth),
                extract_parallel_per_hop=self.extract_parallel_per_hop,
                extract_connect_timeout_s=self.extract_connect_timeout_s,
                extract_read_timeout_s=self.extract_read_timeout_s,
                extract_total_timeout_s=self.extract_total_timeout_s,
                extract_max_bytes=self.extract_max_bytes,
                extract_max_retries=self.extract_max_retries,
                extract_retry_backoff_s=self.extract_retry_backoff_s,
                extract_allowed_mime_prefixes=self.extract_allowed_mime_prefixes,
                enable_extract_bounds=self.enable_extract_bounds,
                enable_extract_mime_filter=self.enable_extract_mime_filter,
                enable_extract_retry=self.enable_extract_retry,
            )
            extract_total_ms = (run_loop.time() - extract_started) * 1000.0
            self._record_component_metric(
                {
                    "component": "crawl",
                    "operation": "query_score_extract_page_total",
                    "provider": "local",
                    "model": "async_extract_links_with_context",
                    "latencyMs": extract_total_ms,
                    "promptTokens": 0,
                    "completionTokens": 0,
                    "totalTokens": 0,
                    "estimatedCostUsd": 0.0,
                    "status": "ok",
                    "error": "",
                    "meta": json.dumps(
                        {
                            "url": url,
                            "depth": int(depth),
                            "maxLinksPerPage": int(self.max_links_per_page),
                            "frontierSize": len(extracted) if extracted else 0,
                        }
                    ),
                }
            )

        if not extracted:
            return {}, set()

        frontier_dict: dict[str, str] = {}
        seen_links: set[str] = set()
        for item_url, item_context in (extracted or {}).items():
            item_url = str(item_url or "").strip()
            if not item_url:
                continue
            seen_links.add(item_url)
            if not self._is_domain_allowed(item_url):
                continue
            block_reason = self._domain_block_reason(item_url)
            if block_reason:
                self._record_domain_policy_skip(url=item_url, reason=block_reason)
                continue
            frontier_dict[item_url] = str(item_context or "")

        return frontier_dict, seen_links

    async def _query_page(
        self,
        url: str,
        *,
        visited: set[str],
        classify_prompt_key: str,
        session: aiohttp.ClientSession,
        classify_semaphore: asyncio.Semaphore,
        crawl_semaphore: asyncio.Semaphore,
        classify_llm_semaphore: asyncio.Semaphore,
        frontier_llm_semaphore: asyncio.Semaphore,
        frontier_extract_cap: int | None = None,
        classify_cap_override: int | None = None,
        return_frontier_stats: bool = False,
    ):
        query_page_started_ts = asyncio.get_running_loop().time()
        if self.stop_event.is_set():
            return [], []
        await self._wait_if_paused()
        if self.stop_event.is_set():
            return [], []

        run_loop = asyncio.get_running_loop()
        crawl_wait_started = run_loop.time()
        async with crawl_semaphore:
            crawl_wait_ms = (run_loop.time() - crawl_wait_started) * 1000.0
            self._record_component_metric(
                {
                    "component": "orchestration",
                    "operation": "semaphore_wait_crawl",
                    "provider": "local",
                    "model": "asyncio_semaphore",
                    "latencyMs": crawl_wait_ms,
                    "promptTokens": 0,
                    "completionTokens": 0,
                    "totalTokens": 0,
                    "estimatedCostUsd": 0.0,
                    "status": "ok",
                    "error": "",
                    "meta": json.dumps({"url": url}),
                }
            )
            hop_started = run_loop.time()
            frontier = await async_hop_with_context(
                url,
                visited=visited,
                n_hops=self.max_depth,
                max_links=self.settings.max_links_per_page,
                session=session,
                metric_callback=self._record_component_metric,
                should_expand_url=self._should_expand_url_for_hop,
                extract_cache_get=self._extract_cache_get,
                extract_cache_set=self._extract_cache_set,
                extract_cache_stats=self._extract_cache_stats,
                max_frontier_items=frontier_extract_cap,
                **self._build_extract_options(),
            )
            hop_total_ms = (run_loop.time() - hop_started) * 1000.0
            self._record_component_metric(
                {
                    "component": "crawl",
                    "operation": "hop_with_context_total",
                    "provider": "local",
                    "model": "async_hop_with_context",
                    "latencyMs": hop_total_ms,
                    "promptTokens": 0,
                    "completionTokens": 0,
                    "totalTokens": 0,
                    "estimatedCostUsd": 0.0,
                    "status": "ok",
                    "error": "",
                    "meta": json.dumps(
                        {
                            "url": url,
                            "maxDepth": self.max_depth,
                            "maxLinksPerPage": self.settings.max_links_per_page,
                            "frontierSize": len(frontier) if frontier else 0,
                        }
                    ),
                }
            )
            crawl_stage_completed_ts = run_loop.time()

        if not frontier:
            if return_frontier_stats:
                return [], [], 0
            return [], []

        frontier_dict: dict[str, str] = {}
        for item in frontier:
            item_url = item.get("url")
            if not item_url or not self._is_domain_allowed(item_url):
                continue
            block_reason = self._domain_block_reason(item_url)
            if block_reason:
                self._record_domain_policy_skip(url=item_url, reason=block_reason)
                self._record_url_attempt()
                continue
            frontier_dict[item_url] = item.get("context", "")

        if frontier_extract_cap is not None and frontier_extract_cap > 0 and len(frontier_dict) > int(frontier_extract_cap):
            trimmed_items = list(frontier_dict.items())[: int(frontier_extract_cap)]
            frontier_dict = dict(trimmed_items)

        frontier_count_for_stats = len(frontier_dict)
        if not frontier_dict:
            if return_frontier_stats:
                return [], [], 0
            return [], []

        frontier_wait_started = run_loop.time()
        async with frontier_llm_semaphore:
            frontier_wait_ms = (run_loop.time() - frontier_wait_started) * 1000.0
            self._record_component_metric(
                {
                    "component": "orchestration",
                    "operation": "semaphore_wait_frontier_llm",
                    "provider": "local",
                    "model": "asyncio_semaphore",
                    "latencyMs": frontier_wait_ms,
                    "promptTokens": 0,
                    "completionTokens": 0,
                    "totalTokens": 0,
                    "estimatedCostUsd": 0.0,
                    "status": "ok",
                    "error": "",
                    "meta": json.dumps({"url": url, "frontierDictSize": len(frontier_dict)}),
                }
            )
            response = await async_classic_crawl(
                frontier_dict,
                self.topic,
                sys_prompt="You are a helpful assistant",
                num_choice=min(self.frontier_select_count, max(1, len(frontier_dict))),
                model_name=self.settings.default_model_name,
                metric_callback=self._record_component_metric,
            )
        frontier_stage_completed_ts = run_loop.time()

        if not response:
            if return_frontier_stats:
                return [], list(frontier_dict.keys()), frontier_count_for_stats
            return [], list(frontier_dict.keys())

        links: list[str] = []
        for link in response.get("urls", []):
            if not self._is_domain_allowed(link):
                continue
            prefetch_reason = self._prefetch_skip_reason(link)
            if prefetch_reason:
                skip_reason = f"Skipped by prefetch heuristic: {prefetch_reason}"
                self._record_component_metric(
                    {
                        "component": "classify",
                        "operation": "relevance_short_circuit",
                        "provider": "local",
                        "model": "rule_based",
                        "latencyMs": 0.0,
                        "promptTokens": 0,
                        "completionTokens": 0,
                        "totalTokens": 0,
                        "estimatedCostUsd": 0.0,
                        "status": "ok",
                        "error": "",
                        "skipReason": skip_reason,
                        "meta": json.dumps({"url": link, "skipReason": skip_reason}),
                    }
                )
                self._record_url_attempt()
                continue
            block_reason = self._domain_block_reason(link)
            if block_reason:
                self._record_domain_policy_skip(url=link, reason=block_reason)
                self._record_url_attempt()
                continue
            links.append(link)
        links = links[: max(1, int(self.frontier_select_count))]

        candidate_links = []
        for link in links:
            if self.stop_event.is_set():
                break
            await self._wait_if_paused()
            if self.stop_event.is_set():
                break
            if link in visited:
                continue
            candidate_links.append(link)
        candidate_links = candidate_links[: max(1, int(self.frontier_select_count))]

        use_batch_classify = (
            self.classifier_mode == "llm"
            and self.classify_batch_size > 1
        )
        effective_classify_cap = (
            max(1, int(classify_cap_override))
            if classify_cap_override is not None
            else max(1, int(self.frontier_classify_cap))
        )
        classify_stage_started_ts = run_loop.time()
        if use_batch_classify:
            classifications = await self._classify_links_batched(
                links=candidate_links,
                session=session,
                classify_prompt_key=classify_prompt_key,
                classify_llm_semaphore=classify_llm_semaphore,
                max_results=effective_classify_cap,
            )
        else:
            capped_links = candidate_links[: effective_classify_cap]
            tasks = [
                asyncio.create_task(
                    self._classify_candidate_link(
                        link=link,
                        session=session,
                        classify_prompt_key=classify_prompt_key,
                        classify_semaphore=classify_semaphore,
                        classify_llm_semaphore=classify_llm_semaphore,
                    )
                )
                for link in capped_links
            ]
            classifications = await asyncio.gather(*tasks, return_exceptions=True)
        classify_stage_completed_ts = run_loop.time()

        judge_results = []
        for item in classifications:
            if isinstance(item, Exception):
                self._record_runtime_exception(item)
                continue
            if isinstance(item, dict) and item.get("__error__"):
                if str(item.get("errorStage", "")) == "domain_policy":
                    self._record_url_attempt()
                else:
                    self._record_url_error(item)
                continue
            if item:
                self._record_url_attempt()
                judge_results.append(item)

        crawl_stage_ms = max(0.0, (crawl_stage_completed_ts - query_page_started_ts) * 1000.0)
        frontier_stage_ms = max(0.0, (frontier_stage_completed_ts - crawl_stage_completed_ts) * 1000.0)
        classify_stage_ms = max(0.0, (classify_stage_completed_ts - classify_stage_started_ts) * 1000.0)
        total_stage_ms = max(0.0, (run_loop.time() - query_page_started_ts) * 1000.0)
        self._record_component_metric(
            {
                "component": "orchestration",
                "operation": "query_page_stage_wallclock",
                "provider": "local",
                "model": "stage_timer",
                "latencyMs": total_stage_ms,
                "promptTokens": 0,
                "completionTokens": 0,
                "totalTokens": 0,
                "estimatedCostUsd": 0.0,
                "status": "ok",
                "error": "",
                "meta": json.dumps(
                    {
                        "url": url,
                        "stage": "query_page_total",
                        "crawlStageMs": round(crawl_stage_ms, 3),
                        "frontierStageMs": round(frontier_stage_ms, 3),
                        "classifyStageMs": round(classify_stage_ms, 3),
                        "totalStageMs": round(total_stage_ms, 3),
                        "selectedLinks": len(links),
                        "candidateLinks": len(candidate_links),
                        "classifiedResults": len(judge_results),
                    }
                ),
            }
        )

        if return_frontier_stats:
            return judge_results, links, frontier_count_for_stats
        return judge_results, links

    async def _record_results(
        self,
        query: str,
        judge_results: list[dict],
        *,
        collect_for_review_gate: bool = False,
    ) -> None:
        for raw_result in judge_results:
            await self._wait_if_paused()
            if self.stop_event.is_set():
                return

            url = raw_result.get("url")
            if not url or url in self.seen_result_urls:
                continue

            self.seen_result_urls.add(url)
            self.result_counter += 1

            result_payload = build_result_payload(
                session_id=self.session_id,
                sequence_number=self.result_counter,
                query=query,
                classifier_result=raw_result,
                min_relevance=self.min_relevance,
            )

            self.artifacts.append_result(result_payload)

            self.stats["pagesScanned"] += 1
            if normalize_prediction(raw_result.get("pred")) == "yes":
                self.stats["relevantFound"] += 1

            await self._emit_event("result.discovered", {"result": result_payload})
            await self._emit_event("crawl.progress", build_progress_payload(self.stats, query=query))

            if not collect_for_review_gate:
                continue

            self.review_gate_result_ids.append(result_payload["id"])

    async def _apply_initial_review_gate(self) -> None:
        if self.review_gate_processed:
            return

        required_result_ids = list(self.review_gate_result_ids[: self.review_gate_limit])
        if not required_result_ids:
            return

        self.review_gate_processed = True
        await self._emit_event(
            "crawl.awaiting_review",
            {
                "requiredCount": len(required_result_ids),
                "remainingReviews": len(required_result_ids),
                "requiredResultIds": required_result_ids,
                "stats": self.stats,
            },
        )

        if self.review_gate_handler is None:
            raise RuntimeError("Review gate is enabled but no review gate handler was configured")

        await self.review_gate_handler(required_result_ids)
        if self.stop_event.is_set():
            return

        await self._emit_event(
            "crawl.resumed",
            {
                "reason": "review_gate",
                "reviewedCount": len(required_result_ids),
                "stats": self.stats,
            },
        )

    async def _wait_if_paused(self) -> None:
        if self.wait_if_paused_handler is None:
            return
        await self.wait_if_paused_handler()

    async def _emit_event(self, event_type: str, payload: dict) -> None:
        event = {
            "type": event_type,
            "sessionId": self.session_id,
            "timestamp": datetime.utcnow().isoformat(),
            "payload": payload,
        }
        await self.event_callback(event)

    def _record_url_attempt(self) -> None:
        self.stats["urlsAttempted"] += 1
        self._refresh_error_rate()

    def _record_url_error(self, _error_payload: dict | None = None) -> None:
        self.stats["urlsAttempted"] += 1
        self.stats["urlErrors"] += 1
        self._refresh_error_rate()

    def _record_runtime_exception(self, _error: Exception) -> None:
        self.stats["urlsAttempted"] += 1
        self.stats["urlErrors"] += 1
        self._refresh_error_rate()

    def _refresh_error_rate(self) -> None:
        attempted = max(1, int(self.stats.get("urlsAttempted", 0)))
        errors = int(self.stats.get("urlErrors", 0))
        self.stats["errorRate"] = round(errors / attempted, 4)
