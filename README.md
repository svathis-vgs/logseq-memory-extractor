# logseq-memory-extractor

A Claude Code **Stop hook** that automatically extracts reusable insights from every session and writes them as structured pages into your Logseq vault — giving Claude persistent memory across conversations.

## How it works

```
Session ends
  → Stop hook fires logseq_memory_extractor.py
  → Script reads the session transcript (JSONL)
  → Calls the claude CLI to extract structured insights
  → Writes one Logseq page per insight under pages/claude/
  → Updates the updated:: date on the master index
  → CLAUDE.md injects the index back into the next session
```

Every time a Claude Code session ends, the script:

1. Reads the session transcript from `~/.claude/projects/`
2. Sends the last ~3 000 tokens to `claude -p` for analysis
3. Writes one Logseq page per insight, organised by category
4. Bumps the `updated::` date on `claude/index.md` — all sessions and insights are discovered automatically via Logseq queries, nothing is appended manually

At the start of the next session, Claude reads `~/.claude/CLAUDE.md` which points to the index, closing the memory loop.

## Prerequisites

- [Claude Code Desktop](https://claude.ai/download) — authentication is reused from the Desktop app, no separate API key needed
- [Logseq](https://logseq.com) with an existing graph (or create a new one)
- Python 3.10+

## Installation

### 1. Copy the script

```sh
mkdir -p ~/.claude/hooks
curl -o ~/.claude/hooks/logseq_memory_extractor.py \
  https://raw.githubusercontent.com/svathis-vgs/logseq-memory-extractor/main/logseq_memory_extractor.py
```

### 2. Configure the vault path

Edit the script and set `LOGSEQ_PAGES_DIR` to your vault's `pages/` directory:

```python
LOGSEQ_PAGES_DIR = Path("~/path/to/your/vault/pages").expanduser()
```

### 3. Register the Stop hook

Add to `~/.claude/settings.json`:

```json
{
  "hooks": {
    "Stop": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "python3 /Users/YOU/.claude/hooks/logseq_memory_extractor.py"
          }
        ]
      }
    ]
  }
}
```

### 4. Add memory injection to CLAUDE.md

Create `~/.claude/CLAUDE.md`:

```markdown
## Persistent Memory

At the start of each session, read `~/path/to/your/vault/pages/claude/index.md`
for accumulated patterns, mistakes, decisions, and context from previous sessions.
```

## Vault structure

The script writes into a `claude/` namespace inside your existing Logseq vault:

```
pages/
└── claude/
    ├── index.md                          ← master index, query-driven
    ├── patterns/
    │   └── pattern-<slug>.md
    ├── mistakes/
    │   └── mistake-<slug>.md
    ├── decisions/
    │   └── decision-<slug>.md
    ├── context/
    │   └── context-<slug>.md
    └── sessions/
        └── 2026-04-21-<session-id>.md
```

## Logseq page format

Every insight page uses Logseq's native property syntax starting on line 1:

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
    - Third step or key point
```

Properties:

- **`title::`** — unique page identifier in Logseq; prefixed with category (`Pattern:`, `Decision:`, etc.) to guarantee global uniqueness across all subdirectories
- **`type::`** — links to a category page (`[[pattern]]`, `[[mistake]]`, `[[decision]]`, `[[context]]`, `[[session]]`) — clickable in Logseq's graph
- **`date::`** — links to the Logseq journal entry for that day (`[[yyyy/MM/dd]]` format)
- **`project::`** — links to a project page, grouping all memory from the same codebase
- **`session::`** — links to the session page that produced this insight; creates a graph edge so the session's backlinks panel lists all insights it generated

Session pages additionally carry `exclude-from-graph-view:: true` to keep the graph focused on long-lived insights rather than transient session nodes.

### Detail field formatting

When a detail contains steps, actions, or a list of items, the script writes them as Logseq outline sub-bullets:

```
- ## Detail
  - When consumer lag spikes suddenly:
    - Classify spike shape — vertical jump means consumption stopped
    - Check consumer pods for restarts, OOMKills, or rebalances
    - Grep consumer logs ±5 min for errors or stuck handlers
```

Single-paragraph explanations remain as a flat bullet.

### Index page

`claude/index.md` uses Logseq queries to auto-discover all pages by type — nothing is appended to it on each run:

```
- ## Sessions
  - {{query (property type [[session]])}}
    query-table:: true
    query-sort-by:: date
    query-sort-desc:: true
    query-properties:: [:title :date :project]

- ## Patterns
  - {{query (property type [[pattern]])}}
    ...
```

## Insight categories

| Category | What gets captured |
|----------|-------------------|
| `pattern` | Reusable code approaches and techniques |
| `mistake` | Errors made and how they were corrected |
| `decision` | Architectural choices with reasoning |
| `context` | Project-specific terms, constraints, or facts |
| `session` | Per-session summary with links to all insights |

Only genuinely reusable items are written — the extraction prompt prefers empty arrays over low-quality filler.

## Manual trigger

Run extraction mid-session without closing the app using the `/extract-memory` custom slash command.

Create `~/.claude/commands/extract-memory.md`:

```markdown
Run the Logseq memory extractor to capture insights from this session into the Logseq graph.

Execute this command:

echo '{"session_id": "manual", "cwd": "CWD_PLACEHOLDER"}' | \
  python3 ~/.claude/hooks/logseq_memory_extractor.py

Replace CWD_PLACEHOLDER with the actual current working directory. Report what was written.
```

Then type `/extract-memory` in any Claude Code session.

## Authentication

The script calls `claude -p` (the Claude Code CLI) which is already authenticated via your Claude Code Desktop app — no separate Anthropic API key is required.

A minimal clean environment is passed to the subprocess to prevent IPC with the parent Desktop session:

```python
env = {
    "PATH": ...,
    "HOME": ...,
    "LOGSEQ_EXTRACTOR_RUNNING": "1",   # recursion guard
    "CLAUDE_CODE_OAUTH_TOKEN": ...,    # reuse Desktop app auth
}
```

### Recursion guard

The Stop hook fires when **any** `claude` process exits — including the `claude -p` subprocess the script spawns. Without a guard, this creates infinite recursion. The script detects `LOGSEQ_EXTRACTOR_RUNNING=1` in its environment and exits immediately, breaking the chain.

## Configuration reference

| Variable | Default | Description |
|----------|---------|-------------|
| `LOGSEQ_PAGES_DIR` | *(must set)* | Absolute path to your vault's `pages/` directory |
| `CLAUDE_BIN` | auto-detected | Path to the `claude` CLI binary |
| `MAX_TRANSCRIPT_CHARS` | `12000` | Transcript truncation limit (~3k tokens) |

## License

MIT
