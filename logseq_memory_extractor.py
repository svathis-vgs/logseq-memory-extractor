#!/usr/bin/env python3
"""
Claude Code memory extractor — writes structured insights to a Logseq vault.
Configured as a Stop hook: fires when a Claude Code session ends.

Uses the local `claude` CLI (already authenticated via Claude Code Desktop) —
no API key or extra dependencies required.
"""

import json
import os
import re
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────
LOGSEQ_PAGES_DIR = Path("~/VGS/Notes/pages").expanduser()
CLAUDE_BIN = shutil.which("claude") or "/Users/spiros/.local/bin/claude"
MAX_TRANSCRIPT_CHARS = 12_000  # ~3k tokens — keeps prompts fast via the claude CLI

EXTRACTION_PROMPT = """\
Analyze this Claude Code session transcript and extract reusable insights.
Return ONLY valid JSON with this exact structure — no prose, no markdown fences:

{
  "patterns": [{"slug": "kebab-case-name", "summary": "one sentence", "detail": "reusable code approach or technique", "tags": ["tag1"]}],
  "mistakes": [{"slug": "kebab-case-name", "summary": "one sentence", "detail": "what went wrong and how it was corrected", "tags": ["tag1"]}],
  "decisions": [{"slug": "kebab-case-name", "summary": "one sentence", "detail": "the decision made and the reasoning behind it", "tags": ["tag1"]}],
  "context": [{"slug": "kebab-case-name", "summary": "one sentence", "detail": "project-specific term, constraint, or fact", "tags": ["tag1"]}],
  "session_summary": "2-3 sentence overview of what was accomplished"
}

Rules:
- Only include items that are genuinely reusable or important across sessions
- Slugs must be lowercase kebab-case, max 6 words, no special characters
- If a category has nothing worth capturing, use an empty array []
- Avoid filler — empty arrays beat low-quality entries
- For the detail field: when describing steps, actions, or any list of items,
  put each item on its own line using Logseq outline format. Start the first
  line with the context/intro sentence, then each item as "\\n    - item text".
  Example: "When X happens:\\n    - First do Y\\n    - Then check Z\\n    - Finally verify W"
  For a single-paragraph explanation with no list, a plain string is fine.

Transcript:
"""

# ── Transcript ─────────────────────────────────────────────────────────────────

