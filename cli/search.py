"""Command-line wrapper around core.search_core."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.search_core import BACKEND_ORDER, search  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Search the web with a free-first fallback chain.")
    parser.add_argument("query", help="Search query")
    parser.add_argument("--json", action="store_true", dest="as_json", help="Print the complete JSON result")
    parser.add_argument("--backend", choices=BACKEND_ORDER, help="Run only this backend (testing/debugging)")
    parser.add_argument("--proxy", help="Override proxy for this call, e.g. http://127.0.0.1:10809")
    args = parser.parse_args(argv)
    result = search(
        args.query,
        backends=[args.backend] if args.backend else None,
        proxy=args.proxy,
    )
    if args.as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif result["sources"]:
        print(f"[{result['backend']}] {result['query']} ({len(result['sources'])} 条)")
        for index, source in enumerate(result["sources"], 1):
            print(f"{index}. {source['title'] or source['url']}")
            print(f"   {source['url']}")
            if source["snippet"]:
                print(f"   {source['snippet']}")
        if result.get("low_quota"):
            print("   SerpAPI 余额不多（≤10 次）")
        if result["paid"]:
            print(f"   paid=true, cost_cny={result['cost_cny']:.3f}")
    else:
        error = result.get("error") or {"code": "empty", "message": "没有返回结果"}
        print(f"搜索失败 [{error['code']}]: {error['message']}", file=sys.stderr)
        if result["attempts"]:
            print("尝试顺序: " + " → ".join(
                f"{item['backend']}={item.get('failure_reason', 'ok')}" for item in result["attempts"]
            ), file=sys.stderr)
    return 0 if result["sources"] else 1


if __name__ == "__main__":
    raise SystemExit(main())