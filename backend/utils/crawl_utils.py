import logging
from urllib.parse import urljoin
import requests
from bs4 import BeautifulSoup
from cleantext import clean
import re
import pickle
import json
import random
from pydantic import BaseModel, TypeAdapter
import os
import pdfplumber
import mimetypes
import pandas as pd
import tldextract
from urllib.parse import urlparse
from googlesearch import search
import time
from collections import defaultdict
import aiohttp
import asyncio
import tempfile

SKIP_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico", ".bmp", ".tiff",
    ".mp4", ".mov", ".avi", ".mkv", ".mp3", ".wav", ".ogg",
    ".zip", ".rar", ".7z", ".tar", ".gz",
    ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx",
    ".csv", ".xml", ".json", ".rss", ".atom",
}

SKIP_NETLOCS = {
    "bit.ly", "t.co", "conta.cc", "mailchi.mp",
}


def should_skip_url(url):
    try:
        parsed = urlparse(url)
        netloc = parsed.netloc.lower().replace("www.", "")
        if netloc in SKIP_NETLOCS:
            return True

        path = parsed.path.lower()
        for ext in SKIP_EXTENSIONS:
            if path.endswith(ext):
                return True

        return False
    except Exception:
        return False


def is_social_media_domain(url):
    """
    Check if a URL belongs to a social media platform that should be skipped during crawling.
    These sites are problematic due to:
    - Heavy JavaScript rendering
    - Bot detection systems
    - Limited external links
    - zstd compression issues
    """
    social_media_domains = {
        # Major social platforms
        'facebook.com', 'www.facebook.com', 'm.facebook.com', 'fb.com',
        'instagram.com', 'www.instagram.com', 'm.instagram.com',
        'twitter.com', 'www.twitter.com', 'm.twitter.com', 'x.com', 'www.x.com',
        'tiktok.com', 'www.tiktok.com', 'm.tiktok.com',
        'snapchat.com', 'www.snapchat.com',
        'pinterest.com', 'www.pinterest.com', 'pin.it',
        'youtube.com', 'www.youtube.com', 'm.youtube.com', 'youtu.be',
        'reddit.com', 'www.reddit.com', 'm.reddit.com',
        'discord.com', 'www.discord.com',
        'telegram.org', 'telegram.me', 't.me',
        'whatsapp.com', 'www.whatsapp.com', 'wa.me',
        # Other problematic domains
        'tumblr.com', 'www.tumblr.com',
        'vimeo.com', 'www.vimeo.com',
        'flickr.com', 'www.flickr.com',
    }
    
    try:
        parsed = urlparse(url)
        domain = parsed.netloc.lower()
        
        # Remove 'www.' prefix for comparison if not already in our set
        domain_without_www = domain.replace('www.', '') if domain.startswith('www.') else domain
        
        return domain in social_media_domains or domain_without_www in social_media_domains
    except Exception:
        return False

def create_robust_session(**kwargs):
    """
    Create an aiohttp ClientSession with settings to handle various content-encoding issues.
    This helps prevent errors with servers that send malformed or unsupported compression.
    """
    # Default connector settings that are more lenient
    connector = aiohttp.TCPConnector(
        limit=100,
        limit_per_host=30,
        ttl_dns_cache=300,
        use_dns_cache=True
    )
    
    # Default timeout
    timeout = aiohttp.ClientTimeout(total=30)
    
    # Custom headers that might help with compression issues
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.5',
        'Accept-Encoding': 'gzip, deflate, br',  # Explicitly exclude zstd if problematic
    }
    
    # Override with any user-provided settings
    session_kwargs = {
        'connector': connector,
        'timeout': timeout,
        'headers': headers,
        **kwargs
    }
    
    return aiohttp.ClientSession(**session_kwargs)

class Relevance(BaseModel):
    pred: bool
    reason: str

def download_url(url):
    return requests.get(url).text


def clean_text_paras(title, paras):
        try:
            clean_title = clean(re.sub(r'\W+\s', ' ', title))
            clean_body = []
            for para in paras:
                text = para.get_text()
                subbed_text = re.sub(r'\W+\s', ' ', text)
                clean_body.append(clean(subbed_text))
            clean_body = ' '.join(clean_body)
            clean_body_start = " ".join(clean_body.split()[0:64])
            return clean_title, clean_body_start
        except Exception as e:
            return 'Failed to clean', e
        
        
def fetch_html_to_csv(urls, output_file):
    """
    Fetches the title of each webpage from a list of URLs and writes the URL and title to a CSV file.

    Args:
        urls (list): List of URLs to fetch.
        output_file (str): Path to the output CSV file.
    """
    with open(output_file, mode='a', newline='', encoding='utf-8') as csv_file:
        writer = csv.writer(csv_file)
        i = 0
        for url in urls:
            try:
                print(f"Processing: {url}")
                response = requests.get(url, timeout=10)
                response.raise_for_status()  # Raise HTTPError for bad responses (4xx and 5xx)

                soup = BeautifulSoup(response.text, 'html.parser')
                title = soup.title.string if soup.title else "No title found"
                paras = soup.findAll('p')
                clean_title, clean_body = clean_text_paras(title, paras)
                
                writer.writerow([url, clean_title, clean_body])
                
                print(i)
            except requests.exceptions.RequestException as e:
                print(f"Error fetching {url}: {e}")
                writer.writerow([url, "Error fetching title"])
            i += 1

def get_content_from_url(url, timeout=10):
    """Determines if a URL is an HTML or PDF and extracts text accordingly."""
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        # Check if the URL is reachable
        response = requests.head(url, allow_redirects=True, headers=headers, timeout=timeout)
        response.raise_for_status()  # Raise an error for HTTP issues (4xx, 5xx)
        content_type = response.headers.get("Content-Type", "")

        if "pdf" in content_type:
            return extract_text_from_pdf(url, timeout)
        elif "html" in content_type:
            return extract_text_from_html(url, timeout)
        
        # If Content-Type is missing, make a GET request to inspect content
        response = requests.get(url, headers=headers, timeout=timeout, stream=True)
        response.raise_for_status()
        
        # Check first few bytes for PDF magic number (PDF file signature)
        first_bytes = response.raw.read(5)
        if first_bytes.startswith(b"%PDF-"):
            return extract_text_from_pdf(url, timeout)
        
        # If the response is text-based, assume it's HTML
        text_preview = response.content[:500].decode(errors="ignore").lower()
        if "<html" in text_preview or "<body" in text_preview:
            return extract_text_from_html(url, timeout)

        # Fallback: Guess based on URL extension
        guessed_type, _ = mimetypes.guess_type(url)
        if guessed_type == "application/pdf":
            return extract_text_from_pdf(url, timeout)
        elif guessed_type == "text/html":
            return extract_text_from_html(url, timeout)
        
        print(f'Unsupported content type: {content_type} with url {url}')
        return None, None

    except Exception as e:
        print(f'Exception in get_content: {e}')
        return None, None

