from __future__ import annotations

from datetime import datetime
import re
from urllib.parse import unquote
from urllib.parse import urlparse


def normalize_prediction(value: str | None) -> str:
    if not value:
        return "no"

    lowered = value.strip().lower()
    if lowered in {"yes", "true", "relevant"}:
        return "yes"

    return "no"


GENERIC_TITLE_TOKENS = {
    "home",
    "index",
    "index.html",
    "index.htm",
    "default",
    "default.aspx",
    "default.asp",
    "untitled",
}


def _clean_whitespace(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _is_informative_title(value: str | None) -> bool:
    if not value:
        return False
    normalized = _clean_whitespace(str(value))
    if len(normalized) < 4:
        return False
    lowered = normalized.lower()
    return lowered not in GENERIC_TITLE_TOKENS


def _build_title_from_url(parsed, domain: str) -> str:
    raw_path = unquote(parsed.path or "").strip("/")
    if not raw_path:
        return f"Page on {domain}"

    segment = raw_path.split("/")[-1]
    segment = re.sub(r"\.[A-Za-z0-9]{2,6}$", "", segment)
    segment = re.sub(r"[-_]+", " ", segment)
    segment = _clean_whitespace(segment)

    if not _is_informative_title(segment):
        parent_segment = raw_path.split("/")[-2] if len(raw_path.split("/")) > 1 else ""
        parent_segment = re.sub(r"[-_]+", " ", parent_segment)
        parent_segment = _clean_whitespace(parent_segment)
        if _is_informative_title(parent_segment):
            return parent_segment.title()
        return f"Page on {domain}"

    return segment.title()


def build_result_payload(
    *,
    session_id: str,
    sequence_number: int,
    query: str,
    classifier_result: dict,
    min_relevance: float,
) -> dict:
    url = classifier_result.get("url", "")
    parsed = urlparse(url)
    domain = parsed.netloc or "unknown"
    relevance_from_pred = 0.9 if normalize_prediction(classifier_result.get("pred")) == "yes" else 0.45
    relevance_score = max(0.3, min(0.99, relevance_from_pred))

    if normalize_prediction(classifier_result.get("pred")) == "yes":
        feedback = "yes"
    elif relevance_score >= min_relevance:
        feedback = "yes"
    else:
        feedback = "no"

    reason = classifier_result.get("reason", "No reason provided")
    model_title = classifier_result.get("title", "")
    if _is_informative_title(model_title):
        title = _clean_whitespace(str(model_title))
    else:
        title = _build_title_from_url(parsed, domain)

    snippet = _clean_whitespace(str(classifier_result.get("snippet", "")))
    if not snippet:
        snippet = reason

    return {
        "id": f"{session_id}-result-{sequence_number}",
        "url": url,
        "domain": domain,
        "title": title,
        "snippet": snippet,
        "relevanceScore": round(relevance_score, 2),
        "timestamp": datetime.now().strftime("%H:%M:%S"),
        "feedback": feedback,
        "notes": "",
        "feedbackSubmitted": False,
        "feedbackSubmittedAt": None,
        "status": "ready",
        "pred": classifier_result.get("pred", "No"),
        "reason": reason,
        "query": query,
    }


def build_progress_payload(stats: dict, query: str | None = None) -> dict:
    payload = {
        "stats": stats,
    }
    if query:
        payload["query"] = query
    return payload
