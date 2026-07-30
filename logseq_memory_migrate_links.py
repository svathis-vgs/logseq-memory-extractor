#!/usr/bin/env python3
"""Convert Logseq creator/model property values to page links."""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path


def _link(value: str) -> str:
    text = value.strip()
    if text.startswith("[[") and text.endswith("]]"):
        return text
    return f"[[{text}]]"


def _linked_values(value: str) -> str:
    return ", ".join(_link(item) for item in value.split(",") if item.strip())


def migrate_text(content: str) -> str:
    result = []
    in_property_block = True
    for line in content.splitlines(keepends=True):
        newline = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
        text = line[: -len(newline)] if newline else line
        if not text.strip():
            in_property_block = False
            result.append(line)
        elif in_property_block and text.startswith("creator:: "):
            value = text[len("creator:: ") :]
            result.append(
                f"creator:: {_link(value)}{newline}" if value.strip() else line
            )
        elif in_property_block and text.startswith("model:: "):
            value = text[len("model:: ") :]
            result.append(
                f"model:: {_linked_values(value)}{newline}" if value.strip() else line
            )
        else:
            result.append(line)
    return "".join(result)


def migrate_pages(
    pages_dir: Path, *, dry_run: bool = False, backup_dir: Path | None = None
) -> list[Path]:
    root = pages_dir / "claude"
    if not root.exists():
        return []
    changed = []
    for path in sorted(root.rglob("*.md")):
        content = path.read_text(errors="replace")
        updated = migrate_text(content)
        if updated == content:
            continue
        if backup_dir and not dry_run:
            destination = backup_dir / path.relative_to(pages_dir)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)
        if not dry_run:
            path.write_text(updated)
        changed.append(path)
    return changed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pages-dir", type=Path, required=True)
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    changed = migrate_pages(
        args.pages_dir, dry_run=args.dry_run, backup_dir=args.backup_dir
    )
    action = "Would migrate" if args.dry_run else "Migrated"
    print(f"{action} {len(changed)} page(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
