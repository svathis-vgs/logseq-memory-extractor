#!/usr/bin/env python3
"""Shared, behavior-compatible Logseq memory primitives.

Claude Code's pre-Codex behavior is the compatibility contract.  The Claude
hook keeps its original public wrappers while delegating to these functions.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Callable, Iterable

try:  # POSIX
    import fcntl
except ImportError:  # Windows
    fcntl = None
    import msvcrt


MAX_TRANSCRIPT_CHARS = 12_000
CATEGORIES = ("patterns", "mistakes", "decisions", "context")
TYPE_MAP = {
    "patterns": "[[pattern]]",
    "mistakes": "[[mistake]]",
    "decisions": "[[decision]]",
    "context": "[[context]]",
}
MAX_ITEMS_PER_CATEGORY = 5

# Agent-neutral locations. The vault subdirectory and the shared home (index,
# lock, installed scripts) are not tied to any one agent; Claude and Codex both
# read and write the same paths. Each can be overridden by environment.
MEMORY_DIRNAME = "memory"
def _default_memory_home() -> Path:
    """LOGSEQ_MEMORY_HOME, else the install this file runs from, else ~/.logseq-memory.

    install.py copies the scripts into <home>/bin next to <home>/config.json,
    so an install in a non-default --home works without any environment.
    """
    if os.environ.get("LOGSEQ_MEMORY_HOME"):
        return Path(os.environ["LOGSEQ_MEMORY_HOME"]).expanduser()
    here = Path(__file__).resolve().parent
    if here.name == "bin" and (here.parent / "config.json").exists():
        return here.parent
    return Path("~/.logseq-memory").expanduser()


MEMORY_HOME = _default_memory_home()
CONFIG_PATH = MEMORY_HOME / "config.json"


def _home_config() -> dict:
    """Settings written by install.py (e.g. {"pages_dir": ...}); empty if absent."""
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


DEFAULT_PAGES_DIR = Path(
    os.environ.get("LOGSEQ_PAGES_DIR")
    or _home_config().get("pages_dir")
    or "~/VGS/Notes/pages"
).expanduser()
DEFAULT_INDEX_PATH = Path(
    os.environ.get("LOGSEQ_INDEX_PATH", str(MEMORY_HOME / "vault_index.npz"))
).expanduser()
RETRIEVER_DISABLED_FLAGS = (
    MEMORY_HOME / "retriever_disabled",
    # Legacy flag still written by the Claude vault-toggle skill.
    Path("~/.claude/vault_retriever_disabled").expanduser(),
)


EMBEDDING_MODEL = "all-MiniLM-L6-v2"
OFFLINE_VARS = ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "HF_DATASETS_OFFLINE")


def _model_cache_dirs(model: str = EMBEDDING_MODEL) -> list[Path]:
    """Where sentence-transformers keeps a downloaded model (current HF hub
    cache layout, then the legacy sentence-transformers cache)."""
    home = Path.home()
    hub = os.environ.get("HF_HUB_CACHE") or os.environ.get("HUGGINGFACE_HUB_CACHE")
    if not hub:
        hf_home = os.environ.get("HF_HOME") or str(
            Path(os.environ.get("XDG_CACHE_HOME") or home / ".cache") / "huggingface"
        )
        hub = str(Path(hf_home) / "hub")
    legacy = os.environ.get("SENTENCE_TRANSFORMERS_HOME") or str(
        home / ".cache" / "torch" / "sentence_transformers"
    )
    return [
        Path(hub).expanduser() / f"models--sentence-transformers--{model}" / "snapshots",
        Path(legacy).expanduser() / f"sentence-transformers_{model}",
    ]


def model_is_cached(model: str = EMBEDDING_MODEL) -> bool:
    return any(d.is_dir() and any(d.iterdir()) for d in _model_cache_dirs(model))


def use_offline_models() -> None:
    """Load the embedding model from the local cache without contacting
    HuggingFace, once it has been downloaded.

    Runs when this module is imported, so every script (hooks, MCP server,
    index rebuilds, ad-hoc imports) avoids network calls that a sandbox or
    proxy would block, with no shell-specific environment prefix. Before the
    first download nothing is set, so that download can still happen. If any
    of the variables is already set, the caller has chosen; leave all three.
    """
    if any(name in os.environ for name in OFFLINE_VARS):
        return
    if model_is_cached():
        for name in OFFLINE_VARS:
            os.environ.setdefault(name, "1")


use_offline_models()


RUN_LOG_MAX_BYTES = 512 * 1024


def run_log_path() -> Path:
    return MEMORY_HOME / "extractor.log"


def log_run(source: str, status: str, message: str = "") -> None:
    """Append one line per extraction run to ~/.logseq-memory/extractor.log.

    Hooks run with their stderr discarded, so this is the only record that an
    extraction ran, succeeded or failed. Never raises; trims itself to the
    newest half once it passes RUN_LOG_MAX_BYTES.
    """
    from datetime import datetime

    line = f"{datetime.now().isoformat(timespec='seconds')} {source} {status}"
    if message:
        line += " " + " ".join(str(message).split())[:2000]
    path = run_log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(line + "\n")
        if path.stat().st_size > RUN_LOG_MAX_BYTES:
            data = path.read_text(encoding="utf-8", errors="replace")
            keep = data[len(data) // 2:]
            path.write_text(keep[keep.find("\n") + 1:], encoding="utf-8", newline="\n")
    except OSError:
        pass


def find_cli(name: str) -> str:
    """Resolve an agent CLI (claude / codex) from PATH, config.json, or the
    usual install locations. GUI apps start hooks without the shell PATH."""
    found = shutil.which(name)
    if found:
        return found
    configured = _home_config().get(f"{name}_bin")
    if configured and Path(configured).exists():
        return configured
    home = Path.home()
    exe = ".exe" if os.name == "nt" else ""
    candidates = [
        home / ".local" / "bin" / f"{name}{exe}",
        home / f".{name}" / "local" / f"{name}{exe}",
        Path("/opt/homebrew/bin") / name,
        Path("/usr/local/bin") / name,
    ]
    if name == "codex":
        candidates.append(Path("/Applications/ChatGPT.app/Contents/Resources/codex"))
        candidates.append(Path("/Applications/Codex.app/Contents/Resources/codex"))
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return name


def memory_root(pages_dir: Path) -> Path:
    """The vault directory holding all agent memory pages."""
    return Path(pages_dir) / MEMORY_DIRNAME
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
    # maxItems enforces the prompt's "Maximum 5 items per category" rule at the
    # API/structured-output level. The prompt instruction alone is a request the
    # model can silently exceed on a long, eventful session — a single Stop hook
    # extraction call once produced 20 decision-category files that way, burying
    # the digest's recent-decisions slot under one session's play-by-play.
    insight = {
        "type": "array",
        "maxItems": MAX_ITEMS_PER_CATEGORY,
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


_SYNTHETIC_WRAPPER_RE = re.compile(
    r"^(?:\s*<(system-reminder|ide_selection|ide_opened_file|local-command-caveat"
    r"|local-command-stdout|local-command-stderr)>.*?</\1>)+",
    re.DOTALL,
)
_IMAGE_TAG_RE = re.compile(r"</?image\b[^>]*>")
_SLASH_COMMAND_RE = re.compile(
    r"^\s*<command-message>.*?</command-message>\s*<command-name>(.*?)</command-name>"
    r"(?:\s*<command-args>(.*?)</command-args>)?",
    re.DOTALL,
)


def _clean_title_text(text: str) -> str:
    """Drop harness-injected wrappers so the title is what the user typed."""
    text = _IMAGE_TAG_RE.sub("", text)
    text = _SYNTHETIC_WRAPPER_RE.sub("", text)
    text = _SLASH_COMMAND_RE.sub(
        lambda m: f"{m.group(1).strip()} {(m.group(2) or '').strip()}", text
    )
    return text.strip()


def extract_conversation_title(raw: str) -> str:
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("isMeta"):
            continue
        msg = entry.get("message", {})
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        content = msg.get("content", "")
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "text":
                continue
            text = _clean_title_text(str(block.get("text", "")))
            if text:
                text = text.replace("\n", " ")
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


def sanitize(text: str) -> str:
    """Escape text for Logseq round-trip safety before it goes into a page.

    Backtick-wraps bare `#123`-style references (PR/ticket numbers) so Logseq
    doesn't parse them as tag links to a phantom "#123" page, and escapes
    `{{ }}` so it isn't parsed as a query/macro block. The digit run is
    matched greedily (`\\d+`, not `\\d`) — matching only the first digit
    would leave the rest of a multi-digit number (e.g. `#3219`) outside the
    backticks and visibly broken.
    """
    text = re.sub(r"(?<!`)#(\d+)", r"`#\1`", text)
    text = text.replace("{{", "`{{").replace("}}", "}}`")
    return text


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
    title = sanitize(title)
    summary = sanitize(summary)
    detail = sanitize(detail)
    tag_str = ", ".join(f"[[{tag}]]" for tag in tags) if tags else ""
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
        lines = path.read_text(errors="replace", encoding="utf-8").splitlines()
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
        model = SentenceTransformer(EMBEDDING_MODEL)
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


# Write-time semantic dedup. Calibrated 2026-10-17 on 112 blind-labelled
# nearest-neighbour pairs from the live vault: at >=0.88 ~1 in 40 matches was
# a genuinely different insight (a correction) and ~60% were near-duplicates
# carrying one extra detail — so a match appends an update to the existing
# page instead of silently dropping the new note.
SEMANTIC_DEDUP_THRESHOLD = float(os.environ.get("LOGSEQ_DEDUP_MIN_SCORE", "0.88"))
_EMBEDDING_MODEL = None


def _embedding_model():
    global _EMBEDDING_MODEL
    if _EMBEDDING_MODEL is None:
        from sentence_transformers import SentenceTransformer

        _EMBEDDING_MODEL = SentenceTransformer(EMBEDDING_MODEL)
    return _EMBEDDING_MODEL


def embedding_text(title: str, summary: str) -> str:
    """The text a page is indexed under (mirrors index_extract_text)."""
    return f"{sanitize(title)}. {sanitize(summary.strip())}".strip(". ")[:500]


class SemanticIndex:
    """Read-only view of vault_index.npz for finding same-meaning pages.

    Fails open: if numpy, sentence-transformers or the index is unavailable,
    find() returns None and writes proceed as before.
    """

    def __init__(
        self,
        index_path: Path | None,
        encode: Callable[[list[str]], object] | None = None,
        threshold: float | None = None,
    ):
        self.threshold = SEMANTIC_DEDUP_THRESHOLD if threshold is None else threshold
        self._encode = encode
        self._embeddings = None
        self._paths: list[str] = []
        self._np = None
        if index_path is None or not Path(index_path).exists():
            return
        try:
            import numpy as np

            data = np.load(index_path, allow_pickle=True)
            self._embeddings = data["embeddings"].astype("float32")
            self._paths = [str(p) for p in data["paths"]]
            self._np = np
        except Exception as exc:
            print(f"[logseq-memory] semantic dedup disabled: {exc}", file=sys.stderr)
            self._embeddings = None

    def _vector(self, text: str):
        if self._encode is None:
            model = _embedding_model()
            self._encode = lambda texts: model.encode(
                texts, normalize_embeddings=True, show_progress_bar=False
            )
        return self._np.asarray(self._encode([text]), dtype="float32")[0]

    def find(self, directory: Path, text: str) -> Path | None:
        """Best match at or above threshold that lives directly in directory.

        Restricting to the directory being written keeps a match inside the
        same category and the same vault, even if the index covers another.
        """
        if self._embeddings is None or not self._paths or not text:
            return None
        directory = Path(directory).resolve()
        try:
            scores = self._embeddings @ self._vector(text)
        except Exception as exc:
            print(f"[logseq-memory] semantic dedup skipped: {exc}", file=sys.stderr)
            return None
        for i in scores.argsort()[::-1]:
            if float(scores[i]) < self.threshold:
                break
            candidate = Path(self._paths[i])
            if candidate.parent.resolve() == directory and candidate.exists():
                return candidate
        return None

    def add(self, path: Path, text: str) -> None:
        """Make a page written earlier in this batch visible to later finds."""
        if self._embeddings is None or not text:
            return
        try:
            vector = self._vector(text)
        except Exception:
            return
        self._embeddings = self._np.vstack([self._embeddings, vector[None, :]])
        self._paths.append(str(path))


def find_slug_duplicate(subdir: Path, file_prefix: str, slug: str) -> Path | None:
    """Existing page with the same slug, or sharing its first three slug words.

    The one slug rule for every writer. Two words proved too broad: e.g.
    "write-insight-*" blocked unrelated pages that merely shared a topic.
    """
    exact = subdir / f"{file_prefix}{slug}.md"
    if exact.exists():
        return exact
    words = slug.split("-")
    if len(words) >= 3:
        matches = sorted(subdir.glob(f"{file_prefix}{'-'.join(words[:3])}*.md"))
        if matches:
            return matches[0]
    return None


def page_title(path: Path) -> str:
    try:
        for line in path.read_text(errors="replace", encoding="utf-8").splitlines()[:5]:
            if line.startswith("title::"):
                return line[7:].strip()
    except OSError:
        pass
    return path.stem


def append_update(
    path: Path,
    summary: str,
    detail: str,
    session_title: str,
    *,
    today: date | None = None,
) -> None:
    """Fold a same-meaning note into an existing page as a dated update."""
    today_text = logseq_date(today)
    lines = path.read_text(errors="replace", encoding="utf-8").rstrip("\n").split("\n")
    for i, line in enumerate(lines):
        if line.startswith("last-updated::"):
            lines[i] = f"last-updated:: {today_text}"
            break
    else:
        for i, line in enumerate(lines):
            if line.startswith("date::"):
                lines.insert(i + 1, f"last-updated:: {today_text}")
                break
    detail_lines = sanitize(detail).split("\n") if detail.strip() else []
    lines += [
        "",
        f"- ## Update {today_text}",
        f"  - {sanitize(summary.strip())}",
        f"  - From [[{session_title}]]",
    ]
    if detail_lines:
        lines += [f"  - {detail_lines[0]}"] + detail_lines[1:]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


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
    index_path: Path | None = None,
    report: list | None = None,
) -> list[str]:
    """Write insight pages; returns [[title]] links for the session page.

    With index_path, a note whose title+summary matches an existing page in
    the same category (cosine >= SEMANTIC_DEDUP_THRESHOLD) is appended to that
    page as an update instead of creating a new file. If report is given,
    ("written" | "appended", path) tuples are added to it.
    """
    del session_id, session_slug  # retained for compatibility with the legacy API
    written: list[str] = []
    new_paths: list[Path] = []
    semantic = SemanticIndex(index_path) if index_path is not None else None
    for key, type_name in TYPE_MAP.items():
        items = insights.get(key, [])
        if not isinstance(items, list):
            continue
        # Hard cap independent of extraction_schema()'s maxItems — a single
        # write_pages() call (Stop hook or write_codex_insight) must never
        # explode one category's worth of one session into more files than
        # the digest can show without one session's play-by-play crowding
        # out everything else recent in that category.
        if len(items) > MAX_ITEMS_PER_CATEGORY:
            items = items[:MAX_ITEMS_PER_CATEGORY]
        subdir = memory_root(pages_dir) / key
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
            dest = subdir / f"{filename}.md"
            if find_slug_duplicate(subdir, f"{category}-", slug) is None:
                summary = item.get("summary", "")
                text = embedding_text(title, summary)
                match = semantic.find(subdir, text) if semantic is not None else None
                if match is not None:
                    append_update(
                        match,
                        summary,
                        item.get("detail", ""),
                        session_title,
                        today=today,
                    )
                    if report is not None:
                        report.append(("appended", match))
                    written.append(f"[[{page_title(match)}]]")
                    continue
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
                dest.write_text(content.lstrip("\n"), encoding="utf-8", newline="\n")
                new_paths.append(dest)
                if semantic is not None:
                    semantic.add(dest, text)
                if report is not None:
                    report.append(("written", dest))
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
    sessions_dir = memory_root(pages_dir) / "sessions" / date_folder
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
        "\n".join(lines).lstrip("\n"), encoding="utf-8", newline="\n"
    )


def write_digest(pages_dir: Path, today: date | None = None) -> None:
    current = today or date.today()
    today_text = current.isoformat()
    all_insights: list[tuple[str, str, str, str]] = []
    for category in CATEGORIES:
        subdir = memory_root(pages_dir) / category
        if not subdir.exists():
            continue
        for path in subdir.glob("*.md"):
            try:
                text = path.read_text(errors="replace", encoding="utf-8")
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
        "# Agent Memory Digest",
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
    (memory_root(pages_dir) / "digest.md").write_text("\n".join(output), encoding="utf-8", newline="\n")


_INDEX_TEMPLATE = """{updated}