def extract_text_from_html(url, timeout):
    """Extracts text from an HTML webpage."""
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        response = requests.get(url, headers=headers, timeout=timeout)
        response.raise_for_status()

        # Get visible text from webpage
        title, content = clean_text_html(response.text)
        return title, content
    except requests.exceptions.RequestException as e:
        return None, f"Error fetching HTML page: {e}"
    
def extract_text_from_pdf(url, timeout):
    """Downloads and extracts text from a PDF file."""
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        response = requests.get(url, headers=headers, timeout=timeout, stream=True)
        response.raise_for_status()

        pdf_filename = "temp.pdf"
        
        with open(pdf_filename, "wb") as file:
            file.write(response.content)

        title, content = extract_pdf_text(pdf_filename)

        # Clean up
        os.remove(pdf_filename)

        return title, content
    except requests.exceptions.RequestException as e:
        return None, f"Error fetching PDF: {e}"
    except Exception as e:
        return None, f"Error processing PDF: {e}"


def extract_pdf_text(pdf_path):
    """Extracts the title and body content from a PDF file."""
    with pdfplumber.open(pdf_path) as pdf:
        all_text = []
        
        for page in pdf.pages:
            text = page.extract_text()
            if text:
                all_text.extend(text.split("\n"))  # Split into lines
        
        if not all_text:
            return None, None

        # Assume first line is the title
        title = all_text[:3]
        content = "\n".join(all_text[3:20])

        return title, content
       
def clean_text_html(html):
    """
    Extract a meaningful title and a body-focused summary from HTML.
    Prioritizes content from main/article-like regions and down-weights
    link-heavy blocks (menus, nav lists, etc).
    """
    soup = BeautifulSoup(html, "html.parser")

    def normalize_ws(text):
        return re.sub(r"\s+", " ", (text or "")).strip()

    def link_density(node):
        all_text = normalize_ws(node.get_text(" ", strip=True))
        if not all_text:
            return 0.0
        total_words = len(all_text.split())
        if total_words == 0:
            return 0.0
        link_text = normalize_ws(" ".join(link.get_text(" ", strip=True) for link in node.find_all("a")))
        link_words = len(link_text.split())
        return link_words / total_words

    # Prefer metadata titles when present.
    title = "No Title"
    og_title = soup.find("meta", attrs={"property": "og:title"})
    twitter_title = soup.find("meta", attrs={"name": "twitter:title"})
    if og_title and og_title.get("content"):
        title = normalize_ws(og_title.get("content"))
    elif twitter_title and twitter_title.get("content"):
        title = normalize_ws(twitter_title.get("content"))
    elif soup.title and soup.title.string:
        title = normalize_ws(soup.title.string)

    # Remove structurally noisy elements before extracting text.
    for tag in soup(
        [
            "script",
            "style",
            "noscript",
            "template",
            "svg",
            "path",
            "iframe",
            "header",
            "footer",
            "nav",
            "aside",
            "form",
            "button",
            "input",
        ]
    ):
        tag.decompose()

    candidate_roots = []
    for selector in [
        "main",
        "article",
        "[role='main']",
        "#main",
        "#content",
        ".main",
        ".content",
        ".post-content",
        ".entry-content",
        ".article-content",
    ]:
        candidate_roots.extend(soup.select(selector))
    candidate_roots.append(soup.body or soup)

    def extract_blocks(root):
        blocks = []
        for node in root.find_all(["h1", "h2", "h3", "p", "li", "blockquote", "td"]):
            text = normalize_ws(node.get_text(" ", strip=True))
            if not text:
                continue
            words = text.split()
            if len(words) < 8:
                continue
            if len(text) < 45:
                continue
            # Filter out link-heavy chunks that are usually nav/resource lists.
            if link_density(node) > 0.6:
                continue
            blocks.append(text)
        return blocks

    best_root = soup.body or soup
    best_score = -1.0
    seen_roots = set()
    for root in candidate_roots:
        if root is None:
            continue
        marker = id(root)
        if marker in seen_roots:
            continue
        seen_roots.add(marker)

        blocks = extract_blocks(root)
        if not blocks:
            continue

        total_words = sum(len(block.split()) for block in blocks)
        score = total_words * (1.0 - min(0.95, link_density(root)))
        if score > best_score:
            best_score = score
            best_root = root

    selected_blocks = extract_blocks(best_root)
    if selected_blocks:
        deduped = []
        seen = set()
        for block in selected_blocks:
            key = block.lower()
            if key in seen:
                continue
            seen.add(key)
            deduped.append(block)
        body_text = " ".join(deduped)
    else:
        body_text = normalize_ws((soup.body or soup).get_text(" ", strip=True))

    words = body_text.split()
    summary = " ".join(words[:256]) if words else "No text found"
    return title or "No Title", summary

async def async_extract_text_from_html(url, timeout=10, session=None):
    """Asynchronously extracts text from an HTML webpage."""
    headers = {"User-Agent": "Mozilla/5.0"}
    close_session = False
    try:
        if session is None:
            session = aiohttp.ClientSession()
            close_session = True

        async with session.get(url, headers=headers, timeout=timeout) as response:
            response.raise_for_status()
            
            # Handle potential content-encoding issues gracefully
            try:
                html = await response.text()
            except aiohttp.ClientPayloadError as e:
                if "Can not decode content-encoding" in str(e):
                    print(f"Content-encoding error for URL {url}, trying raw read: {e}")
                    # Fallback to reading raw bytes and decoding manually
                    try:
                        raw_content = await response.read()
                        html = raw_content.decode('utf-8', errors='ignore')
                    except Exception as decode_error:
                        print(f"Failed to decode raw content for URL {url}: {decode_error}")
                        raise decode_error
                else:
                    raise e

        # Use to_thread to avoid blocking event loop with BeautifulSoup parsing
        title, content = await asyncio.to_thread(clean_text_html, html)

        if close_session:
            await session.close()
        return title, content
    except Exception as e:
        if close_session:
            await session.close()
        return None, f"Error fetching HTML page: {e}"

async def async_extract_text_from_pdf(url, timeout=10, session=None):
    """Asynchronously downloads and extracts text from a PDF file."""
    headers = {"User-Agent": "Mozilla/5.0"}
    close_session = False
    pdf_filename = "temp_async.pdf"
    try:
        if session is None:
            session = aiohttp.ClientSession()
            close_session = True

        async with session.get(url, headers=headers, timeout=timeout) as response:
            response.raise_for_status()
            
            # Handle potential content-encoding issues gracefully
            try:
                content = await response.read()
            except aiohttp.ClientPayloadError as e:
                if "Can not decode content-encoding" in str(e):
                    print(f"Content-encoding error for URL {url}, trying without auto-decompression: {e}")
                    # This shouldn't happen with read() but keeping for consistency
                    raise e
                else:
                    raise e

        # Write PDF to disk in a thread to avoid blocking
        await asyncio.to_thread(write_bytes_to_file, pdf_filename, content)

        # Extract PDF text in a thread
        title, content = await asyncio.to_thread(extract_pdf_text, pdf_filename)

        # Clean up
        await asyncio.to_thread(os.remove, pdf_filename)

        if close_session:
            await session.close()
        return title, content
    except Exception as e:
        if close_session:
            await session.close()
        return None, f"Error fetching or processing PDF: {e}"

