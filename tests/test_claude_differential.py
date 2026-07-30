import importlib.util
import subprocess
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

import logseq_memory_extractor as candidate


BASELINE_COMMIT = "b630a63"


class FrozenDate(date):
    @classmethod
    def today(cls):
        return cls(2026, 7, 30)


def load_baseline(root: Path):
    source = subprocess.run(
        ["git", "show", f"{BASELINE_COMMIT}:logseq_memory_extractor.py"],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout
    path = root / "legacy_logseq_memory_extractor.py"
    path.write_text(source)
    spec = importlib.util.spec_from_file_location("legacy_extractor", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def snapshot_tree(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def linked_values(value: bytes) -> bytes:
    values = [part.strip() for part in value.split(b",") if part.strip()]
    return b", ".join(
        item if item.startswith(b"[[") and item.endswith(b"]]") else b"[[" + item + b"]]"
        for item in values
    )


def current_tree_from_baseline(tree: dict[str, bytes]) -> dict[str, bytes]:
    expected = {}
    page_roots = (
        "claude/patterns/",
        "claude/mistakes/",
        "claude/decisions/",
        "claude/context/",
        "claude/sessions/",
    )
    for path, content in tree.items():
        if not path.startswith(page_roots):
            expected[path] = content
            continue
        lines = content.splitlines(keepends=True)
        for index, line in enumerate(lines):
            if line.startswith(b"date:: "):
                lines.insert(index + 1, b"last-updated:: " + line[len(b"date:: ") :])
                break
        for index, line in enumerate(lines):
            for property_name in (b"creator:: ", b"model:: "):
                if line.startswith(property_name):
                    suffix = b"\n" if line.endswith(b"\n") else b""
                    value = line[len(property_name) :].rstrip(b"\n")
                    lines[index] = property_name + linked_values(value) + suffix
                    break
        expected[path] = b"".join(lines)
    return expected


class ClaudeDifferentialTests(unittest.TestCase):
    def test_refactor_matches_frozen_baseline_with_authorized_properties(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = load_baseline(root)
            raw = "\n".join(
                [
                    '{"type":"user","message":{"role":"user","content":[{"type":"text","text":"Investigate retries"}]}}',
                    '{"type":"assistant","message":{"role":"assistant","model":"claude-sonnet-4-6","content":[{"type":"text","text":"Verified live state"}]}}',
                    "{malformed",
                ]
            )
            self.assertEqual(candidate.EXTRACTION_PROMPT, legacy.EXTRACTION_PROMPT)
            self.assertEqual(
                candidate.extract_conversation_title(raw),
                legacy.extract_conversation_title(raw),
            )
            self.assertEqual(candidate.extract_models(raw), legacy.extract_models(raw))
            self.assertEqual(candidate.parse_transcript(raw), legacy.parse_transcript(raw))

            insights = {
                "patterns": [
                    {
                        "slug": "verify-live-state",
                        "summary": "Verify before retrying.",
                        "detail": "Check the condition.\n    - Retry once",
                        "tags": ["ops"],
                    }
                ],
                "mistakes": [],
                "decisions": [],
                "context": [],
                "session_summary": "Verified the workflow.",
            }
            roots = []
            for module, name in ((legacy, "legacy"), (candidate, "candidate")):
                pages = root / name
                roots.append(pages)
                with (
                    mock.patch.object(module, "LOGSEQ_PAGES_DIR", pages),
                    mock.patch.object(module, "date", FrozenDate),
                    mock.patch.object(module, "_update_vault_index"),
                ):
                    links = module.write_pages(
                        insights,
                        "demo",
                        "abcdef12",
                        "2026-07-30-abcdef12",
                        "Session 2026-07-30 abcdef12 — demo",
                        ["claude-sonnet-4-6"],
                    )
                    module.write_session(
                        insights,
                        "demo",
                        "abcdef12",
                        "2026-07-30-abcdef12",
                        links,
                        "Investigate retries",
                        ["claude-sonnet-4-6"],
                    )
                    module.update_index()
                    module.write_digest()

            self.assertEqual(
                current_tree_from_baseline(snapshot_tree(roots[0])),
                snapshot_tree(roots[1]),
            )


if __name__ == "__main__":
    unittest.main()