def find_transcript(transcript_path: str | None, project_dir: str, session_id: str) -> str:
    # Prefer explicit path from Stop hook payload
    if transcript_path and Path(transcript_path).exists():
        return Path(transcript_path).read_text(errors="replace")

    # Fall back: search ~/.claude/projects/ for a JSONL matching session_id
    projects_dir = Path("~/.claude/projects").expanduser()
    if not projects_dir.exists():
        return ""

    for jsonl in projects_dir.rglob("*.jsonl"):
        if session_id and session_id in jsonl.stem:
            content = jsonl.read_text(errors="replace")
            if content.strip():
                return content

    # Last resort: newest JSONL under the hashed project dir
    safe = project_dir.replace("/", "-").lstrip("-")
    for d in sorted(projects_dir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if d.is_dir():
            for jsonl in sorted(d.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True):
                content = jsonl.read_text(errors="replace")
                if content.strip():
                    return content

    return ""


def extract_conversation_title(raw: str) -> str:
    """Return the first user message content from the JSONL transcript,
    truncated to 120 chars — this is what Claude Code shows as the session title."""
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        msg = entry.get("message", {})
        if not isinstance(msg, dict):
            continue
        if msg.get("role") != "user":
            continue
        content = msg.get("content", "")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    content = block["text"]
                    break
            else:
                continue
        if isinstance(content, str) and content.strip():
            text = content.strip().replace("\n", " ")
            return text[:120] + ("…" if len(text) > 120 else "")
    return ""


def parse_transcript(raw: str) -> str:
    """Convert JSONL lines to readable text, keeping last MAX_TRANSCRIPT_CHARS chars."""
    lines = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue

        role = entry.get("type") or entry.get("role", "")
        msg = entry.get("message", {})
        content = msg.get("content", "") if isinstance(msg, dict) else ""

        if not content:
            continue

        prefix = "User" if role in ("user", "human") else "Assistant" if role in ("assistant",) else None
        if prefix is None:
            continue

        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    lines.append(f"{prefix}: {block['text']}")
        elif isinstance(content, str):
            lines.append(f"{prefix}: {content}")

    text = "\n".join(lines)
    return text[-MAX_TRANSCRIPT_CHARS:] if len(text) > MAX_TRANSCRIPT_CHARS else text


# ── Claude CLI ─────────────────────────────────────────────────────────────────

def call_claude(transcript_text: str) -> dict:
    """Call the local `claude` CLI via stdin, using the Desktop app's auth.

    Sets LOGSEQ_EXTRACTOR_RUNNING=1 so the Stop hook this subprocess fires on
    exit immediately no-ops — breaking what would otherwise be infinite recursion.
    """
    prompt = EXTRACTION_PROMPT + transcript_text

    # Inherit the full environment so auth (CLAUDE_CODE_OAUTH_TOKEN) is available,
    # then set the recursion guard and strip socks5h proxy vars that the claude
    # CLI doesn't support (they cause exit code 1).
    env = dict(os.environ)
    env["LOGSEQ_EXTRACTOR_RUNNING"] = "1"
    for key in ("ALL_PROXY", "all_proxy", "FTP_PROXY", "ftp_proxy", "GRPC_PROXY", "grpc_proxy"):
        env.pop(key, None)

    result = subprocess.run(
        [CLAUDE_BIN, "-p"],
        input=prompt,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,  # capture to surface errors; large output is stdout-only so no deadlock
        env=env,
        text=True,
        timeout=300,
    )

    if result.returncode != 0:
        detail = (result.stderr or "").strip()[:300]
        raise RuntimeError(f"claude CLI exited with code {result.returncode}" + (f": {detail}" if detail else ""))

    raw = result.stdout.strip()
    # Strip markdown code fences if model wraps output in them
    raw = re.sub(r"^```(?:json)?\n?", "", raw)
    raw = re.sub(r"\n?```$", "", raw)
    # Strip null bytes and BOM that trip up json.loads
    raw = raw.replace("\x00", "").replace("﻿", "").strip()
    if not raw:
        detail = (result.stderr or "").strip()[:300]
        raise RuntimeError("claude CLI returned empty output" + (f" — stderr: {detail}" if detail else ""))
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"claude CLI returned non-JSON (char {e.pos}): {raw[:120]!r}") from e
    if not isinstance(parsed, dict):
        # Model returned a list or other non-dict; treat as empty session
        return {"patterns": [], "mistakes": [], "decisions": [], "context": [], "session_summary": ""}
    return parsed


# ── Helpers ───────────────────────────────────────────────────────────────────

def logseq_date() -> str:
    """Return today as a Logseq journal link: [[yyyy/MM/dd]]"""
    return "[[" + date.today().strftime("%Y/%m/%d") + "]]"


# ── Logseq page writers ────────────────────────────────────────────────────────

def _page_content(type_: str, title: str, summary: str, detail: str, tags: list,
                  project: str, session_title: str) -> str:
    today = logseq_date()
    tag_str = " ".join(f"[[{t}]]" for t in tags) if tags else ""
    lines = [
        f"title:: {title}",
        f"type:: {type_}",
        f"date:: {today}",
        f"project:: [[{project}]]",
        f"session:: [[{session_title}]]",
    ]
    if tag_str:
        lines.append(f"tags:: {tag_str}")
    # Render detail: first line gets the bullet prefix; subsequent lines (sub-bullets) pass through
    detail_lines = detail.split("\n")
    detail_block = [f"  - {detail_lines[0]}"] + detail_lines[1:]

    lines += [
        "",
        "- ## Summary",
        f"  - {summary}",
        "",
        "- ## Detail",
    ] + detail_block + [""]
    return "\n".join(lines)


def write_pages(insights: dict, project_name: str, session_id: str, session_slug: str, session_title: str) -> list[str]:
    """Write one page per insight. Returns list of [[namespace/links]]."""
    written: list[str] = []
    type_map = {
        "patterns": "[[pattern]]",
        "mistakes": "[[mistake]]",
        "decisions": "[[decision]]",
        "context": "[[context]]",
    }

    for key, type_name in type_map.items():
        items = insights.get(key, [])
        if not isinstance(items, list):
            continue
        subdir = LOGSEQ_PAGES_DIR / "claude" / key
        subdir.mkdir(parents=True, exist_ok=True)

        # category prefix for filename ensures global uniqueness across subdirs
        # e.g. patterns/pattern-use-pathlib.md, decisions/decision-use-postgres.md
        category = key.rstrip("s")  # "patterns" → "pattern", "context" stays "context"

        for item in items:
            if not isinstance(item, dict):
                continue
            slug = re.sub(r"[^a-z0-9-]+", "-", item.get("slug", "untitled").lower()).strip("-")
            filename = f"{category}-{slug}"
            title = f"{category.title()}: {slug.replace('-', ' ').title()}"
            content = _page_content(
                type_=type_name,
                title=title,
                summary=item.get("summary", ""),
                detail=item.get("detail", ""),
                tags=item.get("tags", []),
                project=project_name,
                session_title=session_title,
            )
            dest = subdir / f"{filename}.md"
            if not dest.exists():
                dest.write_text(content.lstrip("\n"))
            written.append(f"[[{title}]]")

    return written


