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

    # Pass a minimal clean env: PATH + HOME + OAuth token + recursion guard
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "LOGSEQ_EXTRACTOR_RUNNING": "1",
    }
    oauth = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    if oauth:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = oauth

    result = subprocess.run(
        [CLAUDE_BIN, "-p"],
        input=prompt,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,  # discard progress output to avoid pipe buffer deadlock
        env=env,
        text=True,
        timeout=120,
    )

    if result.returncode != 0:
        raise RuntimeError(f"claude CLI exited with code {result.returncode}")

    raw = result.stdout.strip()
    # Strip markdown code fences if model wraps output in them
    raw = re.sub(r"^```(?:json)?\n?", "", raw)
    raw = re.sub(r"\n?```$", "", raw)
    return json.loads(raw)


# ── Helpers ───────────────────────────────────────────────────────────────────

def logseq_date() -> str:
    """Return today as a Logseq journal link: [[yyyy/MM/dd]]"""
    return "[[" + date.today().strftime("%Y/%m/%d") + "]]"


# ── Logseq page writers ────────────────────────────────────────────────────────

def _page_content(type_: str, title: str, summary: str, detail: str, tags: list,
                  project: str, session_id: str, session_title: str) -> str:
    today = logseq_date()
    tag_str = " ".join(f"[[{t}]]" for t in tags) if tags else ""
    lines = [
        f"title:: {title}",
        f"type:: {type_}",
        f"date:: {today}",
        f"project:: [[{project}]]",
        f"session-id:: {session_id}",
    ]
    if tag_str:
        lines.append(f"tags:: {tag_str}")
    lines += [
        "",
        "- ## Summary",
        f"  - {summary}",
        "",
        "- ## Detail",
        f"  - {detail}",
        "",
        "- ## Session",
        f"  - [[{session_title}]]",
        "",
    ]
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
                session_id=session_id,
                session_title=session_title,
            )
            (subdir / f"{filename}.md").write_text(content.lstrip("\n"))
            written.append(f"[[{filename}]]")

    return written


def write_session(insights: dict, project_name: str,
                  session_id: str, session_slug: str, written_links: list[str]) -> None:
    today = logseq_date()
    sessions_dir = LOGSEQ_PAGES_DIR / "claude" / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)

    session_date = today.strip("[]").replace("/", "-")  # [[2026/04/21]] → 2026-04-21
    lines = [
        f"title:: Session {session_date} {session_id} — {project_name}",
        "type:: [[session]]",
        f"date:: {today}",
        f"project:: [[{project_name}]]",
        f"session-id:: {session_id}",
        "",
        "- ## Summary",
        f"  - {insights.get('session_summary', 'No summary generated.')}",
        "",
        "- ## Insights",
    ] + [f"  - {link}" for link in written_links] + [""]

    (sessions_dir / f"{session_slug}.md").write_text("\n".join(lines).lstrip("\n"))


def update_index(written_links: list[str], session_title: str, project_name: str) -> None:
    """Append a new session block to claude/index.md."""
    today = logseq_date()
    index_path = LOGSEQ_PAGES_DIR / "claude" / "index.md"
    index_path.parent.mkdir(parents=True, exist_ok=True)

    entry = "\n".join([
        f"- ## {today} — {project_name}",
        f"  - [[{session_title}]]",
    ] + [f"  - {link}" for link in written_links] + [""]) + "\n"

    if not index_path.exists():
        header = "\n".join([
            f"updated:: {today}",
            "",
            "- # Claude Code Memory Index",
            "  - Auto-generated index. Add your own notes below.",
            "",
            "- ## Query: all patterns",
            "  - {{query (property type pattern)}}",
            "",
            "- ## Query: all mistakes",
            "  - {{query (property type mistake)}}",
            "",
            "- ## Query: all decisions",
            "  - {{query (property type decision)}}",
            "",
            "- ## Sessions",
            "",
        ]) + "\n"
        index_path.write_text(header + entry)
    else:
        existing = index_path.read_text()
        index_path.write_text(existing + entry)


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
    write_session(insights, project_name, session_short, session_slug, written_links)
    update_index(written_links, session_title, project_name)

    print(
        f"[logseq-memory] {len(written_links)} insight(s) → {LOGSEQ_PAGES_DIR}/claude/",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
