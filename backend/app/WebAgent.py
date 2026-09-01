import sys 
import time
import os
import random
import json
import re
import hashlib
import traceback
from datetime import datetime
from collections import OrderedDict
from urllib.parse import urlparse
# 1. Get the absolute path of the current file (main_script.py)
current_file_path = os.path.abspath(__file__)
# 2. Get the path to the directory containing this file (app/)
app_dir = os.path.dirname(current_file_path)
# 3. Get the path to the project root directory (my_project/) by going one level up
project_root = os.path.dirname(app_dir)
# 4. Add the project root to the beginning of sys.path
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from app.settings import get_settings, load_api_keys
settings = get_settings()
API_KEYS_PATH = str(settings.api_keys_path)
PROMPTS_PATH = str(settings.prompts_path)
_api_keys_cache = None
_prompt_templates_cache = None
_prompt_templates_mtime = None
_shared_async_classifier = None
_shared_sync_classifier = None
CLASSIFY_MAX_TOKENS = 320
CLASSIFY_FETCH_TIMEOUT_S = 6
CLASSIFY_REQUEST_TIMEOUT_S = 12
CLASSIFY_MAX_RETRIES = 0
CLASSIFY_RETRY_BACKOFF_S = 0.0


def _load_prompt_templates():
    global _prompt_templates_cache, _prompt_templates_mtime
    current_mtime = None
    try:
        current_mtime = os.path.getmtime(PROMPTS_PATH)
    except OSError:
        current_mtime = None

    if _prompt_templates_cache is not None and _prompt_templates_mtime == current_mtime:
        return _prompt_templates_cache

    with open(PROMPTS_PATH, encoding='utf-8') as file:
        payload = json.load(file)
    _prompt_templates_cache = payload
    _prompt_templates_mtime = current_mtime
    return payload


def _render_prompt_template(template: str, **values) -> str:
    rendered = str(template).replace("\\n", "\n")
    for key, value in values.items():
        rendered = rendered.replace("{" + key + "}", str(value))
    return rendered


def _build_error_result(url, stage, message):
    return {
        'url': url,
        '__error__': True,
        'errorStage': stage,
        'errorMessage': str(message)[:300],
    }


def _url_hash(url: str) -> str:
    return hashlib.sha256(str(url or "").encode("utf-8")).hexdigest()[:12]


def _extract_http_status(title, body):
    text = f"{title or ''}\n{body or ''}"
    match = re.search(r"HTTP error:\s*(\d{3})", text, flags=re.IGNORECASE)
    if match:
        return int(match.group(1))
    return None


def _should_short_circuit_classify(title, body):
    title_txt = str(title or "").strip()
    body_txt = str(body or "").strip()
    if not title_txt and not body_txt:
        return True, "No content extracted"

    lowered = body_txt.lower()
    error_signatures = [
        "http error:",
        "error fetching html page:",
        "error fetching or processing pdf:",
        "content-encoding error:",
        "fetch exception:",
    ]
    if any(sig in lowered for sig in error_signatures):
        return True, body_txt[:260] if body_txt else "Unusable content"

    # Very short/empty body with no title is usually non-informative for LLM judging.
    if not title_txt and len(body_txt) < 40:
        return True, "Insufficient page content for relevance classification"

    return False, ""


def _build_page_snippet(body, max_chars=280):
    if not body:
        return ""

    cleaned = re.sub(r"\s+", " ", str(body)).strip()
    if not cleaned:
        return ""

    # Prefer full sentences so snippets are readable in the UI.
    sentences = re.split(r"(?<=[.!?])\s+", cleaned)
    collected = []
    total_len = 0

    for sentence in sentences:
        sentence = sentence.strip()
        if not sentence:
            continue

        addition = len(sentence) + (1 if collected else 0)
        if total_len + addition > max_chars:
            break

        collected.append(sentence)
        total_len += addition

        if total_len >= int(max_chars * 0.65):
            break

    if collected:
        return " ".join(collected)

    if len(cleaned) <= max_chars:
        return cleaned

    return cleaned[: max_chars - 1].rstrip() + "..."


def _get_search_key():
    global _api_keys_cache

    if _api_keys_cache is None:
        _api_keys_cache = load_api_keys(settings)

    search_key = _api_keys_cache.get('SEARCH_API_KEY')
    if not search_key:
        raise RuntimeError(
            f"SEARCH_API_KEY is not configured. Set env var or key in {API_KEYS_PATH}."
        )

    return search_key


def _get_shared_async_classifier():
    global _shared_async_classifier
    if _shared_async_classifier is None:
        _shared_async_classifier = LLM_lib(
            key_file_path=API_KEYS_PATH,
            max_tokens=CLASSIFY_MAX_TOKENS,
            temperature=0.85,
            request_timeout_s=CLASSIFY_REQUEST_TIMEOUT_S,
            max_retries=CLASSIFY_MAX_RETRIES,
            retry_backoff_s=CLASSIFY_RETRY_BACKOFF_S,
        )
    return _shared_async_classifier


def _get_shared_sync_classifier():
    global _shared_sync_classifier
    if _shared_sync_classifier is None:
        _shared_sync_classifier = LLM_lib(
            key_file_path=API_KEYS_PATH,
            max_tokens=CLASSIFY_MAX_TOKENS,
            temperature=0.85,
            request_timeout_s=CLASSIFY_REQUEST_TIMEOUT_S,
            max_retries=CLASSIFY_MAX_RETRIES,
            retry_backoff_s=CLASSIFY_RETRY_BACKOFF_S,
        )
    return _shared_sync_classifier

