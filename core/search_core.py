"""One-source web search core with strict failure detection and ordered fallbacks."""
from __future__ import annotations

import html
import json
import logging
import os
import re
import socket
import threading
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, unquote, urlencode, urlparse
from urllib.request import Request, getproxies, urlopen

try:
    import requests as _requests
except ImportError:  # requests is optional; urllib is the fallback.
    _requests = None

logger = logging.getLogger(__name__)

BACKEND_ORDER = ("exa-mcp", "bing-html", "ddg-html", "serpapi", "deepseek")
PER_BACKEND_TIMEOUT_S = 15.0
TOTAL_TIMEOUT_S = 40.0
MAX_SOURCES = 10
DEEPSEEK_COST_CNY = 0.005

# A real browser UA. Short/bot-like strings are rejected by some engines
# (DuckDuckGo answers 202 with a challenge page), so this is not cosmetic.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36"
)


def _detect_proxies() -> dict[str, str] | None:
    """Resolve an HTTP proxy for outbound requests.

    Order: explicit FREE_SEARCH_PROXY / HTTPS_PROXY / HTTP_PROXY env vars, then
    the OS proxy settings (getproxies reads the Windows registry, macOS network
    settings, and Linux env). requests only reads env vars, which is why it
    silently went direct on a machine whose proxy lived in the registry.
    """
    explicit = os.environ.get("FREE_SEARCH_PROXY") or os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
    if explicit:
        explicit = explicit.strip()
        if explicit:
            return {"http": explicit, "https": explicit}
    detected = getproxies()
    http_proxy = detected.get("https") or detected.get("http")
    if not http_proxy:
        return None
    return {"http": http_proxy, "https": http_proxy}


_PROXY_CACHE: dict[str, Any] = {"value": None, "resolved": False}
_PROXY_LOCK = threading.Lock()


def _proxies_for_request() -> dict[str, str] | None:
    """Resolve the proxy for this call.

    A per-call override (set by `search(..., proxy=...)`) wins over detection so
    callers can supply a proxy the OS does not advertise. Detection itself is
    memoized for the process lifetime — reading the registry on every request
    would be wasteful.
    """
    override = getattr(_CALL_STATE, "proxy", None)
    if override:
        return {"http": override, "https": override}
    with _PROXY_LOCK:
        if not _PROXY_CACHE["resolved"]:
            _PROXY_CACHE["value"] = _detect_proxies()
            _PROXY_CACHE["resolved"] = True
        return _PROXY_CACHE["value"]

_SERP_QUOTA_CACHE: tuple[float, int] | None = None
_SERP_QUOTA_LOCK = threading.Lock()
_CALL_STATE = threading.local()


class _BackendFailure(Exception):
    def __init__(self, reason: str, message: str, *, paid_cost: float = 0.0):
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.paid_cost = paid_cost


def _fail(reason: str, message: str, *, paid_cost: float = 0.0) -> None:
    raise _BackendFailure(reason, message, paid_cost=paid_cost)


def _http_error_reason(status: int) -> str:
    return "http_4xx" if 400 <= status < 500 else "http_5xx"


