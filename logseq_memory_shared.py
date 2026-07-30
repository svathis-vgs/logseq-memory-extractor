#!/usr/bin/env python3
"""Shared, behavior-compatible Logseq memory primitives.

Claude Code's pre-Codex behavior is the compatibility contract.  The Claude
hook keeps its original public wrappers while delegating to these functions.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import sys
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Callable, Iterable


MAX_TRANSCRIPT_CHARS = 12_000
CATEGORIES = ("patterns", "mistakes", "decisions", "context")
TYPE_MAP = {
    "patterns": "[[pattern]]",
    "mistakes": "[[mistake]]",
    "decisions": "[[decision]]",
    "context": "[[context]]",
}
DAYFLOW_KEYWORDS = (
    "screenshot",
    "screen recording",
    "activity log",
    "timeline cards",
    "dayflow",
    "screen capture",
)

_CLAUDE_EXTRACTION_PROMPT = """\
Your output MUST be a single raw JSON object. Do NOT include any prose, explanation, preamble, summary text, markdown, or code fences. Your response MUST start with `{` and end with `}`. Nothing before the opening brace. Nothing after the closing brace. If you add any text outside the JSON object the output is unusable.

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
- Only include items that are NON-OBVIOUS and would not be known to an experienced engineer without this session. Skip anything that is standard engineering knowledge, easily Google-able, or a generic best practice.
- Maximum 5 items per category. Be ruthless — if you have more candidates, keep only the most surprising or project-specific ones.
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


def extraction_prompt(source_name: str = "Claude Code") -> str:
    """Return the frozen Claude prompt, varying only the platform label."""
    if source_name == "Claude Code":
        return _CLAUDE_EXTRACTION_PROMPT
    return _CLAUDE_EXTRACTION_PROMPT.replace(
        "Analyze this Claude Code session transcript",
        f"Analyze this {source_name} session transcript",
        1,
    )


def extraction_schema() -> dict:
    insight = {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "slug": {"type": "string"},
                "summary": {"type": "string"},
                "detail": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["slug", "summary", "detail", "tags"],
        },
    }
    return {
        "type": "object",
        "properties": {
            "patterns": insight,
            "mistakes": insight,
            "decisions": insight,
            "context": insight,
            "session_summary": {"type": "string"},
        },
        "required": [
            "patterns",
            "mistakes",
            "decisions",
            "context",
            "session_summary",
        ],
    }


def parse_extraction_output(raw: str) -> dict:
    """Parse structured CLI output using Claude's established fallbacks."""
    cleaned = raw.strip().replace("\x00", "").replace("\ufeff", "").strip()
    try:
        outer = json.loads(cleaned)
        if isinstance(outer, dict):
            if "structured_output" in outer and isinstance(
                outer["structured_output"], dict
            ):
                return outer["structured_output"]
            if "result" in outer:
                cleaned = outer["result"]
    except json.JSONDecodeError:
        pass
    cleaned = re.sub(r"^```(?:json)?\n?", "", cleaned.strip())
    cleaned = re.sub(r"\n?```$", "", cleaned).strip()
    if not cleaned.startswith("{"):
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start != -1 and end != -1 and end > start:
            cleaned = cleaned[start : end + 1]
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"claude CLI returned non-JSON (char {exc.pos}): {cleaned[:120]!r}"
        ) from exc
    if not isinstance(parsed, dict):
        return {
            "patterns": [],
            "mistakes": [],
            "decisions": [],
            "context": [],
            "session_summary": "",
        }
    return parsed


def extract_conversation_title(raw: str) -> str:
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        msg = entry.get("message", {})
        if not isinstance(msg, dict) or msg.get("role") != "user":
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


def extract_models(raw: str) -> list[str]:
    import json

    models: list[str] = []
    seen: set[str] = set()
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
        model = msg.get("model")
        if (
            isinstance(model, str)
            and model
            and model != "<synthetic>"
            and model not in seen
        ):
            seen.add(model)
            models.append(model)
    return models


def parse_transcript(raw: str, max_chars: int = MAX_TRANSCRIPT_CHARS) -> str:
    import json

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
        prefix = (
            "User"
            if role in ("user", "human")
            else "Assistant"
            if role in ("assistant",)
            else None
        )
        if prefix is None:
            continue
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    lines.append(f"{prefix}: {block['text']}")
        elif isinstance(content, str):
            lines.append(f"{prefix}: {content}")

    text = "\n".join(lines)
    return text[-max_chars:] if len(text) > max_chars else text


def logseq_date(today: date | None = None) -> str:
    value = today or date.today()
    return "[[" + value.strftime("%Y/%m/%d") + "]]"


def logseq_link(value: str) -> str:
    text = str(value).strip()
    if text.startswith("[[") and text.endswith("]]"):
        return text
    return f"[[{text}]]"


def logseq_links(values: list[str]) -> str:
    return ", ".join(logseq_link(value) for value in values if str(value).strip())