def write_bytes_to_file(filename, content):
    with open(filename, "wb") as f:
        f.write(content)


async def async_extract_text_from_html_bytes(content):
    try:
        html = content.decode('utf-8', errors='ignore')
        title, extracted = await asyncio.to_thread(clean_text_html, html)
        return title, extracted
    except Exception as e:
        return None, f"Error processing HTML content: {e}"


async def async_extract_text_from_pdf_bytes(content):
    pdf_filename = None
    try:
        fd, pdf_filename = tempfile.mkstemp(suffix=".pdf")
        os.close(fd)
        await asyncio.to_thread(write_bytes_to_file, pdf_filename, content)
        title, extracted = await asyncio.to_thread(extract_pdf_text, pdf_filename)
        return title, extracted
    except Exception as e:
        return None, f"Error processing PDF content: {e}"
    finally:
        if pdf_filename and os.path.exists(pdf_filename):
            try:
                await asyncio.to_thread(os.remove, pdf_filename)
            except Exception:
                pass


_TRANSIENT_HTTP_STATUS_CODES = {408, 425, 429, 500, 502, 503, 504}


def _normalize_mime(content_type):
    if not content_type:
        return ""
    return str(content_type).split(";", 1)[0].strip().lower()


def _is_allowed_mime(content_type, allowed_prefixes):
    if not allowed_prefixes:
        return True
    normalized = _normalize_mime(content_type)
    if not normalized:
        return True
    return any(normalized.startswith(prefix) for prefix in allowed_prefixes)


def _is_transient_fetch_status(status_code):
    return int(status_code or 0) in _TRANSIENT_HTTP_STATUS_CODES


async def _read_response_with_limit(response, fetch_max_bytes):
    if not fetch_max_bytes or int(fetch_max_bytes) <= 0:
        content = await response.read()
        return content, len(content), False

    chunks = []
    total_bytes = 0
    max_bytes = int(fetch_max_bytes)
    async for chunk in response.content.iter_chunked(65536):
        if not chunk:
            continue
        total_bytes += len(chunk)
        if total_bytes > max_bytes:
            return b"", total_bytes, True
        chunks.append(chunk)
    return b"".join(chunks), total_bytes, False


async def async_get_content_from_url(
    url,
    timeout=10,
    session=None,
    *,
    fetch_connect_timeout_s=None,
    fetch_read_timeout_s=None,
    fetch_total_timeout_s=None,
    fetch_max_bytes=None,
    fetch_max_retries=0,
    fetch_retry_backoff_s=0.0,
    fetch_allowed_mime_prefixes=None,
    enable_fetch_bounds=True,
    enable_mime_filter=True,
    enable_fetch_retry=True,
    return_metadata=False,
):
    """
    Asynchronously determines if a URL is an HTML or PDF and extracts text accordingly.
    Returns (title, content) or (None, error_message).
    """
    headers = {"User-Agent": "Mozilla/5.0"}
    close_session = False
    started = time.perf_counter()
    metadata = {
        "outcome": "network_error",
        "statusCode": 0,
        "bytesRead": 0,
        "contentType": "",
        "retryCount": 0,
        "error": "",
        "latencyMs": 0.0,
    }
    allowed_prefixes = tuple(
        str(item).strip().lower()
        for item in (fetch_allowed_mime_prefixes or ("text/html", "application/xhtml+xml", "application/pdf"))
        if str(item).strip()
    )
    if fetch_total_timeout_s is None:
        fetch_total_timeout_s = timeout
    if fetch_connect_timeout_s is None:
        fetch_connect_timeout_s = min(float(fetch_total_timeout_s), 5.0)
    if fetch_read_timeout_s is None:
        fetch_read_timeout_s = max(1.0, float(fetch_total_timeout_s))
    retry_cap = max(0, int(fetch_max_retries or 0))
    max_attempts = retry_cap + 1 if enable_fetch_retry else 1
    last_error = None

    async def _finalize(title, body, *, outcome, error_message="", status_code=0, bytes_read=0, content_type="", retry_count=0):
        metadata["outcome"] = str(outcome)
        metadata["statusCode"] = int(status_code or 0)
        metadata["bytesRead"] = int(bytes_read or 0)
        metadata["contentType"] = str(content_type or "")
        metadata["retryCount"] = int(retry_count or 0)
        metadata["error"] = str(error_message or "")[:500]
        metadata["latencyMs"] = round((time.perf_counter() - started) * 1000.0, 3)
        if return_metadata:
            return title, body, dict(metadata)
        return title, body

    try:
        if should_skip_url(url):
            return await _finalize(None, None, outcome="mime_filtered", error_message="URL extension/domain skiplist")

        if session is None:
            session = aiohttp.ClientSession()
            close_session = True
        timeout_config = aiohttp.ClientTimeout(
            total=float(fetch_total_timeout_s),
            connect=float(fetch_connect_timeout_s),
            sock_connect=float(fetch_connect_timeout_s),
            sock_read=float(fetch_read_timeout_s),
        )

        for attempt in range(max_attempts):
            metadata["retryCount"] = attempt
            try:
                async with session.get(url, headers=headers, timeout=timeout_config) as response:
                    status_code = int(response.status or 0)
                    content_type = _normalize_mime(response.headers.get("Content-Type", ""))
                    metadata["statusCode"] = status_code
                    metadata["contentType"] = content_type

                    if status_code >= 400:
                        message = f"HTTP error: {status_code}"
                        if (
                            enable_fetch_retry
                            and attempt < retry_cap
                            and _is_transient_fetch_status(status_code)
                        ):
                            await asyncio.sleep(
                                float(fetch_retry_backoff_s or 0.0) * (2 ** attempt)
                                + random.uniform(0.0, 0.2)
                            )
                            continue
                        return await _finalize(
                            None,
                            message,
                            outcome="http_error",
                            error_message=message,
                            status_code=status_code,
                            content_type=content_type,
                            retry_count=attempt,
                        )

                    if enable_mime_filter and not _is_allowed_mime(content_type, allowed_prefixes):
                        return await _finalize(
                            None,
                            f"MIME filtered: {content_type or 'unknown'}",
                            outcome="mime_filtered",
                            error_message=f"MIME filtered: {content_type or 'unknown'}",
                            status_code=status_code,
                            content_type=content_type,
                            retry_count=attempt,
                        )

                    content, bytes_read, is_too_large = await _read_response_with_limit(
                        response,
                        fetch_max_bytes if enable_fetch_bounds else None,
                    )
                    metadata["bytesRead"] = int(bytes_read or 0)
                    if is_too_large:
                        return await _finalize(
                            None,
                            f"Response exceeded byte limit ({fetch_max_bytes})",
                            outcome="too_large",
                            error_message=f"Response exceeded byte limit ({fetch_max_bytes})",
                            status_code=status_code,
                            bytes_read=bytes_read,
                            content_type=content_type,
                            retry_count=attempt,
                        )

                if not content:
                    return await _finalize(
                        None,
                        None,
                        outcome="ok",
                        status_code=metadata["statusCode"],
                        bytes_read=metadata["bytesRead"],
                        content_type=metadata["contentType"],
                        retry_count=attempt,
                    )

                # Prefer content-type when available, but keep sniffing fallback.
                if "pdf" in metadata["contentType"] or content.startswith(b"%PDF-"):
                    title, body = await async_extract_text_from_pdf_bytes(content)
                elif "html" in metadata["contentType"]:
                    title, body = await async_extract_text_from_html_bytes(content)
                else:
                    text_preview = content[:500].decode(errors="ignore").lower()
                    if "<html" in text_preview or "<body" in text_preview:
                        title, body = await async_extract_text_from_html_bytes(content)
                    else:
                        guessed_type, _ = mimetypes.guess_type(url)
                        if guessed_type == "application/pdf":
                            title, body = await async_extract_text_from_pdf_bytes(content)
                        elif guessed_type == "text/html":
                            title, body = await async_extract_text_from_html_bytes(content)
                        else:
                            title, body = None, None

                if title is None and body and str(body).lower().startswith("error"):
                    return await _finalize(
                        title,
                        body,
                        outcome="network_error",
                        error_message=str(body),
                        status_code=metadata["statusCode"],
                        bytes_read=metadata["bytesRead"],
                        content_type=metadata["contentType"],
                        retry_count=attempt,
                    )

                return await _finalize(
                    title,
                    body,
                    outcome="ok",
                    status_code=metadata["statusCode"],
                    bytes_read=metadata["bytesRead"],
                    content_type=metadata["contentType"],
                    retry_count=attempt,
                )
            except asyncio.TimeoutError as error:
                last_error = error
                if enable_fetch_retry and attempt < retry_cap:
                    await asyncio.sleep(
                        float(fetch_retry_backoff_s or 0.0) * (2 ** attempt) + random.uniform(0.0, 0.2)
                    )
                    continue
                return await _finalize(
                    None,
                    f"Fetch exception: TimeoutError: {error}",
                    outcome="timeout",
                    error_message=str(error),
                    retry_count=attempt,
                )
            except aiohttp.ClientError as error:
                last_error = error
                if enable_fetch_retry and attempt < retry_cap:
                    await asyncio.sleep(
                        float(fetch_retry_backoff_s or 0.0) * (2 ** attempt) + random.uniform(0.0, 0.2)
                    )
                    continue
                return await _finalize(
                    None,
                    f"Fetch exception: {type(error).__name__}: {error}",
                    outcome="network_error",
                    error_message=str(error),
                    retry_count=attempt,
                )
            except Exception as error:
                last_error = error
                return await _finalize(
                    None,
                    f"Fetch exception: {type(error).__name__}: {error}",
                    outcome="network_error",
                    error_message=str(error),
                    retry_count=attempt,
                )

        return await _finalize(
            None,
            f"Fetch exception: {type(last_error).__name__ if last_error else 'Unknown'}: {last_error}",
            outcome="network_error",
            error_message=str(last_error or "unknown fetch failure"),
            retry_count=max(0, max_attempts - 1),
        )
    except Exception as error:
        return await _finalize(
            None,
            f"Fetch exception: {type(error).__name__}: {error}",
            outcome="network_error",
            error_message=str(error),
            retry_count=metadata.get("retryCount", 0),
        )
    finally:
        if close_session and session is not None:
            await session.close()

