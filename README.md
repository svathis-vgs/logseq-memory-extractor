# logseq-memory-extractor

A Claude Code hook framework that gives Claude persistent, semantic memory across sessions via a Logseq vault. Three scripts work together:

| Script | Hook | Role |
|--------|------|------|
| `logseq_memory_extractor.py` | Stop | Extracts insights from the session transcript and writes them as Logseq pages |
| `logseq_memory_index.py` | (one-time CLI) | Builds the semantic search index over all vault files |
| `logseq_memory_retriever.py` | UserPromptSubmit | Retrieves the most relevant vault pages for each new prompt |
| `logseq_memory_mcp.py` | MCP server | On-demand search, read, and write access mid-conversation |

## How it works

```
Session ends
  → Stop hook fires logseq_memory_extractor.py
    → Reads session transcript (JSONL)
    → Calls claude -p to extract structured insights
    → Writes one Logseq page per insight under pages/claude/
    → Updates the vault index incrementally (if built)
    → Regenerates digest.md — plain-text summary for Claude to read at session start

User types a new message
  → UserPromptSubmit hook fires logseq_memory_retriever.py
    → Embeds the prompt with all-MiniLM-L6-v2
    → Cosine-similarity search over vault_index.npz
    → Top-5 matching vault pages injected as a system reminder
    → Claude reads them before responding

Mid-conversation (optional MCP server)
  → Claude calls search_vault with a targeted query
    → Returns the most relevant pages for the current subtopic
  → Claude calls write_insight to capture a learning immediately
    → Page written to vault without waiting for session end
```

## Prerequisites