from utils.LLM_lib import *
from utils.crawl_utils import *

def safe_json_serialize(data_dict):
    """Safely serialize dictionary, cleaning problematic strings"""
    def clean_string(s):
        if not isinstance(s, str):
            return s
        # Remove problematic control characters
        s = re.sub(r'[\x00-\x1f\x7f-\x9f"]', '', s)
        return s
    
    cleaned_dict = {}
    for key, value in data_dict.items():
        # Always include the entry, but clean strings when possible
        if isinstance(key, str):
            cleaned_key = key.strip()
        else:
            cleaned_key = key
            
        if isinstance(value, str):
            cleaned_value = clean_string(value)
        else:
            cleaned_value = value
            
        cleaned_dict[cleaned_key] = cleaned_value
    
    return cleaned_dict

USER_AGENTS = [
   #Chrome
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/60.0.3112.113 Safari/537.36',
    'Mozilla/5.0 (Windows NT 6.1; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/60.0.3112.90 Safari/537.36',
    'Mozilla/5.0 (Windows NT 5.1; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/60.0.3112.90 Safari/537.36',
    'Mozilla/5.0 (Windows NT 6.2; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/60.0.3112.90 Safari/537.36',
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/44.0.2403.157 Safari/537.36',
    'Mozilla/5.0 (Windows NT 6.3; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/60.0.3112.113 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/57.0.2987.133 Safari/537.36',
    'Mozilla/5.0 (Windows NT 6.1; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/57.0.2987.133 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/55.0.2883.87 Safari/537.36',
    'Mozilla/5.0 (Windows NT 6.1; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/55.0.2883.87 Safari/537.36',
    #Firefox
    'Mozilla/4.0 (compatible; MSIE 9.0; Windows NT 6.1)',
    'Mozilla/5.0 (Windows NT 6.1; WOW64; Trident/7.0; rv:11.0) like Gecko',
    'Mozilla/5.0 (compatible; MSIE 9.0; Windows NT 6.1; WOW64; Trident/5.0)',
    'Mozilla/5.0 (Windows NT 6.1; Trident/7.0; rv:11.0) like Gecko',
    'Mozilla/5.0 (Windows NT 6.2; WOW64; Trident/7.0; rv:11.0) like Gecko',
    'Mozilla/5.0 (Windows NT 10.0; WOW64; Trident/7.0; rv:11.0) like Gecko',
    'Mozilla/5.0 (compatible; MSIE 9.0; Windows NT 6.0; Trident/5.0)',
    'Mozilla/5.0 (Windows NT 6.3; WOW64; Trident/7.0; rv:11.0) like Gecko',
    'Mozilla/5.0 (compatible; MSIE 9.0; Windows NT 6.1; Trident/5.0)',
    'Mozilla/5.0 (Windows NT 6.1; Win64; x64; Trident/7.0; rv:11.0) like Gecko',
    'Mozilla/5.0 (compatible; MSIE 10.0; Windows NT 6.1; WOW64; Trident/6.0)',
    'Mozilla/5.0 (compatible; MSIE 10.0; Windows NT 6.1; Trident/6.0)',
    'Mozilla/4.0 (compatible; MSIE 8.0; Windows NT 5.1; Trident/4.0; .NET CLR 2.0.50727; .NET CLR 3.0.4506.2152; .NET CLR 3.5.30729)'
]

def query_generator(model_name, sys_prompt, task_prompt, conversation=None, metric_callback=None):
    query_gen = LLM_lib(
                    key_file_path=API_KEYS_PATH, 
                    max_tokens=1024, 
                    temperature = 1.5
                )
    
    init_queries, llm_meta = query_gen.get_response(
        model_name = model_name,
        sys_prompt=sys_prompt,
        user_prompt=task_prompt,
        messages=conversation,
        return_metadata=True,
    )
    if metric_callback and llm_meta:
        metric_callback(
            {
                "component": "query_gen",
                "operation": "query_generator",
                "status": llm_meta.get("status", "ok"),
                "provider": llm_meta.get("provider", ""),
                "model": llm_meta.get("model", model_name),
                "latencyMs": llm_meta.get("latencyMs", 0.0),
                "promptTokens": llm_meta.get("promptTokens", 0),
                "completionTokens": llm_meta.get("completionTokens", 0),
                "totalTokens": llm_meta.get("totalTokens", 0),
                "estimatedCostUsd": llm_meta.get("estimatedCostUsd", 0.0),
                "error": llm_meta.get("error", ""),
                "meta": json.dumps({"promptChars": len(task_prompt or "")}),
            }
        )
    return init_queries


def web_content_judger(title, body, topic_seed, model_name, sys_prompt_name='default_prompt',task_prompt_name='database_course_classify'):
    content_judger = _get_shared_sync_classifier()
    
    task_templates_lib = _load_prompt_templates()
        
    sys_prompt = task_templates_lib.get(sys_prompt_name, "You are a helpful assistant")
    task_template = task_templates_lib.get(task_prompt_name)
    if not task_template:
        raise RuntimeError(f"Missing prompt template '{task_prompt_name}'")
    task_prompt = _render_prompt_template(
        task_template,
        clean_title=title,
        clean_body=body,
        topic_seed=topic_seed,
    )
    
    try:
        pred = content_judger.get_response(
            model_name = model_name,
            sys_prompt=sys_prompt,
            user_prompt=task_prompt
        )
        return pred
    except Exception as e:
        print('Error in webcontent_judger:', e)
        return {'pred': False, 'reason': "This url gave a network error."}
    