def extract_links(url, max_links=10):
    """
    Fetches a webpage and extracts unique absolute links (URLs) from it,
    prioritizing links likely in the main content area, limiting the total number,
    and excluding links that only point to fragments (#) on the same page.

    Args:
        url (str): The URL of the webpage to scrape.
        max_links (int): The maximum number of links to return. Defaults to 10.

    Returns:
        list: A list of unique absolute URLs found on the page (up to max_links),
              prioritizing main content links and excluding same-page fragments.
              Returns an empty list if an error occurs.
              Prints an error message if fetching or parsing fails.
    """
    priority_links = set()
    other_links = set()
    all_found_links = set() # Keep track of all added links for uniqueness

    try:
        # Send an HTTP GET request to the URL
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/58.0.3029.110 Safari/537.3'
        }
        response = requests.get(url, headers=headers, timeout=10, )
        response.raise_for_status() # Raise an exception for bad status codes

        # Parse the HTML content
        soup = BeautifulSoup(response.text, 'html.parser')

        # Parse the original URL once to compare against fragments
        original_parsed_url = urlparse(url)
        # Normalize path for comparison (e.g., treat '/' and '' as the same for root)
        original_path = original_parsed_url.path if original_parsed_url.path else '/'


        # --- Link Prioritization Logic ---
        # Define tags/selectors often containing non-main content
        non_content_selectors = ['nav', 'header', 'footer', '.sidebar', '#sidebar', '.menu', '#menu', '.navigation', '#navigation']

        # Find all anchor tags
        for a_tag in soup.find_all('a', href=True):
            href = a_tag['href']
            absolute_url = urljoin(url, href) # Resolve relative URLs

            # Parse the absolute URL
            parsed_url = urlparse(absolute_url)
            # Normalize path for comparison
            current_path = parsed_url.path if parsed_url.path else '/'

            # --- Validation and Filtering ---
            # 1. Must be http or https
            # 2. Must not have been added already
            # 3. Must NOT be a fragment-only link pointing to the *same page*
            is_same_page_fragment = (
                parsed_url.fragment and # Does it have a fragment?
                original_parsed_url.netloc == parsed_url.netloc and # Same domain?
                original_path == current_path # Same path?
            )

            if (parsed_url.scheme in ['http', 'https'] and
                    absolute_url not in all_found_links and
                    not is_same_page_fragment and
                    not is_social_media_domain(absolute_url)):

                # --- Prioritization Check ---
                is_non_content = False
                for selector in non_content_selectors:
                    # Check if any parent matches the non-content selectors
                    # Handle both tag names and class/id selectors
                    tag_name = selector.split('.')[0].split('#')[0] if '.' in selector or '#' in selector else selector
                    attrs = {}
                    if '.' in selector:
                        attrs['class'] = selector.split('.')[1]
                    elif '#' in selector:
                        attrs['id'] = selector.split('#')[1]

                    if a_tag.find_parent(tag_name, attrs=attrs):
                        is_non_content = True
                        break

                # Add to the appropriate set
                if is_non_content:
                    other_links.add(absolute_url)
                else:
                    # Prioritize links within <main> or <article> if they exist
                    # Otherwise, links directly under <body> (and not excluded) are priority
                    if a_tag.find_parent('main') or a_tag.find_parent('article') or not is_non_content:
                         priority_links.add(absolute_url)
                    else: # Fallback if not explicitly priority but also not explicitly non-content
                         other_links.add(absolute_url)

                all_found_links.add(absolute_url) # Add to the master set to track uniqueness


    except requests.exceptions.RequestException as e:
        print(f"Error fetching URL {url}: {e}")
        return []
    except Exception as e:
        print(f"An error occurred during parsing or link extraction: {e}")
        return []

    # Combine lists, prioritizing the main content links
    combined_links = list(priority_links) + list(other_links)
    # Return the specified maximum number of links
    return combined_links[:max_links]

