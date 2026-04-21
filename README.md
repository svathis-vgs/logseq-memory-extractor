# logseq-memory-extractor

A Claude Code **Stop hook** that automatically extracts reusable insights from every session and writes them as structured pages into your Logseq vault — giving Claude persistent memory across conversations.

## How it works

```
Session ends
  → Stop hook fires logseq_memory_extractor.py
  → Script reads the session transcript (JSONL)
  → Calls the claude CLI to extract structured insights
  → Writes Logseq pages under pages/claude/
  → CLAUDE.md injects the index back into the next session
```

Every time a Claude Code session ends, the script:

1. Reads the session transcript from `~/.claude/projects/`
2. Sends the last ~3 000 tokens to `claude -p` for analysis
3. Writes one Logseq page per insight, organised by category
4. Appends a new entry to the master index (`claude/index.md`)

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
for accumulated patterns, mistakes, and decisions from previous sessions.
```

## Vault structure

The script writes into a `claude/` namespace inside your existing Logseq vault:

```
pages/
└── claude/
    ├── index.md                          ← master index, auto-appended each session
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

Using real subdirectories means Logseq treats each path as a proper namespace: `claude/patterns/pattern-use-pathlib` is a distinct page with `[[claude]]` and `[[patterns]]` in its hierarchy.

## Logseq page format

Every insight page uses Logseq's native property syntax (must start on line 1):

```
type:: [[pattern]]
date:: [[2026/04/21]]
project:: [[my-project]]
session-id:: abc12345
tags:: [[python]] [[testing]]

- ## Summary
  - One-sentence description of the insight.

- ## Detail
  - Full explanation, approach, or reasoning.

- ## Session
  - [[claude/sessions/2026-04-21-abc12345]]
```

- **`type::`** links to a category page (`[[pattern]]`, `[[mistake]]`, `[[decision]]`, `[[context]]`, `[[session]]`) — clickable in Logseq's graph
- **`date::`** links to the Logseq journal entry for that day
- **`project::`** links to a project page, grouping all memory from the same codebase

### Querying in Logseq

Drop these blocks anywhere in your graph to see all entries by category:

```
{{query (property type pattern)}}
{{query (property type mistake)}}
{{query (property type decision)}}
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
Run the Logseq memory extractor to capture insights from this session.

Execute this command:
```
echo '{"session_id": "manual", "cwd": "/path/to/current/project"}' | \
  python3 ~/.claude/hooks/logseq_memory_extractor.py
```

Report what was written.
```

Then type `/extract-memory` in any Claude Code session.

## Authentication

The script calls `claude -p` (the Claude Code CLI) which is already authenticated via your Claude Code Desktop app — no separate Anthropic API key is required.

A clean environment is passed to the subprocess to prevent it from attempting IPC with the parent Desktop session:

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
