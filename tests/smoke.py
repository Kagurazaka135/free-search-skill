"""Live smoke checks. Use --include-paid for up to 4 SerpAPI searches and 3 DeepSeek calls."""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import core.search_core as core

QUERIES = (
    "北京 2026 年供暖时间",
    "rust tokio select macro",
    "Chrome DevTools Protocol Page.navigate",
)


def simulated_quota_zero_check() -> bool:
    saved_backends = core._BACKENDS.copy()
    saved_balance = core._serpapi_balance
    try:
        def fail(query: str, timeout: float) -> list[dict[str, str]]:
            raise core._BackendFailure("empty", "simulated empty result")
        core._BACKENDS.update({name: fail for name in core.BACKEND_ORDER[:3]})
        core._serpapi_balance = lambda timeout: 0  # type: ignore[assignment]
        core._BACKENDS["deepseek"] = lambda query, timeout: [
            {"title": "offline simulation", "url": "https://example.com/", "snippet": "no network call"}
        ]
        outcome = core.search("quota zero simulation", backends=list(core.BACKEND_ORDER))
        return (
            outcome["backend"] == "deepseek"
            and outcome["paid"] is True
            and outcome["cost_cny"] == 0.005
            and any(a.get("failure_reason") == "quota_exhausted" for a in outcome["attempts"])
        )
    finally:
        core._BACKENDS.clear()
        core._BACKENDS.update(saved_backends)
        core._serpapi_balance = saved_balance  # type: ignore[assignment]


def simulated_preflight_checks() -> bool:
    saved_balance = core._serpapi_balance
    saved_request = core._http_request
    try:
        core._serpapi_balance = lambda timeout: 8  # type: ignore[assignment]
        core._http_request = lambda method, url, **kwargs: (
            200, {}, '{"organic_results":[{"title":"quota simulation","link":"https://example.com/"}]}'
        )  # type: ignore[assignment]
        low = core.search("low quota simulation", backends=["serpapi"])
        def accepted(method: str, url: str, **kwargs: object) -> tuple[int, dict[str, str], str]:
            if url.endswith("/healthz"):
                return 200, {}, '{"ok":true}'
            return 200, {}, '{"results":[{"title":"deepseek simulation","url":"https://example.org/","summary":"summary fallback"}]}'
        core._http_request = accepted  # type: ignore[assignment]
        deepseek = core.search("deepseek success simulation", backends=["deepseek"])
        def down(method: str, url: str, **kwargs: object) -> tuple[int, dict[str, str], str]:
            raise core._BackendFailure("network", "simulated broker offline")
        core._http_request = down  # type: ignore[assignment]
        broker = core.search("broker down simulation", backends=["deepseek"])
        def missing_route(method: str, url: str, **kwargs: object) -> tuple[int, dict[str, str], str]:
            if url.endswith("/healthz"):
                return 200, {}, '{"ok":true}'
            raise core._BackendFailure("http_4xx", "HTTP 404")
        core._http_request = missing_route  # type: ignore[assignment]
        missing = core.search("missing broker route simulation", backends=["deepseek"])
        base_ok = (
            low.get("low_quota") is True and low["sources"] and low["paid"] is True
            and deepseek["sources"] and deepseek["paid"] is True and deepseek["cost_cny"] == 0.005
            and deepseek["sources"][0]["snippet"] == "summary fallback"
            and broker.get("error", {}).get("code") == "broker_down"
            and broker["paid"] is False and broker["cost_cny"] == 0.0
            and missing.get("error", {}).get("code") == "http_4xx"
            and missing["paid"] is False and missing["cost_cny"] == 0.0
        )
    finally:
        core._serpapi_balance = saved_balance  # type: ignore[assignment]
        core._http_request = saved_request  # type: ignore[assignment]

    saved_serpapi = core._BACKENDS["serpapi"]
    called = False
    def forbidden_serpapi(query: str, timeout: float) -> list[dict[str, str]]:
        nonlocal called
        called = True
        raise core._BackendFailure("other:test", "SerpAPI must stay untouched")
    try:
        core._BACKENDS["serpapi"] = forbidden_serpapi
        priority = core.search("rust tokio select macro", backends=list(core.BACKEND_ORDER[:4]))
    finally:
        core._BACKENDS["serpapi"] = saved_serpapi

    saved_free = {name: core._BACKENDS[name] for name in core.BACKEND_ORDER[:3]}
    try:
        def offline(query: str, timeout: float) -> list[dict[str, str]]:
            raise core._BackendFailure("network", "simulated offline")
        core._BACKENDS.update({name: offline for name in core.BACKEND_ORDER[:3]})
        disconnected = core.search("offline simulation", backends=list(core.BACKEND_ORDER[:3]))
    finally:
        core._BACKENDS.update(saved_free)
    return bool(
        base_ok and priority["sources"] and priority["backend"] in core.BACKEND_ORDER[:3] and not called
        and disconnected["sources"] == [] and disconnected.get("error", {}).get("code") == "network"
    )


