#!/usr/bin/env python3
"""
Logseq vault MCP server — on-demand semantic search and write access.

Exposes five tools:
  search_vault   — semantic similarity search over vault_index.npz
  read_page      — read the full content of a vault page by path
  write_insight  — create a new Logseq insight page mid-session
  list_recent    — list recently modified vault pages by category
  lint_vault     — scan vault for Logseq format violations

The server loads the embedding model and index once at startup.
Set TRANSFORMERS_OFFLINE=1 and HF_DATASETS_OFFLINE=1 in the MCP server
env config to avoid HuggingFace network calls when the model is cached.

Requirements:
    pip install mcp sentence-transformers numpy

Register in ~/.claude/settings.json:
    {
      "mcpServers": {
        "logseq-vault": {
          "command": "python3",
          "args": ["/path/to/logseq_memory_mcp.py"],
          "env": {
            "TRANSFORMERS_OFFLINE": "1",
            "HF_DATASETS_OFFLINE": "1"
          }
        }
      }
    }
"""

import asyncio
import re
import subprocess
from datetime import datetime
from pathlib import Path


def _notify(title: str, message: str) -> None:
    subprocess.run(
        ["osascript", "-e",
         f'display notification "{message}" with title "{title}"'],
        capture_output=True,
    )

LOGSEQ_PAGES_DIR = Path("~/VGS/Notes/pages").expanduser()
INDEX_PATH = Path("~/.claude/vault_index.npz").expanduser()
MODEL_NAME = "all-MiniLM-L6-v2"
MIN_SCORE = 0.38
MAX_FILE_CHARS = 1500
CATEGORIES = {"patterns", "mistakes", "decisions", "context"}
STALENESS_DAYS = {"fresh": 7, "aging": 14, "stale": 30}

_TYPE_LABEL = {
    "patterns":  "Pattern",
    "mistakes":  "Mistake",
    "decisions": "Decision",
    "context":   "Context",
}
_TYPE_PREFIX = {
    "patterns":  "pattern-",
    "mistakes":  "mistake-",
    "decisions": "decision-",
    "context":   "context-",
}

# ---------------------------------------------------------------------------
# Index + model — loaded once at first use
# ---------------------------------------------------------------------------

_model = None
_embeddings = None
_paths = None


def _ensure_loaded() -> bool:
    global _model, _embeddings, _paths
    if _embeddings is not None:
        return True
    try:
        import numpy as np
        from sentence_transformers import SentenceTransformer
    except ImportError:
        return False
    if not INDEX_PATH.exists():
        return False
    data = np.load(INDEX_PATH, allow_pickle=True)
    _embeddings = data["embeddings"]
    _paths = data["paths"]
    _model = SentenceTransformer(MODEL_NAME)
    return True


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def _staleness_label(path: Path) -> str:
    text = path.read_text(errors="replace")
    for line in text.splitlines()[:8]:
        if line.startswith("last-updated::"):
            date_str = line.split("::", 1)[1].strip().strip("[]")
            try:
                updated = datetime.strptime(date_str, "%Y/%m/%d")
                age = (datetime.now() - updated).days
                if age <= STALENESS_DAYS["fresh"]:
                    return "fresh"
                elif age <= STALENESS_DAYS["aging"]:
                    return "aging"
                elif age <= STALENESS_DAYS["stale"]:
                    return "stale"
                else:
                    return f"abandoned ({age}d)"
            except ValueError:
                pass
        if line.startswith("date::"):
            date_str = line.split("::", 1)[1].strip().strip("[]")
            try:
                created = datetime.strptime(date_str, "%Y/%m/%d")
                age = (datetime.now() - created).days
                if age <= STALENESS_DAYS["fresh"]:
                    return "fresh"
                elif age <= STALENESS_DAYS["aging"]:
                    return "aging"
                elif age <= STALENESS_DAYS["stale"]:
                    return "stale"
                else:
                    return f"abandoned ({age}d)"
            except ValueError:
                pass
    return "unknown"


def _search(query: str, top_k: int, category: str | None) -> list[dict]:
    import numpy as np
    if not _ensure_loaded():
        return []
    q = _model.encode([query], normalize_embeddings=True)[0].astype("float32")
    scores = _embeddings @ q
    results = []
    for idx in scores.argsort()[::-1]:
        score = float(scores[idx])
        if score < MIN_SCORE:
            break
        path = Path(str(_paths[idx]))
        if category and path.parent.name != category:
            continue
        if path.exists():
            results.append({
                "path": str(path),
                "score": round(score, 3),
                "staleness": _staleness_label(path),
                "content": path.read_text(errors="replace")[:MAX_FILE_CHARS],
            })
        if len(results) >= top_k:
            break
    return results