def _http_request(method: str, url: str, *, headers: dict[str, str] | None = None,
                  data: bytes | str | None = None, timeout: float = 15.0) -> tuple[int, dict[str, str], str]:
    """Small HTTP adapter; error text intentionally never includes URLs or credentials."""
    headers = headers or {}
    payload = data.encode("utf-8") if isinstance(data, str) else data
    if _requests is not None:
        try:
            # requests only honours proxy env vars, so pass the resolved proxy
            # explicitly or it goes direct on registry-configured machines.
            response = _requests.request(
                method, url, headers=headers, data=payload,
                timeout=max(0.05, timeout), proxies=_proxies_for_request(),
            )
        except _requests.Timeout:
            _fail("timeout", "request timed out")
        except _requests.RequestException as exc:
            if isinstance(exc, _requests.ConnectionError):
                _fail("network", "connection failed")
            _fail("network", "request failed")
        status = int(response.status_code)
        if status >= 400:
            _fail(_http_error_reason(status), f"HTTP {status}")
        content_type = response.headers.get("content-type", "")
        charset_match = re.search(r"charset\s*=\s*['\"]?([\w.-]+)", content_type, re.I)
        charset = charset_match.group(1) if charset_match else None
        if charset and charset.lower() not in {"iso-8859-1", "ascii", "us-ascii"}:
            encoding = charset
        else:
            guessed = getattr(response, "apparent_encoding", None)
            encoding = (
                guessed
                if guessed and guessed.lower() not in {"iso-8859-1", "ascii", "us-ascii"}
                else "utf-8"
            )
        try:
            body = response.content.decode(encoding, errors="replace")
        except LookupError:
            encoding = "utf-8"
            body = response.content.decode(encoding, errors="replace")
        return status, dict(response.headers), body
    req = Request(url, data=payload, headers=headers, method=method.upper())
    try:
        with urlopen(req, timeout=max(0.05, timeout)) as response:
            status = int(response.status)
            body = response.read().decode("utf-8", errors="replace")
            if status >= 400:
                _fail(_http_error_reason(status), f"HTTP {status}")
            return status, dict(response.headers.items()), body
    except HTTPError as exc:
        _fail(_http_error_reason(int(exc.code)), f"HTTP {int(exc.code)}")
    except (TimeoutError, socket.timeout):
        _fail("timeout", "request timed out")
    except URLError as exc:
        if isinstance(getattr(exc, "reason", None), (TimeoutError, socket.timeout)):
            _fail("timeout", "request timed out")
        _fail("network", "connection failed")
    except OSError:
        _fail("network", "request failed")


def _parse_sse_json(body: str) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for frame in re.split(r"\r?\n\r?\n", body):
        data_lines = [line[5:].lstrip() for line in re.split(r"\r?\n", frame) if line.startswith("data:")]
        if not data_lines:
            continue
        raw = "\n".join(data_lines).strip()
        if raw == "[DONE]":
            continue
        try:
            value = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            _fail("parse_error", "invalid SSE JSON")
        if isinstance(value, dict):
            messages.append(value)
    if not messages:
        _fail("parse_error", "SSE response contained no JSON message")
    return messages


def _sources_from_object(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if not isinstance(value, dict):
        return []
    for key in ("results", "sources", "organic_results"):
        candidate = value.get(key)
        if isinstance(candidate, list):
            return [item for item in candidate if isinstance(item, dict)]
    for key in ("structuredContent", "result", "data"):
        nested = _sources_from_object(value.get(key))
        if nested:
            return nested
    return []


def _parse_exa_text(text: str) -> list[dict[str, Any]]:
    # Exa's MCP server currently returns repeated Title/URL/Highlights text blocks.
    pattern = re.compile(
        r"(?ms)^Title:\s*(.*?)\s*\r?\nURL:\s*(https?://\S+)(.*?)(?=^Title:\s*|\Z)"
    )
    sources: list[dict[str, Any]] = []
    for match in pattern.finditer(text):
        title, url, remainder = match.groups()
        highlight = re.search(r"(?ms)^Highlights:\s*\r?\n(.*)", remainder)
        snippet = highlight.group(1).strip() if highlight else ""
        snippet = re.sub(r"\n(?:Published|Author):[^\n]*", "", snippet).strip()
        sources.append({"title": title.strip(), "url": url.strip(), "snippet": snippet})
    return sources


def _backend_exa_mcp(query: str, timeout: float) -> list[dict[str, Any]]:
    url = "https://mcp.exa.ai/mcp"
    headers = {"content-type": "application/json", "accept": "application/json, text/event-stream"}
    started = time.monotonic()
    initialize = {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "free-search", "version": "1.0"},
        },
    }
    _, init_headers, _ = _http_request("POST", url, headers=headers,
                                       data=json.dumps(initialize), timeout=timeout)
    session_id = next((v for k, v in init_headers.items() if k.lower() == "mcp-session-id"), None)
    if not session_id:
        _fail("parse_error", "Exa initialize response omitted the session id")
    remaining = timeout - (time.monotonic() - started)
    if remaining <= 0:
        _fail("timeout", "Exa session initialization timed out")
    headers["mcp-session-id"] = session_id
    call = {
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "web_search_exa", "arguments": {"query": query, "numResults": MAX_SOURCES}},
    }
    _, _, body = _http_request("POST", url, headers=headers,
                               data=json.dumps(call, ensure_ascii=False), timeout=remaining)
    messages = _parse_sse_json(body)
    result: list[dict[str, Any]] = []
    for message in messages:
        rpc_result = message.get("result")
        if isinstance(rpc_result, dict) and rpc_result.get("isError"):
            _fail("other:Exa tool error", "Exa search returned a tool error")
        result.extend(_sources_from_object(message))
        if isinstance(rpc_result, dict):
            for block in rpc_result.get("content", []):
                if isinstance(block, dict) and isinstance(block.get("text"), str):
                    result.extend(_parse_exa_text(block["text"]))
    if not result:
        _fail("empty", "Exa returned no parseable results")
    return result