async def async_web_content_judger(
    title,
    body,
    topic_seed,
    model_name,
    sys_prompt_name='default_prompt',
    task_prompt_name='database_course_classify',
    metric_callback=None,
    return_llm_meta=False,
):
    started = time.perf_counter()
    content_judger = _get_shared_async_classifier()
    
    task_templates_lib = _load_prompt_templates()
        
    sys_prompt = task_templates_lib.get(sys_prompt_name, "You are a helpful assistant")
    task_template = task_templates_lib.get(task_prompt_name)
    if not task_template:
        raise RuntimeError(f"Missing prompt template '{task_prompt_name}'")
    task_prompt = _render_prompt_template(
        task_template,
        clean_title=title,
        clean_body=body,
        topic_seed=topic_seed,
    )
    
    try:
        pred, llm_meta = await content_judger.async_get_response(
            model_name = model_name,
            sys_prompt=sys_prompt,
            user_prompt=task_prompt,
            return_metadata=True,
        )
        if metric_callback and llm_meta:
            metric_callback(
                {
                    "component": "classify",
                    "operation": "relevance_judge",
                    "status": llm_meta.get("status", "ok"),
                    "provider": llm_meta.get("provider", ""),
                    "model": llm_meta.get("model", model_name),
                    "latencyMs": llm_meta.get("latencyMs", 0.0),
                    "promptTokens": llm_meta.get("promptTokens", 0),
                    "completionTokens": llm_meta.get("completionTokens", 0),
                    "totalTokens": llm_meta.get("totalTokens", 0),
                    "estimatedCostUsd": llm_meta.get("estimatedCostUsd", 0.0),
                    "error": llm_meta.get("error", ""),
                    "meta": json.dumps({"titleLen": len(str(title or ""))}),
                }
            )
        if return_llm_meta:
            return pred, llm_meta
        return pred
    except Exception as e:
        print('Error in async_webcontent_judger:', e)
        fallback = {'pred': False, 'reason': "This url gave a network error."}
        fallback_meta = {
            "timestamp": datetime.utcnow().isoformat(),
            "provider": "deepseek",
            "model": model_name,
            "latencyMs": round((time.perf_counter() - started) * 1000.0, 3),
            "promptTokens": 0,
            "completionTokens": 0,
            "totalTokens": 0,
            "estimatedCostUsd": 0.0,
            "status": "error",
            "error": str(e)[:500],
        }
        if return_llm_meta:
            return fallback, fallback_meta
        return fallback

def _normalize_binary_pred(raw_pred):
    normalized = str(raw_pred or "").strip().lower()
    if normalized in {"yes", "y", "true", "1", "relevant"}:
        return "Yes"
    if normalized in {"no", "n", "false", "0", "irrelevant"}:
        return "No"
    return "No"


async def async_batch_snippet_relevance_judger(
    *,
    items,
    topic_seed,
    model_name,
    metric_callback=None,
    return_llm_meta=False,
):
    started = time.perf_counter()
    content_judger = _get_shared_async_classifier()

    safe_items = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        item_id = str(item.get("id", "")).strip()
        if not item_id:
            continue
        title = str(item.get("title", "") or "").strip()
        snippet = str(item.get("snippet", "") or "").strip()
        if len(title) > 260:
            title = title[:260]
        if len(snippet) > 550:
            snippet = snippet[:550]
        safe_items.append({"id": item_id, "title": title, "snippet": snippet})

    if not safe_items:
        fallback = {}
        fallback_meta = {
            "timestamp": datetime.utcnow().isoformat(),
            "provider": "deepseek",
            "model": model_name,
            "latencyMs": 0.0,
            "promptTokens": 0,
            "completionTokens": 0,
            "totalTokens": 0,
            "estimatedCostUsd": 0.0,
            "status": "error",
            "error": "No valid items for batch classification",
        }
        if return_llm_meta:
            return fallback, fallback_meta
        return fallback

    sys_prompt = "You are a careful relevance classifier."
    batch_payload = json.dumps(
        {"topic_seed": str(topic_seed or ""), "pages": safe_items},
        ensure_ascii=False,
    )
    user_prompt = (
        "Classify each page for relevance to the topic.\n"
        "Use only title and snippet.\n"
        "Return only valid JSON with schema:\n"
        "{\"results\":[{\"id\":\"<id>\",\"pred\":\"Yes|No\",\"reason\":\"<=40 words\"}]}\n"
        "Classify all ids exactly once.\n"
        f"Input:\n{batch_payload}"
    )

    try:
        payload, llm_meta = await content_judger.async_get_response(
            model_name=model_name,
            sys_prompt=sys_prompt,
            user_prompt=user_prompt,
            return_metadata=True,
        )

        result_map = {}
        result_items = []
        if isinstance(payload, dict):
            if isinstance(payload.get("results"), list):
                result_items = payload.get("results")
            elif isinstance(payload.get("items"), list):
                result_items = payload.get("items")

        for row in result_items:
            if not isinstance(row, dict):
                continue
            row_id = str(row.get("id", "")).strip()
            if not row_id:
                continue
            pred = _normalize_binary_pred(row.get("pred"))
            reason = str(row.get("reason", "") or "").strip()
            if not reason:
                reason = "Batch classification returned no reason."
            result_map[row_id] = {"pred": pred, "reason": reason[:220]}

        if metric_callback and llm_meta:
            metric_callback(
                {
                    "component": "classify",
                    "operation": "relevance_judge_batch",
                    "status": llm_meta.get("status", "ok"),
                    "provider": llm_meta.get("provider", ""),
                    "model": llm_meta.get("model", model_name),
                    "latencyMs": llm_meta.get("latencyMs", 0.0),
                    "promptTokens": llm_meta.get("promptTokens", 0),
                    "completionTokens": llm_meta.get("completionTokens", 0),
                    "totalTokens": llm_meta.get("totalTokens", 0),
                    "estimatedCostUsd": llm_meta.get("estimatedCostUsd", 0.0),
                    "error": llm_meta.get("error", ""),
                    "meta": json.dumps({"batchSize": len(safe_items)}),
                }
            )

        if return_llm_meta:
            return result_map, llm_meta
        return result_map
    except Exception as e:
        fallback_meta = {
            "timestamp": datetime.utcnow().isoformat(),
            "provider": "deepseek",
            "model": model_name,
            "latencyMs": round((time.perf_counter() - started) * 1000.0, 3),
            "promptTokens": 0,
            "completionTokens": 0,
            "totalTokens": 0,
            "estimatedCostUsd": 0.0,
            "status": "error",
            "error": str(e)[:500],
        }
        if return_llm_meta:
            return {}, fallback_meta
        return {}

