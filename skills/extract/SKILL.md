---
name: extract
description: "Extract content from specific URLs using Tavily's extraction API. Returns clean markdown/text from web pages. Use when you have specific URLs and need their content."
metadata: {"nanobot":{"emoji":"📄"}}
---

# Extract Skill

Extract clean content from specific URLs. Ideal when you know which pages you want content from.

## Quick Start

```
exec python tools/tavily.py --mode extract --urls "https://example.com/article"
```

**Examples:**
```
# Single URL
exec python tools/tavily.py --mode extract --urls "https://example.com/article"

# Multiple URLs
exec python tools/tavily.py --mode extract --urls "https://example.com/page1,https://example.com/page2"

# With query focus and chunks
exec python tools/tavily.py --mode extract --urls "https://example.com/docs" --query "authentication API" --chunks-per-source 3

# Advanced extraction for JS-rendered pages
exec python tools/tavily.py --mode extract --urls "https://app.example.com" --extract-depth advanced --timeout 60
```

## Parameters

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--urls` | string | Required | Comma-separated URLs to extract (max 20) |
| `--query` | string | — | Reranks chunks by relevance |
| `--chunks-per-source` | integer | 3 | Chunks per URL (1-5, requires `--query`) |
| `--extract-depth` | string | `basic` | `basic` or `advanced` (for JS pages) |
| `--timeout` | float | — | Max wait in seconds (1-60) |

## Response Format

```json
{
  "results": [
    {
      "url": "https://example.com/article",
      "content": "# Article Title\n\nContent in markdown..."
    }
  ],
  "failed": []
}
```

## Extract Depth

| Depth | When to Use |
|-------|-------------|
| `basic` | Simple text extraction, faster |
| `advanced` | Dynamic/JS-rendered pages, tables, structured data |

## Examples

### Single URL Extraction
```
exec python tools/tavily.py --mode extract --urls "https://docs.python.org/3/tutorial/classes.html"
```

### Targeted Extraction with Query
```
exec python tools/tavily.py --mode extract --urls "https://example.com/react-hooks,https://example.com/react-state" --query "useState and useEffect patterns" --chunks-per-source 2
```

### JavaScript-Heavy Pages
```
exec python tools/tavily.py --mode extract --urls "https://app.example.com/dashboard" --extract-depth advanced --timeout 60
```

### Batch Extraction
```
exec python tools/tavily.py --mode extract --urls "https://example.com/p1,https://example.com/p2,https://example.com/p3"
```

## Tips

- **Max 20 URLs per request** — batch larger lists
- **Use `--query` + `--chunks-per-source`** to get only relevant content
- **Try `basic` first**, fall back to `--extract-depth advanced` if content is missing
- **Set longer `--timeout`** for slow pages (up to 60s)
- **Check `failed`** in the response for URLs that couldn't be extracted
