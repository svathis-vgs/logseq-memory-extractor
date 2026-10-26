import os
import re
import tempfile
import unittest
import zlib
from datetime import date
from pathlib import Path
from unittest import mock

import logseq_memory_mcp as mcp
import logseq_memory_shared as shared

# Keep the vault write lock out of the real ~/.logseq-memory during tests.
os.environ.setdefault(
    "LOGSEQ_LOCK_PATH", os.path.join(tempfile.gettempdir(), "logseq-memory-test.lock")
)

try:
    import numpy as np
except ImportError:  # pragma: no cover - numpy is optional at runtime
    np = None


DIM = 256
TODAY = date(2026, 10, 17)


def fake_encode(texts, **_kwargs):
    """Hashed bag-of-words: same words -> cosine 1.0, disjoint words -> 0."""
    out = np.zeros((len(texts), DIM), dtype="float32")
    for row, text in enumerate(texts):
        for word in re.findall(r"[a-z0-9]+", text.lower()):
            # crc32, not hash(): str hashes are randomised per process, which made
            # slot collisions (and so the scores) differ between runs.
            out[row, zlib.crc32(word.encode()) % DIM] += 1.0
        norm = np.linalg.norm(out[row])
        if norm:
            out[row] /= norm
    return out


class FakeModel:
    def encode(self, texts, **kwargs):
        return fake_encode(texts)


def existing_page(path: Path, title: str, summary: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"title:: {title}\n"
        "type:: [[pattern]]\n"
        "date:: [[2026/09/01]]\n"
        "last-updated:: [[2026/09/01]]\n"
        "\n"
        "- ## Summary\n"
        f"  - {summary}\n"
        "\n"
        "- ## Detail\n"
        "  - Original detail.\n", encoding="utf-8"
    )
    return path


def build_index(index_path: Path, pages: list[Path]) -> None:
    texts = [shared.index_extract_text(p) for p in pages]
    np.savez_compressed(
        index_path,
        embeddings=fake_encode(texts),
        paths=np.array([str(p) for p in pages]),
    )


