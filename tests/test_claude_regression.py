import contextlib
import hashlib
import io
import json
import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import logseq_memory_extractor as extractor
import logseq_memory_index as indexer
import logseq_memory_mcp as mcp
import logseq_memory_retriever as retriever


FIXED_DATE = date(2026, 7, 30)


class FrozenDate(date):
    @classmethod
    def today(cls):
        return cls(2026, 7, 30)


def claude_entry(role, content, model=None, entry_type=None):
    message = {"role": role, "content": content}
    if model is not None:
        message["model"] = model
    return json.dumps({"type": entry_type or role, "message": message})


class ClaudePromptContractTests(unittest.TestCase):
    def test_prompt_is_frozen(self):
        self.assertEqual(len(extractor.EXTRACTION_PROMPT), 2095)
        self.assertEqual(
            hashlib.sha256(extractor.EXTRACTION_PROMPT.encode()).hexdigest(),
            "a2eaeb4b2bee9adb1baeed09b4efd843c1588c3824339ad966d59896807642fe",
        )

    def test_call_claude_preserves_command_environment_and_schema(self):
        response = {
            "patterns": [],
            "mistakes": [],
            "decisions": [],
            "context": [],
            "session_summary": "done",
        }
        completed = SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"structured_output": response}),
            stderr="",
        )
        inherited = {
            "PATH": "/bin",
            "HTTP_PROXY": "http://proxy",
            "https_proxy": "http://proxy",
            "CLOUDSDK_PROXY_ADDRESS": "proxy",
            "CLAUDECODE": "1",
            "CLAUDE_CODE_ENTRYPOINT": "desktop",
            "KEEP_ME": "yes",
        }
        with (
            mock.patch.object(extractor, "CLAUDE_BIN", "/fake/claude"),
            mock.patch.dict(os.environ, inherited, clear=True),
            mock.patch.object(extractor.subprocess, "run", return_value=completed) as run,
        ):
            self.assertEqual(extractor.call_claude("User: test"), response)

        args, kwargs = run.call_args
        command = args[0]
        self.assertEqual(command[:4], ["/fake/claude", "-p", "--no-session-persistence", "--output-format"])
        self.assertEqual(command[4], "json")
        self.assertEqual(command[5], "--json-schema")
        schema = json.loads(command[6])
        self.assertEqual(
            schema["required"],
            ["patterns", "mistakes", "decisions", "context", "session_summary"],
        )
        self.assertEqual(kwargs["input"], extractor.EXTRACTION_PROMPT + "User: test")
        self.assertEqual(kwargs["timeout"], 300)
        self.assertTrue(kwargs["text"])
        self.assertEqual(kwargs["env"]["LOGSEQ_EXTRACTOR_RUNNING"], "1")
        self.assertEqual(kwargs["env"]["KEEP_ME"], "yes")
        for removed in (
            "HTTP_PROXY",
            "https_proxy",
            "CLOUDSDK_PROXY_ADDRESS",
            "CLAUDECODE",
            "CLAUDE_CODE_ENTRYPOINT",
        ):
            self.assertNotIn(removed, kwargs["env"])

    def test_call_claude_parsing_fallbacks_are_stable(self):
        empty = {
            "patterns": [],
            "mistakes": [],
            "decisions": [],
            "context": [],
            "session_summary": "",
        }
        cases = [
            (json.dumps({"result": "prefix " + json.dumps(empty) + " suffix"}), empty),
            ("```json\n" + json.dumps(empty) + "\n```", empty),
            (json.dumps([1, 2]), empty),
        ]
        for stdout, expected in cases:
            completed = SimpleNamespace(returncode=0, stdout=stdout, stderr="")
            with mock.patch.object(extractor.subprocess, "run", return_value=completed):
                self.assertEqual(extractor.call_claude("transcript"), expected)