def _read_page(path_str: str) -> str:
    p = Path(path_str).expanduser()
    if not p.exists():
        return f"File not found: {path_str}"
    return p.read_text(errors="replace")


def _make_slug(title: str) -> str:
    slug = title.lower()
    slug = re.sub(r"[^a-z0-9\s-]", "", slug)
    slug = re.sub(r"\s+", "-", slug.strip())
    return re.sub(r"-+", "-", slug)[:60]


def _format_detail(detail: str) -> str:
    """Convert detail text to Logseq outline block format.

    First line → '  - {line}' (depth-1 block under ## Detail)
    Subsequent lines → '    - {line}' (depth-2 child block)
    Leading '- ' stripped from each line before adding the block prefix.
    """
    lines = [l.strip() for l in detail.strip().splitlines() if l.strip()]
    if not lines:
        return "  - (no detail)"
    result = []
    for i, line in enumerate(lines):
        text = line[2:].strip() if line.startswith("- ") else line
        prefix = "  - " if i == 0 else "    - "
        result.append(f"{prefix}{text}")
    return "\n".join(result)


def _sanitize(text: str) -> str:
    """Sanitize text for Logseq round-trip safety."""
    text = re.sub(r'(?<!\`)#(\d)', r'`#\1`', text)
    text = text.replace("{{", "`{{").replace("}}", "}}`")
    return text


def _strip_backtick_spans(line: str) -> str:
    """Remove backtick-wrapped spans so lint doesn't flag escaped content."""
    return re.sub(r'`[^`]+`', '', line)


def _check_line(line: str, lineno: int, is_property_zone: bool) -> list[str]:
    """Check a single line for Logseq format violations."""
    issues = []
    stripped = _strip_backtick_spans(line)
    if re.search(r'#\d', stripped) and not line.startswith("tags::"):
        issues.append(f"line {lineno}: bare #digit (creates phantom tag page)")
    if "{{" in stripped and not line.strip().startswith("```"):
        issues.append(f"line {lineno}: unescaped {{{{ macro")
    if is_property_zone and re.match(r'^(title|type|date|tags|project|session|last-updated|status):(?!:)', line):
        issues.append(f"line {lineno}: single-colon property (should be ::)")
    return issues


def _verify_page(filepath: Path) -> list[str]:
    """Post-write verification — check a single file for format violations."""
    issues = []
    text = filepath.read_text(errors="replace")
    lines = text.splitlines()

    backtick_count = text.count("`")
    if backtick_count % 2 != 0:
        issues.append("odd backtick count (unclosed inline code)")

    for i, line in enumerate(lines, 1):
        issues.extend(_check_line(line, i, is_property_zone=(i <= 8)))

    return issues


def _lint_vault(category: str | None, limit: int) -> list[dict]:
    """Scan vault files for Logseq format violations."""
    dirs = (
        [LOGSEQ_PAGES_DIR / "claude" / category] if category
        else [LOGSEQ_PAGES_DIR / "claude" / c for c in CATEGORIES]
    )
    if category and category not in CATEGORIES:
        return [{"error": f"Unknown category '{category}'"}]

    findings: list[dict] = []
    scanned = 0
    for d in dirs:
        if not d.exists():
            continue
        for p in sorted(d.glob("*.md")):
            scanned += 1
            text = p.read_text(errors="replace")
            lines = text.splitlines()
            issues = []

            backtick_count = text.count("`")
            if backtick_count % 2 != 0:
                issues.append("odd backtick count (unclosed inline code)")

            has_title = False
            has_type = False
            has_date = False
            for i, line in enumerate(lines, 1):
                if line.startswith("title::"):
                    has_title = True
                if line.startswith("type::"):
                    has_type = True
                if line.startswith("date::"):
                    has_date = True
                issues.extend(_check_line(line, i, is_property_zone=(i <= 8)))

            if not has_title:
                issues.append("missing title:: property")
            if not has_type:
                issues.append("missing type:: property")
            if not has_date:
                issues.append("missing date:: property")

            if issues:
                findings.append({
                    "path": str(p),
                    "name": p.name,
                    "issues": issues,
                })
                if len(findings) >= limit:
                    break
        if len(findings) >= limit:
            break

    return [{"scanned": scanned, "findings_count": len(findings), "findings": findings}]