def page_content(
    type_: str,
    title: str,
    summary: str,
    detail: str,
    tags: list,
    project: str,
    session_title: str,
    models: list[str] | None = None,
    *,
    creator: str = "claude",
    today: date | None = None,
) -> str:
    today_text = logseq_date(today)
    tag_str = " ".join(f"[[{tag}]]" for tag in tags) if tags else ""
    lines = [
        f"title:: {title}",
        f"type:: {type_}",
        f"date:: {today_text}",
        f"last-updated:: {today_text}",
        f"project:: [[{project}]]",
        f"session:: [[{session_title}]]",
        f"creator:: {logseq_link(creator)}",
    ]
    if models:
        lines.append(f"model:: {logseq_links(models)}")
    if tag_str:
        lines.append(f"tags:: {tag_str}")
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


def index_extract_text(path: Path) -> str:
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return ""
    title = ""
    summary_parts: list[str] = []
    in_summary = False
    for line in lines:
        if line.startswith("title::"):
            title = line[7:].strip()
        elif "## Summary" in line:
            in_summary = True
        elif in_summary:
            stripped = line.strip()
            if stripped.startswith("- ##"):
                break
            if stripped.startswith("- "):
                summary_parts.append(stripped[2:].strip())
                if len(summary_parts) >= 3:
                    break
    return f"{title}. {' '.join(summary_parts)}".strip(". ")[:500]


def update_vault_index(new_paths: list[Path], index_path: Path) -> None:
    if not new_paths or not index_path.exists():
        return
    try:
        import numpy as np
        from sentence_transformers import SentenceTransformer
    except ImportError:
        return

    texts: list[str] = []
    valid_paths: list[str] = []
    for path in new_paths:
        if not path.exists():
            continue
        text = index_extract_text(path)
        if text and len(text) > 10:
            texts.append(text)
            valid_paths.append(str(path))
    if not texts:
        return

    try:
        data = np.load(index_path, allow_pickle=True)
        old_embeddings = data["embeddings"]
        old_paths = list(data["paths"])
        model = SentenceTransformer("all-MiniLM-L6-v2")
        new_embeddings = model.encode(
            texts, normalize_embeddings=True, show_progress_bar=False
        ).astype("float32")
        combined = np.vstack([old_embeddings, new_embeddings])
        np.savez_compressed(
            index_path,
            embeddings=combined,
            paths=np.array(old_paths + valid_paths),
        )
    except Exception as exc:
        print(f"[logseq-memory] index update skipped: {exc}", file=sys.stderr)


def write_pages(
    insights: dict,
    project_name: str,
    session_id: str,
    session_slug: str,
    session_title: str,
    models: list[str] | None,
    *,
    pages_dir: Path,
    update_index_fn: Callable[[list[Path]], None],
    creator: str = "claude",
    today: date | None = None,
) -> list[str]:
    del session_id, session_slug  # retained for compatibility with the legacy API
    written: list[str] = []
    new_paths: list[Path] = []
    for key, type_name in TYPE_MAP.items():
        items = insights.get(key, [])
        if not isinstance(items, list):
            continue
        subdir = pages_dir / "claude" / key
        subdir.mkdir(parents=True, exist_ok=True)
        category = key.rstrip("s")
        for item in items:
            if not isinstance(item, dict):
                continue
            slug = re.sub(
                r"[^a-z0-9-]+", "-", item.get("slug", "untitled").lower()
            ).strip("-")
            filename = f"{category}-{slug}"
            title = f"{category.title()}: {slug.replace('-', ' ').title()}"
            slug_words = slug.split("-")
            prefix_2 = "-".join(slug_words[:2]) if len(slug_words) >= 2 else slug
            prefix_pattern = f"{category}-{prefix_2}-"
            already_exists = any(True for _ in subdir.glob(f"{prefix_pattern}*.md"))
            dest = subdir / f"{filename}.md"
            if not dest.exists() and not already_exists:
                content = page_content(
                    type_=type_name,
                    title=title,
                    summary=item.get("summary", ""),
                    detail=item.get("detail", ""),
                    tags=item.get("tags", []),
                    project=project_name,
                    session_title=session_title,
                    models=models,
                    creator=creator,
                    today=today,
                )
                dest.write_text(content.lstrip("\n"))
                new_paths.append(dest)
            written.append(f"[[{title}]]")
    update_index_fn(new_paths)
    return written


def is_dayflow_session(conversation_title: str, session_summary: str) -> bool:
    haystack = (conversation_title + " " + session_summary).lower()
    return any(keyword in haystack for keyword in DAYFLOW_KEYWORDS)