class ClaudeTranscriptContractTests(unittest.TestCase):
    def test_title_models_and_text_parsing(self):
        raw = "\n".join(
            [
                "{malformed",
                claude_entry("user", [{"type": "text", "text": "First request\ncontinued"}]),
                claude_entry("assistant", [{"type": "tool_use", "name": "Bash"}], "model-a"),
                claude_entry("assistant", [{"type": "text", "text": "Result"}], "model-a"),
                claude_entry("assistant", "Second result", "model-b"),
                claude_entry("assistant", "Synthetic", "<synthetic>"),
            ]
        )
        self.assertEqual(extractor.extract_conversation_title(raw), "First request continued")
        self.assertEqual(extractor.extract_models(raw), ["model-a", "model-b"])
        self.assertEqual(
            extractor.parse_transcript(raw),
            "User: First request\ncontinued\nAssistant: Result\nAssistant: Second result\nAssistant: Synthetic",
        )

    def test_transcript_keeps_last_12000_characters(self):
        raw = claude_entry("user", "A" * 13_000)
        parsed = extractor.parse_transcript(raw)
        self.assertEqual(len(parsed), extractor.MAX_TRANSCRIPT_CHARS)
        self.assertEqual(parsed, "A" * extractor.MAX_TRANSCRIPT_CHARS)

    def test_dayflow_detection_is_frozen(self):
        self.assertTrue(extractor._is_dayflow_session("Screen recording review", "done"))
        self.assertFalse(extractor._is_dayflow_session("Kafka review", "done"))


class ClaudePageContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.pages = Path(self.tmp.name) / "pages"
        self.pages_patch = mock.patch.object(extractor, "LOGSEQ_PAGES_DIR", self.pages)
        self.date_patch = mock.patch.object(extractor, "date", FrozenDate)
        self.pages_patch.start()
        self.date_patch.start()

    def tearDown(self):
        self.date_patch.stop()
        self.pages_patch.stop()
        self.tmp.cleanup()

    def test_insight_renderer_is_byte_frozen(self):
        actual = extractor._page_content(
            type_="[[pattern]]",
            title="Pattern: Retry Safely",
            summary="Retry only after verification.",
            detail="Check the live condition.\n    - Retry once",
            tags=["incident", "retry"],
            project="demo",
            session_title="Session 2026-07-30 abcdef12 — demo",
            models=["claude-sonnet-4-6", "claude-opus-4-7"],
        )
        expected = """title:: Pattern: Retry Safely
type:: [[pattern]]
date:: [[2026/07/30]]
project:: [[demo]]
session:: [[Session 2026-07-30 abcdef12 — demo]]
creator:: claude
model:: claude-sonnet-4-6, claude-opus-4-7
tags:: [[incident]] [[retry]]

- ## Summary
  - Retry only after verification.

- ## Detail
  - Check the live condition.
    - Retry once
"""
        self.assertEqual(actual, expected)

    def test_write_pages_preserves_two_word_dedup_and_links(self):
        existing = self.pages / "claude" / "patterns" / "pattern-kafka-rebalance-storm.md"
        existing.parent.mkdir(parents=True)
        existing.write_text("existing")
        insights = {
            "patterns": [
                {
                    "slug": "kafka-rebalance-recovery",
                    "summary": "duplicate prefix",
                    "detail": "skip",
                    "tags": [],
                },
                {
                    "slug": "verify-live-state",
                    "summary": "new",
                    "detail": "write",
                    "tags": ["ops"],
                },
            ]
        }
        with mock.patch.object(extractor, "_update_vault_index") as update:
            links = extractor.write_pages(
                insights,
                "demo",
                "abcdef12",
                "2026-07-30-abcdef12",
                "Session 2026-07-30 abcdef12 — demo",
                ["claude-sonnet-4-6"],
            )
        self.assertEqual(
            links,
            ["[[Pattern: Kafka Rebalance Recovery]]", "[[Pattern: Verify Live State]]"],
        )
        self.assertFalse(
            (existing.parent / "pattern-kafka-rebalance-recovery.md").exists()
        )
        written = existing.parent / "pattern-verify-live-state.md"
        self.assertTrue(written.exists())
        update.assert_called_once_with([written])

    def test_session_renderer_is_byte_frozen(self):
        insights = {"session_summary": "Completed the investigation."}
        extractor.write_session(
            insights,
            "demo",
            "abcdef12",
            "2026-07-30-abcdef12",
            ["[[Pattern: Retry Safely]]"],
            "Investigate retries",
            ["claude-sonnet-4-6"],
        )
        path = (
            self.pages
            / "claude"
            / "sessions"
            / "2026_07_30"
            / "2026-07-30-abcdef12.md"
        )
        expected = """title:: Session 2026-07-30 abcdef12 — demo
description:: Investigate retries
type:: [[session]]
date:: [[2026/07/30]]
project:: [[demo]]
session:: [[Session 2026-07-30 abcdef12 — demo]]
creator:: claude
model:: claude-sonnet-4-6
exclude-from-graph-view:: true

- ## Summary
  - Completed the investigation.

- ## Insights
  - [[Pattern: Retry Safely]]
"""
        self.assertEqual(path.read_text(), expected)

    def test_digest_and_index_are_byte_frozen(self):
        page = self.pages / "claude" / "patterns" / "pattern-retry.md"
        page.parent.mkdir(parents=True)
        page.write_text(
            "title:: Pattern: Retry Safely\n"
            "type:: [[pattern]]\n"
            "date:: [[2026/07/29]]\n\n"
            "- ## Summary\n"
            "  - Verify before retrying.\n"
        )
        extractor.write_digest()
        digest = (self.pages / "claude" / "digest.md").read_text()
        self.assertEqual(
            digest,
            """# Claude Code Memory Digest
_Updated: 2026-07-30 — 1 patterns, 0 mistakes, 0 decisions, 0 context_

Read this at session start to recall accumulated insights.

## Patterns
- **Retry Safely** (2026-07-29) — Verify before retrying.
""",
        )
        extractor.update_index()
        index = (self.pages / "claude" / "index.md").read_text()
        self.assertIn("- # Claude Code Memory Index", index)
        self.assertIn("query-properties:: [:title :date :project :tags]", index)
        self.assertTrue(index.startswith("updated:: [[2026/07/30]]\n"))