def _write_insight(
    insight_type: str,
    title: str,
    summary: str,
    detail: str,
    tags: list[str] | None = None,
    project: str | None = None,
    session: str | None = None,
) -> str:
    if insight_type not in CATEGORIES:
        return f"Invalid type '{insight_type}'. Must be one of: {', '.join(sorted(CATEGORIES))}"

    prefix = _TYPE_PREFIX[insight_type]
    slug = _make_slug(title)
    subdir = LOGSEQ_PAGES_DIR / "claude" / insight_type
    subdir.mkdir(parents=True, exist_ok=True)

    # Near-match dedup: exact slug match always blocks; prefix match needs
    # ≥3 slug segments to avoid false positives (e.g. "access-logger" matching
    # unrelated pages like "access-logger-rocksdb-state-dir-footprint").
    slug_parts = slug.split("-")
    existing = list(subdir.glob(f"{prefix}{slug}.md"))
    if not existing and len(slug_parts) >= 3:
        prefix_key = "-".join(slug_parts[:3])
        existing = list(subdir.glob(f"{prefix}{prefix_key}*.md"))
    if existing:
        return f"Skipped — similar page already exists: {existing[0].name}"

    filepath = subdir / f"{prefix}{slug}.md"
    today = datetime.now().strftime("%Y/%m/%d")
    tags_str = " ".join(f"[[{t}]]" for t in (tags or []))
    project_str = project or "VGS"
    session_str = session or f"Session {datetime.now().strftime('%Y-%m-%d')} — manual"
    type_label = _TYPE_LABEL[insight_type]

    title_safe = _sanitize(title)
    summary_safe = _sanitize(summary.strip())
    detail_safe = _sanitize(detail)

    content = (
        f"title:: {type_label}: {title_safe}\n"
        f"type:: [[{type_label.lower()}]]\n"
        f"date:: [[{today}]]\n"
        f"last-updated:: [[{today}]]\n"
        f"project:: [[{project_str}]]\n"
        f"session:: [[{session_str}]]\n"
        f"tags:: {tags_str}\n"
        f"\n"
        f"- ## Summary\n"
        f"  - {summary_safe}\n"
        f"\n"
        f"- ## Detail\n"
        f"{_format_detail(detail_safe)}\n"
    )
    filepath.write_text(content, encoding="utf-8")

    verify_issues = _verify_page(filepath)
    if verify_issues:
        return f"Written: {filepath}\n⚠️ Post-write issues: {'; '.join(verify_issues)}"
    return f"Written: {filepath}"


def _list_recent(category: str | None, limit: int) -> list[dict]:
    dirs = (
        [LOGSEQ_PAGES_DIR / "claude" / category] if category
        else [LOGSEQ_PAGES_DIR / "claude" / c for c in CATEGORIES]
    )
    if category and category not in CATEGORIES:
        return [{"error": f"Unknown category '{category}'"}]

    files = []
    for d in dirs:
        if d.exists():
            files.extend(d.glob("*.md"))
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)

    results = []
    for p in files[:limit]:
        mtime = datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
        title = p.stem
        try:
            for line in p.read_text(errors="replace").splitlines()[:5]:
                if line.startswith("title::"):
                    title = line.split("::", 1)[1].strip()
                    break
        except OSError:
            pass
        results.append({"path": str(p), "title": title, "modified": mtime})
    return results


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------

