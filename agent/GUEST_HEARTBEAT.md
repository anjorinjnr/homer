# Heartbeat Tasks

This file is checked every 5 minutes by the guest agent.

## CRITICAL: Silent Mode Rules

You are a background process. You are NOT in a conversation. There is no user waiting for a reply.

RULE 1 — DO NOT WRITE TEXT. Any text you write gets sent to users as a message. This is a bug.
Your response must contain ONLY tool calls (exec or message). Zero words. Zero sentences. Nothing.

RULE 2 — The ONLY way to communicate with users is the message tool.
Only call message when you have real content to deliver (a resolved escalation response).

RULE 3 — Empty results mean silence. No undelivered escalations → write nothing.

## User Tasks
For each task: if today >= Schedule date/time → handle it.

### Deliver resolved escalations
Type: system
Schedule: 2026-01-01 00:00
Pre-check: escalations
Recur: every 5 minutes

## Completed

