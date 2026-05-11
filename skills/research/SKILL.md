---
name: research
description: "Comprehensive research grounded in web data with explicit citations. Use when you need multi-source synthesis—comparisons, current events, market analysis, detailed reports."
metadata: {"nanobot":{"emoji":"🔬"}}
---

# Research Skill

Conduct comprehensive research on any topic with automatic source gathering, analysis, and response generation with citations.

> **Note**: Research can take 30-120 seconds depending on the model.

## Quick Start

```
exec python tools/tavily.py --mode research --query "quantum computing trends"
```

**Examples:**
```
# Basic research (default: mini model, ~30s)
exec python tools/tavily.py --mode research --query "quantum computing trends"

# Comprehensive analysis (pro model, ~60-120s)
exec python tools/tavily.py --mode research --query "AI agents comparison" --model pro

# Quick targeted research
exec python tools/tavily.py --mode research --query "climate change impacts" --model mini
```

## Parameters

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--query` | string | Required | Research topic or question |
| `--model` | string | `mini` | `mini`, `pro`, `auto` |

## Model Selection

**Rule of thumb**: "what does X do?" → mini. "X vs Y vs Z" or "best way to..." → pro.

| Model | Use Case | Speed |
|-------|----------|-------|
| `mini` | Single topic, targeted research | ~30s |
| `pro` | Comprehensive multi-angle analysis | ~60-120s |
| `auto` | API chooses based on complexity | Varies |

## Response Format

```json
{
  "query": "quantum computing trends",
  "answer": "Synthesized multi-source response with inline citations...",
  "sources": [
    { "title": "Source Title", "url": "https://example.com/page" }
  ]
}
```

## Examples

### Quick Overview
```
exec python tools/tavily.py --mode research --query "What is retrieval augmented generation?" --model mini
```

### Technical Comparison
```
exec python tools/tavily.py --mode research --query "LangGraph vs CrewAI for multi-agent systems" --model pro
```

### Market Research
```
exec python tools/tavily.py --mode research --query "Fintech startup landscape 2025" --model pro
```
