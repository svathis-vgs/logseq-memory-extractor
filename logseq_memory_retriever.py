#!/usr/bin/env python3
"""
UserPromptSubmit hook: retrieves semantically relevant vault pages for the user's prompt.

Claude Code calls this hook before every user message, passing a JSON payload on stdin.
This script embeds the prompt, searches the vault index, and prints matching file
contents to stdout — which Claude Code injects into the next model context as a
system reminder.

Prerequisites:
  1. Run logseq_memory_index.py once to build ~/.claude/vault_index.npz
  2. Register this script as a UserPromptSubmit hook in ~/.claude/settings.json
     (see README for the exact snippet)

Requirements:
    pip install sentence-transformers numpy
"""

import json
import os
import sys
from pathlib import Path

INDEX_PATH = Path("~/.claude/vault_index.npz").expanduser()
MODEL_NAME = "all-MiniLM-L6-v2"
TOP_K = 5
MIN_SCORE = 0.38   # cosine similarity floor — results below this are noise
MAX_FILE_CHARS = 600  # chars per result (keeps total injection under ~8k chars)


def retrieve(query: str) -> list[dict]:
    try:
        import numpy as np
        from sentence_transformers import SentenceTransformer
    except ImportError:
        return []

    if not INDEX_PATH.exists():
        return []

    try:
        data = np.load(INDEX_PATH, allow_pickle=True)
        embeddings: "np.ndarray" = data["embeddings"]   # (N, 384) float32, pre-normalised
        paths: "np.ndarray" = data["paths"]
    except Exception:
        return []

    model = SentenceTransformer(MODEL_NAME)
    query_vec = model.encode([query], normalize_embeddings=True)[0].astype("float32")

    # Pre-normalised embeddings: dot product == cosine similarity
    scores = embeddings @ query_vec
    top_indices = scores.argsort()[::-1][:TOP_K]

    results = []
    for idx in top_indices:
        score = float(scores[idx])
        if score < MIN_SCORE:
            break
        p = Path(str(paths[idx]))
        if p.exists():
            results.append({
                "path": p,
                "score": score,
                "content": p.read_text(errors="replace")[:MAX_FILE_CHARS],
            })
    return results


def main() -> None:
    # Don't run inside the extractor's claude subprocess (recursion guard)
    if os.environ.get("LOGSEQ_EXTRACTOR_RUNNING"):
        sys.exit(0)

    # Honour manual disable flag (set via /vault-off skill)
    if Path("~/.claude/vault_retriever_disabled").expanduser().exists():
        sys.exit(0)

    # Parse hook payload — Claude Code writes JSON on stdin for UserPromptSubmit
    raw = sys.stdin.read()
    query = ""
    try:
        payload = json.loads(raw)
        # Try the documented field path first, then common fallbacks
        query = (
            (payload.get("tool_input") or {}).get("user_message")
            or payload.get("user_message")
            or payload.get("message")
            or payload.get("prompt")
            or ""
        )
    except (json.JSONDecodeError, AttributeError):
        query = raw.strip()

    if not query or len(query.strip()) < 15:
        sys.exit(0)

    try:
        results = retrieve(query.strip())
    except Exception:
        sys.exit(0)  # Never interrupt the user's session on failure

    if not results:
        sys.exit(0)

    def extract_title(content: str, fallback: str) -> str:
        for line in content.splitlines():
            if line.startswith("title::"):
                return line.split("::", 1)[1].strip()
        return fallback

    lines = [f"🔍 *Vault index consulted — {len(results)} match(es) above {int(MIN_SCORE * 100)}% threshold*\n",
             "## Relevant vault memories (semantic search)\n"]
    for r in results:
        title = extract_title(r["content"], r["path"].stem)
        pct = int(r["score"] * 100)
        lines.append(f"### {title} ({pct}% match) <!-- path:{r['path']} -->\n")
        lines.append(r["content"])
        lines.append("")

    print("\n".join(lines))


if __name__ == "__main__":
    main()