def judge_query_urls(query, visited, task_prompt_name, topic, number_urls=3, time_sleep=1.0):
    try:
        search_url = "https://www.searchapi.io/api/v1/search"
        search_params = {
            'engine': 'google',
            'q': query,
            'api_key': _get_search_key()
        }
        response = requests.get(search_url, params=search_params)
        search_results = response.json()['organic_results']
        search_urls = [result['link'] for result in search_results]
        time.sleep(time_sleep)
        search_urls = list(search_urls)
        judge_res_all = []

        for item_url in search_urls:
            print('item_url:', item_url)
            if item_url not in visited:
                visited.add(item_url)
                title, body = get_content_from_url(url=item_url)
                judge_res = web_content_judger(title, body, topic_seed =topic, model_name = 'deepseek-chat', task_prompt_name=task_prompt_name)
                if judge_res:
                    if 'pred' in judge_res and 'reason' in judge_res:
                        judge_res_all.append({'query':query, 'url': item_url, 'hop': 0, 'pred':judge_res['pred'], 'reason': judge_res['reason']})
                    else:
                        judge_res_all.append({'query': query, 'url': item_url, 'hop': 0, 'pred': 'No', 'reason': 'This url gave a network error.'})
        return judge_res_all, search_urls
    except Exception as e:
        print('Error in judge_query_urls:', e)
        return [], []
    
async def process_link(link, topic, classifiy_prompt, max_links=10, session=None):
    try:
        new_links = await async_extract_links_with_context(link, max_links=max_links, session=session)
        title, body = await async_get_content_from_url(link, session=session)
        if title is None and body is None:
            return _build_error_result(link, 'fetch', 'No content extracted'), new_links
        page_snippet = _build_page_snippet(body)
        judge_res = await async_web_content_judger(title, body, topic_seed=topic, model_name='deepseek-chat', task_prompt_name=classifiy_prompt)
        if judge_res:
            if 'pred' in judge_res and 'reason' in judge_res:
                result = {
                    'url': link,
                    'pred': judge_res['pred'],
                    'reason': judge_res['reason'],
                    'title': title or '',
                    'snippet': page_snippet,
                }
            else:
                result = _build_error_result(link, 'llm_response', 'Missing pred/reason')
        return result, new_links
    except Exception as e:
        print(f"Error in process link: {e}")
        return _build_error_result(link, 'process_link', e), []

