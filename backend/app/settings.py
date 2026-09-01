from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    project_root: Path
    app_dir: Path
    prompts_path: Path
    api_keys_path: Path
    session_artifacts_root: Path
    session_artifact_ttl_seconds: int
    default_prompt_profile: str
    default_model_name: str
    max_iterations: int
    max_results: int
    max_links_per_page: int
    max_parallel_crawl_targets: int
    classify_batch_size: int
    policy_mode: str
    query_classify_target: int
    query_fallback_classify_target: int
    query_min_positive_seeds: int
    query_extract_cap_multiplier: float
    score_batch_size: int
    score_request_timeout_s: float
    classify_request_timeout_s: float
    max_frontier_buffer_items: int
    hop_link_limits: tuple[int, ...]
    extract_parallel_per_hop: int
    extract_connect_timeout_s: float
    extract_read_timeout_s: float
    extract_total_timeout_s: float
    extract_max_bytes: int
    extract_max_retries: int
    extract_retry_backoff_s: float
    extract_allowed_mime_prefixes: tuple[str, ...]
    enable_extract_bounds: bool
    enable_extract_mime_filter: bool
    enable_extract_retry: bool
    extract_cache_enabled: bool
    extract_cache_max_entries: int
    extract_cache_ttl_s: float
    fetch_connect_timeout_s: float
    fetch_read_timeout_s: float
    fetch_total_timeout_s: float
    fetch_max_bytes: int
    fetch_max_retries: int
    fetch_retry_backoff_s: float
    fetch_allowed_mime_prefixes: tuple[str, ...]
    host_min_access_interval_s: float
    max_pages_per_domain: int
    domain_fail_cooldown_s: float
    domain_fail_threshold: int
    enable_fetch_bounds: bool
    enable_domain_cooldown: bool
    enable_mime_filter: bool
    enable_fetch_retry: bool
    api_host: str
    api_port: int
    api_allowed_origins: tuple[str, ...]
    openai_api_key: str | None
    deepseek_api_key: str | None
    qwen_api_key: str | None
    search_api_key: str | None


def _resolve_path(raw_value: str | None, default_path: Path) -> Path:
    if not raw_value:
        return default_path

    candidate = Path(raw_value).expanduser()
    if candidate.is_absolute():
        return candidate

    return (default_path.parent / candidate).resolve()


def _read_env_int(name: str, default_value: int) -> int:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default_value

    try:
        return int(raw_value)
    except ValueError:
        return default_value


def _read_env_float(name: str, default_value: float) -> float:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default_value

    try:
        return float(raw_value)
    except ValueError:
        return default_value


def _read_env_bool(name: str, default_value: bool) -> bool:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default_value
    normalized = str(raw_value).strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    return default_value


def _read_env_csv(name: str, default_values: tuple[str, ...]) -> tuple[str, ...]:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default_values
    items = tuple(item.strip() for item in raw_value.split(",") if item.strip())
    if not items:
        return default_values
    return items


def _read_env_int_csv(name: str, default_values: tuple[int, ...]) -> tuple[int, ...]:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default_values
    items: list[int] = []
    for raw_item in raw_value.split(","):
        item = raw_item.strip()
        if not item:
            continue
        try:
            parsed = int(item)
        except ValueError:
            continue
        if parsed > 0:
            items.append(parsed)
    if not items:
        return default_values
    return tuple(items)


