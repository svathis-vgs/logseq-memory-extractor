import importlib.util
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "property_link_migration", ROOT / "logseq_memory_migrate_links.py"
)
migration = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(migration)


class PropertyLinkMigrationTests(unittest.TestCase):
    def test_migrates_creator_and_ordered_models_idempotently(self):
        content = (
            "title:: Pattern: Provenance\n"
            "creator:: claude\n"
            "model:: claude-sonnet-4-6, claude-opus-4-6\n"
            "\n"
            "- Detail with creator:: text must stay unchanged\n"
            "creator:: body text must also stay unchanged\n"
        )
        expected = (
            "title:: Pattern: Provenance\n"
            "creator:: [[claude]]\n"
            "model:: [[claude-sonnet-4-6]], [[claude-opus-4-6]]\n"
            "\n"
            "- Detail with creator:: text must stay unchanged\n"
            "creator:: body text must also stay unchanged\n"
        )
        self.assertEqual(migration.migrate_text(content), expected)
        self.assertEqual(migration.migrate_text(expected), expected)

    def test_preserves_existing_links_and_migrates_mixed_model_values(self):
        content = "creator:: [[codex]]\nmodel:: [[gpt-5.6-sol]], gpt-5.6-terra\n"
        self.assertEqual(
            migration.migrate_text(content),
            "creator:: [[codex]]\nmodel:: [[gpt-5.6-sol]], [[gpt-5.6-terra]]\n",
        )

    def test_migrates_only_markdown_files_and_can_create_rollback_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "pages"
            page = root / "claude" / "patterns" / "pattern-provenance.md"
            page.parent.mkdir(parents=True)
            page.write_text("creator:: claude\nmodel:: claude-sonnet-5\n")
            ignored = root / "claude" / "notes.txt"
            ignored.write_text("creator:: claude\n")
            backup = Path(tmp) / "backup"

            changed = migration.migrate_pages(root, backup_dir=backup)

            self.assertEqual(changed, [page])
            self.assertEqual(
                page.read_text(),
                "creator:: [[claude]]\nmodel:: [[claude-sonnet-5]]\n",
            )
            self.assertEqual(
                (backup / "claude" / "patterns" / "pattern-provenance.md").read_text(),
                "creator:: claude\nmodel:: claude-sonnet-5\n",
            )
            self.assertEqual(ignored.read_text(), "creator:: claude\n")


if __name__ == "__main__":
    unittest.main()