async def async_classify_link(
    link,
    topic,
    classifiy_prompt,
    session=None,
    fail_fast=False,
    metric_callback=None,
    llm_semaphore=None,
    fetch_metric_callback=None,
    fetch_options=None,
    cooldown_applied=False,
):
    try:
        run_loop = asyncio.get_running_loop()
        start_ts = run_loop.time()
        fetch_started = run_loop.time()
        fetch_kwargs = dict(fetch_options or {})
        title, body, fetch_meta = await async_get_content_from_url(
            link,
            timeout=CLASSIFY_FETCH_TIMEOUT_S,
            session=session,
            return_metadata=True,
            **fetch_kwargs,
        )
        fetch_ms = (run_loop.time() - fetch_started) * 1000.0
        fetch_meta = fetch_meta or {}
        fetch_outcome = str(fetch_meta.get("outcome", "network_error"))
        fetch_status = int(fetch_meta.get("statusCode", 0) or 0)
        fetch_bytes = int(fetch_meta.get("bytesRead", 0) or 0)
        fetch_type = str(fetch_meta.get("contentType", "") or "")
        fetch_retries = int(fetch_meta.get("retryCount", 0) or 0)
        if fetch_metric_callback:
            fetch_metric_callback(
                {
                    "timestamp": datetime.utcnow().isoformat(),
                    "url": link,
                    "domain": urlparse(link).netloc.lower(),
                    "fetchMs": float(fetch_meta.get("latencyMs", fetch_ms) or fetch_ms),
                    "statusCode": fetch_status,
                    "bytesRead": fetch_bytes,
                    "contentType": fetch_type,
                    "outcome": fetch_outcome,
                    "retryCount": fetch_retries,
                    "cooldownApplied": bool(cooldown_applied),
                }
            )

        if fetch_outcome != "ok":
            reason = str(body or fetch_meta.get("error") or f"Fetch outcome: {fetch_outcome}")[:300]
            if metric_callback:
                metric_callback(
                    {
                        "component": "classify",
                        "operation": "relevance_short_circuit",
                        "status": "ok",
                        "provider": "local",
                        "model": "rule_based",
                        "latencyMs": float(fetch_meta.get("latencyMs", fetch_ms) or fetch_ms),
                        "promptTokens": 0,
                        "completionTokens": 0,
                        "totalTokens": 0,
                        "estimatedCostUsd": 0.0,
                        "error": "",
                        "skipReason": reason,
                        "meta": json.dumps(
                            {
                                "urlHash": _url_hash(link),
                                "httpStatus": fetch_status,
                                "fetchOutcome": fetch_outcome,
                                "fetchMs": round(float(fetch_meta.get("latencyMs", fetch_ms) or fetch_ms), 3),
                                "parseMs": None,
                                "llmMs": 0.0,
                                "totalMs": round((run_loop.time() - start_ts) * 1000.0, 3),
                                "titleLen": len(str(title or "")),
                                "bodyChars": len(str(body or "")),
                                "skipReason": reason,
                                "fetchRetries": fetch_retries,
                                "bytesRead": fetch_bytes,
                                "contentType": fetch_type,
                            }
                        ),
                    }
                )
            return {
                "url": link,
                "pred": "No",
                "reason": reason,
                "title": title or "",
                "snippet": _build_page_snippet(body),
                "fetchOutcome": fetch_outcome,
                "fetchStatusCode": fetch_status,
                "fetchBytesRead": fetch_bytes,
                "fetchRetryCount": fetch_retries,
                "fetchContentType": fetch_type,
            }

        http_status = fetch_status or _extract_http_status(title, body)
        short_circuit, short_reason = _should_short_circuit_classify(title, body)
        if short_circuit:
            reason = short_reason or "Unusable page content"
            if metric_callback:
                metric_callback(
                    {
                        "component": "classify",
                        "operation": "relevance_short_circuit",
                        "status": "ok",
                        "provider": "local",
                        "model": "rule_based",
                        "latencyMs": fetch_ms,
                        "promptTokens": 0,
                        "completionTokens": 0,
                        "totalTokens": 0,
                        "estimatedCostUsd": 0.0,
                        "error": "",
                        "skipReason": reason,
                        "meta": json.dumps(
                            {
                                "urlHash": _url_hash(link),
                                "httpStatus": http_status,
                                "fetchOutcome": fetch_outcome,
                                "fetchMs": round(float(fetch_meta.get("latencyMs", fetch_ms) or fetch_ms), 3),
                                "parseMs": None,
                                "llmMs": 0.0,
                                "totalMs": round((run_loop.time() - start_ts) * 1000.0, 3),
                                "titleLen": len(str(title or "")),
                                "bodyChars": len(str(body or "")),
                                "skipReason": reason,
                                "fetchRetries": fetch_retries,
                                "bytesRead": fetch_bytes,
                                "contentType": fetch_type,
                            }
                        ),
                    }
                )
            return {
                "url": link,
                "pred": "No",
                "reason": reason,
                "title": title or "",
                "snippet": _build_page_snippet(body),
                "fetchOutcome": fetch_outcome,
                "fetchStatusCode": fetch_status,
                "fetchBytesRead": fetch_bytes,
                "fetchRetryCount": fetch_retries,
                "fetchContentType": fetch_type,
            }

        page_snippet = _build_page_snippet(body)
        llm_started = run_loop.time()
        if llm_semaphore is None:
            judge_res, llm_meta = await async_web_content_judger(
                title,
                body,
                topic_seed=topic,
                model_name='deepseek-chat',
                task_prompt_name=classifiy_prompt,
                metric_callback=None,
                return_llm_meta=True,
            )
        else:
            async with llm_semaphore:
                judge_res, llm_meta = await async_web_content_judger(
                    title,
                    body,
                    topic_seed=topic,
                    model_name='deepseek-chat',
                    task_prompt_name=classifiy_prompt,
                    metric_callback=None,
                    return_llm_meta=True,
                )
        llm_ms = (run_loop.time() - llm_started) * 1000.0

        if metric_callback:
            llm_meta = llm_meta or {}
            llm_status = llm_meta.get("status", "ok")
            llm_error = llm_meta.get("error", "")
            if llm_status != "ok" and not llm_error:
                llm_error = str((judge_res or {}).get("reason", ""))[:300]
            metric_callback(
                {
                    "component": "classify",
                    "operation": "relevance_judge",
                    "status": llm_status,
                    "provider": llm_meta.get("provider", ""),
                    "model": llm_meta.get("model", "deepseek-chat"),
                    "latencyMs": llm_meta.get("latencyMs", llm_ms),
                    "promptTokens": llm_meta.get("promptTokens", 0),
                    "completionTokens": llm_meta.get("completionTokens", 0),
                    "totalTokens": llm_meta.get("totalTokens", 0),
                    "estimatedCostUsd": llm_meta.get("estimatedCostUsd", 0.0),
                    "error": llm_error,
                    "meta": json.dumps(
                        {
                            "urlHash": _url_hash(link),
                            "httpStatus": http_status,
                            "fetchMs": round(fetch_ms, 3),
                            "parseMs": None,
                            "llmMs": round(llm_ms, 3),
                            "queueWaitMs": round(max(0.0, llm_ms - float(llm_meta.get("latencyMs", llm_ms))), 3),
                            "totalMs": round((run_loop.time() - start_ts) * 1000.0, 3),
                            "titleLen": len(str(title or "")),
                            "bodyChars": len(str(body or "")),
                            "fetchOutcome": fetch_outcome,
                            "fetchRetries": fetch_retries,
                            "bytesRead": fetch_bytes,
                            "contentType": fetch_type,
                            "classifyFetchTimeoutS": CLASSIFY_FETCH_TIMEOUT_S,
                            "classifyRequestTimeoutS": CLASSIFY_REQUEST_TIMEOUT_S,
                            "classifyMaxRetries": CLASSIFY_MAX_RETRIES,
                        }
                    ),
                }
            )

        if judge_res:
            if 'pred' in judge_res and 'reason' in judge_res:
                result = {
                    'url': link,
                    'pred': judge_res['pred'],
                    'reason': judge_res['reason'],
                    'title': title or '',
                    'snippet': page_snippet,
                    "fetchOutcome": fetch_outcome,
                    "fetchStatusCode": fetch_status,
                    "fetchBytesRead": fetch_bytes,
                    "fetchRetryCount": fetch_retries,
                    "fetchContentType": fetch_type,
                }
            else:
                result = _build_error_result(link, 'llm_response', 'Missing pred/reason')
        else:
            result = _build_error_result(link, 'llm_response', 'No judge_res returned')
        return result
    except Exception as e:
        if fail_fast:
            raise
        return _build_error_result(link, 'classify_exception', e)

