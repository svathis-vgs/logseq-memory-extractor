# logseq-memory-extractor

A shared Claude Code and Codex framework that provides persistent, semantic
memory through one Logseq vault. Claude's established page and deduplication
contracts are reused by Codex and protected by regression tests.

| Script | Hook | Role |
|--------|------|------|
| `logseq_memory_extractor.py` | Stop | Extracts insights from the session transcript and writes them as Logseq pages |
| `logseq_memory_index.py` | (one-time CLI) | Builds the semantic search index over the four insight categories (not sessions) |
| `logseq_memory_retriever.py` | UserPromptSubmit | Retrieves the most relevant vault pages for each new prompt |
| `logseq_memory_mcp.py` | MCP server | On-demand search, read, and write access mid-conversation |
| `logseq_memory_shared.py` | Shared library | Frozen prompt, transcript, rendering, dedup, index, digest, and write-lock behavior |
| `logseq_memory_codex.py` | Codex SessionEnd | Queues and processes Codex task extraction |
| `logseq_memory_migrate_links.py` | One-time CLI | Converts existing creator/model provenance values to Logseq page links |

## How it works

```
Session ends
  → Stop hook fires logseq_memory_extractor.py
    → Reads session transcript (JSONL)
    → Calls claude -p to extract structured insights
    → Writes one Logseq page per insight under pages/memory/
      (or appends to an existing same-meaning page, see Accumulation controls)
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

Codex task ends
  → SessionEnd snapshots the filtered task transcript and original models
  → Detached ephemeral gpt-5.6-terra worker extracts structured insights
  → Shared writer saves pages with creator:: [[codex]]
  → $extract-memory writes individual Codex insights on demand
```

Every newly created insight page, and every automatic session page, receives
`date::` and `last-updated::` properties. Both dates are identical at creation.
The only operation that later rewrites an insight page is semantic dedup: a new
note that matches an existing page is appended to it as a `## Update <date>`
block and `last-updated::` is set to that date. Exact or slug-prefix duplicates
are still skipped without touching the existing page.

## Prerequisites