_DAYFLOW_KEYWORDS = (
    "screenshot", "screen recording", "activity log",
    "timeline cards", "dayflow", "screen capture",
)


def _is_dayflow_session(conversation_title: str, session_summary: str) -> bool:
    """Return True if this session looks like a Dayflow screen-recording analysis."""
    haystack = (conversation_title + " " + session_summary).lower()
    return any(kw in haystack for kw in _DAYFLOW_KEYWORDS)


def write_session(insights: dict, project_name: str,
                  session_id: str, session_slug: str, written_links: list[str],
                  conversation_title: str = "") -> None:
    today = logseq_date()
    date_folder = date.today().strftime("%Y_%m_%d")
    sessions_dir = LOGSEQ_PAGES_DIR / "claude" / "sessions" / date_folder
    sessions_dir.mkdir(parents=True, exist_ok=True)

    session_date = today.strip("[]").replace("/", "-")  # [[2026/04/21]] → 2026-04-21
    session_title = f"Session {session_date} {session_id} — {project_name}"
    description = conversation_title or session_title
    session_summary = insights.get("session_summary", "No summary generated.")

    lines = [
        f"title:: {session_title}",
        f"description:: {description}",
        "type:: [[session]]",
        f"date:: {today}",
        f"project:: [[{project_name}]]",
        f"session:: [[{session_title}]]",
        "exclude-from-graph-view:: true",
    ]
    if _is_dayflow_session(conversation_title, session_summary):
        lines.append("tags:: [[dayflow]]")
    lines += [
        "",
        "- ## Summary",
        f"  - {session_summary}",
        "",
        "- ## Insights",
    ] + [f"  - {link}" for link in written_links] + [""]

    (sessions_dir / f"{session_slug}.md").write_text("\n".join(lines).lstrip("\n"))


def write_digest() -> None:
    """Regenerate a plain-text digest of all insights so Claude can read it at session start."""
    today = date.today().isoformat()
    categories = ["patterns", "mistakes", "decisions", "context"]

    all_insights: list[tuple[str, str, str, str]] = []

    for cat in categories:
        subdir = LOGSEQ_PAGES_DIR / "claude" / cat
        if not subdir.exists():
            continue
        for f in subdir.glob("*.md"):
            try:
                text = f.read_text(errors="replace")
            except OSError:
                continue
            lines = text.splitlines()
            meta: dict[str, str] = {}
            for line in lines:
                if "::" in line and not line.startswith("-"):
                    k, _, v = line.partition("::")
                    meta[k.strip()] = v.strip()

            summary = ""
            in_summary = False
            for line in lines:
                stripped = line.strip()
                if stripped == "- ## Summary":
                    in_summary = True
                    continue
                if in_summary:
                    if stripped.startswith("- ##"):
                        break
                    if stripped.startswith("- "):
                        summary = stripped[2:].strip()
                        break

            date_str = meta.get("date", "").strip("[[]]").replace("/", "-")
            title = meta.get("title", f.stem)
            all_insights.append((date_str, cat, title, summary))

    all_insights.sort(key=lambda x: x[0], reverse=True)

    by_cat: dict[str, list[tuple[str, str, str]]] = {c: [] for c in categories}
    for date_str, cat, title, summary in all_insights:
        by_cat[cat].append((date_str, title, summary))

    totals = {c: len(by_cat[c]) for c in categories}

    out = [
        "# Claude Code Memory Digest",
        f"_Updated: {today} — "
        f"{totals['patterns']} patterns, {totals['mistakes']} mistakes, "
        f"{totals['decisions']} decisions, {totals['context']} context_",
        "",
        "Read this at session start to recall accumulated insights.",
        "",
    ]

    MAX_PER_CAT = 15
    prefixes = ("Pattern: ", "Mistake: ", "Decision: ", "Context: ")
    for cat in categories:
        items = by_cat[cat][:MAX_PER_CAT]
        if not items:
            continue
        out.append(f"## {cat.title()}")
        for date_str, title, summary in items:
            short = title
            for p in prefixes:
                if title.startswith(p):
                    short = title[len(p):]
                    break
            out.append(f"- **{short}** ({date_str}) — {summary}")
        out.append("")

    digest_path = LOGSEQ_PAGES_DIR / "claude" / "digest.md"
    digest_path.write_text("\n".join(out))


