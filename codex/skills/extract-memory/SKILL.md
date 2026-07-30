---
name: extract-memory
description: Review the current Codex task and save non-obvious, reusable insights to the shared Logseq vault with exact Codex creator and originating-model provenance. Use when the user invokes $extract-memory, asks to remember or capture the current task, or asks to save a specific learning, mistake, decision, or project fact to Logseq.
---

# Extract Memory

Capture durable task knowledge through the `logseq_vault` MCP server. Write
immediately after deduplication; an empty extraction is valid.

## Workflow

1. Resolve provenance before considering any write:
   - Run `/Users/spiros/.pyenv/versions/3.13.0/bin/python3 /Users/spiros/.codex/hooks/logseq_memory_codex.py resolve-session`.
   - Retain the returned `session`, `project`, and ordered `models`.
   - Stop without writing if the command fails, no models are returned, or the current task cannot be identified. Never guess from global config and never substitute the Terra extraction model.
2. Review the visible task:
   - If the user named one insight, consider only that insight.
   - Otherwise consider up to eight high-value candidates across `patterns`, `mistakes`, `decisions`, and `context`.
   - Exclude generic advice, temporary state, unsupported theories, and credentials.
3. Deduplicate every candidate:
   - Call `mcp__logseq_vault__search_vault` with a specific query and `top_k: 3`.
   - Read a promising match when the preview is insufficient.
   - Skip a candidate when an existing page already captures the conclusion.
4. Write each novel candidate with `mcp__logseq_vault__write_codex_insight`:
   - Pass `type`, `title`, `summary`, `detail`, relevant tags, resolved project, resolved session, and the resolved models unchanged and in their original order.
   - Never include a model used only to perform memory extraction.
5. Report titles written, duplicates skipped, and candidates rejected by the quality bar.

## Quality Bar

Write only when the insight is non-obvious, reusable, concrete, supported by
the current task, and not already represented in the vault. Prefer zero writes
over filler.