- [Claude Code Desktop](https://claude.ai/download) — authentication is reused from the Desktop app
- [Logseq](https://logseq.com) with an existing graph (or create a new one)
- Python 3.10+ on macOS, Linux or Windows (CI runs the regression suite on all three with Python 3.11 and 3.13)
- For semantic search and MCP server: `pip install sentence-transformers numpy mcp`

## Installation

Nothing in the shared layer belongs to one agent. Scripts, the semantic index and
the write lock live in one agent-neutral home, `~/.logseq-memory/`
(`LOGSEQ_MEMORY_HOME`), and memory pages live in `<vault>/pages/memory/`.
Claude Code and Codex each only register hooks and the MCP server that point at
the shared scripts.

```
~/.logseq-memory/
├── bin/                  ← all scripts + codex_extraction_schema.json
├── venv/                 ← private Python env (installer)
├── config.json           ← {"pages_dir": ...} written by the installer
├── vault_index.npz       ← shared semantic index
├── vault_index.lock      ← shared write lock
├── extractor.log         ← one line per Claude/Codex extraction run (ok / failed / error)
├── backups/<timestamp>/  ← copies of config files the installer edited
└── retriever_disabled    ← optional: create to pause prompt retrieval
```

### Quick install (`install.py`)

On a new computer, clone the repo and run the installer with any Python 3.10+.
It needs only the standard library to start:

```sh
git clone https://github.com/svathis-vgs/logseq-memory-extractor.git
cd logseq-memory-extractor
python3 install.py --vault ~/path/to/your/graph/pages
```

On Windows run it the same way from PowerShell (`py install.py --vault ...`).

It installs for whichever agents it finds: Claude Code (`~/.claude` or `claude`
on `PATH`), the Claude Desktop app (its config folder, or the app in
`/Applications` / `%LOCALAPPDATA%`), and Codex (`~/.codex`, `codex` on `PATH`, or
the Codex / ChatGPT app). It warns when an agent is registered but its CLI is
not on `PATH`: retrieval and the MCP server still work, but saving insights at
session end runs `claude -p` / `codex exec`.

1. Copies the scripts to `~/.logseq-memory/bin/` and records the vault (and the
   `claude` / `codex` CLI paths it found) in `~/.logseq-memory/config.json`.
   Scripts read it when the matching environment variable is unset, so hooks
   started by GUI apps without a shell `PATH` still find everything.
2. Creates `~/.logseq-memory/venv` with sentence-transformers, numpy and mcp,
   and caches the embedding model (the only step that needs the network).
3. Creates `<vault>/memory/` and builds the index (an empty one for a new vault).
4. Claude Code: Stop and UserPromptSubmit hooks and the `logseq-vault` MCP server
   in `~/.claude/settings.json` (and the Desktop app's config when present), plus
   `sandbox.filesystem.allowWrite` entries when a sandbox is configured.
5. Codex: hooks in `~/.codex/hooks.json`, the `logseq_vault` MCP server and
   `[features] hooks = true` in `~/.codex/config.toml`, and the `$extract-memory`
   skill.
6. A marked block in `~/.claude/CLAUDE.md` / `~/.codex/AGENTS.md` pointing the
   agent at the vault and digest, skipped when the file already mentions the
   digest.

Re-running is safe: it replaces its own entries (including ones from older
install locations such as `~/.claude/hooks/`), keeps everything else, and only
rewrites files that change. Each edited file is backed up first. Useful flags:
`--dry-run`, `--claude` / `--no-claude`, `--codex` / `--no-codex`,
`--skip-deps`, `--skip-index`, `--home DIR`, and `--uninstall` (removes the
agent registrations and managed blocks; leaves the vault, venv and index).
Afterwards restart Claude Desktop / Claude Code and Codex, and trust the new
hooks with `/hooks` in Codex.

The steps below describe the same setup done by hand.

### 1. Install the shared scripts

```sh
mkdir -p ~/.logseq-memory/bin
for f in logseq_memory_shared.py logseq_memory_extractor.py logseq_memory_index.py \
         logseq_memory_retriever.py logseq_memory_mcp.py logseq_memory_migrate_links.py \
         logseq_memory_codex.py codex_extraction_schema.json; do
  curl -o ~/.logseq-memory/bin/$f \
    https://raw.githubusercontent.com/svathis-vgs/logseq-memory-extractor/main/$f
done
```

### 2. Configure paths

Defaults: vault pages at `~/VGS/Notes/pages`, index and lock under
`~/.logseq-memory/`. Override with environment variables in each hook and MCP
registration (`env` blocks), or edit the defaults in `logseq_memory_shared.py`:

| Variable | Default |
|----------|---------|
| `LOGSEQ_PAGES_DIR` | `~/VGS/Notes/pages` |
| `LOGSEQ_MEMORY_HOME` | `~/.logseq-memory` |
| `LOGSEQ_INDEX_PATH` | `$LOGSEQ_MEMORY_HOME/vault_index.npz` |
| `LOGSEQ_LOCK_PATH` | `$LOGSEQ_MEMORY_HOME/vault_index.lock` |

### 3. Build the semantic index (one-time)

```sh
pip install sentence-transformers numpy mcp
python3 ~/.logseq-memory/bin/logseq_memory_index.py
```

This embeds the title and summary of every page in `memory/patterns/`,
`memory/mistakes/`, `memory/decisions/` and `memory/context/` (session pages are
not indexed). Rebuilding takes roughly 1 minute per 8,000 files and holds the
vault write lock for its duration. New pages from every write path are appended
to the index automatically.

### 4. Register with Claude Code

Desktop apps launch hooks and MCP servers without sourcing your shell profile,
so use the full Python path printed by
`python3 -c "import sys; print(sys.executable)"` (shown below as `PY`).

Hooks, in `~/.claude/settings.json`. The exec form (`command` plus `args`)
runs without a shell, so it behaves the same under bash and PowerShell and
copes with spaces in paths. No environment prefix is needed: importing
`logseq_memory_shared` switches HuggingFace offline (`HF_HUB_OFFLINE`,
`TRANSFORMERS_OFFLINE`, `HF_DATASETS_OFFLINE`) once the embedding model is in
the local cache, and the scripts locate their install from `bin/`:

```json
{
  "hooks": {
    "Stop": [
      {"matcher": "", "hooks": [{"type": "command", "command": "PY",
        "args": ["/Users/YOU/.logseq-memory/bin/logseq_memory_extractor.py"]}]}
    ],
    "UserPromptSubmit": [
      {"matcher": "", "hooks": [{"type": "command", "command": "PY",
        "args": ["/Users/YOU/.logseq-memory/bin/logseq_memory_retriever.py"],
        "timeout": 10}]}
    ]
  }
}
```

On Windows `PY` is `%USERPROFILE%\.logseq-memory\venv\Scripts\python.exe` and the
Desktop app's config is `%APPDATA%\Claude\claude_desktop_config.json`.

The Stop hook entry has no explicit `timeout`, so Claude Code's default applies;
the extractor itself gives `claude -p` up to 300 s. Transcripts under 200
characters are skipped. A session page is written even when no insight is
extracted, and the hook exits 1 if extraction fails.

MCP server (optional but recommended), under `"mcpServers"`. The reference
install registers the same entry in both `~/.claude/settings.json` and the
Desktop app's `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "logseq-vault": {
      "command": "PY",
      "args": ["/Users/YOU/.logseq-memory/bin/logseq_memory_mcp.py"],
      "env": {"TRANSFORMERS_OFFLINE": "1", "HF_DATASETS_OFFLINE": "1"}
    }
  }
}
```

The `TRANSFORMERS_OFFLINE` flags stop the embedding model from reaching
HuggingFace on startup; it is cached locally after the index build. Restart
Claude Desktop after editing.

Point the agent at the digest, e.g. in `~/.claude/CLAUDE.md`:

```markdown
At the start of each session, read `~/path/to/your/vault/pages/memory/digest.md`
for accumulated patterns, mistakes, decisions, and context from previous sessions.
```

The digest is regenerated by the Stop hook, the Codex worker and
`write_codex_insight`; mid-session `write_insight` calls do not refresh it.

### 5. Register with Codex

Copy `codex/hooks.json` to `~/.codex/hooks.json` and
`codex/skills/extract-memory/` to `~/.codex/skills/extract-memory/`. Both are
templates: replace `__LOGSEQ_PYTHON__` with your Python path and
`__LOGSEQ_BIN__` with `~/.logseq-memory/bin` (absolute). The hooks
are `SessionEnd` → `logseq_memory_codex.py enqueue` (3 s timeout; it parses and
queues the transcript synchronously, then detaches the worker) and
`UserPromptSubmit` → `logseq_memory_retriever.py` (10 s timeout,
`additionalContextLimit` 5000).

`codex_extraction_schema.json` is a hand-maintained copy of
`logseq_memory_shared.extraction_schema()` (plus `additionalProperties: false`
and `$defs`). Nothing keeps the two in sync automatically; update both together.

Enable lifecycle hooks in `~/.codex/config.toml`:

```toml
[features]
hooks = true
```

Restart Codex, then use `/hooks` to review and trust the hook definitions.
`SessionEnd` runs when an open task is archived or deleted, when Codex closes
normally, or after an unopened task has been idle for 30 minutes. Switching
tasks alone does not end the session.

Register the shared MCP server in `~/.codex/config.toml`. Use `logseq_vault`
(underscore) as the server name: the bundled skill calls `mcp__logseq_vault__*`
tools.

```toml
[mcp_servers.logseq_vault]
command = "PY"
args = ["/Users/YOU/.logseq-memory/bin/logseq_memory_mcp.py"]

