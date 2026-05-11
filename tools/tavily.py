#!/usr/bin/env python3
"""
tavily.py — Web search, research, and URL extraction via Tavily API.

Usage:
    python tools/tavily.py --mode search --query "latest developments in quantum computing"
    python tools/tavily.py --mode search --query "AI news" --time-range week --max-results 10
    python tools/tavily.py --mode search --query "machine learning" --include-domains "arxiv.org,github.com" --depth advanced
    python tools/tavily.py --mode research --query "LangGraph vs CrewAI" --model pro
    python tools/tavily.py --mode extract --urls "https://example.com/article"
    python tools/tavily.py --mode extract --urls "https://a.com,https://b.com" --query "pricing" --chunks-per-source 3

Output (JSON):
    search:   { "query": "...", "results": [{"title", "url", "content", "score"}], "response_time": N }
    research: { "query": "...", "answer": "...", "sources": [{"title", "url"}] }
    extract:  { "results": [{"url", "content"}], "failed": [...] }
"""

import argparse
import json
import os
import sys
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

SEARCH_URL   = "https://api.tavily.com/search"
RESEARCH_URL = "https://api.tavily.com/research"
EXTRACT_URL  = "https://api.tavily.com/extract"

MAX_CONTENT_CHARS = 8_000  # per result, to keep context manageable


def get_api_key() -> str:
    key = os.environ.get("TAVILY_API_KEY", "")
    if not key:
        print(json.dumps({"error": "TAVILY_API_KEY not set in environment."}))
        sys.exit(1)
    return key


def post(url: str, payload: dict, api_key: str) -> dict:
    body = json.dumps(payload).encode()
    req = Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(req, timeout=150) as resp:
            return json.loads(resp.read().decode())
    except HTTPError as e:
        body = e.read().decode(errors="replace")
        print(json.dumps({"error": f"HTTP {e.code}: {body}"}))
        sys.exit(1)
    except URLError as e:
        print(json.dumps({"error": f"Request failed: {e.reason}"}))
        sys.exit(1)


def do_search(args, api_key: str) -> None:
    payload: dict = {
        "query": args.query,
        "search_depth": args.depth,
        "max_results": args.max_results,
    }
    if args.time_range:
        payload["time_range"] = args.time_range
    if args.start_date:
        payload["start_date"] = args.start_date
    if args.end_date:
        payload["end_date"] = args.end_date
    if args.include_domains:
        payload["include_domains"] = [d.strip() for d in args.include_domains.split(",")]
    if args.exclude_domains:
        payload["exclude_domains"] = [d.strip() for d in args.exclude_domains.split(",")]
    if args.country:
        payload["country"] = args.country
    if args.include_raw_content:
        payload["include_raw_content"] = True

    data = post(SEARCH_URL, payload, api_key)

    results = []
    for r in data.get("results", []):
        content = r.get("raw_content") or r.get("content", "")
        if len(content) > MAX_CONTENT_CHARS:
            content = content[:MAX_CONTENT_CHARS] + " [truncated]"
        results.append({
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "content": content,
            "score": round(r.get("score", 0), 3),
        })

    print(json.dumps({
        "query": args.query,
        "results": results,
        "response_time": data.get("response_time"),
    }, indent=2))


def do_research(args, api_key: str) -> None:
    payload: dict = {
        "query": args.query,
        "search_depth": "advanced",
    }
    if args.model:
        payload["model"] = args.model

    data = post(RESEARCH_URL, payload, api_key)

    answer = data.get("answer", data.get("response", ""))
    sources = []
    for r in data.get("sources", data.get("results", [])):
        sources.append({
            "title": r.get("title", ""),
            "url": r.get("url", ""),
        })

    print(json.dumps({"query": args.query, "answer": answer, "sources": sources}, indent=2))


def do_extract(args, api_key: str) -> None:
    urls = [u.strip() for u in args.urls.split(",") if u.strip()]
    payload: dict = {"urls": urls}
    if args.query:
        payload["query"] = args.query
    if args.chunks_per_source:
        payload["chunks_per_source"] = args.chunks_per_source
    if args.extract_depth:
        payload["extract_depth"] = args.extract_depth
    if args.timeout:
        payload["timeout"] = args.timeout

    data = post(EXTRACT_URL, payload, api_key)

    results = []
    for r in data.get("results", []):
        content = r.get("raw_content", "")
        if len(content) > MAX_CONTENT_CHARS:
            content = content[:MAX_CONTENT_CHARS] + " [truncated]"
        results.append({"url": r.get("url", ""), "content": content})

    failed = [r.get("url", "") for r in data.get("failed_results", [])]

    print(json.dumps({"results": results, "failed": failed}, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description="Tavily web search/research/extract for Homer.")
    parser.add_argument("--mode", required=True, choices=["search", "research", "extract"])

    # shared
    parser.add_argument("--query", help="Search query, research question, or extraction focus")

    # search
    parser.add_argument("--depth", default="basic",
                        choices=["ultra-fast", "fast", "basic", "advanced"],
                        help="Search depth (default: basic)")
    parser.add_argument("--max-results", type=int, default=10,
                        help="Max results 0-20 (default: 10)")
    parser.add_argument("--time-range", choices=["day", "week", "month", "year"])
    parser.add_argument("--start-date", metavar="YYYY-MM-DD")
    parser.add_argument("--end-date", metavar="YYYY-MM-DD")
    parser.add_argument("--include-domains", metavar="dom1,dom2",
                        help="Comma-separated domains to include")
    parser.add_argument("--exclude-domains", metavar="dom1,dom2",
                        help="Comma-separated domains to exclude")
    parser.add_argument("--country", help="Boost results from a specific country")
    parser.add_argument("--include-raw-content", action="store_true",
                        help="Include full page content instead of snippet")

    # research
    parser.add_argument("--model", choices=["mini", "pro", "auto"],
                        help="Research model: mini (~30s), pro (~60-120s), auto (default: mini)")

    # extract
    parser.add_argument("--urls", help="Comma-separated URLs to extract")
    parser.add_argument("--chunks-per-source", type=int, choices=range(1, 6), metavar="1-5",
                        help="Chunks per URL (requires --query)")
    parser.add_argument("--extract-depth", choices=["basic", "advanced"],
                        help="basic (default) or advanced for JS-rendered pages")
    parser.add_argument("--timeout", type=float, help="Max wait in seconds (1-60)")

    args = parser.parse_args()
    api_key = get_api_key()

    if args.mode == "search":
        if not args.query:
            print(json.dumps({"error": "--query is required for search mode"}))
            sys.exit(1)
        do_search(args, api_key)

    elif args.mode == "research":
        if not args.query:
            print(json.dumps({"error": "--query is required for research mode"}))
            sys.exit(1)
        do_research(args, api_key)

    elif args.mode == "extract":
        if not args.urls:
            print(json.dumps({"error": "--urls is required for extract mode"}))
            sys.exit(1)
        do_extract(args, api_key)


if __name__ == "__main__":
    main()