- # Agent Memory Index
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
    path = memory_root(pages_dir) / "index.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(_INDEX_TEMPLATE.format(updated=f"updated:: {today_text}"), encoding="utf-8", newline="\n")
    else:
        existing = path.read_text(encoding="utf-8")
        updated = re.sub(
            r"^updated::.*$",
            f"updated:: {today_text}",
            existing,
            count=1,
            flags=re.MULTILINE,
        )
        path.write_text(updated, encoding="utf-8", newline="\n")


def find_session_entries(subdir: Path, session_marker: str) -> list[Path]:
    """Return existing pages in subdir whose session:: property references session_marker.

    session_marker is the raw session title (e.g. "Session 2026-09-10 9ce26f0e —
    buzzer-worker-webhook"), not yet wrapped in [[ ]] — matched against the
    session:: line as written by page_content()/write_session().
    """
    if not subdir.exists():
        return []
    marker = session_marker if session_marker.startswith("[[") else f"[[{session_marker}]]"
    matches: list[Path] = []
    for path in sorted(subdir.glob("*.md")):
        try:
            for line in path.read_text(errors="replace", encoding="utf-8").splitlines()[:10]:
                if line.startswith("session::") and marker in line:
                    matches.append(path)
                    break
        except OSError:
            continue
    return matches


def default_lock_path() -> Path:
    return Path(
        os.environ.get("LOGSEQ_LOCK_PATH", str(MEMORY_HOME / "vault_index.lock"))
    ).expanduser()


@contextmanager
def vault_lock(path: Path | None = None):
    lock = path or default_lock_path()
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a+", encoding="utf-8") as handle:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        else:
            handle.seek(0)
            while True:  # LK_LOCK gives up after ~10 s; keep waiting like flock
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                    break
                except OSError:
                    continue
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            else:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