[mcp_servers.logseq_vault.env]
TRANSFORMERS_OFFLINE = "1"
HF_DATASETS_OFFLINE = "1"
```

Restart Codex after editing the configuration.

### Migrating from the `pages/claude/` layout

Earlier versions wrote pages to `<vault>/pages/claude/` and kept scripts, index
and lock under `~/.claude/` (and a script copy in `~/.codex/hooks/`). Logseq
names pages by file name, not folder, so moving the folder keeps every page
name and link intact. With both agents closed:

```sh
mv ~/VGS/Notes/pages/claude ~/VGS/Notes/pages/memory
mkdir -p ~/.logseq-memory/bin
cp <repo>/logseq_memory_*.py <repo>/codex_extraction_schema.json ~/.logseq-memory/bin/
python3 ~/.logseq-memory/bin/logseq_memory_index.py
```

Then repoint the Claude and Codex hook and MCP registrations at
`~/.logseq-memory/bin/`, update any agent instructions that reference
`pages/claude/digest.md`, and remove the old copies from `~/.claude/hooks/` and
`~/.codex/hooks/`. The old `~/.claude/vault_retriever_disabled` flag is still
honored.

Codex pages use the existing `Session YYYY-MM-DD <id> — <project>` naming and
carry explicit provenance:

```text
creator:: [[codex]]
model:: [[gpt-5.6-sol]]
```

The Terra extraction worker is not included in `model::` unless it participated
in the original user-facing task. Codex pages are dated by the task's start
date, not the date the worker ran.

Before queuing, the adapter excludes system/developer instructions, injected
`AGENTS.md` context, reasoning, and tool calls/results, then applies best-effort
secret redaction to the transcript and the conversation title (which becomes
the session page's `description::`). Queue snapshots are written atomically with mode `0600`.
Session processing is idempotent across queued, running, processed, and failed
states. Completed and failed jobs retain metadata only, never transcript text. Files in
`failed/`, `processed/` and `logs/` are deleted after 7 days; `queue/` and
`running/` are not pruned, so a stuck job keeps its transcript until it is
processed. Transcripts under 200 characters are skipped, and a transcript with
no identifiable model exits 1.

Codex adapter settings (environment variables):

| Variable | Default | Purpose |
|----------|---------|---------|
| `LOGSEQ_PAGES_DIR` | `~/VGS/Notes/pages` | Vault `pages/` directory |
| `LOGSEQ_INDEX_PATH` | `~/.logseq-memory/vault_index.npz` | Shared semantic index |
| `CODEX_LOGSEQ_STATE_DIR` | `~/.codex/logseq-memory` | Queue, running, processed, failed and logs |
| `CODEX_LOGSEQ_EXTRACTION_MODEL` | `gpt-5.6-terra` | Model for the detached extraction worker |
| `CODEX_BIN` | `codex` on `PATH` | Codex CLI used by the worker |
| `CODEX_HOME` | `~/.codex` | Codex home used by `resolve-session` |
| `CODEX_LOGSEQ_SYNC` | unset | `1` processes the job inline instead of detaching (tests, debugging) |

Subcommands: `enqueue` (the hook), `process <job>` (the worker), and
`resolve-session [id]` (finds the transcript for a Codex thread, defaulting to
`CODEX_THREAD_ID`; used by the `$extract-memory` skill).
The detached Terra worker runs ephemerally with low reasoning effort, hooks and
user configuration disabled, a read-only sandbox, and a temporary working
directory.

## MCP tools

The shared server exposes the five existing Claude tools plus one Codex-specific
writer and, on macOS only, fires notifications (via `osascript`) on key events:

| Tool | Description | Notification |
|------|-------------|--------------|
| `search_vault(query, top_k, category)` | Semantic search with staleness labels (fresh/aging/stale/abandoned) | `🔍 search_vault — N match(es)` or no-match |
| `read_page(path)` | Read a file inside the Logseq graph (`pages/`, `journals/`, ...); other paths are refused | — |
| `write_insight(type, title, summary, detail, model, tags, project, force)` | Write with compose-time sanitization, post-write verification, semantic append, and a burst guard (see [Accumulation controls](#accumulation-controls)). `project` defaults to `VGS`; the page's `session::` is `Session <date> — manual` | `✍️ write_insight — <title>` (or `⏭️` if dedup/burst-held) |
| `list_recent(category, limit)` | Browse recently modified pages | — |
| `lint_vault(category, limit)` | Scan for Logseq format violations (phantom tags, broken backticks, bad properties) | `🔍 lint_vault — N files with issues` |
| `write_codex_insight(type, title, summary, detail, session, models, tags, project)` | Write a Codex insight with original-model provenance using the automatic Claude page contract | `✍️ write_codex_insight — <title>` (or `⏭️` if dedup skipped) |

## Usage

### Codex

With lifecycle hooks enabled, Codex automatically searches the shared vault
before handling each user prompt. The `UserPromptSubmit` retriever ignores
prompts shorter than 15 characters, embeds eligible prompts with
`all-MiniLM-L6-v2`, and injects up to five matches at or above 0.38 cosine
similarity into the next model context as a system reminder.

Retrieval silently produces no injected context when the index or embedding
dependencies are unavailable, the manual disable flag
`~/.logseq-memory/retriever_disabled` (or the legacy `~/.claude/vault_retriever_disabled`) exists, or no page clears the similarity
threshold. It never interrupts the task.

Codex can also use `search_vault` and `read_page` through MCP whenever the
topic changes or prior context may help. The `$extract-memory` skill searches
for each candidate insight before calling `write_codex_insight`; the shared
slug check and semantic append in `write_pages()` remain the final
race-safe guard.

`SessionEnd` does not query the vault. It filters and queues the completed task
transcript, then a detached worker extracts new insights and persists them with
Codex provenance.

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

These controls, mostly in `logseq_memory_shared.py`, keep the vault from growing unbounded:

**Extraction prompt** — instructs the extractor to only capture non-obvious, project-specific insights (max 5 per category per session). Empirically cuts the daily accumulation rate from ~330 to ~140 files/day.

**Extraction schema cap** — `extraction_schema()` declares `maxItems: 5` on each category array, enforced at the structured-output API level rather than left as a prompt instruction the model can silently exceed. Added after a single Stop hook extraction call produced 20 files in one category on an eventful session — the prompt asked for at most 5 but nothing stopped the model from returning more.

**write_pages hard truncate** — `write_pages()` additionally truncates each category's insight list to 5 items before writing, independent of the schema cap. No caller (Stop hook or `write_codex_insight`) can write more than 5 files per category in one call, regardless of what upstream extraction returns.

**write_insight burst guard** — the MCP server's `write_insight` tool counts how many pages it has already written to a category in this server process (one process per client session) and holds the write once that reaches 5 (`BURST_THRESHOLD`), suggesting the caller consolidate into one entry instead; pass `force: true` to write anyway. It counts per process rather than by `session::` marker because every `write_insight` page carries the same `Session <date> — manual` marker, which would pool unrelated sessions from the same day. Covers the interactive, one-insight-at-a-time path that the schema cap and hard truncate — both scoped to a single batch call — don't reach.

**Slug near-match dedup** — before writing a new file, every writer calls `find_slug_duplicate()` and skips the write if the category already has a file with the same slug or the same first three slug words (e.g. `kafka-rebalance-storm` blocks `kafka-rebalance-storm-recovery`, but not `kafka-rebalance-recovery`). Two words proved too broad: `access-logger-*` and `write-insight-*` silently blocked unrelated pages (`write_insight` moved to three words in July 2026, the batch writers on 2026-10-17).

**Semantic dedup (append, not skip)** — after the slug checks, every write path (Stop hook, Codex adapter, `write_insight`, `write_codex_insight`) embeds the new note's `title. summary` and compares it with `vault_index.npz`. If an existing page in the same category directory scores at or above `LOGSEQ_DEDUP_MIN_SCORE` (default `0.88`, cosine), no new file is written: the note is appended to that page as a `## Update <date>` block (summary, source session, detail) and `last-updated::` is bumped. `write_insight(force=true)` writes a separate page anyway. Fails open: no numpy, no sentence-transformers or no index means the slug checks alone apply.

