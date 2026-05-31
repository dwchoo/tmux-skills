from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STYLE_GUIDE = ROOT / "docs" / "llm-wiki-style-guide.md"
MARKDOWN_FILES = [
    ROOT / "README.md",
    ROOT / "SKILL.md",
    ROOT / "llms.txt",
    *sorted((ROOT / "docs").glob("*.md")),
    *sorted((ROOT / "references").glob("*.md")),
]


def markdown_links(path: Path) -> list[str]:
    return re.findall(r"\[[^\]]+\]\(([^)]+)\)", path.read_text(encoding="utf-8"))


def repo_relative_targets(path: Path) -> set[str]:
    targets: set[str] = set()
    for link in markdown_links(path):
        if "://" in link:
            continue
        target = link.split("#", 1)[0]
        if not target:
            continue
        resolved = (path.parent / target).resolve()
        targets.add(str(resolved.relative_to(ROOT)))
    return targets


def style_guide_canonical_paths() -> set[str]:
    text = STYLE_GUIDE.read_text(encoding="utf-8")
    table = text.split("## Canonical Ownership", 1)[1].split("## Page Shape", 1)[0]
    return set(re.findall(r"\|\s+[^|]+\s+\|\s+`([^`]+)`\s+\|", table))


def markdown_h2_titles(path: Path) -> list[str]:
    return re.findall(r"^##\s+(.+)$", path.read_text(encoding="utf-8"), flags=re.MULTILINE)


class DocsIndexTests(unittest.TestCase):
    def test_markdown_links_resolve(self) -> None:
        for path in MARKDOWN_FILES:
            with self.subTest(path=path.relative_to(ROOT)):
                for link in markdown_links(path):
                    if "://" in link:
                        continue
                    target = link.split("#", 1)[0]
                    if not target:
                        continue
                    resolved = (path.parent / target).resolve()
                    self.assertTrue(resolved.exists(), f"{path.relative_to(ROOT)} links to missing {link}")

    def test_llms_lists_all_canonical_docs_and_references(self) -> None:
        targets = repo_relative_targets(ROOT / "llms.txt")
        canonical_docs = {str(path.relative_to(ROOT)) for path in sorted((ROOT / "docs").glob("*.md"))}
        canonical_refs = {str(path.relative_to(ROOT)) for path in sorted((ROOT / "references").glob("*.md"))}

        self.assertLessEqual({"README.md", "SKILL.md"}, targets)
        self.assertLessEqual(canonical_docs, targets)
        self.assertLessEqual(canonical_refs, targets)

    def test_docs_readme_lists_all_detail_docs_and_references(self) -> None:
        targets = repo_relative_targets(ROOT / "docs" / "README.md")
        detail_docs = {
            str(path.relative_to(ROOT))
            for path in sorted((ROOT / "docs").glob("*.md"))
            if path.name != "README.md"
        }
        canonical_refs = {str(path.relative_to(ROOT)) for path in sorted((ROOT / "references").glob("*.md"))}

        self.assertLessEqual({"README.md", "SKILL.md", "llms.txt"}, targets)
        self.assertLessEqual(detail_docs, targets)
        self.assertLessEqual(canonical_refs, targets)

    def test_style_guide_canonical_owner_paths_exist(self) -> None:
        expected_paths = {
            "README.md",
            "SKILL.md",
            "llms.txt",
            "docs/README.md",
            "docs/workflows-and-features.md",
            "docs/managed-workers.md",
            "docs/real-use-e2e.md",
            "references/",
        }
        paths = style_guide_canonical_paths()

        self.assertEqual(paths, expected_paths)
        for target in paths:
            with self.subTest(target=target):
                self.assertTrue((ROOT / target).exists())

    def test_llms_txt_has_required_shape(self) -> None:
        text = (ROOT / "llms.txt").read_text(encoding="utf-8")
        lines = text.splitlines()

        self.assertEqual(lines[0], "# tmux-skills")
        self.assertTrue(lines[2].startswith("> "))
        self.assertEqual(markdown_h2_titles(ROOT / "llms.txt"), ["Core", "Detailed docs", "References", "Optional"])
        self.assertIn("No optional docs are required for normal agent execution yet.", text)


if __name__ == "__main__":
    unittest.main()