async def process_frontier(frontier, topic, classifiy_prompt, num_choice=10, session=None):
    try:
        links_to_judge = await async_classic_crawl(frontier, topic, sys_prompt="You are a helpful assistant", num_choice=num_choice, model_name='deepseek-chat')
        judge_res = []
        links = links_to_judge['urls']
        async with aiohttp.ClientSession() as session:
            tasks = []
            for link in links:
                task = asyncio.create_task(async_classify_link(link, topic, classifiy_prompt, session=session))
            results = await asyncio.gather(*tasks)
            for result in results:
                judge_res.append(result)
        return judge_res
    except Exception as e:
        print('Error in process_frontier:', e)
        return []

async def async_judge_query_urls(
    query,
    visited,
    task_prompt_name,
    topic,
    time_sleep=0.25,
    session=None,
    fail_fast=False,
    classify_limit=10,
    metric_callback=None,
    classify_fn=None,
):
    close_session = False
    try:
        search_url = "https://www.searchapi.io/api/v1/search"
        search_params = {
            'engine': 'google',
            'q': query,
            'api_key': _get_search_key()
        }

        if session is None:
            session = aiohttp.ClientSession()
            close_session = True

        search_started = time.perf_counter()
        response_status = 0
        async with session.get(search_url, params=search_params, timeout=20) as response:
            response_status = int(response.status or 0)
            if response.status != 200:
                body = await response.text()
                raise RuntimeError(
                    f"SearchAPI request failed ({response.status}): {body[:200]}"
                )
            payload = await response.json(content_type=None)
        search_latency_ms = (time.perf_counter() - search_started) * 1000.0

        search_results = payload.get('organic_results', []) if isinstance(payload, dict) else []
        search_urls = [item.get('link') for item in search_results if item.get('link')]
        if metric_callback:
            metric_callback(
                {
                    "component": "search",
                    "operation": "search_api_query",
                    "status": "ok",
                    "provider": "searchapi",
                    "model": "google-organic",
                    "latencyMs": search_latency_ms,
                    "promptTokens": 0,
                    "completionTokens": 0,
                    "totalTokens": 0,
                    "estimatedCostUsd": 0.0,
                    "error": "",
                    "meta": json.dumps(
                        {
                            "queryChars": len(str(query or "")),
                            "organicResults": len(search_results),
                            "urlsExtracted": len(search_urls),
                            "statusCode": response_status,
                        }
                    ),
                }
            )

        if time_sleep and time_sleep > 0:
            await asyncio.sleep(time_sleep)

        judge_res_all = []
        tasks = []
        for link in search_urls:
            if link not in visited:
                if classify_fn is not None:
                    task = asyncio.create_task(classify_fn(link, session))
                else:
                    task = asyncio.create_task(
                        async_classify_link(
                            link,
                            topic,
                            task_prompt_name,
                            session=session,
                            fail_fast=False,
                            metric_callback=metric_callback,
                        )
                    )
                tasks.append(task)
            if len(tasks) >= max(1, int(classify_limit)):
                break

        results_links = await asyncio.gather(*tasks, return_exceptions=True)
        for result in results_links:
            if isinstance(result, Exception):
                if fail_fast:
                    raise RuntimeError(f"Classification task failed: {result}") from result
                continue
            if result:
                judge_res_all.append(result)

        return judge_res_all, list(search_urls)
    except Exception as e:
        if metric_callback:
            metric_callback(
                {
                    "component": "search",
                    "operation": "search_api_query",
                    "status": "error",
                    "provider": "searchapi",
                    "model": "google-organic",
                    "latencyMs": 0.0,
                    "promptTokens": 0,
                    "completionTokens": 0,
                    "totalTokens": 0,
                    "estimatedCostUsd": 0.0,
                    "error": str(e)[:300],
                    "meta": json.dumps(
                        {
                            "queryChars": len(str(query or "")),
                        }
                    ),
                }
            )
        if fail_fast:
            raise RuntimeError(f"Error in async_judge_query_urls: {e}") from e
        print('Error in async_judge_query_urls:', e)
        return [], []
    finally:
        if close_session and session is not None:
            await session.close()