The threshold was calibrated on 112 blind-labelled nearest-neighbour pairs from the live vault (2026-10-17). At ≥0.88 about 1 in 40 matches was a genuinely different insight (a correction) and ~60% were near-duplicates carrying one extra detail, which is why matches append rather than skip. Below ~0.86 different insights become common. Loose paraphrases often score 0.70–0.85, so this reduces but does not replace periodic consolidation. `write_insight` also appends its page to the index, so later writes in the same session see it. The calibration data is not stored in this repo.

## Vault health

Three features keep the vault healthy over time:

**Staleness tracking** — every newly created insight page and automatic session
page carries `last-updated::`, set to the creation date. Search results include a
staleness label: `fresh` (0–7 days), `aging` (8–14d), `stale` (15–30d), or
`abandoned (Nd)`. Existing pages are not backfilled; search falls back to
`date::` when the property is absent. `last-updated::` is refreshed when a
same-meaning note is appended to a page (semantic dedup). The property is not
part of semantic-index embedding input, which is derived from the page title and
summary. The digest lists the 15 newest entries per category ordered by
`date::`, so an appended update does not move a page up the digest.

**Compose-time sanitization** — `logseq_memory_shared.sanitize()` is the single implementation both write paths call before content ever reaches disk: `page_content()` (Stop hook auto-extraction and `write_codex_insight`, together the dominant source of vault growth) and `write_insight` (interactive mid-session writes, via a thin `_sanitize()` delegate in `logseq_memory_mcp.py`). It:
- Escapes bare `#digits` → `` `#3219` `` (prevents phantom Logseq tag pages) — matches the *whole* digit run, not just the first digit, and safely wraps a reference that's already inside a markdown link (`[#894](url)` → `[`#894`](url)`) without breaking the hyperlink
- Escapes `{{ }}` macros → backtick-wrapped (prevents broken Logseq macros)
- Post-write verification re-reads the file and checks for odd backtick counts, surviving bare `#digits`, unescaped macros, and single-colon properties

