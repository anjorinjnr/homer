---
name: web_search
description: "Search the web using Tavily's LLM-optimized search API. Returns relevant results with content snippets, scores, and metadata. Use when you need to find current web content on any topic."
metadata: {"nanobot":{"emoji":"🔎"}}
---

# Search Skill

Search the web and get relevant results optimized for LLM consumption.

## Quick Start

```
exec python tools/tavily.py --mode search --query "latest developments in quantum computing"
```

**Examples:**
```
# Basic search
exec python tools/tavily.py --mode search --query "python async patterns"

# With options
exec python tools/tavily.py --mode search --query "React hooks tutorial" --max-results 10

# Advanced search with filters
exec python tools/tavily.py --mode search --query "AI news" --time-range week --max-results 10

# Domain-filtered search
exec python tools/tavily.py --mode search --query "machine learning" --include-domains "arxiv.org,github.com" --depth advanced
```

## Parameters

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--query` | string | Required | Search query (keep under 400 chars) |
| `--max-results` | integer | 10 | Maximum results (0-20) |
| `--depth` | string | `basic` | `ultra-fast`, `fast`, `basic`, `advanced` |
| `--time-range` | string | — | `day`, `week`, `month`, `year` |
| `--start-date` | string | — | Return results after this date (`YYYY-MM-DD`) |
| `--end-date` | string | — | Return results before this date (`YYYY-MM-DD`) |
| `--include-domains` | string | — | Comma-separated domains to include (max 300) |
| `--exclude-domains` | string | — | Comma-separated domains to exclude (max 150) |
| `--country` | string | — | Boost results from a specific country |
| `--include-raw-content` | flag | false | Include full page content instead of snippet |

## Response Format

```json
{
  "query": "latest developments in quantum computing",
  "results": [
    {
      "title": "Page Title",
      "url": "https://example.com/page",
      "content": "Extracted text snippet...",
      "score": 0.85
    }
  ],
  "response_time": 1.2
}
```

## Search Depth

| Depth | Latency | Relevance | Content Type |
|-------|---------|-----------|--------------|
| `ultra-fast` | Lowest | Lower | NLP summary |
| `fast` | Low | Good | Chunks |
| `basic` | Medium | High | NLP summary |
| `advanced` | Higher | Highest | Chunks |

**When to use each:**
- `ultra-fast`: Real-time chat, autocomplete
- `fast`: Need chunks but latency matters
- `basic`: General-purpose, balanced
- `advanced`: Precision matters (default recommendation)

## Examples

### Domain-Filtered Search
```
exec python tools/tavily.py --mode search --query "Python async best practices" --include-domains "docs.python.org,realpython.com,github.com" --depth advanced
```

### Search with Full Content
```
exec python tools/tavily.py --mode search --query "React hooks tutorial" --max-results 3 --include-raw-content
```

### Recent News
```
exec python tools/tavily.py --mode search --query "AI news" --time-range week --max-results 10
```

## Tips

- **Keep queries under 400 characters** — think search query, not prompt
- **Break complex queries into sub-queries** — better results than one massive query
- **Use `--include-domains`** to focus on trusted sources
- **Use `--time-range`** for recent information
- **Filter by `score`** (0-1) to get highest relevance results
