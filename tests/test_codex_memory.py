import json
import os
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

import logseq_memory_codex as codex
import logseq_memory_mcp as mcp
import logseq_memory_shared as shared


def jsonl(*entries):
    return "\n".join(json.dumps(entry) for entry in entries)


def session_meta(cwd="/tmp/demo", timestamp="2026-07-29T10:00:00Z"):
    return {
        "type": "session_meta",
        "payload": {"cwd": cwd, "timestamp": timestamp},
    }


def turn(model):
    return {"type": "turn_context", "payload": {"model": model}}


def message(role, text):
    return {
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": role,
            "content": [
                {
                    "type": "input_text" if role == "user" else "output_text",
                    "text": text,
                }
            ],
        },
    }


class TempEnvironment(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.env = mock.patch.dict(
            os.environ,
            {
                "LOGSEQ_PAGES_DIR": str(self.root / "pages"),
                "LOGSEQ_INDEX_PATH": str(self.root / "vault_index.npz"),
                "LOGSEQ_LOCK_PATH": str(self.root / "vault.lock"),
                "CODEX_LOGSEQ_STATE_DIR": str(self.root / "state"),
                "CODEX_HOME": str(self.root / "codex-home"),
            },
            clear=False,
        )
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()


class CodexTranscriptTests(unittest.TestCase):
    def test_normalizes_through_shared_claude_parser(self):
        raw = jsonl(
            session_meta("/work/project"),
            turn("gpt-5.6-sol"),
            message("user", "Investigate the queue"),
            message("assistant", "Initial result"),
            turn("gpt-5.6-terra"),
            message("assistant", "Final result"),
        )
        parsed = codex.parse_transcript(raw)
        self.assertEqual(parsed.title, "Investigate the queue")
        self.assertEqual(parsed.models, ("gpt-5.6-sol", "gpt-5.6-terra"))
        self.assertEqual(parsed.started_on.isoformat(), "2026-07-29")
        self.assertEqual(parsed.cwd, "/work/project")
        self.assertEqual(
            parsed.text,
            "User: Investigate the queue\n"
            "Assistant: Initial result\n"
            "Assistant: Final result",
        )

    def test_excludes_injected_context_developer_tools_and_reasoning(self):
        raw = "\n".join(
            [
                "{malformed",
                json.dumps(session_meta()),
                json.dumps(turn("gpt-5.6-sol")),
                json.dumps(message("user", "# AGENTS.md instructions\nhidden")),
                json.dumps(
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "developer",
                            "content": [{"type": "input_text", "text": "developer"}],
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "function_call_output",
                            "output": "tool secret",
                        },
                    }
                ),
                json.dumps(message("user", "Actual task")),
                json.dumps(message("assistant", "Useful result")),
            ]
        )
        parsed = codex.parse_transcript(raw)
        self.assertEqual(parsed.title, "Actual task")
        self.assertNotIn("AGENTS", parsed.text)
        self.assertNotIn("developer", parsed.text)
        self.assertNotIn("tool secret", parsed.text)

    def test_redacts_common_secrets_and_uses_claude_truncation_limit(self):
        raw = jsonl(
            session_meta(),
            turn("gpt-5.6-sol"),
            message("user", "token=top-secret-value password=hunter2 " + "A" * 13_000),
        )
        parsed = codex.parse_transcript(raw)
        self.assertNotIn("top-secret-value", parsed.text)
        self.assertNotIn("hunter2", parsed.text)
        self.assertLessEqual(len(parsed.text), shared.MAX_TRANSCRIPT_CHARS)