def extract_links_with_context(url, max_links=10):
    """
    Fetches a webpage and extracts a dictionary of unique absolute links,
    mapping each URL to its surrounding text context. It prioritizes links
    from the main content area and limits the total number returned.

    Args:
        url (str): The URL of the webpage to scrape.
        max_links (int): The maximum number of links to return. Defaults to 10.

    Returns:
        dict: A dictionary of {'url': context} pairs (up to max_links),
              prioritizing main content links.
              Returns an empty dictionary if an error occurs.
              Prints an error message if fetching or parsing fails.
    """
    ### MODIFICATION: Changed data structures from sets to dictionaries
    priority_links = {}
    other_links = {}
    all_found_urls = set() # Keep a set of URLs for fast uniqueness checks

    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/58.0.3029.110 Safari/537.3'
        }
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()

        soup = BeautifulSoup(response.text, 'html.parser')

        original_parsed_url = urlparse(url)
        original_path = original_parsed_url.path if original_parsed_url.path else '/'

        non_content_selectors = ['nav', 'header', 'footer', '.sidebar', '#sidebar', '.menu', '#menu', '.navigation', '#navigation']

        for a_tag in soup.find_all('a', href=True):
            href = a_tag['href']
            absolute_url = urljoin(url, href)
            parsed_url = urlparse(absolute_url)
            current_path = parsed_url.path if parsed_url.path else '/'

            is_same_page_fragment = (
                parsed_url.fragment and
                original_parsed_url.netloc == parsed_url.netloc and
                original_path == current_path
            )

            if (parsed_url.scheme in ['http', 'https'] and
                    absolute_url not in all_found_urls and # Check for uniqueness
                    not is_same_page_fragment and
                    not is_social_media_domain(absolute_url)):

                ### MODIFICATION: Get the text context from the link's parent
                # The separator adds a space between text from different child tags
                content = a_tag.parent.get_text(strip=True, separator=' ')
                words = content.split()
                context = " ".join(words[:32]) if words else "No context found"


                # --- Prioritization Check (Your original logic is unchanged) ---
                is_non_content = False
                for selector in non_content_selectors:
                    tag_name = selector.split('.')[0].split('#')[0] if '.' in selector or '#' in selector else selector
                    attrs = {}
                    if '.' in selector:
                        attrs['class'] = selector.split('.')[1]
                    elif '#' in selector:
                        attrs['id'] = selector.split('#')[1]

                    if a_tag.find_parent(tag_name, attrs=attrs):
                        is_non_content = True
                        break

                ### MODIFICATION: Store URL and context in the appropriate dictionary
                if is_non_content:
                    other_links[absolute_url] = context
                else:
                    if a_tag.find_parent('main') or a_tag.find_parent('article') or not is_non_content:
                         priority_links[absolute_url] = context
                    else:
                         other_links[absolute_url] = context

                all_found_urls.add(absolute_url) # Add to the master set to track uniqueness

    except requests.exceptions.RequestException as e:
        print(f"Error fetching URL {url}: {e}")
        ### MODIFICATION: Return an empty dictionary on error
        return {}
    except Exception as e:
        print(f"An error occurred during parsing or link extraction: {e}")
        ### MODIFICATION: Return an empty dictionary on error
        return {}

    ### MODIFICATION: Combine dictionaries, keeping priority links first
    combined_links = priority_links.copy()
    combined_links.update(other_links)

    ### MODIFICATION: Slice the dictionary items to respect max_links
    # Convert to a list of (key, value) pairs to slice it
    sliced_items = list(combined_links.items())[:max_links]
    
    # Convert the sliced list back into a dictionary
    return dict(sliced_items)

