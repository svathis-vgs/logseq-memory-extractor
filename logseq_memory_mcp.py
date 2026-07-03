#!/usr/bin/env python3
"""
Logseq vault MCP server — on-demand semantic search and write access.

Exposes four tools:
  search_vault   — semantic similarity search over vault_index.npz
  read_page      — read the full content of a vault page by path
  write_insight  — create a new Logseq insight page mid-session
  list_recent    — list recently modified vault pages by category

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
from datetime import datetime
from pathlib import Path

LOGSEQ_PAGES_DIR = Path("~/VGS/Notes/pages").expanduser()
INDEX_PATH = Path("~/.claude/vault_index.npz").expanduser()
MODEL_NAME = "all-MiniLM-L6-v2"
MIN_SCORE = 0.38
MAX_FILE_CHARS = 1500
CATEGORIES = {"patterns", "mistakes", "decisions", "context"}

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

    # Near-match dedup — same 2-word slug prefix already exists → skip
    slug_parts = slug.split("-")
    prefix_key = "-".join(slug_parts[:2]) if len(slug_parts) >= 2 else slug
    existing = list(subdir.glob(f"{prefix}{prefix_key}*.md"))
    if existing:
        return f"Skipped — similar page already exists: {existing[0].name}"

    filepath = subdir / f"{prefix}{slug}.md"
    today = datetime.now().strftime("%Y/%m/%d")
    tags_str = " ".join(f"[[{t}]]" for t in (tags or []))
    project_str = project or "VGS"
    session_str = session or f"Session {datetime.now().strftime('%Y-%m-%d')} — manual"
    type_label = _TYPE_LABEL[insight_type]

    content = (
        f"title:: {type_label}: {title}\n"
        f"type:: [[{type_label.lower()}]]\n"
        f"date:: [[{today}]]\n"
        f"project:: [[{project_str}]]\n"
        f"session:: [[{session_str}]]\n"
        f"tags:: {tags_str}\n"
        f"\n"
        f"- ## Summary\n"
        f"  - {summary.strip()}\n"
        f"\n"
        f"- ## Detail\n"
        f"{_format_detail(detail)}\n"
    )
    filepath.write_text(content, encoding="utf-8")
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
                text = "No matches above similarity threshold."
            else:
                parts = [
                    f"### {Path(r['path']).stem} ({int(r['score'] * 100)}%)\n"
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

        return [types.TextContent(type="text", text=f"Unknown tool: {name}")]

    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


if __name__ == "__main__":
    asyncio.run(serve())
