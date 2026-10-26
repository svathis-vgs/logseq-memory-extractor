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

import logseq_memory_shared as shared  # also switches HF offline when the model is cached

# ── Config ────────────────────────────────────────────────────────────────────
LOGSEQ_PAGES_DIR = shared.DEFAULT_PAGES_DIR
INDEX_PATH = shared.DEFAULT_INDEX_PATH
CLAUDE_BIN = shared.find_cli("claude")
MAX_TRANSCRIPT_CHARS = 12_000  # ~3k tokens — keeps prompts fast via the claude CLI

EXTRACTION_PROMPT = shared.extraction_prompt("Claude Code")

# ── Transcript ─────────────────────────────────────────────────────────────────

def find_transcript(transcript_path: str | None, project_dir: str, session_id: str) -> str:
    # Prefer explicit path from Stop hook payload
    if transcript_path and Path(transcript_path).exists():
        return Path(transcript_path).read_text(errors="replace", encoding="utf-8")

    # Fall back: search ~/.claude/projects/ for a JSONL matching session_id
    projects_dir = Path("~/.claude/projects").expanduser()
    if not projects_dir.exists():
        return ""

    for jsonl in projects_dir.rglob("*.jsonl"):
        if session_id and session_id in jsonl.stem:
            content = jsonl.read_text(errors="replace", encoding="utf-8")
            if content.strip():
                return content

    # Last resort: newest JSONL under the hashed project dir
    safe = project_dir.replace("/", "-").lstrip("-")
    for d in sorted(projects_dir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if d.is_dir():
            for jsonl in sorted(d.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True):
                content = jsonl.read_text(errors="replace", encoding="utf-8")
                if content.strip():
                    return content

    return ""


def extract_conversation_title(raw: str) -> str:
    """Return the first user message content from the JSONL transcript,
    truncated to 120 chars — this is what Claude Code shows as the session title."""
    return shared.extract_conversation_title(raw)


def extract_models(raw: str) -> list[str]:
    """Return the distinct assistant model names/versions used in this session,
    in first-seen order, from the JSONL transcript's message.model field."""
    return shared.extract_models(raw)


def parse_transcript(raw: str) -> str:
    """Convert JSONL lines to readable text, keeping last MAX_TRANSCRIPT_CHARS chars."""
    return shared.parse_transcript(raw, MAX_TRANSCRIPT_CHARS)


# ── Claude CLI ─────────────────────────────────────────────────────────────────

def call_claude(transcript_text: str) -> dict:
    """Call the local `claude` CLI via stdin, using the Desktop app's auth.

    Sets LOGSEQ_EXTRACTOR_RUNNING=1 so the Stop hook this subprocess fires on
    exit immediately no-ops — breaking what would otherwise be infinite recursion.
    """
    prompt = EXTRACTION_PROMPT + transcript_text

    # Inherit the environment, set the recursion guard, and strip ALL proxy vars
    # that interfere with the claude CLI's API calls (Teleport socks proxies,
    # HTTP proxies, etc.) plus every CLAUDE_CODE* variable, so `claude -p` runs
    # as a fresh CLI invocation using the Desktop login.
    env = dict(os.environ)
    env["LOGSEQ_EXTRACTOR_RUNNING"] = "1"
    for key in list(env):
        if key.lower().replace("_", "") in (
            "allproxy", "httpproxy", "httpsproxy", "ftpproxy", "grpcproxy",
            "rsyncproxy", "dockerhttpproxy", "dockerhttpsproxy",
        ) or key.startswith("CLOUDSDK_PROXY"):
            env.pop(key, None)
    for key in list(env):
        if key.startswith("CLAUDE_CODE") or key == "CLAUDECODE":
            env.pop(key, None)

    # JSON Schema enforces structured output at the API level — model cannot return prose.
    schema = json.dumps(shared.extraction_schema())

    result = subprocess.run(
        [CLAUDE_BIN, "-p", "--no-session-persistence", "--output-format", "json", "--json-schema", schema],
        input=prompt,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,  # capture to surface errors; large output is stdout-only so no deadlock
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )

    if result.returncode != 0:
        # With --output-format json the CLI reports errors (e.g. an expired
        # login) in stdout, not stderr; keep both.
        detail = " | ".join(
            part for part in ((result.stderr or "").strip()[:300], _cli_error_text(result.stdout))
            if part
        )
        raise RuntimeError(f"claude CLI exited with code {result.returncode}" + (f": {detail}" if detail else ""))

    raw = result.stdout.strip()
    # Strip null bytes and BOM that trip up json.loads
    raw = raw.replace("\x00", "").replace("﻿", "").strip()
    if not raw:
        detail = (result.stderr or "").strip()[:300]
        raise RuntimeError("claude CLI returned empty output" + (f" — stderr: {detail}" if detail else ""))

    return shared.parse_extraction_output(raw)


def _cli_error_text(stdout: str | None) -> str:
    """The human-readable part of a failed `claude -p --output-format json` run.

    The CLI reports failures as a JSON result object whose message sits in
    "result" (or "error"/"message"); fall back to the raw text.
    """
    text = (stdout or "").strip()
    try:
        data = json.loads(text)
    except ValueError:
        return text[:500]
    if isinstance(data, dict):
        for key in ("result", "error", "message"):
            value = data.get(key)
            if value:
                return str(value)[:500]
    return text[:500]


# ── Helpers ───────────────────────────────────────────────────────────────────

def logseq_date() -> str:
    """Return today as a Logseq journal link: [[yyyy/MM/dd]]"""
    return shared.logseq_date(date.today())


# ── Logseq page writers ────────────────────────────────────────────────────────

def _page_content(type_: str, title: str, summary: str, detail: str, tags: list,
                  project: str, session_title: str, models: list[str] | None = None,
                  creator: str = "claude") -> str:
    return shared.page_content(
        type_,
        title,
        summary,
        detail,
        tags,
        project,
        session_title,
        models,
        creator=creator,
        today=date.today(),
    )


def _index_extract_text(path: Path) -> str:
    """Extract title + summary for embedding; same rule as logseq_memory_index.extract_text (a separate copy, keep both in sync)."""
    return shared.index_extract_text(path)


def _update_vault_index(new_paths: list[Path]) -> None:
    """Incrementally append newly written insight pages to the semantic index.
    No-op if the index hasn't been built yet or if sentence-transformers is absent."""
    shared.update_vault_index(new_paths, INDEX_PATH)


def write_pages(insights: dict, project_name: str, session_id: str, session_slug: str, session_title: str,
                models: list[str] | None = None, creator: str = "claude") -> list[str]:
    """Write one page per insight. Returns list of [[namespace/links]]."""
    return shared.write_pages(
        insights,
        project_name,
        session_id,
        session_slug,
        session_title,
        models,
        pages_dir=LOGSEQ_PAGES_DIR,
        update_index_fn=_update_vault_index,
        creator=creator,
        today=date.today(),
        index_path=INDEX_PATH,
    )


_DAYFLOW_KEYWORDS = shared.DAYFLOW_KEYWORDS


def _is_dayflow_session(conversation_title: str, session_summary: str) -> bool:
    """Return True if this session looks like a Dayflow screen-recording analysis."""
    return shared.is_dayflow_session(conversation_title, session_summary)


def write_session(insights: dict, project_name: str,
                  session_id: str, session_slug: str, written_links: list[str],
                  conversation_title: str = "", models: list[str] | None = None,
                  creator: str = "claude") -> None:
    shared.write_session(
        insights,
        project_name,
        session_id,
        session_slug,
        written_links,
        conversation_title,
        models,
        pages_dir=LOGSEQ_PAGES_DIR,
        creator=creator,
        today=date.today(),
    )


def write_digest() -> None:
    """Regenerate a plain-text digest of all insights so Claude can read it at session start."""
    shared.write_digest(LOGSEQ_PAGES_DIR, date.today())


def update_index() -> None:
    """Keep the index fresh by updating its updated:: date. Sessions and
    insights are discovered automatically via Logseq queries — no manual
    listing needed."""
    shared.update_index(LOGSEQ_PAGES_DIR, date.today())


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    # Break recursion: the claude -p subprocess we spawn also fires the Stop hook.
    # When that happens, LOGSEQ_EXTRACTOR_RUNNING is set in its environment — exit immediately.
    if os.environ.get("LOGSEQ_EXTRACTOR_RUNNING"):
        sys.exit(0)
    try:
        _run()
    except SystemExit:
        raise
    except BaseException as exc:  # hook stderr is discarded: record it, then fail
        import traceback

        where = traceback.extract_tb(exc.__traceback__)[-1]
        shared.log_run("claude", "error",
                       f"{type(exc).__name__}: {exc} (at {Path(where.filename).name}:{where.lineno})")
        raise


def _run() -> None:
    payload = json.loads(sys.stdin.read() or "{}")
    session_id = payload.get("session_id", "unknown")
    project_dir = payload.get("cwd") or payload.get("project_dir") or os.getcwd()
    transcript_path = payload.get("transcript_path")

    transcript_raw = find_transcript(transcript_path, project_dir, session_id)
    if not transcript_raw.strip():
        sys.exit(0)

    conversation_title = extract_conversation_title(transcript_raw)
    models = extract_models(transcript_raw)
    transcript_text = parse_transcript(transcript_raw)
    if len(transcript_text) < 200:
        sys.exit(0)  # Session too short to be worth extracting

    try:
        insights = call_claude(transcript_text)
    except Exception as e:
        print(f"[logseq-memory] extraction failed: {e}", file=sys.stderr)
        shared.log_run("claude", "failed", f"session={session_id[:8]} {e}")
        sys.exit(1)

    project_name = Path(project_dir).name or "unknown"
    today = date.today().isoformat()
    session_short = (session_id or "unknown")[:8]
    session_slug = f"{today}-{session_short}"
    session_title = f"Session {today} {session_short} — {project_name}"

    with shared.vault_lock():
        written_links = write_pages(
            insights,
            project_name,
            session_short,
            session_slug,
            session_title,
            models,
        )
        write_session(
            insights,
            project_name,
            session_short,
            session_slug,
            written_links,
            conversation_title,
            models,
        )
        update_index()
        write_digest()

    print(
        f"[logseq-memory] {len(written_links)} insight(s) → {shared.memory_root(LOGSEQ_PAGES_DIR)}/",
        file=sys.stderr,
    )
    shared.log_run("claude", "ok", f"session={session_short} project={project_name} insights={len(written_links)}")


if __name__ == "__main__":
    main()