def write_session(
    insights: dict,
    project_name: str,
    session_id: str,
    session_slug: str,
    written_links: list[str],
    conversation_title: str,
    models: list[str] | None,
    *,
    pages_dir: Path,
    creator: str = "claude",
    today: date | None = None,
) -> None:
    current = today or date.today()
    today_text = logseq_date(current)
    date_folder = current.strftime("%Y_%m_%d")
    sessions_dir = pages_dir / "claude" / "sessions" / date_folder
    sessions_dir.mkdir(parents=True, exist_ok=True)
    session_date = today_text.strip("[]").replace("/", "-")
    session_title = f"Session {session_date} {session_id} — {project_name}"
    description = conversation_title or session_title
    session_summary = insights.get("session_summary", "No summary generated.")
    lines = [
        f"title:: {session_title}",
        f"description:: {description}",
        "type:: [[session]]",
        f"date:: {today_text}",
        f"last-updated:: {today_text}",
        f"project:: [[{project_name}]]",
        f"session:: [[{session_title}]]",
        f"creator:: {logseq_link(creator)}",
    ]
    if models:
        lines.append(f"model:: {logseq_links(models)}")
    lines.append("exclude-from-graph-view:: true")
    if is_dayflow_session(conversation_title, session_summary):
        lines.append("tags:: [[dayflow]]")
    lines += [
        "",
        "- ## Summary",
        f"  - {session_summary}",
        "",
        "- ## Insights",
    ] + [f"  - {link}" for link in written_links] + [""]
    (sessions_dir / f"{session_slug}.md").write_text(
        "\n".join(lines).lstrip("\n")
    )


def write_digest(pages_dir: Path, today: date | None = None) -> None:
    current = today or date.today()
    today_text = current.isoformat()
    all_insights: list[tuple[str, str, str, str]] = []
    for category in CATEGORIES:
        subdir = pages_dir / "claude" / category
        if not subdir.exists():
            continue
        for path in subdir.glob("*.md"):
            try:
                text = path.read_text(errors="replace")
            except OSError:
                continue
            lines = text.splitlines()
            metadata: dict[str, str] = {}
            for line in lines:
                if "::" in line and not line.startswith("-"):
                    key, _, value = line.partition("::")
                    metadata[key.strip()] = value.strip()
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
            date_str = metadata.get("date", "").strip("[[]]").replace("/", "-")
            title = metadata.get("title", path.stem)
            all_insights.append((date_str, category, title, summary))
    all_insights.sort(key=lambda item: item[0], reverse=True)
    by_category: dict[str, list[tuple[str, str, str]]] = {
        category: [] for category in CATEGORIES
    }
    for date_str, category, title, summary in all_insights:
        by_category[category].append((date_str, title, summary))
    totals = {category: len(by_category[category]) for category in CATEGORIES}
    output = [
        "# Claude Code Memory Digest",
        f"_Updated: {today_text} — "
        f"{totals['patterns']} patterns, {totals['mistakes']} mistakes, "
        f"{totals['decisions']} decisions, {totals['context']} context_",
        "",
        "Read this at session start to recall accumulated insights.",
        "",
    ]
    prefixes = ("Pattern: ", "Mistake: ", "Decision: ", "Context: ")
    for category in CATEGORIES:
        items = by_category[category][:15]
        if not items:
            continue
        output.append(f"## {category.title()}")
        for date_str, title, summary in items:
            short = title
            for prefix in prefixes:
                if title.startswith(prefix):
                    short = title[len(prefix) :]
                    break
            output.append(f"- **{short}** ({date_str}) — {summary}")
        output.append("")
    (pages_dir / "claude" / "digest.md").write_text("\n".join(output))


_INDEX_TEMPLATE = """{updated}

- # Claude Code Memory Index
  - Auto-generated. Add your own notes below the query sections.

- ## Sessions
  - {{{{query (property type [[session]])}}}}
    query-table:: true
    query-sort-by:: date
    query-sort-desc:: true
    query-properties:: [:title :date :project]

- ## Patterns
  collapsed:: true
  - {{{{query (property type [[pattern]])}}}}
    query-table:: true
    query-sort-by:: date
    query-sort-desc:: true
    query-properties:: [:title :date :project :tags]

- ## Mistakes
  collapsed:: true
  - {{{{query (property type [[mistake]])}}}}
    query-table:: true
    query-sort-by:: date
    query-sort-desc:: true
    query-properties:: [:title :date :project :tags]

- ## Decisions
  collapsed:: true
  - {{{{query (property type [[decision]])}}}}
    query-table:: true
    query-sort-by:: date
    query-sort-desc:: true
    query-properties:: [:title :date :project :tags]

- ## Context
  collapsed:: true
  - {{{{query (property type [[context]])}}}}
    query-table:: true
    query-sort-by:: date
    query-sort-desc:: true
    query-properties:: [:title :date :project :tags]
"""


def update_index(pages_dir: Path, today: date | None = None) -> None:
    today_text = logseq_date(today)
    path = pages_dir / "claude" / "index.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(_INDEX_TEMPLATE.format(updated=f"updated:: {today_text}"))
    else:
        existing = path.read_text()
        updated = re.sub(
            r"^updated::.*$",
            f"updated:: {today_text}",
            existing,
            count=1,
            flags=re.MULTILINE,
        )
        path.write_text(updated)


def default_lock_path() -> Path:
    return Path(
        os.environ.get("LOGSEQ_LOCK_PATH", "~/.claude/vault_index.lock")
    ).expanduser()


@contextmanager
def vault_lock(path: Path | None = None):
    lock = path or default_lock_path()
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