async def serve() -> None:
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    import mcp.types as types

    server = Server("logseq-vault")

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return [
            types.Tool(
                name="search_vault",
                description=(
                    "Semantic similarity search over the Logseq vault. Returns the most "
                    "relevant insight pages (patterns, mistakes, decisions, context). "
                    "Use mid-conversation when the topic shifts or you need a targeted query."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Search query — be specific for better results",
                        },
                        "top_k": {
                            "type": "integer",
                            "default": 5,
                            "description": "Maximum number of results to return",
                        },
                        "category": {
                            "type": "string",
                            "enum": ["patterns", "mistakes", "decisions", "context"],
                            "description": "Restrict search to a single category (optional)",
                        },
                    },
                    "required": ["query"],
                },
            ),
            types.Tool(
                name="read_page",
                description="Read the full content of a vault page by its file path.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Absolute path to the .md file (from a search_vault result)",
                        },
                    },
                    "required": ["path"],
                },
            ),
            types.Tool(
                name="write_insight",
                description=(
                    "Create a new Logseq insight page in the vault immediately, "
                    "without waiting for the session to end. Use this to capture "
                    "a non-obvious learning, fix, or decision as it happens."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "type": {
                            "type": "string",
                            "enum": ["patterns", "mistakes", "decisions", "context"],
                            "description": "Insight category",
                        },
                        "title": {
                            "type": "string",
                            "description": "Short descriptive title — becomes the file slug",
                        },
                        "summary": {
                            "type": "string",
                            "description": "One-sentence summary of the insight",
                        },
                        "detail": {
                            "type": "string",
                            "description": (
                                "Full explanation. First line is the intro sentence; "
                                "each subsequent line becomes a child bullet in Logseq outline format. "
                                "Example: 'Context sentence.\\n- Step one\\n- Step two'"
                            ),
                        },
                        "tags": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Logseq tags (optional)",
                        },
                        "project": {
                            "type": "string",
                            "description": "Project name (default: VGS)",
                        },
                    },
                    "required": ["type", "title", "summary", "detail"],
                },
            ),
            types.Tool(
                name="list_recent",
                description="List recently modified vault pages, optionally filtered by category.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "category": {
                            "type": "string",
                            "enum": ["patterns", "mistakes", "decisions", "context"],
                            "description": "Filter to a single category (optional)",
                        },
                        "limit": {
                            "type": "integer",
                            "default": 20,
                            "description": "Maximum number of pages to return",
                        },
                    },
                },
            ),
            types.Tool(
                name="lint_vault",
                description=(
                    "Scan vault files for Logseq format violations: odd backtick counts, "
                    "bare #digits (phantom tag pages), unescaped {{ macros, single-colon "
                    "properties, and missing required properties (title, type, date)."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "category": {
                            "type": "string",
                            "enum": ["patterns", "mistakes", "decisions", "context"],
                            "description": "Restrict scan to a single category (optional)",
                        },
                        "limit": {
                            "type": "integer",
                            "default": 50,
                            "description": "Maximum number of files with issues to return",
                        },
                    },
                },
            ),
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
        if name == "search_vault":
            results = _search(
                query=arguments["query"],
                top_k=int(arguments.get("top_k", 5)),
                category=arguments.get("category"),
            )
            if not results:
                _notify("🔍 Logseq Vault", "search_vault — no matches")
                text = "No matches above similarity threshold."
            else:
                _notify("🔍 Logseq Vault", f"search_vault — {len(results)} match(es)")
                parts = [
                    f"### {Path(r['path']).stem} ({int(r['score'] * 100)}%) [{r['staleness']}]\n"
                    f"**Path:** {r['path']}\n\n{r['content']}"
                    for r in results
                ]
                text = "\n\n---\n\n".join(parts)
            return [types.TextContent(type="text", text=text)]

        elif name == "read_page":
            return [types.TextContent(type="text", text=_read_page(arguments["path"]))]

        elif name == "write_insight":
            result = _write_insight(
                insight_type=arguments["type"],
                title=arguments["title"],
                summary=arguments["summary"],
                detail=arguments["detail"],
                tags=arguments.get("tags"),
                project=arguments.get("project"),
            )
            skipped = result.startswith("Skipped")
            icon = "⏭️" if skipped else "✍️"
            _notify(f"{icon} Logseq Vault", f"write_insight — {arguments['title'][:50]}")
            return [types.TextContent(type="text", text=result)]

        elif name == "list_recent":
            items = _list_recent(
                category=arguments.get("category"),
                limit=int(arguments.get("limit", 20)),
            )
            if not items or (len(items) == 1 and "error" in items[0]):
                text = items[0].get("error", "No pages found.") if items else "No pages found."
            else:
                lines = [
                    f"- {r['modified']}  {r['title']}\n  {r['path']}"
                    for r in items
                ]
                text = "\n".join(lines)
            return [types.TextContent(type="text", text=text)]

        elif name == "lint_vault":
            results = _lint_vault(
                category=arguments.get("category"),
                limit=int(arguments.get("limit", 50)),
            )
            if results and "error" in results[0]:
                text = results[0]["error"]
            else:
                info = results[0]
                if info["findings_count"] == 0:
                    text = f"Scanned {info['scanned']} files — no issues found."
                else:
                    lines = [f"Scanned {info['scanned']} files — {info['findings_count']} with issues:\n"]
                    for f in info["findings"]:
                        lines.append(f"**{f['name']}**")
                        for issue in f["issues"]:
                            lines.append(f"  - {issue}")
                    text = "\n".join(lines)
            _notify("🔍 Logseq Vault", f"lint_vault — {results[0].get('findings_count', '?')} files with issues")
            return [types.TextContent(type="text", text=text)]

        return [types.TextContent(type="text", text=f"Unknown tool: {name}")]

    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


if __name__ == "__main__":
    asyncio.run(serve())