def _read_allowed_origins() -> tuple[str, ...]:
    raw_value = os.getenv("CRAWL_API_ALLOWED_ORIGINS", "*")
    items = [item.strip() for item in raw_value.split(",") if item.strip()]
    if not items:
        return ("*",)
    return tuple(items)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    app_dir = Path(__file__).resolve().parent
    project_root = app_dir.parent

    prompts_path = _resolve_path(
        os.getenv("CRAWL_PROMPTS_PATH"),
        app_dir / "Prompts.json",
    )
    api_keys_path = _resolve_path(
        os.getenv("CRAWL_API_KEYS_PATH"),
        app_dir / "API_KEY.json",
    )

    session_root_default = Path(tempfile.gettempdir()) / "llm-crawl-sessions"
    session_artifacts_root = _resolve_path(
        os.getenv("CRAWL_SESSION_ARTIFACTS_ROOT"),
        session_root_default,
    )

    settings = Settings(
        project_root=project_root,
        app_dir=app_dir,
        prompts_path=prompts_path,
        api_keys_path=api_keys_path,
        session_artifacts_root=session_artifacts_root,
        session_artifact_ttl_seconds=_read_env_int("CRAWL_SESSION_ARTIFACT_TTL_SECONDS", 1800),
        default_prompt_profile=os.getenv("CRAWL_DEFAULT_PROMPT_PROFILE", "general"),
        default_model_name=os.getenv("CRAWL_DEFAULT_MODEL", "deepseek-chat"),
        max_iterations=_read_env_int("CRAWL_MAX_ITERATIONS", 100),
        max_results=_read_env_int("CRAWL_MAX_RESULTS", 10000),
        max_links_per_page=_read_env_int("CRAWL_MAX_LINKS_PER_PAGE", 8),
        max_parallel_crawl_targets=_read_env_int("CRAWL_MAX_PARALLEL_CRAWL_TARGETS", 0),
        classify_batch_size=_read_env_int("CRAWL_CLASSIFY_BATCH_SIZE", 1),
        policy_mode=os.getenv("CRAWL_POLICY_MODE", "legacy").strip().lower() or "legacy",
        query_classify_target=_read_env_int("CRAWL_QUERY_CLASSIFY_TARGET", 100),
        query_fallback_classify_target=_read_env_int("CRAWL_QUERY_FALLBACK_CLASSIFY_TARGET", 25),
        query_min_positive_seeds=_read_env_int("CRAWL_QUERY_MIN_POSITIVE_SEEDS", 3),
        query_extract_cap_multiplier=_read_env_float("CRAWL_QUERY_EXTRACT_CAP_MULTIPLIER", 3.0),
        score_batch_size=_read_env_int("CRAWL_SCORE_BATCH_SIZE", 32),
        score_request_timeout_s=_read_env_float("CRAWL_SCORE_REQUEST_TIMEOUT_S", 45.0),
        classify_request_timeout_s=_read_env_float("CRAWL_CLASSIFY_REQUEST_TIMEOUT_S", 12.0),
        max_frontier_buffer_items=_read_env_int("CRAWL_MAX_FRONTIER_BUFFER_ITEMS", 256),
        hop_link_limits=_read_env_int_csv("CRAWL_HOP_LINK_LIMITS", (8, 4, 2)),
        extract_parallel_per_hop=_read_env_int("CRAWL_EXTRACT_PARALLEL_PER_HOP", 12),
        extract_connect_timeout_s=_read_env_float("EXTRACT_CONNECT_TIMEOUT_S", 2.0),
        extract_read_timeout_s=_read_env_float("EXTRACT_READ_TIMEOUT_S", 3.0),
        extract_total_timeout_s=_read_env_float("EXTRACT_TOTAL_TIMEOUT_S", 5.0),
        extract_max_bytes=_read_env_int("EXTRACT_MAX_BYTES", 600_000),
        extract_max_retries=_read_env_int("EXTRACT_MAX_RETRIES", 1),
        extract_retry_backoff_s=_read_env_float("EXTRACT_RETRY_BACKOFF_S", 0.25),
        extract_allowed_mime_prefixes=_read_env_csv(
            "EXTRACT_ALLOWED_MIME_PREFIXES",
            ("text/html", "application/xhtml+xml"),
        ),
        enable_extract_bounds=_read_env_bool("ENABLE_EXTRACT_BOUNDS", True),
        enable_extract_mime_filter=_read_env_bool("ENABLE_EXTRACT_MIME_FILTER", True),
        enable_extract_retry=_read_env_bool("ENABLE_EXTRACT_RETRY", True),
        extract_cache_enabled=_read_env_bool("CRAWL_EXTRACT_CACHE_ENABLED", True),
        extract_cache_max_entries=_read_env_int("CRAWL_EXTRACT_CACHE_MAX_ENTRIES", 2500),
        extract_cache_ttl_s=_read_env_float("CRAWL_EXTRACT_CACHE_TTL_S", 1500.0),
        fetch_connect_timeout_s=_read_env_float("FETCH_CONNECT_TIMEOUT_S", 5.0),
        fetch_read_timeout_s=_read_env_float("FETCH_READ_TIMEOUT_S", 10.0),
        fetch_total_timeout_s=_read_env_float("FETCH_TOTAL_TIMEOUT_S", 15.0),
        fetch_max_bytes=_read_env_int("FETCH_MAX_BYTES", 800_000),
        fetch_max_retries=_read_env_int("FETCH_MAX_RETRIES", 1),
        fetch_retry_backoff_s=_read_env_float("FETCH_RETRY_BACKOFF_S", 0.5),
        fetch_allowed_mime_prefixes=_read_env_csv(
            "FETCH_ALLOWED_MIME_PREFIXES",
            ("text/html", "application/xhtml+xml", "application/pdf"),
        ),
        host_min_access_interval_s=_read_env_float("HOST_MIN_ACCESS_INTERVAL_S", 0.2),
        max_pages_per_domain=_read_env_int("MAX_PAGES_PER_DOMAIN", 100),
        domain_fail_cooldown_s=_read_env_float("DOMAIN_FAIL_COOLDOWN_S", 45.0),
        domain_fail_threshold=_read_env_int("DOMAIN_FAIL_THRESHOLD", 3),
        enable_fetch_bounds=_read_env_bool("ENABLE_FETCH_BOUNDS", True),
        enable_domain_cooldown=_read_env_bool("ENABLE_DOMAIN_COOLDOWN", True),
        enable_mime_filter=_read_env_bool("ENABLE_MIME_FILTER", True),
        enable_fetch_retry=_read_env_bool("ENABLE_FETCH_RETRY", True),
        api_host=os.getenv("CRAWL_API_HOST", "0.0.0.0"),
        api_port=_read_env_int("CRAWL_API_PORT", 8000),
        api_allowed_origins=_read_allowed_origins(),
        openai_api_key=os.getenv("OPENAI_API_KEY"),
        deepseek_api_key=os.getenv("DEEPSEEK_API_KEY"),
        qwen_api_key=os.getenv("QWEN_API_KEY"),
        search_api_key=os.getenv("SEARCH_API_KEY"),
    )

    settings.session_artifacts_root.mkdir(parents=True, exist_ok=True)
    return settings


def load_api_keys(settings: Settings | None = None) -> dict[str, str]:
    """Load API keys from file and env vars; env values override file values."""
    settings = settings or get_settings()
    key_data: dict[str, str] = {}

    if settings.api_keys_path.exists():
        with settings.api_keys_path.open("r", encoding="utf-8") as file:
            raw_data = json.load(file)
            if isinstance(raw_data, dict):
                key_data.update({str(k): str(v) for k, v in raw_data.items()})

    env_overlay = {
        "OPENAI_API_KEY": settings.openai_api_key,
        "DeepSeek_Michale": settings.deepseek_api_key,
        "QWEN_API_KEY": settings.qwen_api_key,
        "SEARCH_API_KEY": settings.search_api_key,
    }

    for key_name, key_value in env_overlay.items():
        if key_value:
            key_data[key_name] = key_value

    return key_data
