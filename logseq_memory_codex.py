#!/usr/bin/env python3
"""Codex SessionEnd dispatcher, worker, and current-session resolver."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import logseq_memory_shared as shared


EXTRACTION_MODEL = os.environ.get("CODEX_LOGSEQ_EXTRACTION_MODEL", "gpt-5.6-terra")
CODEX_BIN = (
    os.environ.get("CODEX_BIN")
    or shutil.which("codex")
    or "/Applications/ChatGPT.app/Contents/Resources/codex"
)
SCHEMA_PATH = Path(__file__).with_name("codex_extraction_schema.json")
FAILURE_RETENTION_DAYS = 7


@dataclass(frozen=True)
class ParsedTranscript:
    title: str
    text: str
    models: tuple[str, ...]
    started_on: date
    cwd: str


def pages_dir() -> Path:
    return Path(
        os.environ.get("LOGSEQ_PAGES_DIR", "~/VGS/Notes/pages")
    ).expanduser()


def index_path() -> Path:
    return Path(
        os.environ.get("LOGSEQ_INDEX_PATH", "~/.claude/vault_index.npz")
    ).expanduser()


def state_dir() -> Path:
    return Path(
        os.environ.get("CODEX_LOGSEQ_STATE_DIR", "~/.codex/logseq-memory")
    ).expanduser()


def _ordered_add(values: list[str], candidate: object) -> None:
    if isinstance(candidate, str):
        value = candidate.strip()
        if value and value != "<synthetic>" and value not in values:
            values.append(value)


def _is_injected_context(text: str) -> bool:
    stripped = text.lstrip()
    return (
        stripped.startswith("# AGENTS.md instructions")
        or stripped.startswith("<environment_context>")
        or stripped.startswith("<app-context>")
        or stripped.startswith("<collaboration_mode>")
        or stripped.startswith("<apps_instructions>")
        or stripped.startswith("<plugins_instructions>")
        or stripped.startswith("<skills_instructions>")
    )


def _redact_secrets(text: str) -> str:
    patterns = (
        (r"\bsk-[A-Za-z0-9_-]{12,}\b", "[REDACTED_OPENAI_KEY]"),
        (r"\b(?:ghp|github_pat)_[A-Za-z0-9_]{12,}\b", "[REDACTED_GITHUB_TOKEN]"),
        (r"\bAKIA[0-9A-Z]{16}\b", "[REDACTED_AWS_ACCESS_KEY]"),
        (r"(?i)\b(authorization\s*:\s*bearer)\s+\S+", r"\1 [REDACTED]"),
        (
            r"(?i)\b(password|passwd|api[_-]?key|token)\s*[:=]\s*[^\s,;]+",
            r"\1=[REDACTED]",
        ),
    )
    result = text
    for pattern, replacement in patterns:
        result = re.sub(pattern, replacement, result)
    return result


def _parse_timestamp(value: object) -> date | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def parse_transcript(raw: str, fallback_model: str | None = None) -> ParsedTranscript:
    """Normalize Codex JSONL into Claude's established transcript format."""
    normalized: list[str] = []
    models: list[str] = []
    current_model: str | None = None
    started_on: date | None = None
    cwd = ""

    for raw_line in raw.splitlines():
        try:
            entry = json.loads(raw_line)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(entry, dict):
            continue
        entry_type = entry.get("type")
        payload = entry.get("payload")
        if not isinstance(payload, dict):
            continue

        if entry_type == "session_meta":
            started_on = (
                _parse_timestamp(payload.get("timestamp"))
                or _parse_timestamp(entry.get("timestamp"))
                or started_on
            )
            if isinstance(payload.get("cwd"), str):
                cwd = payload["cwd"]
            continue

        if entry_type == "turn_context":
            candidate = payload.get("model")
            if isinstance(candidate, str) and candidate.strip():
                current_model = candidate.strip()
                _ordered_add(models, current_model)
            continue

        if entry_type != "response_item" or payload.get("type") != "message":
            continue
        role = payload.get("role")
        if role not in ("user", "assistant"):
            continue
        content = payload.get("content")
        if not isinstance(content, list):
            continue
        texts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") not in ("input_text", "output_text"):
                continue
            text = block.get("text")
            if (
                not isinstance(text, str)
                or not text.strip()
                or _is_injected_context(text)
            ):
                continue
            texts.append(text.strip())
        if not texts:
            continue
        message: dict[str, object] = {
            "role": role,
            "content": [{"type": "text", "text": "\n".join(texts)}],
        }
        if role == "assistant" and current_model:
            message["model"] = current_model
        normalized.append(json.dumps({"type": role, "message": message}))

    _ordered_add(models, fallback_model)
    normalized_raw = "\n".join(normalized)
    return ParsedTranscript(
        title=shared.extract_conversation_title(normalized_raw),
        text=_redact_secrets(shared.parse_transcript(normalized_raw)),
        models=tuple(models),
        started_on=started_on or date.today(),
        cwd=cwd,
    )