- [Claude Code Desktop](https://claude.ai/download) — authentication is reused from the Desktop app
- [Logseq](https://logseq.com) with an existing graph (or create a new one)
- Python 3.10+
- For semantic search and MCP server: `pip install sentence-transformers numpy mcp`

## Installation

### 1. Copy the scripts

```sh
mkdir -p ~/.claude/hooks
for script in logseq_memory_extractor logseq_memory_index logseq_memory_retriever logseq_memory_mcp; do
  curl -o ~/.claude/hooks/${script}.py \
    https://raw.githubusercontent.com/svathis-vgs/logseq-memory-extractor/main/${script}.py
done
```

### 2. Configure the vault path

Edit `logseq_memory_extractor.py` and set `LOGSEQ_PAGES_DIR` to your vault's `pages/` directory:

```python
LOGSEQ_PAGES_DIR = Path("~/path/to/your/vault/pages").expanduser()
```

Set the same path in `logseq_memory_index.py`:

```python
LOGSEQ_PAGES_DIR = Path("~/path/to/your/vault/pages").expanduser()
```

### 3. Register the hooks

Add to `~/.claude/settings.json`:

```json
{
  "hooks": {
    "Stop": [
      {
        "matcher": "",
        "hooks": [{"type": "command", "command": "python3 /Users/YOU/.claude/hooks/logseq_memory_extractor.py"}]
      }
    ],
    "UserPromptSubmit": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "python3 /Users/YOU/.claude/hooks/logseq_memory_retriever.py",
            "timeout": 10
          }
        ]
      }
    ]
  }
}
```

Replace `/Users/YOU` with your home directory path.

### 4. Register the MCP server (optional but recommended)

The MCP server enables Claude to search the vault mid-conversation and write insights immediately without waiting for session end.

**Important:** Desktop apps (Claude Code Desktop, Codex Desktop) launch MCP servers without sourcing your shell profile, so `python3` resolves to nothing and pyenv shims fail silently. Use the full resolved binary path:

```sh
python3 -c "import sys; print(sys.executable)"
```

**Claude Code Desktop** — add to `~/.claude/settings.json` under `"mcpServers"`:

```json
{
  "mcpServers": {
    "logseq-vault": {
      "command": "/Users/YOU/.pyenv/versions/3.x.x/bin/python3",
      "args": ["/Users/YOU/.claude/hooks/logseq_memory_mcp.py"],
      "env": {
        "TRANSFORMERS_OFFLINE": "1",
        "HF_DATASETS_OFFLINE": "1"
      }
    }
  }
}
```

**Codex Desktop** — add to `~/.codex/config.toml`:

```toml
[mcp_servers.logseq-vault]
command = "/Users/YOU/.pyenv/versions/3.x.x/bin/python3"
args = ["/Users/YOU/.claude/hooks/logseq_memory_mcp.py"]

[mcp_servers.logseq-vault.env]
TRANSFORMERS_OFFLINE = "1"
HF_DATASETS_OFFLINE = "1"
```

Replace the python path with the output of the command above. The `TRANSFORMERS_OFFLINE` flags prevent the embedding model from trying to reach HuggingFace on startup — the model is already cached locally after the index build step.

The server exposes five tools and fires macOS notifications on key events:

| Tool | Description | Notification |
|------|-------------|--------------|
| `search_vault(query, top_k, category)` | Semantic search with staleness labels (fresh/aging/stale/abandoned) | `🔍 search_vault — N match(es)` |
| `read_page(path)` | Read a vault page returned by search | — |
| `write_insight(type, title, summary, detail, tags, project)` | Write with compose-time sanitization and post-write verification | `✍️ write_insight — <title>` (or `⏭️` if dedup skipped) |
| `list_recent(category, limit)` | Browse recently modified pages | — |
| `lint_vault(category, limit)` | Scan for Logseq format violations (phantom tags, broken backticks, bad properties) | `🔍 lint_vault — N files with issues` |

Restart the desktop app after editing the config for the server to appear.

### 5. Add memory injection to CLAUDE.md

Create or edit `~/.claude/CLAUDE.md`:

```markdown
## Persistent Memory

At the start of each session, read `~/path/to/your/vault/pages/claude/digest.md`
for accumulated patterns, mistakes, decisions, and context from previous sessions.
This file is plain text regenerated by the Stop hook — it is always current.
```

### 6. Build the semantic index (one-time)

```sh
pip install sentence-transformers numpy mcp
python3 ~/.claude/hooks/logseq_memory_index.py
```

This embeds all vault files and saves `~/.claude/vault_index.npz`. Rebuilding takes
roughly 1 minute per 8,000 files. Future sessions append new files automatically.

## Retrieval behaviour

The `UserPromptSubmit` hook fires before every user message. It:

1. Embeds the prompt using `all-MiniLM-L6-v2` (384 dimensions, offline, ~22 MB)
2. Computes cosine similarity against the pre-built index
3. Returns up to 5 results above a 0.38 similarity floor
4. Injects their contents as a `system-reminder` Claude reads before responding

The MCP server complements this with on-demand targeted queries mid-conversation — useful when the topic shifts after the initial retrieval.

**Latency:** ~600–900 ms per message (model load from disk cache: ~500 ms; encode + search: <50 ms). The model is never downloaded at query time — only at index build time.

**Fallback:** If the index hasn't been built or `sentence-transformers` is not installed, the hook exits silently without affecting the session.

## Accumulation controls

Two settings in `logseq_memory_extractor.py` keep the vault from growing unbounded:

**Extraction prompt** — instructs the extractor to only capture non-obvious, project-specific insights (max 5 per category per session). Empirically cuts the daily accumulation rate from ~330 to ~140 files/day.

**Slug near-match dedup** — before writing a new file, checks whether a file with the same 2-word slug prefix already exists in the category subdirectory. Prevents same-concept files with different trailing words from accumulating (e.g. `kafka-rebalance-storm` blocks `kafka-rebalance-recovery`).

## Vault health

Three features keep the vault healthy over time:

**Staleness tracking** — every insight page carries a `last-updated::` property (set at write time). Search results include a staleness label: `fresh` (0–7 days), `aging` (8–14d), `stale` (15–30d), or `abandoned (Nd)`. This surfaces context that may be outdated — a decision from 3 months ago carries less weight than one from yesterday. Falls back to `date::` if `last-updated::` is missing.

**Compose-time sanitization** — `write_insight` sanitizes content before writing:
- Escapes bare `#digits` → `` `#1` `` (prevents phantom Logseq tag pages)
- Escapes `{{ }}` macros → backtick-wrapped (prevents broken Logseq macros)
- Post-write verification re-reads the file and checks for odd backtick counts, surviving bare `#digits`, unescaped macros, and single-colon properties

**Lint tool** — `lint_vault` scans all vault files (or a single category) for format violations:
- Odd backtick count (unclosed inline code)
- Bare `#digit` outside `tags::` lines (phantom tag pages)
- Unescaped `{{ }}` macros
- Single-colon properties (`key:` instead of `key::`)
- Missing required properties (`title::`, `type::`, `date::`)

Run periodically or after bulk imports to catch format issues before they create phantom pages in Logseq.

## Vault structure

```
pages/
└── claude/
    ├── index.md          ← Logseq query-driven master index (never appended manually)
    ├── digest.md         ← Plain-text summary for Claude to read (regenerated each session)
    ├── patterns/
    │   └── pattern-<slug>.md
    ├── mistakes/
    │   └── mistake-<slug>.md
    ├── decisions/
    │   └── decision-<slug>.md
    ├── context/
    │   └── context-<slug>.md
    └── sessions/
        └── <yyyy_mm_dd>/
            └── <date>-<session-id>.md
```

## Insight page format

Every insight page uses Logseq's native property syntax:

```
title:: Pattern: Use Pathlib Over Os
type:: [[pattern]]
date:: [[2026/04/21]]
project:: [[my-project]]
session:: [[Session 2026-04-21 abc12345 — my-project]]
tags:: [[python]] [[filesystem]]

- ## Summary
  - One-sentence description of the insight.

- ## Detail
  - Context sentence describing when this applies:
    - First step or key point
    - Second step or key point
```

## Insight categories

| Category | What gets captured |
|----------|--------------------|
| `pattern` | Reusable code approaches and techniques |
| `mistake` | Errors made and how they were corrected |
| `decision` | Architectural choices with reasoning |
| `context` | Project-specific terms, constraints, or facts |
| `session` | Per-session summary with links to all insights |

Only genuinely non-obvious items are written — the extraction prompt prefers empty arrays over low-quality filler.

## Manual operations

**Trigger extraction mid-session** using the `/extract-memory` custom slash command.

Create `~/.claude/commands/extract-memory.md` with content that tells Claude to:
1. Review the current conversation directly
2. Call `mcp__logseq-vault__search_vault` before each candidate to check for duplicates
3. Call `mcp__logseq-vault__write_insight` only for insights not already in the vault
4. Report count written, count skipped, and titles written

This approach requires the MCP server (step 4). It has no subprocess or recursion risk, and
dedup is checked per-insight with full conversation context. The Stop hook's `logseq_memory_extractor.py`
still runs automatically at session end — `/extract-memory` is the manual mid-session trigger.

Example `~/.claude/commands/extract-memory.md`:

```markdown
Review this conversation and capture non-obvious, reusable insights into the Logseq vault
using the `logseq-vault` MCP server.

For each genuine insight, call `mcp__logseq-vault__search_vault` first (top_k=3). If a
matching page exists and covers it well, skip it. Otherwise call `mcp__logseq-vault__write_insight`.

Quality bar — only capture if ALL are true:
1. Non-obvious: a competent engineer wouldn't already know this
2. Reusable: applies beyond this specific task
3. Concrete: specific enough to act on
4. Not already in vault (verified by search above)

Aim for 3–8 insights per session; never more than 15.
After writing, report: count written, count skipped (duplicate), list of titles written.
```

**Rebuild the full index** (after vault consolidation or model change):

```sh
TRANSFORMERS_OFFLINE=1 HF_DATASETS_OFFLINE=1 python3 ~/.claude/hooks/logseq_memory_index.py
```

If you have a SOCKS proxy active (`ALL_PROXY` env var), also unset it: `env -u ALL_PROXY ...`. The model is cached locally and never needs a network call after the first build.

**Rebuild quietly** (no progress bar, for cron jobs):

```sh
TRANSFORMERS_OFFLINE=1 python3 ~/.claude/hooks/logseq_memory_index.py --quiet
```

## Authentication

The extractor calls `claude -p` (the Claude Code CLI) which reuses authentication
from your Claude Code Desktop app — no separate API key needed.

The retriever uses `sentence-transformers` entirely locally — no network calls at
query time. The model is downloaded once on first use (~22 MB from HuggingFace)
and cached in `~/.cache/torch/sentence_transformers/`.

### Recursion guard

The Stop hook fires when **any** `claude` process exits — including the `claude -p`
subprocess the extractor spawns. Without a guard, this creates infinite recursion.
The extractor sets `LOGSEQ_EXTRACTOR_RUNNING=1` in the subprocess environment, and
both the extractor and retriever exit immediately when this variable is detected.

## Configuration reference

### logseq_memory_extractor.py

| Variable | Default | Description |
|----------|---------|-------------|
| `LOGSEQ_PAGES_DIR` | *(must set)* | Absolute path to your vault's `pages/` directory |
| `CLAUDE_BIN` | auto-detected | Path to the `claude` CLI binary |
| `MAX_TRANSCRIPT_CHARS` | `12000` | Transcript truncation limit (~3k tokens) |

### logseq_memory_index.py / logseq_memory_retriever.py

| Variable | Default | Description |
|----------|---------|-------------|
| `LOGSEQ_PAGES_DIR` | *(must set)* | Same vault path as the extractor |
| `INDEX_PATH` | `~/.claude/vault_index.npz` | Where the compressed index is stored |
| `MODEL_NAME` | `all-MiniLM-L6-v2` | sentence-transformers model (384-dim, ~22 MB) |
| `TOP_K` | `5` | Max results returned per prompt (retriever) |
| `MIN_SCORE` | `0.38` | Cosine similarity floor — results below this are dropped |
| `MAX_FILE_CHARS` | `600` | Characters per result injected into context (retriever) |

### logseq_memory_mcp.py

| Variable | Default | Description |
|----------|---------|-------------|
| `LOGSEQ_PAGES_DIR` | *(must set)* | Same vault path as the other scripts |
| `INDEX_PATH` | `~/.claude/vault_index.npz` | Shared index file |
| `MODEL_NAME` | `all-MiniLM-L6-v2` | Same model as the retriever |
| `MIN_SCORE` | `0.38` | Cosine similarity floor for `search_vault` |
| `MAX_FILE_CHARS` | `1500` | Characters per search result (larger than retriever — not auto-injected) |
| `STALENESS_DAYS` | `{"fresh": 7, "aging": 14, "stale": 30}` | Day thresholds for staleness labels in search results |

## License

MIT