async def async_extract_links_with_context(
    url,
    max_links=10,
    visited=set(),
    session=None,
    metric_callback=None,
    hop_level=None,
    extract_parallel_per_hop=12,
    extract_connect_timeout_s=2.0,
    extract_read_timeout_s=3.0,
    extract_total_timeout_s=5.0,
    extract_max_bytes=600000,
    extract_max_retries=1,
    extract_retry_backoff_s=0.25,
    extract_allowed_mime_prefixes=None,
    enable_extract_bounds=True,
    enable_extract_mime_filter=True,
    enable_extract_retry=True,
):
    """
    Asynchronously fetches a webpage and extracts a dictionary of unique absolute links,
    mapping each URL to its surrounding text context. Prioritizes main content links.

    Args:
        url (str): The URL of the webpage to scrape.
        max_links (int): The maximum number of links to return.
        session (aiohttp.ClientSession, optional): Existing session for connection pooling.

    Returns:
        dict: {'url': context} pairs (up to max_links), prioritizing main content links.
    """
    priority_links = {}
    other_links = {}
    all_found_urls = set()

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/58.0.3029.110 Safari/537.3"
    }
    total_started = time.perf_counter()
    fetch_ms = 0.0
    parse_ms = 0.0
    status_code = 0
    content_type = ""
    bytes_read = 0
    retry_count = 0
    outcome = "network_error"
    exception_type = ""
    exception_message = ""
    allowed_prefixes = tuple(
        str(item).strip().lower()
        for item in (extract_allowed_mime_prefixes or ("text/html", "application/xhtml+xml"))
        if str(item).strip()
    )
    retry_cap = max(0, int(extract_max_retries or 0))
    max_attempts = retry_cap + 1 if enable_extract_retry else 1
    close_session = False

    def _emit_metric(*, status, error="", links_found=0, visited_count=0):
        if metric_callback is None:
            return
        total_ms = (time.perf_counter() - total_started) * 1000.0
        metric_callback(
            {
                "component": "crawl_fetch",
                "operation": "extract_links_with_context",
                "status": status,
                "provider": "http",
                "model": "aiohttp",
                "latencyMs": total_ms,
                "promptTokens": 0,
                "completionTokens": 0,
                "totalTokens": 0,
                "estimatedCostUsd": 0.0,
                "error": str(error)[:300],
                "meta": json.dumps(
                    {
                        "url": url,
                        "hop": hop_level,
                        "outcome": outcome,
                        "statusCode": status_code,
                        "contentType": content_type,
                        "bytesRead": int(bytes_read),
                        "retryCount": int(retry_count),
                        "fetchMs": round(fetch_ms, 3),
                        "parseMs": round(parse_ms, 3),
                        "totalMs": round(total_ms, 3),
                        "linksFound": int(links_found),
                        "visitedCount": int(visited_count),
                        "maxLinks": int(max_links),
                        "extractParallelPerHop": int(extract_parallel_per_hop),
                        "extractMaxBytes": int(extract_max_bytes or 0),
                        "extractMaxRetries": int(extract_max_retries or 0),
                        "extractRetryBackoffS": float(extract_retry_backoff_s or 0.0),
                        "enableExtractBounds": bool(enable_extract_bounds),
                        "enableExtractMimeFilter": bool(enable_extract_mime_filter),
                        "enableExtractRetry": bool(enable_extract_retry),
                        "extractAllowedMimePrefixes": list(allowed_prefixes),
                        "exceptionType": exception_type,
                        "exceptionMessage": exception_message[:300],
                        "extractConnectTimeoutS": float(extract_connect_timeout_s),
                        "extractReadTimeoutS": float(extract_read_timeout_s),
                        "extractTotalTimeoutS": float(extract_total_timeout_s),
                    }
                ),
            }
        )

    try:
        if should_skip_url(url):
            outcome = "mime_filtered"
            _emit_metric(
                status="ok",
                error="url_skiplist",
                links_found=0,
                visited_count=len(visited),
            )
            return {}

        close_session = False
        if session is None:
            session = aiohttp.ClientSession()
            close_session = True

        timeout_config = aiohttp.ClientTimeout(
            total=float(extract_total_timeout_s),
            connect=float(extract_connect_timeout_s),
            sock_connect=float(extract_connect_timeout_s),
            sock_read=float(extract_read_timeout_s),
        )
        text = ""
        for attempt in range(max_attempts):
            retry_count = attempt
            fetch_started = time.perf_counter()
            try:
                async with session.get(url, headers=headers, timeout=timeout_config) as response:
                    status_code = int(response.status or 0)
                    content_type = _normalize_mime(response.headers.get("Content-Type", ""))

                    if status_code >= 400:
                        outcome = "http_error"
                        if enable_extract_retry and attempt < retry_cap and _is_transient_fetch_status(status_code):
                            await asyncio.sleep(
                                float(extract_retry_backoff_s or 0.0) * (2 ** attempt) + random.uniform(0.0, 0.2)
                            )
                            continue
                        fetch_ms = (time.perf_counter() - fetch_started) * 1000.0
                        _emit_metric(
                            status="ok",
                            error=f"http_status_{status_code}",
                            links_found=0,
                            visited_count=len(visited),
                        )
                        return {}

                    if enable_extract_mime_filter and not _is_allowed_mime(content_type, allowed_prefixes):
                        outcome = "mime_filtered"
                        fetch_ms = (time.perf_counter() - fetch_started) * 1000.0
                        _emit_metric(
                            status="ok",
                            error=f"mime_filtered_{content_type or 'unknown'}",
                            links_found=0,
                            visited_count=len(visited),
                        )
                        return {}

                    if enable_extract_bounds and int(extract_max_bytes or 0) > 0:
                        content_length = response.headers.get("Content-Length")
                        if content_length:
                            try:
                                if int(content_length) > int(extract_max_bytes):
                                    outcome = "too_large"
                                    bytes_read = int(content_length)
                                    fetch_ms = (time.perf_counter() - fetch_started) * 1000.0
                                    _emit_metric(
                                        status="ok",
                                        error=f"content_length_exceeds_limit_{extract_max_bytes}",
                                        links_found=0,
                                        visited_count=len(visited),
                                    )
                                    return {}
                            except Exception:
                                pass
                        content, bytes_read, is_too_large = await _read_response_with_limit(
                            response,
                            extract_max_bytes,
                        )
                        if is_too_large:
                            outcome = "too_large"
                            fetch_ms = (time.perf_counter() - fetch_started) * 1000.0
                            _emit_metric(
                                status="ok",
                                error=f"response_exceeded_limit_{extract_max_bytes}",
                                links_found=0,
                                visited_count=len(visited),
                            )
                            return {}
                    else:
                        content = await response.read()
                        bytes_read = len(content)

                fetch_ms = (time.perf_counter() - fetch_started) * 1000.0
                outcome = "ok"
                text = content.decode("utf-8", errors="ignore")
                break
            except asyncio.TimeoutError as e:
                outcome = "timeout"
                exception_type = type(e).__name__
                exception_message = str(e)
                if enable_extract_retry and attempt < retry_cap:
                    await asyncio.sleep(
                        float(extract_retry_backoff_s or 0.0) * (2 ** attempt) + random.uniform(0.0, 0.2)
                    )
                    continue
                fetch_ms = (time.perf_counter() - fetch_started) * 1000.0
                _emit_metric(
                    status="error",
                    error=f"{exception_type}: {exception_message}",
                    links_found=0,
                    visited_count=len(visited),
                )
                return {}
            except aiohttp.ClientError as e:
                outcome = "network_error"
                exception_type = type(e).__name__
                exception_message = str(e)
                if enable_extract_retry and attempt < retry_cap:
                    await asyncio.sleep(
                        float(extract_retry_backoff_s or 0.0) * (2 ** attempt) + random.uniform(0.0, 0.2)
                    )
                    continue
                fetch_ms = (time.perf_counter() - fetch_started) * 1000.0
                _emit_metric(
                    status="error",
                    error=f"{exception_type}: {exception_message}",
                    links_found=0,
                    visited_count=len(visited),
                )
                return {}
            except Exception as e:
                outcome = "extract_error"
                exception_type = type(e).__name__
                exception_message = str(e)
                fetch_ms = (time.perf_counter() - fetch_started) * 1000.0
                _emit_metric(
                    status="error",
                    error=f"{exception_type}: {exception_message}",
                    links_found=0,
                    visited_count=len(visited),
                )
                return {}
        else:
            _emit_metric(
                status="error",
                error="extract_fetch_retries_exhausted",
                links_found=0,
                visited_count=len(visited),
            )
            return {}

        parse_started = time.perf_counter()

        soup = BeautifulSoup(text, "html.parser")
        original_parsed_url = urlparse(url)
        original_path = original_parsed_url.path if original_parsed_url.path else '/'

        non_content_selectors = ['nav', 'header', 'footer', '.sidebar', '#sidebar', '.menu', '#menu', '.navigation', '#navigation']

        for a_tag in soup.find_all('a', href=True):
            href = a_tag['href']
            absolute_url = urljoin(url, href)
            parsed_url = urlparse(absolute_url)
            current_path = parsed_url.path if parsed_url.path else '/'

            is_same_page_fragment = (
                parsed_url.fragment and
                original_parsed_url.netloc == parsed_url.netloc and
                original_path == current_path
            )

            if (parsed_url.scheme in ['http', 'https'] and
                    absolute_url not in all_found_urls and
                    absolute_url not in visited and
                    not should_skip_url(absolute_url) and
                    not is_same_page_fragment and
                    not is_social_media_domain(absolute_url)):

                content = a_tag.parent.get_text(strip=True, separator=' ')
                words = content.split()
                context = " ".join(words[:32]) if words else "No context found"

                is_non_content = False
                for selector in non_content_selectors:
                    tag_name = selector.split('.')[0].split('#')[0] if '.' in selector or '#' in selector else selector
                    attrs = {}
                    if '.' in selector:
                        attrs['class'] = selector.split('.')[1]
                    elif '#' in selector:
                        attrs['id'] = selector.split('#')[1]
                    if a_tag.find_parent(tag_name, attrs=attrs):
                        is_non_content = True
                        break

                if is_non_content:
                    other_links[absolute_url] = context
                else:
                    if a_tag.find_parent('main') or a_tag.find_parent('article') or not is_non_content:
                        priority_links[absolute_url] = context
                    else:
                        other_links[absolute_url] = context

                all_found_urls.add(absolute_url)

        parse_ms = (time.perf_counter() - parse_started) * 1000.0

    except Exception as e:
        outcome = "extract_error"
        exception_type = type(e).__name__
        exception_message = str(e)
        _emit_metric(
            status="error",
            error=f"{exception_type}: {exception_message}",
            links_found=0,
            visited_count=len(visited),
        )
        return {}
    finally:
        if close_session and session is not None:
            await session.close()

    combined_links = priority_links.copy()
    combined_links.update(other_links)
    sliced_items = list(combined_links.items())[:max_links]
    _emit_metric(
        status="ok",
        error="",
        links_found=len(sliced_items),
        visited_count=len(visited),
    )
    return dict(sliced_items)