Before this was unified, `page_content()` didn't sanitize at all — every Stop hook extraction wrote titles/summaries/details raw, so any session whose insight happened to mention "PR #158" reintroduced the phantom-tag risk regardless of how many times the vault had been lint-cleaned. `write_insight`'s own sanitizer also had a `#(\d)` (single-digit-only) regex bug that broke multi-digit references (`#3219` → `` `#3`219 ``) instead of escaping them cleanly.

**Lint tool** — `lint_vault` scans all vault files (or a single category) for format violations:
- Odd backtick count (unclosed inline code)
- Bare `#digit` outside `tags::` lines (phantom tag pages)
- Unescaped `{{ }}` macros
- Single-colon properties (`key:` instead of `key::`)
- Missing required properties (`title::`, `type::`, `date::`)

Run periodically or after bulk imports to catch format issues before they create phantom pages in Logseq.

`limit` only caps how many findings are *returned* — the scan itself always covers every file in scope, so `scanned` and `findings_count` are always the true totals. (Earlier versions stopped scanning as soon as `limit` findings were hit, silently leaving the rest of the vault unchecked while still reporting a `scanned` count that looked complete.) A response with more findings than `limit` says so and tells you to raise it.

## Vault structure

```
pages/
└── memory/
    ├── index.md          ← Logseq query-driven master index (created once; later only `updated::` is rewritten, so your own notes below the queries are kept)
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
last-updated:: [[2026/04/21]]
project:: [[my-project]]
session:: [[Session 2026-04-21 abc12345 — my-project]]
creator:: [[claude]]
model:: [[claude-sonnet-4-6]]
tags:: [[python]], [[filesystem]]

- ## Summary
  - One-sentence description of the insight.

- ## Detail
  - Context sentence describing when this applies:
    - First step or key point
    - Second step or key point

- ## Update [[2026/05/02]]
  - Summary of a later same-meaning note (present only after a semantic append).
  - From [[Session 2026-05-02 def67890 — my-project]]
  - Detail of the later note.
```