class AdjacentClaudeComponentTests(unittest.TestCase):
    def test_index_extract_text_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "page.md"
            path.write_text(
                "title:: Pattern: Verify Live\n"
                "- ## Summary\n"
                "  - First fact\n"
                "  - Second fact\n"
                "- ## Detail\n"
            )
            self.assertEqual(
                indexer.extract_text(path),
                "Pattern: Verify Live. First fact Second fact",
            )
            self.assertEqual(
                extractor._index_extract_text(path),
                "Pattern: Verify Live. First fact Second fact",
            )

    def test_retriever_no_index_is_stable(self):
        with mock.patch.object(retriever, "INDEX_PATH", Path("/does/not/exist")):
            self.assertEqual(retriever.retrieve("anything"), [])

    def test_manual_mcp_writer_is_byte_frozen(self):
        with tempfile.TemporaryDirectory() as tmp:
            pages = Path(tmp) / "pages"
            with (
                mock.patch.object(mcp, "LOGSEQ_PAGES_DIR", pages),
                mock.patch.object(mcp, "datetime") as dt,
            ):
                dt.now.return_value = SimpleNamespace(
                    strftime=lambda fmt: "2026/07/30"
                    if fmt == "%Y/%m/%d"
                    else "2026-07-30"
                )
                result = mcp._write_insight(
                    "decisions",
                    "Use SessionEnd",
                    "Use the lifecycle event.",
                    "It captures the final task.\n- Keep provenance",
                    "claude-sonnet-4-6",
                    ["codex", "hooks"],
                    "demo",
                )
            self.assertTrue(result.startswith("Written: "))
            path = next((pages / "claude" / "decisions").glob("*.md"))
            content = path.read_text()
            self.assertIn("creator:: claude\nmodel:: claude-sonnet-4-6\n", content)
            self.assertIn("last-updated:: [[2026/07/30]]", content)
            self.assertIn("  - It captures the final task.\n    - Keep provenance\n", content)

    def test_main_recursion_guard_exits_without_reading_stdin(self):
        with (
            mock.patch.dict(os.environ, {"LOGSEQ_EXTRACTOR_RUNNING": "1"}),
            mock.patch("sys.stdin", io.StringIO("not json")),
        ):
            with self.assertRaises(SystemExit) as raised:
                extractor.main()
        self.assertEqual(raised.exception.code, 0)

    def test_short_main_session_is_silent_success(self):
        payload = json.dumps(
            {"session_id": "abc", "cwd": "/tmp/demo", "transcript_path": None}
        )
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch("sys.stdin", io.StringIO(payload)),
            mock.patch.object(extractor, "find_transcript", return_value=claude_entry("user", "short")),
            mock.patch.object(extractor, "call_claude") as call,
        ):
            with self.assertRaises(SystemExit) as raised:
                extractor.main()
        self.assertEqual(raised.exception.code, 0)
        call.assert_not_called()


if __name__ == "__main__":
    unittest.main()