def hop(start_url, n_hops, max_links_per_page=10):
    """
    Crawls webpages starting from start_url up to n_hops deep, collecting unique links
    and the hop number they were first discovered on.

    Args:
        start_url (str): The initial URL to start crawling from.
        n_hops (int): The number of levels deep to crawl (0 means only the start_url).
        max_links_per_page (int): Max links to extract from each individual page.

    Returns:
        list: A list of dictionaries, where each dictionary has 'url' (str) and
              'hop' (int) keys representing a unique URL found and the hop it
              was first discovered on. Returns an empty list if the start_url
              cannot be processed or no links are found.
    """
    visited_urls = set()             # Keep track of URLs whose links have been extracted
    # Use a dictionary internally to track unique URLs and their first hop found
    found_urls_with_hop = {}
    urls_to_visit_current_hop = {start_url} # URLs for the current hop level

    print(f"Starting crawl from: {start_url} for {n_hops} hops.")

    for hop in range(n_hops):
        print(f"\n--- Hop {hop} ---")
        urls_to_visit_next_hop = set() # URLs found in this hop, to visit in the next

        if not urls_to_visit_current_hop:
            print("  No more URLs to visit at this level.")
            break # Stop if no new links were found in the previous hop

        # Process each URL at the current hop level
        for current_url in list(urls_to_visit_current_hop): # Iterate over a copy
            # Record the URL and its first hop found (if not already recorded)
            if current_url not in found_urls_with_hop:
                found_urls_with_hop[current_url] = hop

            # Skip link extraction if this URL's links have already been processed
            if current_url in visited_urls:
                continue

            visited_urls.add(current_url) # Mark as processed for link extraction

            # Extract links from the current page
            found_links = extract_links(current_url, max_links=max_links_per_page)

            # Process newly found links
            for link in found_links:
                # Record the link and its first hop found (if not already recorded)
                if link not in found_urls_with_hop:
                     found_urls_with_hop[link] = hop + 1
                # If the link hasn't had its own links extracted yet, add it to the next hop queue
                if link not in visited_urls:
                    urls_to_visit_next_hop.add(link)

        # Prepare for the next hop
        urls_to_visit_current_hop = urls_to_visit_next_hop

    print(f"\n--- Crawl Finished ---")
    print(f"Visited {len(visited_urls)} unique pages for link extraction.")
    print(f"Collected {len(found_urls_with_hop)} unique links in total.")

    # Convert the internal dictionary to the desired list of dictionaries format
    result_list = [{'url': url, 'hop': hop_num} for url, hop_num in found_urls_with_hop.items()]
    print(result_list)
    return result_list