@unittest.skipIf(np is None, "numpy not installed")
class SemanticDedupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.pages = self.root / "pages"
        self.patterns = self.pages / "memory" / "patterns"
        self.index = self.root / "vault_index.npz"
        self.model_patch = mock.patch.object(shared, "_EMBEDDING_MODEL", FakeModel())
        self.model_patch.start()
        # The fake encoder is cruder than MiniLM; the production threshold is
        # covered by calibration, these tests cover the write-path behavior.
        self.threshold_patch = mock.patch.object(shared, "SEMANTIC_DEDUP_THRESHOLD", 0.8)
        self.threshold_patch.start()
        self.existing = existing_page(
            self.patterns / "pattern-retry-kafka-producer-sends.md",
            "Pattern: Retry Kafka Producer Sends",
            "Retry kafka producer sends with backoff",
        )
        build_index(self.index, [self.existing])

    def tearDown(self):
        self.threshold_patch.stop()
        self.model_patch.stop()
        self.tmp.cleanup()

    def write(self, items, index_path=None):
        report = []
        links = shared.write_pages(
            {"patterns": items},
            "demo",
            "abc",
            "slug",
            "Session 2026-10-17 abc — demo",
            ["claude-opus-5-5"],
            pages_dir=self.pages,
            update_index_fn=lambda paths: None,
            today=TODAY,
            index_path=self.index if index_path is None else index_path,
            report=report,
        )
        return links, report

    def test_same_meaning_appends_update_instead_of_new_file(self):
        links, report = self.write(
            [
                {
                    "slug": "backoff-for-kafka-producer-retries",
                    "summary": "Retry kafka producer sends with backoff",
                    "detail": "Use 200ms base delay.",
                    "tags": [],
                }
            ]
        )
        self.assertEqual(sorted(p.name for p in self.patterns.glob("*.md")),
                         [self.existing.name])
        self.assertEqual(report, [("appended", self.existing)])
        self.assertEqual(links, ["[[Pattern: Retry Kafka Producer Sends]]"])
        text = self.existing.read_text(encoding="utf-8")
        self.assertIn("last-updated:: [[2026/10/17]]", text)
        self.assertIn("- ## Update [[2026/10/17]]", text)
        self.assertIn("  - From [[Session 2026-10-17 abc — demo]]", text)
        self.assertIn("  - Use 200ms base delay.", text)
        self.assertIn("  - Original detail.", text)

    def test_different_meaning_writes_new_page(self):
        _, report = self.write(
            [
                {
                    "slug": "pin-helm-chart-versions",
                    "summary": "Pin helm chart versions in gitops releases",
                    "detail": "x",
                    "tags": [],
                }
            ]
        )
        self.assertEqual(report[0][0], "written")
        self.assertTrue((self.patterns / "pattern-pin-helm-chart-versions.md").exists())
        self.assertNotIn("## Update", self.existing.read_text(encoding="utf-8"))

    def test_same_batch_duplicates_fold_into_first_new_page(self):
        item = {"summary": "Pin helm chart versions in gitops releases", "detail": "x", "tags": []}
        _, report = self.write(
            [dict(item, slug="pin-helm-chart-versions"),
             dict(item, slug="always-pin-chart-version")]
        )
        first = self.patterns / "pattern-pin-helm-chart-versions.md"
        self.assertEqual(report, [("written", first), ("appended", first)])
        self.assertFalse((self.patterns / "pattern-always-pin-chart-version.md").exists())

    def test_missing_index_keeps_legacy_behavior(self):
        _, report = self.write(
            [{"slug": "backoff-for-kafka-producer-retries",
              "summary": "Retry kafka producer sends with backoff", "detail": "x", "tags": []}],
            index_path=self.root / "missing.npz",
        )
        self.assertEqual(report[0][0], "written")
        self.assertNotIn("## Update", self.existing.read_text(encoding="utf-8"))

    def test_match_outside_target_directory_is_ignored(self):
        other = existing_page(
            self.root / "other-vault" / "memory" / "patterns" / "pattern-x.md",
            "Pattern: Pin Helm Chart Versions",
            "Pin helm chart versions in gitops releases",
        )
        build_index(self.index, [other])
        _, report = self.write(
            [{"slug": "pin-helm-chart-versions",
              "summary": "Pin helm chart versions in gitops releases", "detail": "x", "tags": []}]
        )
        self.assertEqual(report[0][0], "written")
        self.assertNotIn("## Update", other.read_text(encoding="utf-8"))

    def test_score_below_threshold_writes_new_page(self):
        index = shared.SemanticIndex(self.index, encode=fake_encode, threshold=0.99)
        self.assertIsNone(
            index.find(self.patterns, "Pattern: Retry Kafka Producer Sends. Retry kafka sends")
        )
        index.threshold = 0.5
        self.assertEqual(
            index.find(self.patterns, "Pattern: Retry Kafka Producer Sends. Retry kafka sends"),
            self.existing,
        )

    def test_mcp_write_insight_appends_and_force_writes(self):
        with (
            mock.patch.object(mcp, "LOGSEQ_PAGES_DIR", self.pages),
            mock.patch.object(mcp, "INDEX_PATH", self.index),
        ):
            result = mcp._write_insight(
                "patterns", "Backoff On Producer Retries",
                "Retry kafka producer sends with backoff", "More detail.",
                "claude-opus-5-5", [], "demo", "Session 2026-10-17 abc — demo",
            )
            self.assertIn("Appended as update to similar page", result)
            self.assertIn("## Update", self.existing.read_text(encoding="utf-8"))
            forced = mcp._write_insight(
                "patterns", "Backoff On Producer Retries",
                "Retry kafka producer sends with backoff", "More detail.",
                "claude-opus-5-5", [], "demo", "Session 2026-10-17 abc — demo",
                force=True,
            )
            self.assertTrue(forced.startswith("Written:"), forced)


if __name__ == "__main__":
    unittest.main()
