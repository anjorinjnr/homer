# Heartbeat Tasks

This file is checked every 30 minutes by your nanobot agent.

## CRITICAL: Silent Mode Rules

You are a background process. You are NOT in a conversation. There is no user waiting for a reply.

RULE 1 — DO NOT WRITE TEXT. Any text you write gets sent to users as a message. This is a bug.
Your response must contain ONLY tool calls (exec or message). Zero words. Zero sentences. Nothing.

RULE 2 — The ONLY way to communicate with users is the message tool.
Only call message when you have real content to deliver (an email alert, the morning briefing, a due reminder).

RULE 3 — SKIP means stop. If any exec output starts with "SKIP:", tick the task and write nothing else.

RULE 4 — Empty results mean silence. No emails → tick, no message. No due tasks → no message.

## Announcements
For each entry: send the Message to Recipients (same routing rules as task Recipients), then call announce_update.py --done "[title]" to remove it.

## User Tasks
For each task: if today >= Schedule date/time → handle it, then tick.
If today < Schedule date/time → write nothing, call no tools.

System tasks (Type: system): run the task's exec per Heartbeat Execution rules in AGENTS.md, then tick.
Agentic tasks (Type: agentic): use tools/skills to accomplish the goal, send results to Recipients, then tick. See AGENTS.md for full rules.
Reminder tasks (no Type): send message with the task description, then tick.

When calling `tasks_update.py --tick / --complete / --remove / --edit`, pass the task's `Id:` value (e.g. `t_a2b3c4d5`) as the keyword, never the title. Each task block has an `Id:` line directly beneath its heading.

Model field (optional): overrides the LLM model for this task's execution.
Available presets (all routed via OpenRouter): auto, cheap, gemini-fast, gpt-fast, claude-fast, gemini-balanced, gpt-balanced, claude-balanced, gemini-smart, gpt-smart, claude-smart, default-cheap. Tasks without a Model field use the agent's default model. Prefer `auto` for simple reminder tasks so OpenRouter picks the cheapest viable model per call.

### Morning briefing
Type: system
Schedule: 2026-01-01 07:00
Recur: every 1 day
Prompt-file: users/{{recipient}}.brief.md

### Check escalations
Type: system
Schedule: 2026-01-01 00:00
Recur: every 30 minutes
Pre-check: escalations

## Completed