class _SearchHTMLParser:
    """Extract result cards from Bing/DDG HTML without a third-party parser."""
    _VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self, backend: str):
        from html.parser import HTMLParser

        class Parser(HTMLParser):
            def __init__(self, outer: "_SearchHTMLParser"):
                super().__init__(convert_charrefs=True)
                self.outer = outer

            def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
                self.outer._start(tag.lower(), dict(attrs))

            def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
                self.outer._start(tag.lower(), dict(attrs))
                self.outer._end(tag.lower())

            def handle_endtag(self, tag: str) -> None:
                self.outer._end(tag.lower())

            def handle_data(self, data: str) -> None:
                self.outer._data(data)

        self.backend = backend
        self.results: list[dict[str, str]] = []
        self.current: dict[str, str] | None = None
        self.root_tag: str | None = None
        self.stack: list[str] = []
        self.anchor_capture = False
        self.title_capture = False
        self.citation_capture = False
        self.in_h2 = False
        self.snippet_capture = False
        self.parser = Parser(self)

    def feed(self, body: str) -> list[dict[str, str]]:
        self.parser.feed(body)
        if self.current is not None:
            self._finish()
        return self.results

    def _is_root(self, tag: str, attrs: dict[str, str | None]) -> bool:
        classes = set((attrs.get("class") or "").split())
        if self.backend == "bing-html":
            return tag == "li" and "b_algo" in classes
        return tag == "div" and "result" in classes

    def _start(self, tag: str, attrs: dict[str, str | None]) -> None:
        if self.current is None:
            if self._is_root(tag, attrs):
                self.current = {"title": "", "url": "", "snippet": "", "citation": ""}
                self.root_tag = tag
                self.stack = [tag]
            return
        if tag not in self._VOID:
            self.stack.append(tag)
        if self.backend == "bing-html" and tag == "h2":
            self.in_h2 = True
            self.title_capture = True
        if self.backend == "bing-html" and tag == "cite":
            self.citation_capture = True
        classes = set((attrs.get("class") or "").split())
        if tag == "a" and not self.current["url"]:
            href = _unwrap_result_href(html.unescape(attrs.get("href") or "").strip())
            if href.startswith(("https://", "http://")) and (self.backend != "bing-html" or self.in_h2):
                self.current["url"] = href
                if self.backend != "bing-html":
                    self.anchor_capture = True
        snippet_classes = {"b_caption", "b_algoSlug", "result__snippet", "result__body"}
        if classes.intersection(snippet_classes):
            self.snippet_capture = True

    def _data(self, data: str) -> None:
        if self.current is None:
            return
        if self.backend == "bing-html" and self.title_capture:
            self.current["title"] += data
        elif self.anchor_capture:
            self.current["title"] += data
        if self.snippet_capture:
            self.current["snippet"] += data
        if self.citation_capture:
            self.current["citation"] += data

    def _end(self, tag: str) -> None:
        if self.current is None:
            return
        if tag == "a":
            self.anchor_capture = False
        if tag == "h2":
            self.title_capture = False
            self.in_h2 = False
        if tag == "cite":
            self.citation_capture = False
        if tag in self.stack:
            for index in range(len(self.stack) - 1, -1, -1):
                if self.stack[index] == tag:
                    del self.stack[index:]
                    break
        if not self.stack:
            self._finish()

    def _finish(self) -> None:
        if self.current is not None:
            for key in ("title", "snippet", "citation"):
                self.current[key] = re.sub(r"\s+", " ", html.unescape(self.current[key])).strip()
            if self.current.get("url"):
                self.results.append(self.current)
        self.current = None
        self.root_tag = None
        self.stack = []
        self.anchor_capture = False
        self.title_capture = False
        self.citation_capture = False
        self.in_h2 = False
        self.snippet_capture = False