Session pages for sessions whose title or summary matches the Dayflow keywords
are tagged and marked `exclude-from-graph-view:: true`.

## Insight categories

| Category | What gets captured |
|----------|--------------------|
| `pattern` | Reusable code approaches and techniques |
| `mistake` | Errors made and how they were corrected |
| `decision` | Architectural choices with reasoning |
| `context` | Project-specific terms, constraints, or facts |
| `session` | Per-session summary with links to all insights |

Only genuinely non-obvious items are written — the extraction prompt prefers empty arrays over low-quality filler.

Slug-prefix deduplication is checked before each page write (see
[Accumulation controls](#accumulation-controls)). For compatibility with the
original Claude implementation, an automatically generated session page still
lists the proposed insight title even when the corresponding page write was
skipped as a slug duplicate. When a note is instead appended to a same-meaning
page, the session page links to that existing page. This can produce a dangling
wikilink and is covered by regression tests; changing it requires an explicit
compatibility decision.

## Manual operations

**Trigger extraction mid-session** using the `/extract-memory` custom slash command.

Create `~/.claude/commands/extract-memory.md` with content that tells Claude to:
1. Review the current conversation directly
2. Call `mcp__logseq-vault__search_vault` before each candidate to check for duplicates
3. Call `mcp__logseq-vault__write_insight` only for insights not already in the vault

(Tool names follow the server name you registered: `logseq-vault` here, `logseq_vault` for Codex.)
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

## Existing vault migration

New pages write `creator::` and each ordered `model::` value as a Logseq page
link. The identifiers themselves are preserved: for example,
`claude-opus-4-6` becomes `[[claude-opus-4-6]]`, not a renamed display value.

To preview existing-page changes from a repository checkout:

```sh
python3 logseq_memory_migrate_links.py --pages-dir /absolute/path/to/vault/pages --dry-run
```

To make the migration with rollback copies of only changed files:

```sh
python3 logseq_memory_migrate_links.py --pages-dir /absolute/path/to/vault/pages --backup-dir /absolute/path/to/provenance-links-backup
```

The migration is idempotent. It changes only `creator::` and `model::` property
lines in Markdown files under `pages/memory/`; already linked values remain
unchanged.

**Rebuild the full index** (after vault consolidation or model change):

```sh
python3 ~/.logseq-memory/bin/logseq_memory_index.py
```

Once the model is cached (after the installer or the first build), every script
loads it offline automatically, so rebuilds work behind a proxy or sandbox that
blocks HuggingFace. Setting any of the three variables yourself (e.g.
`HF_HUB_OFFLINE=0` to force an online check) turns the automatic switch off.

**Rebuild quietly** (no progress bar, for cron jobs):

```sh
python3 ~/.logseq-memory/bin/logseq_memory_index.py --quiet
```

## Search index freshness

The MCP server checks the index file identity, size and nanosecond modification
and change times on each search. A changed index is reloaded under the shared
writer lock; unchanged searches reuse the loaded arrays and embedding model.
A missing or invalid index is retried on the next search without serving stale
results. Reconnect the MCP server once after installing updated server code;
subsequent index updates do not require a restart.

## Regression and compatibility tests

Claude's original implementation is the compatibility specification. Commit
`b630a63` is the frozen pre-refactor baseline used by the differential test.
Run the suite used by CI (89 tests; the 13 that need numpy are skipped
when it is not installed) with the command below. CI installs `numpy` and `mcp`
on Ubuntu, macOS and Windows (Python 3.11 and 3.13), plus one Ubuntu job
without them to keep the stdlib-only, fail-open path covered:

```sh
python -m unittest discover -s tests -v
```

or, without installing dependencies globally:

```sh
PYTHONPATH=. uv run --with pytest,mcp,numpy pytest tests/ -q
```

The differential runner compares the legacy and current implementations against
identical temporary vaults. It permits two intentional Claude-visible changes:
the renderer adds `last-updated::` immediately after `date::`, and wraps
`creator::` plus each ordered `model::` value in Logseq links on insight and
session pages. Its expected tree applies exactly those transformations to the
frozen baseline; all other bytes must remain identical. Prompt bytes, extraction
schema, `claude -p` invocation, transcript parsing, dedup decisions, digest and
index input, diagnostics, existing MCP schemas, and all other rendered bytes
remain protected.

When changing persistence behavior, add a failing regression test first and run
the complete suite on Python 3.11 and the deployed Python 3.13 runtime. A
Claude-visible mismatch beyond an explicitly authorized contract change blocks
deployment.

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
The subprocess environment also drops proxy variables and every `CLAUDE_CODE*`
variable, so `claude -p` authenticates as a fresh CLI invocation.

## Configuration reference

Paths come from `logseq_memory_shared.py` and can be overridden by environment:
`LOGSEQ_PAGES_DIR`, `LOGSEQ_MEMORY_HOME`, `LOGSEQ_INDEX_PATH`, `LOGSEQ_LOCK_PATH`
(see [Configure paths](#2-configure-paths)) and `LOGSEQ_DEDUP_MIN_SCORE`
(semantic-dedup threshold, default `0.88`). The remaining settings below are
module constants. Codex adapter variables are listed in the Codex section.

### logseq_memory_extractor.py

| Constant | Default | Description |
|----------|---------|-------------|
| `LOGSEQ_PAGES_DIR` | `shared.DEFAULT_PAGES_DIR` | Absolute path to your vault's `pages/` directory |
| `INDEX_PATH` | `shared.DEFAULT_INDEX_PATH` | Index used for incremental updates and semantic dedup |
| `CLAUDE_BIN` | `claude` on `PATH`, else `/Users/spiros/.local/bin/claude` | Path to the `claude` CLI binary |
| `MAX_TRANSCRIPT_CHARS` | `12000` | Transcript truncation limit (~3k tokens) |

### logseq_memory_index.py / logseq_memory_retriever.py

| Constant | Default | Description |
|----------|---------|-------------|
| `LOGSEQ_PAGES_DIR` | `shared.DEFAULT_PAGES_DIR` | Same vault path as the extractor (index only) |
| `INDEX_PATH` | `shared.DEFAULT_INDEX_PATH` | Where the compressed index is stored |
| `MODEL_NAME` | `all-MiniLM-L6-v2` | sentence-transformers model (384-dim, ~22 MB) |
| `TOP_K` | `5` | Max results returned per prompt (retriever) |
| `MIN_SCORE` | `0.38` | Cosine similarity floor — results below this are dropped |
| `MAX_FILE_CHARS` | `600` | Characters per result injected into context (retriever) |

### logseq_memory_mcp.py

| Constant | Default | Description |
|----------|---------|-------------|
| `LOGSEQ_PAGES_DIR` | `shared.DEFAULT_PAGES_DIR` | Same vault path as the other scripts |
| `INDEX_PATH` | `shared.DEFAULT_INDEX_PATH` | Shared index file |
| `MODEL_NAME` | `all-MiniLM-L6-v2` | Same model as the retriever |
| `MIN_SCORE` | `0.38` | Cosine similarity floor for `search_vault` |
| `MAX_FILE_CHARS` | `1500` | Characters per search result (larger than retriever — not auto-injected) |
| `STALENESS_DAYS` | `{"fresh": 7, "aging": 14, "stale": 30}` | Day thresholds for staleness labels in search results |
| `BURST_THRESHOLD` | `5` | Pages per category per server process before `write_insight` holds |

### logseq_memory_shared.py

| Constant | Default | Description |
|----------|---------|-------------|
| `MAX_ITEMS_PER_CATEGORY` | `5` | Schema `maxItems` and hard truncate per category per batch |
| `SEMANTIC_DEDUP_THRESHOLD` | `0.88` (env `LOGSEQ_DEDUP_MIN_SCORE`) | Cosine score at which a note is appended to an existing page |

## License

MIT