class CodexWorkerTests(TempEnvironment):
    def _write_transcript(self, session_id="session-123"):
        directory = self.root / "codex-home" / "sessions" / "2026" / "07" / "29"
        directory.mkdir(parents=True)
        path = directory / f"rollout-{session_id}.jsonl"
        path.write_text(
            jsonl(
                session_meta(str(self.root / "project")),
                turn("gpt-5.6-sol"),
                message("user", "A sufficiently long task prompt " * 12),
                message("assistant", "A sufficiently long result " * 12),
            )
        )
        return path

    def test_enqueue_is_fast_private_and_idempotent(self):
        transcript = self._write_transcript()
        payload = {
            "session_id": "session-123",
            "transcript_path": str(transcript),
            "cwd": str(self.root / "project"),
            "model": "gpt-5.6-sol",
        }
        with mock.patch.object(codex.subprocess, "Popen") as popen:
            started = time.monotonic()
            self.assertEqual(codex.enqueue(payload), 0)
            self.assertLess(time.monotonic() - started, 3)
            self.assertEqual(codex.enqueue(payload), 0)
            self.assertEqual(popen.call_count, 1)
        job = self.root / "state" / "queue" / "session-123.json"
        self.assertEqual(job.stat().st_mode & 0o777, 0o600)

    def test_worker_uses_original_model_and_claude_page_contract(self):
        fake = self.root / "fake-codex"
        response = {
            "patterns": [],
            "mistakes": [],
            "decisions": [],
            "context": [
                {
                    "slug": "worker-provenance",
                    "summary": "The original model is retained.",
                    "detail": "Terra only performs extraction.",
                    "tags": ["codex"],
                }
            ],
            "session_summary": "Tested worker provenance.",
        }
        fake.write_text(
            "#!/usr/bin/env python3\n"
            "import json\n"
            f"print(json.dumps({response!r}))\n"
        )
        fake.chmod(0o755)
        job_path = self.root / "state" / "queue" / "session-456.json"
        job_path.parent.mkdir(parents=True)
        job_path.write_text(
            json.dumps(
                {
                    "session_id": "session-456",
                    "session_short": "session-",
                    "started_on": "2026-07-29",
                    "project": "test",
                    "conversation_title": "Worker test",
                    "transcript": "User: test\nAssistant: result",
                    "models": ["gpt-5.6-sol"],
                }
            )
        )
        with mock.patch.object(codex, "CODEX_BIN", str(fake)):
            self.assertEqual(codex.process_job(job_path), 0)

        page = next((self.root / "pages" / "claude" / "context").glob("*.md"))
        content = page.read_text()
        self.assertIn("creator:: [[codex]]", content)
        self.assertIn("model:: [[gpt-5.6-sol]]", content)
        self.assertNotIn("model:: [[gpt-5.6-terra]]", content)
        self.assertIn("last-updated:: [[2026/07/29]]", content)
        session = next(
            (self.root / "pages" / "claude" / "sessions").rglob("*.md")
        ).read_text()
        self.assertIn("title:: Session 2026-07-29 session- — test", session)
        self.assertIn("creator:: [[codex]]", session)
        self.assertIn("last-updated:: [[2026/07/29]]", session)
        marker = self.root / "state" / "processed" / "session-456.json"
        self.assertTrue(marker.exists())

    def test_worker_failure_retains_metadata_not_transcript(self):
        job_path = self.root / "state" / "queue" / "session-789.json"
        job_path.parent.mkdir(parents=True)
        job_path.write_text(
            json.dumps(
                {
                    "session_id": "session-789",
                    "session_short": "session-",
                    "started_on": "2026-07-29",
                    "project": "test",
                    "conversation_title": "Failure test",
                    "transcript": "SECRET TRANSCRIPT",
                    "models": ["gpt-5.6-sol"],
                }
            )
        )
        with mock.patch.object(
            codex, "call_codex", side_effect=RuntimeError("synthetic failure")
        ):
            self.assertEqual(codex.process_job(job_path), 1)
        failed = self.root / "state" / "failed" / "session-789.json"
        content = failed.read_text()
        self.assertIn("synthetic failure", content)
        self.assertNotIn("SECRET TRANSCRIPT", content)
        self.assertFalse((self.root / "state" / "running" / "session-789.json").exists())

    def test_resolve_session_returns_canonical_identity(self):
        self._write_transcript("thread-abc")
        with tempfile.SpooledTemporaryFile(mode="w+") as stdout:
            with mock.patch("sys.stdout", stdout):
                self.assertEqual(codex.resolve_session("thread-abc"), 0)
            stdout.seek(0)
            resolved = json.loads(stdout.read())
        self.assertEqual(
            resolved["session"], "Session 2026-07-29 thread-a — project"
        )
        self.assertEqual(resolved["models"], ["gpt-5.6-sol"])
        self.assertEqual(resolved["project"], "project")


class CodexOnDemandTests(TempEnvironment):
    def setUp(self):
        super().setUp()
        self.pages_patch = mock.patch.object(mcp, "LOGSEQ_PAGES_DIR", self.root / "pages")
        self.index_patch = mock.patch.object(
            mcp, "INDEX_PATH", self.root / "vault_index.npz"
        )
        self.pages_patch.start()
        self.index_patch.start()

    def tearDown(self):
        self.index_patch.stop()
        self.pages_patch.stop()
        super().tearDown()

    def test_on_demand_writer_uses_codex_provenance_and_two_word_dedup(self):
        session = "Session 2026-07-29 abcdef12 — demo"
        first = mcp._write_codex_insight(
            "decisions",
            "Use SessionEnd Lifecycle",
            "Use the lifecycle event.",
            "It captures task exit.\n    - Keep the original model",
            session,
            ["gpt-5.6-sol", "gpt-5.6-sol"],
            ["codex"],
            "demo",
        )
        second = mcp._write_codex_insight(
            "decisions",
            "Use SessionEnd Hook",
            "Duplicate prefix.",
            "Must skip.",
            session,
            ["gpt-5.6-sol"],
            [],
            "demo",
        )
        self.assertTrue(first.startswith("Written: "))
        self.assertTrue(second.startswith("Skipped"))
        pages = list((self.root / "pages" / "claude" / "decisions").glob("*.md"))
        self.assertEqual(len(pages), 1)
        content = pages[0].read_text()
        self.assertIn("creator:: [[codex]]", content)
        self.assertIn("model:: [[gpt-5.6-sol]]", content)
        self.assertIn(f"session:: [[{session}]]", content)
        self.assertIn(
            "  - It captures task exit.\n    - Keep the original model\n",
            content,
        )
        self.assertRegex(content, r"(?m)^last-updated:: \[\[\d{4}/\d{2}/\d{2}\]\]$")

    def test_concurrent_same_prefix_writes_create_one_page(self):
        session = "Session 2026-07-29 abcdef12 — demo"

        def write(index):
            return mcp._write_codex_insight(
                "patterns",
                f"Concurrent Lock Variant {index}",
                "Only one prefix should win.",
                "The shared lock serializes the check and write.",
                session,
                ["gpt-5.6-sol"],
            )

        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(write, range(4)))
        self.assertEqual(sum(result.startswith("Written") for result in results), 1)
        pages = list((self.root / "pages" / "claude" / "patterns").glob("*.md"))
        self.assertEqual(len(pages), 1)

    def test_missing_models_and_noncanonical_session_do_not_write(self):
        self.assertIn(
            "originating task model",
            mcp._write_codex_insight(
                "context", "No model", "x", "y", "Session valid", []
            ),
        )
        self.assertIn(
            "canonical Session",
            mcp._write_codex_insight(
                "context", "Bad session", "x", "y", "manual", ["gpt-5.6-sol"]
            ),
        )
        self.assertFalse((self.root / "pages").exists())


if __name__ == "__main__":
    unittest.main()