def _unwrap_result_href(href: str) -> str:
    """Resolve engine-wrapped result links to the destination URL.

    DuckDuckGo links results through `//duckduckgo.com/l/?uddg=<urlencoded>`,
    and protocol-relative hrefs (`//host/path`) carry no scheme at all. Both
    would be dropped by a plain `startswith(("http://", "https://"))` check.
    """
    if href.startswith("//"):
        href = "https:" + href
    parsed = urlparse(href)
    if "duckduckgo.com" in parsed.netloc and parsed.path.startswith("/l/"):
        target = parse_qs(parsed.query).get("uddg")
        if target and target[0]:
            return unquote(target[0])
    return href


def _looks_like_captcha(page: str) -> bool:
    """Detect visible challenge pages and dedicated markers, not script URLs."""
    source = re.sub(r"(?is)<(script|style|noscript)\b[^>]*>.*?</\1\s*>", " ", page)
    source = re.sub(r"(?s)<!--.*?-->", " ", source)
    attrs = " ".join(re.findall(r"(?is)\b(?:id|class|name)\s*=\s*['\"]([^'\"]+)['\"]", source)).lower()
    if re.search(r"\b(?:captcha|challenge-platform|cf-challenge)\b", attrs):
        return True
    visible = html.unescape(re.sub(r"(?s)<[^>]+>", " ", source)).lower()
    visible = re.sub(r"\s+", " ", visible)
    patterns = (
        r"\bcaptcha\b", r"verify (?:that )?you are (?:a )?human", r"verify you(?:'re| are) not a robot",
        r"unusual traffic", r"complete (?:the|this|following) challenge", r"select all squares",
        r"confirm this search was made by a human", r"access denied.*challenge",
    )
    return any(re.search(pattern, visible) for pattern in patterns)


def _looks_like_bing_empty_notice(page: str) -> bool:
    """Check visible page-level text for a search engine empty-results notice."""
    source = re.sub(
        r"(?is)<li\b[^>]*class=['\"][^'\"]*\bb_algo\b[^'\"]*['\"][^>]*>.*?</li>",
        " ", page,
    )
    source = re.sub(r"(?is)<(script|style|noscript)\b[^>]*>.*?</\1\s*>", " ", source)
    source = re.sub(r"(?s)<!--.*?-->", " ", source)
    visible = html.unescape(re.sub(r"(?s)<[^>]+>", " ", source)).lower()
    visible = re.sub(r"\s+", " ", visible)
    patterns = (
        r"\bno results(?: were)? found\b", r"\bno results for\b",
        r"\bdid not match any documents\b", r"\byour search did not return any results\b",
        r"没有找到(?:相关)?(?:的)?(?:搜索)?结果", r"找不到(?:相关)?结果", r"没有相关结果",
        r"未找到(?:相关)?结果", r"沒有找到(?:相關)?(?:搜尋)?結果", r"找不到相關結果",
    )
    return any(re.search(pattern, visible, re.I) for pattern in patterns)