def parse_transcript_file(
    path: Path, fallback_model: str | None = None
) -> ParsedTranscript:
    return parse_transcript(path.read_text(errors="replace"), fallback_model)


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def _clean_old_state() -> None:
    cutoff = datetime.now(timezone.utc) - timedelta(days=FAILURE_RETENTION_DAYS)
    for name in ("failed", "processed", "logs"):
        directory = state_dir() / name
        if not directory.exists():
            continue
        for path in directory.iterdir():
            if not path.is_file():
                continue
            modified = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
            if modified < cutoff:
                path.unlink(missing_ok=True)


def _worker_environment() -> dict[str, str]:
    environment = dict(os.environ)
    environment["CODEX_LOGSEQ_WORKER_RUNNING"] = "1"
    retained_codex = {
        "CODEX_HOME",
        "CODEX_BIN",
        "CODEX_LOGSEQ_STATE_DIR",
        "CODEX_LOGSEQ_EXTRACTION_MODEL",
        "CODEX_LOGSEQ_WORKER_RUNNING",
        "CODEX_LOGSEQ_SYNC",
    }
    for key in list(environment):
        normalized = key.lower().replace("_", "")
        if normalized in {
            "allproxy",
            "httpproxy",
            "httpsproxy",
            "ftpproxy",
            "grpcproxy",
            "rsyncproxy",
            "dockerhttpproxy",
            "dockerhttpsproxy",
        } or key.startswith("CLOUDSDK_PROXY"):
            environment.pop(key, None)
        elif key.startswith("CODEX_") and key not in retained_codex:
            environment.pop(key, None)
    return environment


def enqueue(payload: dict) -> int:
    _clean_old_state()
    session_id = str(payload.get("session_id") or "").strip()
    transcript_value = payload.get("transcript_path")
    if not session_id or not transcript_value:
        return 0
    transcript_path = Path(str(transcript_value)).expanduser()
    if not transcript_path.exists():
        return 0
    parsed = parse_transcript_file(transcript_path, payload.get("model"))
    if len(parsed.text) < 200:
        return 0
    if not parsed.models:
        print("[logseq-codex] task model could not be resolved", file=sys.stderr)
        return 1

    root = state_dir()
    safe_id = re.sub(r"[^A-Za-z0-9_.-]", "-", session_id)
    paths = {
        name: root / name / f"{safe_id}.json"
        for name in ("queue", "running", "processed", "failed")
    }
    if any(path.exists() for path in paths.values()):
        return 0
    cwd = str(payload.get("cwd") or parsed.cwd or os.getcwd())
    project = Path(cwd).name or "unknown"
    job = {
        "version": 1,
        "session_id": session_id,
        "session_short": session_id[:8],
        "started_on": parsed.started_on.isoformat(),
        "cwd": cwd,
        "project": project,
        "conversation_title": parsed.title,
        "transcript": parsed.text,
        "models": list(parsed.models),
        "queued_at": datetime.now(timezone.utc).isoformat(),
    }
    _atomic_json(paths["queue"], job)

    if os.environ.get("CODEX_LOGSEQ_SYNC") == "1":
        return process_job(paths["queue"])
    log_path = root / "logs" / f"{safe_id}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "process",
        str(paths["queue"]),
    ]
    with log_path.open("ab") as log_handle:
        subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=log_handle,
            env=_worker_environment(),
            start_new_session=True,
            close_fds=True,
        )
    return 0


def call_codex(transcript: str) -> dict:
    if not SCHEMA_PATH.exists():
        raise FileNotFoundError(f"missing extraction schema: {SCHEMA_PATH}")
    command = [
        CODEX_BIN,
        "exec",
        "--ephemeral",
        "--ignore-user-config",
        "--disable",
        "hooks",
        "--sandbox",
        "read-only",
        "--skip-git-repo-check",
        "-C",
        tempfile.gettempdir(),
        "-m",
        EXTRACTION_MODEL,
        "-c",
        'model_reasoning_effort="low"',
        "--output-schema",
        str(SCHEMA_PATH),
        "-",
    ]
    result = subprocess.run(
        command,
        input=shared.extraction_prompt("Codex") + transcript,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_worker_environment(),
        timeout=300,
    )
    if result.returncode != 0:
        detail = (result.stderr or "").strip()[-500:]
        raise RuntimeError(
            f"codex exec failed with code {result.returncode}"
            + (f": {detail}" if detail else "")
        )
    if not result.stdout.strip():
        raise RuntimeError("codex exec returned empty output")
    return shared.parse_extraction_output(result.stdout)