def simulated_regression_checks() -> bool:
    class FakeResponse:
        status_code = 200

        def __init__(self, content: bytes, headers: dict[str, str]):
            self.content = content
            self.headers = headers
            self.encoding = "ISO-8859-1"  # requests' default for text/* without charset
            self.apparent_encoding = None

    class FakeRequests:
        def __init__(self, responses: list[FakeResponse]):
            self.responses = responses

        def request(self, *args: object, **kwargs: object) -> FakeResponse:
            return self.responses.pop(0)

    saved_requests = core._requests
    saved_request = core._http_request
    saved_backend = core._BACKENDS["exa-mcp"]
    try:
        q_chinese = "\u5317\u4eac 2026 \u5e74\u4f9b\u6696\u65f6\u95f4"
        title_chinese = "\u5317\u4eac\u4f9b\u6696\u65f6\u95f4\u516c\u5e03"
        text = f"Title: {title_chinese}\nURL: https://example.com/heating\nHighlights:\n\u5e02\u6c11\u53ef\u67e5\u8be2\u4f9b\u6696\u5b89\u6392"
        payload = {"jsonrpc": "2.0", "id": 2, "result": {"content": [{"type": "text", "text": text}]}}
        sse = "data: " + core.json.dumps(payload, ensure_ascii=False) + "\n\n"
        core._requests = FakeRequests([
            FakeResponse(b"{}", {"content-type": "text/event-stream", "mcp-session-id": "smoke"}),
            FakeResponse(sse.encode("utf-8"), {"content-type": "text/event-stream"}),
        ])  # type: ignore[assignment]
        decoded = core.search(q_chinese, backends=["exa-mcp"])
        title = decoded["sources"][0]["title"] if decoded["sources"] else ""
        bad_mojibake = any(mark in title for mark in ("Ã", "â", "é")) or any(
            "\u0080" <= char <= "\u00ff" for char in title
        )
        chinese_ok = "\u4f9b\u6696" in title and not bad_mojibake

        core._requests = FakeRequests([
            FakeResponse(
                "\u6807\u9898".encode("utf-8"),
                {"content-type": "text/html; charset=utf-8"},
            )
        ])  # type: ignore[assignment]
        _, _, explicit_charset_text = core._http_request("GET", "https://example.com/", timeout=1)
        explicit_charset_ok = explicit_charset_text == "\u6807\u9898"
    finally:
        core._requests = saved_requests  # type: ignore[assignment]

    saved_backend_set = {name: core._BACKENDS[name] for name in core.BACKEND_ORDER[:3]}
    try:
        core._BACKENDS["exa-mcp"] = lambda query, timeout: [
            {"title": f"sample {index}", "url": f"https://example.com/{index}", "snippet": "fixture"}
            for index in range(12)
        ]
        zero = core.search("limit test", backends=["exa-mcp"], max_results=0)
        negative = core.search("limit test", backends=["exa-mcp"], max_results=-5)
        invalid = core.search("limit test", backends=["exa-mcp"], max_results="abc")
        clamped = core.search("limit test", backends=["exa-mcp"], max_results=100)
        three = core.search("limit test", backends=["exa-mcp"], max_results=3)
        limits_ok = all(
            outcome.get("error", {}).get("code") and outcome["sources"] == []
            for outcome in (zero, negative, invalid)
        ) and len(clamped["sources"]) == 10 and clamped["truncated"] is True and (
            len(three["sources"]) == 3 and three["truncated"] is False
        )
    finally:
        core._BACKENDS.update(saved_backend_set)

    def bing_page(count: int, *, relevant: bool, cite: bool = True) -> str:
        cards = []
        for index in range(count):
            title = f"Rust Tokio guide {index}" if relevant else f"Popular unrelated topic {index}"
            snippet = "Rust Tokio select macro reference" if relevant else "Popular articles and recommendations"
            citation = f"<div class='b_attribution'><cite>https://example{index}.com/</cite></div>" if cite else ""
            cards.append(
                f"<li class='b_algo'>{citation}<h2><a href='https://example{index}.com/{index}'>{title}</a></h2>"
                f"<div class='b_caption'><p>{snippet}</p></div></li>"
            )
        return "<html><body><ol id='b_results'>" + "".join(cards) + "</ol></body></html>"

    try:
        core._http_request = lambda *args, **kwargs: (  # type: ignore[assignment]
            200, {"content-type": "text/html; charset=utf-8"}, bing_page(10, relevant=False)
        )
        empty = core.search("asdfqwerzxcv12345qwertyuiop", backends=["bing-html"])
        core._http_request = lambda *args, **kwargs: (  # type: ignore[assignment]
            200, {"content-type": "text/html; charset=utf-8"}, bing_page(2, relevant=True, cite=False)
        )
        uncited = core.search("rust tokio select macro", backends=["bing-html"])
        core._http_request = lambda *args, **kwargs: (  # type: ignore[assignment]
            200, {"content-type": "text/html; charset=utf-8"}, bing_page(5, relevant=True)
        )
        normal = core.search("rust tokio select macro", backends=["bing-html"])
        core._http_request = lambda *args, **kwargs: (  # type: ignore[assignment]
            200, {"content-type": "text/html; charset=utf-8"}, bing_page(2, relevant=False)
        )
        few = core.search("rare query with no literal overlap", backends=["bing-html"])
        bing_ok = empty.get("error", {}).get("code") == "empty" and empty["sources"] == [] and (
            uncited.get("error", {}).get("code") == "empty" and uncited["sources"] == []
        ) and (
            len(normal["sources"]) >= 5 and len(few["sources"]) == 2
        )
    finally:
        core._http_request = saved_request  # type: ignore[assignment]

    # DuckDuckGo wraps result links as //duckduckgo.com/l/?uddg=<encoded>; a
    # plain scheme check drops them and the page parses to zero results.
    ddg_ok = (
        core._unwrap_result_href("https://duckduckgo.com/l/?uddg=https%3A%2F%2Ftokio.rs%2F&rut=x")
        == "https://tokio.rs/"
        and core._unwrap_result_href("//example.com/a") == "https://example.com/a"
        and core._unwrap_result_href("https://plain.example/b") == "https://plain.example/b"
    )
    ddg_html = (
        '<div class="result results_links">'
        '<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Ftokio.rs%2F&amp;rut=1">Tokio</a>'
        '<a class="result__snippet">An async runtime</a>'
        "</div>"
    )
    ddg_results = core._SearchHTMLParser("ddg-html").feed(ddg_html)
    ddg_ok = ddg_ok and len(ddg_results) == 1 and ddg_results[0]["url"] == "https://tokio.rs/"

    # A bot-like UA makes DuckDuckGo answer 202 with a challenge page.
    ua_ok = "Chrome/" in core.USER_AGENT and "compatible" not in core.USER_AGENT

    # requests only reads proxy env vars, so registry-configured proxies must be
    # resolved explicitly or every request silently goes direct.
    proxy_ok = callable(core._detect_proxies) and callable(core._proxies_for_request)

    print(f"Chinese response decoding and UTF-8 charset: {'OK' if chinese_ok and explicit_charset_ok else 'FAIL'}")
    print(f"max_results validation and truncation: {'OK' if limits_ok else 'FAIL'}")
    print(f"Bing recommendation filtering and 1-2 result boundary: {'OK' if bing_ok else 'FAIL'}")
    print(f"DuckDuckGo redirect unwrap and browser UA: {'OK' if ddg_ok and ua_ok else 'FAIL'}")
    print(f"Proxy detection helper present: {'OK' if proxy_ok else 'FAIL'}")
    return (
        chinese_ok and explicit_charset_ok and limits_ok and bing_ok
        and ddg_ok and ua_ok and proxy_ok
    )