def _bing_cards_have_no_query_match(query: str, results: list[dict[str, str]]) -> bool:
    """Catch Bing's full-page recommendation cards when none mention the query."""
    if len(results) < MAX_SOURCES:
        return False
    terms = re.findall(r"[a-z0-9]+|[\u3400-\u9fff]+", query.lower())
    if not terms:
        return False
    visible = " ".join(
        f"{item.get('title', '')} {item.get('snippet', '')}" for item in results
    ).lower()
    return not any(term in visible for term in terms)


def _backend_bing_html(query: str, timeout: float) -> list[dict[str, Any]]:
    url = "https://cn.bing.com/search?" + urlencode({"q": query})
    _, _, body = _http_request("GET", url, headers={"user-agent": USER_AGENT}, timeout=timeout)
    if _looks_like_captcha(body):
        _fail("captcha", "Bing returned a challenge page")
    if _looks_like_bing_empty_notice(body):
        _fail("empty", "Bing reported no search results")
    results = _SearchHTMLParser("bing-html").feed(body)
    results = [item for item in results if item.get("citation")]
    if _bing_cards_have_no_query_match(query, results):
        _fail("empty", "Bing returned unrelated recommendation cards")
    if not results:
        _fail("empty", "Bing returned no parseable results")
    return results


def _backend_ddg_html(query: str, timeout: float) -> list[dict[str, Any]]:
    url = "https://html.duckduckgo.com/html/?" + urlencode({"q": query})
    _, _, body = _http_request("GET", url, headers={"user-agent": USER_AGENT}, timeout=timeout)
    if _looks_like_captcha(body):
        _fail("captcha", "DuckDuckGo returned a human verification challenge")
    results = _SearchHTMLParser("ddg-html").feed(body)
    if not results:
        _fail("empty", "DuckDuckGo returned no parseable results")
    return results


def _find_serpapi_key(value: Any, in_serpapi: bool = False) -> str | None:
    if isinstance(value, dict):
        for key, child in value.items():
            name = re.sub(r"[^a-z0-9]", "", str(key).lower())
            branch = in_serpapi or "serpapi" in name or name == "serp"
            if isinstance(child, str) and child.strip():
                if ("serpapi" in name and ("key" in name or "token" in name)) or (in_serpapi and name in {"key", "apikey", "token"}):
                    return child.strip()
            found = _find_serpapi_key(child, branch)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_serpapi_key(child, in_serpapi)
            if found:
                return found
    return None


def _serpapi_key() -> str | None:
    import os

    for name in ("SERPAPI_API_KEY", "SERPAPI_KEY"):
        if os.environ.get(name, "").strip():
            return os.environ[name].strip()
    path = Path.home() / ".config" / "opencode" / "search_config.json"
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return _find_serpapi_key(config)


def _serpapi_balance(timeout: float) -> int:
    global _SERP_QUOTA_CACHE
    now = time.monotonic()
    with _SERP_QUOTA_LOCK:
        if _SERP_QUOTA_CACHE and now - _SERP_QUOTA_CACHE[0] < 600:
            return _SERP_QUOTA_CACHE[1]
        key = _serpapi_key()
        if not key:
            _fail("other:SerpAPI key not found", "SerpAPI key is not configured")
        url = "https://serpapi.com/account?" + urlencode({"api_key": key})
        _, _, body = _http_request("GET", url, headers={"accept": "application/json"}, timeout=timeout)
        try:
            data = json.loads(body)
            left = int(data["total_searches_left"])
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            _fail("parse_error", "Could not read SerpAPI quota response")
        _SERP_QUOTA_CACHE = (time.monotonic(), left)
        return left