async def async_hop_with_context(
    start_url,
    visited=set(),
    n_hops=3,
    max_links=10,
    session=None,
    metric_callback=None,
    hop_link_limits=None,
    extract_parallel_per_hop=12,
    extract_connect_timeout_s=2.0,
    extract_read_timeout_s=3.0,
    extract_total_timeout_s=5.0,
    extract_max_bytes=600000,
    extract_max_retries=1,
    extract_retry_backoff_s=0.25,
    extract_allowed_mime_prefixes=None,
    enable_extract_bounds=True,
    enable_extract_mime_filter=True,
    enable_extract_retry=True,
    should_expand_url=None,
    extract_cache_get=None,
    extract_cache_set=None,
    extract_cache_stats=None,
    max_frontier_items=None,
):
    """
    Asynchronously crawls webpages starting from start_url up to n_hops deep,
    collecting unique links, their context, and the hop number they were first discovered on.

    Args:
        start_url (str): The initial URL to start crawling from.
        n_hops (int): The number of levels deep to crawl (0 means only the start_url).
        max_links_per_page (int): Max links to extract from each individual page.
        session (aiohttp.ClientSession, optional): Existing session for connection pooling.

    Returns:
        list: A list of dictionaries, each with 'url', 'hop', and 'context' keys.
    """
    visited_urls = visited.copy()  # Keep track of URLs whose links have been extracted
    found_links = {}  # url -> {'hop': hop_num, 'context': context}
    urls_to_visit_current_hop = {start_url}
    discovered_links_count = 0
    try:
        frontier_limit = int(max_frontier_items) if max_frontier_items is not None else 0
    except (TypeError, ValueError):
        frontier_limit = 0
    frontier_limit = max(0, frontier_limit)

    hop_limits = []
    if hop_link_limits:
        for item in hop_link_limits:
            try:
                parsed = int(item)
            except (TypeError, ValueError):
                continue
            if parsed > 0:
                hop_limits.append(parsed)
    default_hop_limit = max(1, int(max_links))

    def _max_links_for_hop(hop_index: int) -> int:
        if not hop_limits:
            return default_hop_limit
        if hop_index < len(hop_limits):
            return hop_limits[hop_index]
        return hop_limits[-1]

    # print(f"Starting async crawl from: {start_url} for {n_hops} hops.")

    close_session = False
    if session is None:
        session = aiohttp.ClientSession()
        close_session = True
    extract_semaphore = asyncio.Semaphore(max(1, int(extract_parallel_per_hop or 1)))

    for hop in range(n_hops):
        if frontier_limit > 0 and discovered_links_count >= frontier_limit:
            break
        hop_started = time.perf_counter()
        urls_to_visit_next_hop = set()
        max_links_this_hop = _max_links_for_hop(hop)

        if not urls_to_visit_current_hop:
            break

        # Prepare async tasks for all URLs in this hop
        tasks = []
        url_list = []
        cached_results: list[tuple[str, dict[str, str]]] = []
        for current_url in list(urls_to_visit_current_hop):
            if frontier_limit > 0 and discovered_links_count >= frontier_limit:
                break
            if should_expand_url is not None:
                decision = should_expand_url(current_url)
                allowed = decision[0] if isinstance(decision, tuple) else bool(decision)
                reason = decision[1] if isinstance(decision, tuple) and len(decision) > 1 else ""
                if not allowed:
                    if metric_callback is not None:
                        metric_callback(
                            {
                                "component": "crawl",
                                "operation": "domain_skip_expand",
                                "status": "ok",
                                "provider": "local",
                                "model": "domain_policy",
                                "latencyMs": 0.0,
                                "promptTokens": 0,
                                "completionTokens": 0,
                                "totalTokens": 0,
                                "estimatedCostUsd": 0.0,
                                "error": "",
                                "meta": json.dumps(
                                    {
                                        "url": current_url,
                                        "hop": hop,
                                        "reason": reason or "blocked",
                                        "stage": "hop_input",
                                    }
                                ),
                                }
                            )
                    continue

            cache_payload = None
            if extract_cache_get is not None:
                try:
                    cache_payload = extract_cache_get(current_url)
                except Exception:
                    cache_payload = None
                if metric_callback is not None:
                    cache_stats = extract_cache_stats() if extract_cache_stats is not None else {}
                    metric_callback(
                        {
                            "component": "crawl_fetch",
                            "operation": "extract_links_cache_hit" if cache_payload is not None else "extract_links_cache_miss",
                            "status": "ok",
                            "provider": "local",
                            "model": "extract_cache",
                            "latencyMs": 0.0,
                            "promptTokens": 0,
                            "completionTokens": 0,
                            "totalTokens": 0,
                            "estimatedCostUsd": 0.0,
                            "error": "",
                            "meta": json.dumps(
                                {
                                    "url": current_url,
                                    "hop": hop,
                                    "maxLinksThisHop": max_links_this_hop,
                                    **(cache_stats or {}),
                                }
                            ),
                        }
                    )
            if cache_payload is not None:
                visited_urls.add(current_url)
                cached_results.append((current_url, cache_payload))
                continue
            visited_urls.add(current_url)
            url_list.append(current_url)
            async def _extract_with_limit(target_url=current_url):
                async with extract_semaphore:
                    return await async_extract_links_with_context(
                        target_url,
                        visited=visited_urls,
                        max_links=max_links_this_hop,
                        session=session,
                        metric_callback=metric_callback,
                        hop_level=hop,
                        extract_parallel_per_hop=extract_parallel_per_hop,
                        extract_connect_timeout_s=extract_connect_timeout_s,
                        extract_read_timeout_s=extract_read_timeout_s,
                        extract_total_timeout_s=extract_total_timeout_s,
                        extract_max_bytes=extract_max_bytes,
                        extract_max_retries=extract_max_retries,
                        extract_retry_backoff_s=extract_retry_backoff_s,
                        extract_allowed_mime_prefixes=extract_allowed_mime_prefixes,
                        enable_extract_bounds=enable_extract_bounds,
                        enable_extract_mime_filter=enable_extract_mime_filter,
                        enable_extract_retry=enable_extract_retry,
                    )

            tasks.append(_extract_with_limit())

        # Wait for all link extraction tasks to finish
        results = []
        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            # Normalize exceptions to empty dicts so a single failure doesn't abort the crawl
            normalized_results = []
            for r in results:
                if isinstance(r, Exception):
                    normalized_results.append({})
                else:
                    normalized_results.append(r)
            results = normalized_results

        # Process newly found links and their context
        def _process_found(parent_url, found_links_dict):
            nonlocal discovered_links_count
            # Record the parent URL itself (hop 0, context empty)
            if parent_url not in found_links:
                found_links[parent_url] = {'hop': hop, 'context': ''}
            for link, context in found_links_dict.items():
                if frontier_limit > 0 and discovered_links_count >= frontier_limit:
                    break
                if should_expand_url is not None:
                    decision = should_expand_url(link)
                    allowed = decision[0] if isinstance(decision, tuple) else bool(decision)
                    reason = decision[1] if isinstance(decision, tuple) and len(decision) > 1 else ""
                    if not allowed:
                        if metric_callback is not None:
                            metric_callback(
                                {
                                    "component": "crawl",
                                    "operation": "domain_skip_expand",
                                    "status": "ok",
                                    "provider": "local",
                                    "model": "domain_policy",
                                    "latencyMs": 0.0,
                                    "promptTokens": 0,
                                    "completionTokens": 0,
                                    "totalTokens": 0,
                                    "estimatedCostUsd": 0.0,
                                    "error": "",
                                    "meta": json.dumps(
                                        {
                                            "url": link,
                                            "hop": hop + 1,
                                            "reason": reason or "blocked",
                                            "stage": "hop_output",
                                        }
                                    ),
                                }
                            )
                        continue
                if link not in found_links:
                    found_links[link] = {'hop': hop + 1, 'context': context}
                    urls_to_visit_next_hop.add(link)
                    discovered_links_count += 1

        for parent_url, found_links_dict in cached_results:
            if frontier_limit > 0 and discovered_links_count >= frontier_limit:
                break
            _process_found(parent_url, found_links_dict)

        for idx, found_links_dict in enumerate(results):
            if frontier_limit > 0 and discovered_links_count >= frontier_limit:
                break
            parent_url = url_list[idx]
            if extract_cache_set is not None:
                try:
                    extract_cache_set(parent_url, found_links_dict)
                except Exception:
                    pass
            _process_found(parent_url, found_links_dict)

        if metric_callback is not None:
            metric_callback(
                {
                    "component": "crawl",
                    "operation": "hop_iteration",
                    "status": "ok",
                    "provider": "local",
                    "model": "async_hop_with_context",
                    "latencyMs": (time.perf_counter() - hop_started) * 1000.0,
                    "promptTokens": 0,
                    "completionTokens": 0,
                    "totalTokens": 0,
                    "estimatedCostUsd": 0.0,
                    "error": "",
                    "meta": json.dumps(
                        {
                            "startUrl": start_url,
                            "hop": hop,
                            "urlsInput": len(url_list),
                            "urlsNextHop": len(urls_to_visit_next_hop),
                            "linksAccumulated": len(found_links),
                            "maxLinksThisHop": max_links_this_hop,
                            "extractParallelPerHop": int(extract_parallel_per_hop),
                            "extractMaxBytes": int(extract_max_bytes or 0),
                            "maxFrontierItems": int(frontier_limit),
                            "discoveredLinksCount": int(discovered_links_count),
                        }
                    ),
                }
            )

        urls_to_visit_current_hop = urls_to_visit_next_hop

    if close_session:
        await session.close()

    # print(f"\n--- Async Crawl for {start_url} Finished ---")
    # print(f"Visited {len(visited_urls)} unique pages for link extraction.")
    # print(f"Collected {len(found_links)} unique links in total.")

    result_list = [
        {'url': url, 'context': info['context']}
        for url, info in found_links.items() if url not in visited_urls
    ]
    return result_list
