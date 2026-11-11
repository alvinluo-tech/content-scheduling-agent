"""仓库自洽性：文档引用的证据文件必须存在，归档的用例必须四件套齐全。"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

from src.server import CASE_META
from tests.support import EXAMPLES_DIR, REPO_ROOT

IMAGE_REF = re.compile(r"docs/images/([A-Za-z0-9_.-]+\.png)")


def docs_text() -> str:
    files = [REPO_ROOT / "README.md"] + sorted((REPO_ROOT / "docs").glob("*.md"))
    return "".join(path.read_text(encoding="utf-8") for path in files)


class RepoDocsTest(unittest.TestCase):
    def test_every_referenced_screenshot_exists(self) -> None:
        missing = [name for name in set(IMAGE_REF.findall(docs_text())) if not (REPO_ROOT / "docs" / "images" / name).exists()]
        self.assertEqual(missing, [])

    def test_no_screenshot_sits_unreferenced(self) -> None:
        text = docs_text()
        orphans = [path.name for path in sorted((REPO_ROOT / "docs" / "images").glob("*.png")) if path.name not in text]
        self.assertEqual(orphans, [])

    def test_every_example_ships_input_output_trace_and_report(self) -> None:
        for path in sorted(EXAMPLES_DIR.glob("*.input.json")):
            case = path.name[: -len(".input.json")]
            if ".live" in case:
                continue
            for suffix in (".output.json", ".trace.json", ".report.md"):
                self.assertTrue((EXAMPLES_DIR / (case + suffix)).exists(), case + suffix)

    def test_case_menu_matches_the_examples_on_disk(self) -> None:
        on_disk = sorted(p.name[: -len(".input.json")] for p in EXAMPLES_DIR.glob("*.input.json") if ".live" not in p.name)
        self.assertEqual(sorted(item["file"] for item in CASE_META), on_disk)


if __name__ == "__main__":
    unittest.main()