def _backend_serpapi(query: str, timeout: float) -> list[dict[str, Any]]:
    _CALL_STATE.serpapi_called = False
    _CALL_STATE.low_quota = False
    started = time.monotonic()
    remaining = _serpapi_balance(timeout)
    if remaining <= 0:
        _fail("quota_exhausted", "SerpAPI quota is exhausted")
    _CALL_STATE.low_quota = remaining <= 10
    _CALL_STATE.serpapi_called = True
    key = _serpapi_key()
    if not key:
        _fail("other:SerpAPI key not found", "SerpAPI key is not configured")
    request_timeout = timeout - (time.monotonic() - started)
    if request_timeout <= 0:
        _fail("timeout", "SerpAPI quota check exhausted the backend time limit")
    url = "https://serpapi.com/search?" + urlencode({
        "engine": "google", "q": query, "api_key": key, "num": MAX_SOURCES,
    })
    _, _, body = _http_request("GET", url, headers={"accept": "application/json"}, timeout=request_timeout)
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        _fail("parse_error", "SerpAPI returned invalid JSON")
    if data.get("error"):
        _fail("other:SerpAPI search error", "SerpAPI returned an error response")
    results = _sources_from_object(data)
    if not results:
        _fail("empty", "SerpAPI returned no organic results")
    return results


def _backend_deepseek(query: str, timeout: float) -> list[dict[str, Any]]:
    started = time.monotonic()
    base = os.environ.get("FREE_SEARCH_BROKER", "http://127.0.0.1:9999").rstrip("/")
    api_key = os.environ.get("FREE_SEARCH_BROKER_KEY", "local-broker")
    try:
        _http_request("GET", base + "/healthz", timeout=min(timeout, 5.0))
    except _BackendFailure:
        _fail("broker_down", "DeepSeek broker health check failed")
    request_timeout = timeout - (time.monotonic() - started)
    if request_timeout <= 0:
        _fail("timeout", "broker health check exhausted the backend time limit")
    body = json.dumps({"query": query, "count": 5}, ensure_ascii=False)
    try:
        _, _, text = _http_request(
            "POST", base + "/v1/tools/search",
            headers={"content-type": "application/json", "x-api-key": api_key},
            data=body, timeout=request_timeout,
        )
    except _BackendFailure as exc:
        # A rejected route/auth request did not start a search; dispatched requests
        # that fail later may still have incurred the fee.
        charged = 0.0 if exc.reason == "http_4xx" else DEEPSEEK_COST_CNY
        _fail(exc.reason, exc.message, paid_cost=charged)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        _fail("parse_error", "DeepSeek broker returned invalid JSON", paid_cost=DEEPSEEK_COST_CNY)
    results = _sources_from_object(data)
    if not results:
        _fail("empty", "DeepSeek broker returned no results", paid_cost=DEEPSEEK_COST_CNY)
    for item in results:
        if not item.get("snippet") and item.get("summary"):
            item["snippet"] = item["summary"]
    return results


_BACKENDS = {
    "exa-mcp": _backend_exa_mcp,
    "bing-html": _backend_bing_html,
    "ddg-html": _backend_ddg_html,
    "serpapi": _backend_serpapi,
    "deepseek": _backend_deepseek,
}