def update_index() -> None:
    """Keep the index fresh by updating its updated:: date. Sessions and
    insights are discovered automatically via Logseq queries — no manual
    listing needed."""
    today = logseq_date()
    index_path = LOGSEQ_PAGES_DIR / "claude" / "index.md"
    index_path.parent.mkdir(parents=True, exist_ok=True)

    if not index_path.exists():
        index_path.write_text("\n".join([
            f"updated:: {today}",
            "",
            "- # Claude Code Memory Index",
            "  - Auto-generated. Add your own notes below the query sections.",
            "",
            "- ## Sessions",
            "  - {{query (property type [[session]])}}",
            "    query-table:: true",
            "    query-sort-by:: date",
            "    query-sort-desc:: true",
            "    query-properties:: [:title :date :project]",
            "",
            "- ## Patterns",
            "  collapsed:: true",
            "  - {{query (property type [[pattern]])}}",
            "    query-table:: true",
            "    query-sort-by:: date",
            "    query-sort-desc:: true",
            "    query-properties:: [:title :date :project :tags]",
            "",
            "- ## Mistakes",
            "  collapsed:: true",
            "  - {{query (property type [[mistake]])}}",
            "    query-table:: true",
            "    query-sort-by:: date",
            "    query-sort-desc:: true",
            "    query-properties:: [:title :date :project :tags]",
            "",
            "- ## Decisions",
            "  collapsed:: true",
            "  - {{query (property type [[decision]])}}",
            "    query-table:: true",
            "    query-sort-by:: date",
            "    query-sort-desc:: true",
            "    query-properties:: [:title :date :project :tags]",
            "",
            "- ## Context",
            "  collapsed:: true",
            "  - {{query (property type [[context]])}}",
            "    query-table:: true",
            "    query-sort-by:: date",
            "    query-sort-desc:: true",
            "    query-properties:: [:title :date :project :tags]",
            "",
        ]))
    else:
        existing = index_path.read_text()
        updated = re.sub(r'^updated::.*$', f"updated:: {today}", existing, count=1, flags=re.MULTILINE)
        index_path.write_text(updated)


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    # Break recursion: the claude -p subprocess we spawn also fires the Stop hook.
    # When that happens, LOGSEQ_EXTRACTOR_RUNNING is set in its environment — exit immediately.
    if os.environ.get("LOGSEQ_EXTRACTOR_RUNNING"):
        sys.exit(0)

    payload = json.loads(sys.stdin.read() or "{}")
    session_id = payload.get("session_id", "unknown")
    project_dir = payload.get("cwd") or payload.get("project_dir") or os.getcwd()
    transcript_path = payload.get("transcript_path")

    transcript_raw = find_transcript(transcript_path, project_dir, session_id)
    if not transcript_raw.strip():
        sys.exit(0)

    conversation_title = extract_conversation_title(transcript_raw)
    transcript_text = parse_transcript(transcript_raw)
    if len(transcript_text) < 200:
        sys.exit(0)  # Session too short to be worth extracting

    try:
        insights = call_claude(transcript_text)
    except Exception as e:
        print(f"[logseq-memory] extraction failed: {e}", file=sys.stderr)
        sys.exit(1)

    project_name = Path(project_dir).name or "unknown"
    today = date.today().isoformat()
    session_short = (session_id or "unknown")[:8]
    session_slug = f"{today}-{session_short}"
    session_title = f"Session {today} {session_short} — {project_name}"

    written_links = write_pages(insights, project_name, session_short, session_slug, session_title)
    write_session(insights, project_name, session_short, session_slug, written_links, conversation_title)
    update_index()
    write_digest()

    print(
        f"[logseq-memory] {len(written_links)} insight(s) → {LOGSEQ_PAGES_DIR}/claude/",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