def query_feedback(query, judge_res_all, feedback_level='query'):
    query_feedback = "The query **{query_item}** gave the following results: \n".format(query_item=query)
    if len(judge_res_all) == 0:
        query_feedback = "The query **{query_item}** did not return any webpages that we have not already seen. Consider adding more variation to the queries, such as removing words or phrases which have been included in many previous queries.".format(query_item=query)
    else:
        if feedback_level == 'url':
            for item in judge_res_all:
                if item['pred'] == 'Yes':
                    url_quality = 'Good'
                else:
                    url_quality = 'Bad'
                    
                query_feedback += "URL_Quality: {url_quality}, Reason: {url_reason}, URL: {url_detail}\n".format(url_quality=url_quality,url_reason=item[3],url_detail=item[1])
            print(query_feedback)
            
        elif feedback_level == 'query':
            quality_score = 0
            for item in judge_res_all:
                if item['pred'] == 'Yes':
                    quality_score += 1
            quality_score = quality_score / len(judge_res_all)
            # to percent string
            quality_score_str = "{:.2%}".format(quality_score)
                
            query_feedback += "Query Quality: {eval_score} of the urls from this query is good.\n".format(eval_score=quality_score_str)
            print(query_feedback)
    return query_feedback

def explore(query, search_urls, visited, topic, task_prompt_name, n_hops=3, link_per_page=5):
    try:
        results = []
        all_links = []
        visited = visited
        for url in search_urls:
            new_links = hop(url, n_hops=n_hops, max_links_per_page=link_per_page)
            all_links.extend(new_links)
        for item in all_links:
            item_url = item['url']
            print('item_url:', item_url)
            if item_url not in visited:
                visited.add(item_url)
                title, body = get_content_from_url(url=item_url)
                judge_res = web_content_judger(title, body, topic_seed=topic, model_name = 'deepseek-chat', task_prompt_name=task_prompt_name)
                if judge_res:
                    if 'pred' in judge_res and 'reason' in judge_res:
                        results.append({'query':query, 'url': item_url, 'hop': item['hop'], 'pred':judge_res['pred'], 'reason': judge_res['reason']})
                    else:
                        results.append({'query': query, 'url': item_url, 'hop': item['hop'], 'pred': 'No', 'reason': 'This url gave a network error.'})
        return results, visited
    except Exception as e:
        print('Error in explore:', e)
        return [], visited
    
def classic_crawl(frontier, topic, sys_prompt, num_choice=10, model_name='deepseek-chat', max_frontier_size=1000):
    """
    A robust version of classic_crawl that handles large frontiers and provides better error reporting
    """
    
    # Create a copy of the frontier to avoid modifying the original
    working_frontier = OrderedDict(frontier)
    
    # Limit frontier size to prevent issues
    if len(working_frontier) > max_frontier_size:
        print(f"Frontier size ({len(working_frontier)}) exceeds limit ({max_frontier_size}), truncating...")
        working_frontier = OrderedDict(list(working_frontier.items())[:max_frontier_size])
    
    # Clean the frontier to avoid JSON issues
    working_frontier = safe_json_serialize(working_frontier)
    
    sample_urls = list(working_frontier.keys())[:3] if working_frontier else []
    sample_text = f"Example URLs from the dictionary: {sample_urls}" if sample_urls else ""
    
    prompt = f"""You are helping with a focused web crawl for the topic: {topic}

CRITICAL INSTRUCTIONS:
1. You MUST only choose URLs that appear EXACTLY in the dictionary below
2. Do NOT suggest any URLs that are not in this dictionary  
3. Do NOT suggest root domains, parent pages, or modified versions of the URLs
4. Do NOT hallucinate or create new URLs
5. Copy the URLs EXACTLY as they appear - do not modify them in any way

{sample_text}

Below is a dictionary of available URLs and their context. Choose the {num_choice} URLs from this dictionary that are most relevant to the topic.

Format your response as a JSON object with a single key 'urls' and the value being an array of the EXACT URLs you choose from the dictionary.

Available URLs Dictionary:
{working_frontier}

REMINDER: Only choose URLs that appear EXACTLY in the dictionary above. Do not modify or suggest alternative URLs."""

    try:
        llm = LLM_lib(
            key_file_path=API_KEYS_PATH,
            max_tokens=8000,  # Increase max tokens for response
            temperature=0.5   # Lower temperature for more consistent results
        )
        
        print(f"Making API call with frontier size: {len(working_frontier)}")
        print(f"Estimated prompt tokens: {len(prompt) / 4:,.0f}")
        
        response = llm.get_response(
            model_name=model_name,
            sys_prompt=sys_prompt,
            user_prompt=prompt
        )
        
        if response is None:
            print("❌ API call returned None - check the API error above")
            return None
            
        if not isinstance(response, dict):
            print(f"❌ Unexpected response type: {type(response)}")
            print(f"Response content: {response}")
            return None
            
        if 'urls' not in response:
            print(f"❌ Response missing 'urls' key. Keys found: {list(response.keys())}")
            print(f"Full response: {response}")
            return None
            
        urls = response['urls']
        if not isinstance(urls, list):
            print(f"❌ 'urls' is not a list: {type(urls)}")
            return None
            
        print(f"✅ Successfully got {len(urls)} URLs from API")
        return response
        
    except Exception as e:
        print(f"❌ Error in classic_crawl: {e}")
        print(traceback.format_exc())
        return None