def well_formed(result: dict[str, object]) -> bool:
    sources = result.get("sources")
    attempts = result.get("attempts")
    error = result.get("error")
    if isinstance(sources, list) and sources:
        return error is None and isinstance(attempts, list) and bool(attempts) and attempts[-1].get("ok") is True
    return (
        isinstance(sources, list) and not sources and isinstance(error, dict)
        and isinstance(attempts, list) and bool(attempts)
        and bool(attempts[-1].get("failure_reason"))
    )


def run_live(backends: list[str], *, include_paid: bool) -> tuple[int, int, int]:
    successes = 0
    total = 0
    valid = 0
    for backend in backends:
        for index, query in enumerate(QUERIES, 1):
            total += 1
            started = time.monotonic()
            result = core.search(query, backends=[backend])
            elapsed = round((time.monotonic() - started) * 1000)
            valid += int(well_formed(result))
            if result["sources"]:
                successes += 1
                print(f"{backend} q{index}: OK {len(result['sources'])} results {elapsed}ms")
            else:
                reason = result.get("attempts", [{}])[-1].get("failure_reason", result.get("error", {}).get("code", "unknown"))
                print(f"{backend} q{index}: FAIL {reason} {elapsed}ms")
    if include_paid:
        # Force free backends to fail so one real SerpAPI request exercises fallback.
        total += 1
        saved = {name: core._BACKENDS[name] for name in core.BACKEND_ORDER[:3]}
        try:
            def fail(query: str, timeout: float) -> list[dict[str, str]]:
                raise core._BackendFailure("network", "forced by fallback smoke check")
            core._BACKENDS.update({name: fail for name in core.BACKEND_ORDER[:3]})
            result = core.search(QUERIES[1], backends=list(core.BACKEND_ORDER[:4]))
            valid += int(well_formed(result))
            if result["backend"] == "serpapi" and result["sources"]:
                successes += 1
                print(f"fallback chain: OK SerpAPI ({len(result['sources'])} results)")
            else:
                reason = result.get("attempts", [{}])[-1].get("failure_reason", result.get("error", {}).get("code", "unknown"))
                print(f"fallback chain: FAIL {reason}")
        finally:
            core._BACKENDS.update(saved)
    return successes, total, valid


def main() -> int:
    parser = argparse.ArgumentParser(description="Run 3 queries against each selected backend.")
    parser.add_argument("--include-paid", action="store_true", help="Also use up to 4 SerpAPI searches and 3 DeepSeek searches")
    args = parser.parse_args()
    simulated = simulated_quota_zero_check()
    print(f"quota-zero simulated route: {'OK' if simulated else 'FAIL'}")
    preflight = simulated_preflight_checks()
    print(f"quota/DeepSeek/free-priority simulations: {'OK' if preflight else 'FAIL'}")
    regression = simulated_regression_checks()
    backends = list(core.BACKEND_ORDER if args.include_paid else core.BACKEND_ORDER[:3])
    successes, total, valid = run_live(backends, include_paid=args.include_paid)
    print(f"live summary: {successes}/{total} returned sources; {valid}/{total} valid success/error structures")
    return 0 if simulated and preflight and regression and valid == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
