---
name: analytics
description: Answer usage and cost questions about Homer — who's using it, how often, which skills/tools are most popular, and what it costs. Uses session log data indexed in SQLite.
metadata: {"nanobot":{"always":false,"emoji":"📊"}}
---

# Analytics Skill

Homer tracks its own usage by parsing nanobot session logs and indexing them into a local SQLite database. Use this skill to answer questions about usage patterns, costs, and trends.

## Rules

- Always run `analytics_query.py` with the appropriate mode — never guess metrics from memory.
- Auto-sync is built in — every query command runs an incremental sync first. Never run `--sync` separately before a query.
- The primary user (admin) can see all metrics. Future per-user reports will scope to each individual.
- Costs are **estimates** based on message length (~4 chars/token) and per-model pricing. Flag this when reporting cost figures.
- The database lives at `data/analytics.db` and is gitignored. If it doesn't exist yet, `--sync` creates it.

## On-demand queries

### Usage summary (last N days)
```
{HOMER_VENV} {HOMER_TOOLS}/analytics_query.py --summary --days 7
{HOMER_VENV} {HOMER_TOOLS}/analytics_query.py --summary --days 30
{HOMER_VENV} {HOMER_TOOLS}/analytics_query.py --summary --days 30 --user <primary_user>
{HOMER_VENV} {HOMER_TOOLS}/analytics_query.py --summary --month 2026-03
```
Output includes: total messages, breakdown by user/channel/skill, top tools, cost, avg response time.

### Breakdown by dimension
```
{HOMER_VENV} {HOMER_TOOLS}/analytics_query.py --breakdown skill --days 30
{HOMER_VENV} {HOMER_TOOLS}/analytics_query.py --breakdown tool --days 30
{HOMER_VENV} {HOMER_TOOLS}/analytics_query.py --breakdown user --days 30
{HOMER_VENV} {HOMER_TOOLS}/analytics_query.py --breakdown channel --days 30
```

### Cost report
```
{HOMER_VENV} {HOMER_TOOLS}/analytics_query.py --cost-report --days 30
{HOMER_VENV} {HOMER_TOOLS}/analytics_query.py --cost-report --month 2026-03
```
Output includes total estimated cost (USD), breakdown by model and by user.

## Answering common questions

**"How many messages did I send this week?"**
Run `--summary --days 7`. Report the count for the relevant user from `by_user`.

**"What's my most-used skill?"**
Run `--summary --days 30`. Report the top key in `by_skill`.

**"How much has Homer cost this month?"**
Run `--cost-report --month YYYY-MM`. Report `total_cost_usd` and note it is an estimate.

**"Show me usage trends for the last 30 days"**
Run `--summary --days 30`. Summarize total, by-user, by-skill, and top tools naturally.

**"Which tools does Homer use most?"**
Run `--breakdown tool --days 30`. List the top entries with call counts.

## Formatting responses

Summarize JSON output naturally — do not dump raw JSON to the user. Example:

> Over the last 7 days, Homer handled 47 interactions: 32 from the primary user, 10 from another household member, and 5 system heartbeat tasks. Most-used skill: Finance (12 interactions). Top tool: plaid_fetch (18 calls). Estimated cost: $0.14 (Claude Sonnet).

Always note that cost figures are estimates based on message length, not actual token counts.

## Automated weekly report (heartbeat — do not run on-demand)

The weekly report task in `agent/HEARTBEAT.md` runs every Monday at 8am:

```
{HOMER_VENV} {HOMER_TOOLS}/analytics_query.py --weekly-report
```

Output is JSON. If output starts with `SKIP:` → tick, no message. Otherwise format and send to the recipients in the task's `Recipients` field:

```
Homer Weekly Usage (Mar 15-22)

Messages: 156 (+12% vs last week)
  [user]: 89  |  [user]: 42  |  System: 25

Top skills: Finance (18), Weather (12), Web Search (8)
Top tools: plaid_fetch (24), gmail_fetch (20)

Cost: ~$2.47 est. (Haiku $1.12, Sonnet $1.35)
Avg response: 1.8s
```

If `trend_vs_prior_week` is present, include it in the message. If `top_skill` is present, mention it.