async def async_classic_crawl(
    frontier,
    topic,
    sys_prompt,
    num_choice=10,
    model_name='deepseek-chat',
    metric_callback=None,
    max_frontier_items=180,
):
    # Keep frontier payload bounded to reduce latency and token cost.
    if isinstance(frontier, dict) and len(frontier) > max_frontier_items:
        frontier = dict(list(frontier.items())[:max_frontier_items])
    # Show sample URLs to make it crystal clear what format is expected
    sample_urls = list(frontier.keys())[:3] if frontier else []
    sample_text = f"Example URLs from the dictionary: {sample_urls}" if sample_urls else ""
    
    prompt = f"""You are helping with a focused web crawl for the topic: {topic}
We are trying to compile a list of all {topic}
CRITICAL INSTRUCTIONS:
1. You MUST only choose URLs that appear EXACTLY in the dictionary below
2. Do NOT suggest any URLs that are not in this dictionary
3. Do NOT suggest root domains, parent pages, or modified versions of the URLs
4. Do NOT hallucinate or create new URLs
5. Copy the URLs EXACTLY as they appear - do not modify them in any way

{sample_text}

Below is a dictionary of available URLs and their context. Choose the {num_choice} URLs from this dictionary that are most relevant to the topic.
Try to choose URLs that are different from each other to get more diversity in the crawl results, rather than choosing many URLs from the same domain.

Format your response as a JSON object with a single key 'urls' and the value being an array of the EXACT URLs you choose from the dictionary.

Available URLs Dictionary:
{frontier}

REMINDER: Only choose URLs that appear EXACTLY in the dictionary above. Do not modify or suggest alternative URLs."""
    llm = LLM_lib(
                    key_file_path=API_KEYS_PATH,
                    max_tokens=8000,
                    temperature=0.5
                )
    response, llm_meta = await llm.async_get_response(
        model_name=model_name,
        sys_prompt=sys_prompt,
        user_prompt=prompt,
        return_metadata=True,
    )
    if metric_callback and llm_meta:
        metric_callback(
            {
                "component": "frontier_select",
                "operation": "async_classic_crawl",
                "status": llm_meta.get("status", "ok"),
                "provider": llm_meta.get("provider", ""),
                "model": llm_meta.get("model", model_name),
                "latencyMs": llm_meta.get("latencyMs", 0.0),
                "promptTokens": llm_meta.get("promptTokens", 0),
                "completionTokens": llm_meta.get("completionTokens", 0),
                "totalTokens": llm_meta.get("totalTokens", 0),
                "estimatedCostUsd": llm_meta.get("estimatedCostUsd", 0.0),
                "error": llm_meta.get("error", ""),
                "meta": json.dumps(
                    {
                        "frontierItems": len(frontier) if isinstance(frontier, dict) else 0,
                        "numChoice": int(num_choice),
                    }
                ),
            }
        )
    return response

def score_crawl(frontier, topic, sys_prompt, model_name='deepseek-chat'):
    prompt = f"Here is a dictionary of urls and the context in which they appear in a webpage. Please provide a precise score between 0 and 1000 for each url based on how relevant you think the webpage will be to the topic: {topic}. Your scores should be precise, not just rounded to the nearest 100 or 50. Format your response as a json object with the url from my dictionary as the key and the score as the value. Only use urls in this dictionary for keys. Dictionary: {frontier}"
    llm = LLM_lib(
                    key_file_path=API_KEYS_PATH,
                    max_tokens=8000,
                    temperature=0.85
                )
    response = llm.get_response(
        model_name=model_name,
        sys_prompt=sys_prompt,
        user_prompt=prompt
    )
    return response

async def async_score_crawl(frontier, topic, sys_prompt, model_name='deepseek-chat'):
    prompt = (
        f"Here is a dictionary of urls and the context in which they appear in a webpage. "
        f"Please provide a precise score between 0 and 1000 for each url based on how relevant you think the webpage will be to the topic: {topic}. "
        f"Your scores should be precise, not just rounded to the nearest 100 or 50. "
        f"Format your response as a json object with the url from my dictionary as the key and the score as the value. "
        f"Only use urls in this dictionary for keys. Dictionary: {frontier}"
    )
    llm = LLM_lib(
        key_file_path=API_KEYS_PATH,
        max_tokens=8000,
        temperature=0.85
    )
    response = await llm.async_get_response(  # This must be async!
        model_name=model_name,
        sys_prompt=sys_prompt,
        user_prompt=prompt
    )
    return response

async def async_dfs(frontier, topic, sys_prompt, model_name='deepseek-chat'):
    prompt = (
        f"We are performing a focused web crawl to find pages which are related to the topic: {topic}. "
        f"Here is a dictionary of urls and the context in which they appear in a webpage. "
        f"Please choose one url from the dictionary which you think is most relevant to the topic or would lead to more relevant pages."
        f"Format your response as a json object with a single key 'url' and the value being the url you choose."
        f"Only use urls in this dictionary for keys. Dictionary: {frontier}"
    )
    llm = LLM_lib(
        key_file_path=API_KEYS_PATH,
        max_tokens=8000,
        temperature=0.85
    )
    response = await llm.async_get_response(  # This must be async!
        model_name=model_name,
        sys_prompt=sys_prompt,
        user_prompt=prompt
    )
    return response