def _persist(job: dict, insights: dict) -> Path:
    started_on = date.fromisoformat(str(job["started_on"]))
    session_id = str(job["session_short"])
    project = str(job.get("project") or "unknown")
    session_slug = f"{started_on.isoformat()}-{session_id}"
    session_title = f"Session {started_on.isoformat()} {session_id} — {project}"
    models = [str(model) for model in job["models"] if str(model).strip()]

    with shared.vault_lock():
        links = shared.write_pages(
            insights,
            project,
            session_id,
            session_slug,
            session_title,
            models,
            pages_dir=pages_dir(),
            update_index_fn=lambda paths: shared.update_vault_index(
                paths, index_path()
            ),
            creator="codex",
            today=started_on,
        )
        shared.write_session(
            insights,
            project,
            session_id,
            session_slug,
            links,
            str(job.get("conversation_title") or ""),
            models,
            pages_dir=pages_dir(),
            creator="codex",
            today=started_on,
        )
        shared.update_index(pages_dir(), started_on)
        shared.write_digest(pages_dir(), date.today())
    return (
        pages_dir()
        / "claude"
        / "sessions"
        / started_on.strftime("%Y_%m_%d")
        / f"{session_slug}.md"
    )


def process_job(queue_path: Path) -> int:
    root = state_dir()
    running_path = root / "running" / queue_path.name
    processed_path = root / "processed" / queue_path.name
    failed_path = root / "failed" / queue_path.name
    running_path.parent.mkdir(parents=True, exist_ok=True)
    job: dict = {}
    try:
        if queue_path.exists():
            os.replace(queue_path, running_path)
        if not running_path.exists():
            return 0
        job = json.loads(running_path.read_text())
        models = job.get("models")
        if not isinstance(models, list) or not any(
            str(model).strip() for model in models
        ):
            raise ValueError("job has no originating task models")
        insights = call_codex(str(job.get("transcript", "")))
        session_path = _persist(job, insights)
        _atomic_json(
            processed_path,
            {
                "session_id": job.get("session_id"),
                "models": models,
                "session_path": str(session_path),
                "processed_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        running_path.unlink(missing_ok=True)
        return 0
    except Exception as exc:
        _atomic_json(
            failed_path,
            {
                "session_id": job.get("session_id"),
                "models": job.get("models", []),
                "error": str(exc)[:500],
                "failed_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        running_path.unlink(missing_ok=True)
        print(f"[logseq-codex] extraction failed: {exc}", file=sys.stderr)
        return 1


def _find_session_transcript(session_id: str) -> Path | None:
    codex_home = Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser()
    matches: list[Path] = []
    for base in (codex_home / "sessions", codex_home / "archived_sessions"):
        if base.exists():
            matches.extend(base.rglob(f"*{session_id}*.jsonl"))
    return max(matches, key=lambda path: path.stat().st_mtime) if matches else None


def resolve_session(session_id: str | None = None) -> int:
    resolved_id = (session_id or os.environ.get("CODEX_THREAD_ID") or "").strip()
    if not resolved_id:
        print("CODEX_THREAD_ID is unavailable", file=sys.stderr)
        return 1
    transcript_path = _find_session_transcript(resolved_id)
    if transcript_path is None:
        print(f"no Codex transcript found for {resolved_id}", file=sys.stderr)
        return 1
    parsed = parse_transcript_file(transcript_path)
    if not parsed.models:
        print(f"no task model metadata found for {resolved_id}", file=sys.stderr)
        return 1
    project = Path(parsed.cwd).name if parsed.cwd else "unknown"
    short = resolved_id[:8]
    session = f"Session {parsed.started_on.isoformat()} {short} — {project}"
    print(
        json.dumps(
            {
                "session_id": resolved_id,
                "session_short": short,
                "started_on": parsed.started_on.isoformat(),
                "project": project,
                "session": session,
                "models": list(parsed.models),
                "transcript_path": str(transcript_path),
            }
        )
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("enqueue")
    process_parser = subparsers.add_parser("process")
    process_parser.add_argument("job", type=Path)
    resolve_parser = subparsers.add_parser("resolve-session")
    resolve_parser.add_argument("session_id", nargs="?")
    args = parser.parse_args()
    if args.command == "enqueue":
        try:
            payload = json.loads(sys.stdin.read() or "{}")
        except json.JSONDecodeError:
            return 1
        return enqueue(payload)
    if args.command == "process":
        return process_job(args.job)
    return resolve_session(args.session_id)


if __name__ == "__main__":
    raise SystemExit(main())