def _normalize_sources(items: list[dict[str, Any]], limit: int) -> tuple[list[dict[str, str]], bool]:
    normalized: list[dict[str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        url = item.get("url") or item.get("link")
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            continue
        title = item.get("title") or ""
        snippet = item.get("snippet") or item.get("summary") or ""
        normalized.append({
            "title": str(title).strip(), "url": url.strip(), "snippet": str(snippet).strip(),
        })
    return normalized[:limit], len(normalized) > limit


def _error_code(reason: str) -> str:
    return reason.split(":", 1)[0]


def search(query: str, *, max_results: int = 10, backends: list[str] | None = None,
           timeout_s: float = 40.0, proxy: str | None = None) -> dict[str, Any]:
    """Search in fixed fallback order. Never raises; failures are returned in ``error``.

    ``proxy`` overrides OS proxy detection for this call (e.g. "http://127.0.0.1:10809").
    """
    query = query if isinstance(query, str) else str(query)
    result: dict[str, Any] = {
        "query": query, "backend": "none", "attempts": [], "sources": [],
        "truncated": False, "paid": False, "cost_cny": 0.0, "error": None,
    }
    _CALL_STATE.proxy = (proxy or "").strip() or None
    if not query.strip():
        result["error"] = {"code": "other", "message": "query must not be empty"}
        return result
    try:
        try:
            requested_limit = int(max_results)
        except (TypeError, ValueError, OverflowError):
            result["error"] = {"code": "other", "message": "max_results must be an integer"}
            return result
        if requested_limit <= 0:
            result["error"] = {
                "code": "other",
                "message": f"max_results must be positive, got {requested_limit}",
            }
            return result
        was_limit_clamped = requested_limit > MAX_SOURCES
        limit = MAX_SOURCES if was_limit_clamped else requested_limit
        if was_limit_clamped:
            result["truncated"] = True
        if backends is None:
            selected = list(BACKEND_ORDER)
        else:
            requested = set(backends)
            unknown = sorted(requested.difference(_BACKENDS))
            if unknown:
                result["error"] = {"code": "other", "message": "unknown backend: " + ", ".join(unknown)}
                return result
            selected = [backend for backend in BACKEND_ORDER if backend in requested]
        if not selected:
            result["error"] = {"code": "other", "message": "no backends selected"}
            return result
        try:
            budget = max(0.0, min(float(timeout_s), TOTAL_TIMEOUT_S))
        except (TypeError, ValueError):
            budget = TOTAL_TIMEOUT_S
        deadline = time.monotonic() + budget
        last_failure: _BackendFailure | None = None
        for backend in selected:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                last_failure = _BackendFailure("timeout", "overall search deadline exceeded")
                break
            timeout = min(PER_BACKEND_TIMEOUT_S, remaining)
            started = time.monotonic()
            _CALL_STATE.serpapi_called = False
            _CALL_STATE.low_quota = False
            try:
                raw_sources = _BACKENDS[backend](query, timeout)
                sources, _ = _normalize_sources(raw_sources, limit)
                if not sources:
                    _fail("empty", f"{backend} returned no parseable sources")
                ms = int((time.monotonic() - started) * 1000)
                result["backend"] = backend
                result["sources"] = sources
                result["truncated"] = was_limit_clamped
                result["attempts"].append({"backend": backend, "ok": True, "ms": ms, "count": len(sources)})
                result["paid"] = backend in {"serpapi", "deepseek"}
                if backend == "deepseek":
                    result["cost_cny"] = DEEPSEEK_COST_CNY
                if backend == "serpapi" and getattr(_CALL_STATE, "low_quota", False):
                    result["low_quota"] = True
                return result
            except _BackendFailure as exc:
                last_failure = exc
                ms = int((time.monotonic() - started) * 1000)
                result["attempts"].append({
                    "backend": backend, "ok": False, "ms": ms, "failure_reason": exc.reason,
                })
                if backend == "serpapi" and getattr(_CALL_STATE, "serpapi_called", False):
                    result["paid"] = True
                if exc.paid_cost:
                    result["paid"] = True
                    result["cost_cny"] += exc.paid_cost
                if backend == "serpapi" and getattr(_CALL_STATE, "low_quota", False):
                    result["low_quota"] = True
                logger.debug("search backend %s failed: %s", backend, exc.reason)
            except Exception as exc:  # Public API must never leak exceptions.
                last_failure = _BackendFailure("other:" + type(exc).__name__, "backend failed unexpectedly")
                ms = int((time.monotonic() - started) * 1000)
                result["attempts"].append({
                    "backend": backend, "ok": False, "ms": ms,
                    "failure_reason": last_failure.reason,
                })
                logger.debug("search backend %s raised %s", backend, type(exc).__name__)
            result["backend"] = backend
        if last_failure is None:
            last_failure = _BackendFailure("other:no backend result", "No backend returned results")
        result["sources"] = []
        result["error"] = {"code": _error_code(last_failure.reason), "message": last_failure.message}
        return result
    except Exception as exc:  # Includes malformed caller arguments.
        logger.debug("search failed before routing: %s", type(exc).__name__)
        result["sources"] = []
        result["error"] = {"code": "other", "message": "search could not be completed"}
        return result
