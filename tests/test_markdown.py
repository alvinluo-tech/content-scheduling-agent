"""Markdown 报告：同一份 report 换一种呈现，不额外推断。"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from src.markdown import render_report
from tests.support import EXAMPLES_DIR, REPO_ROOT


def load(name: str, kind: str) -> dict:
    return json.loads((EXAMPLES_DIR / (name + "." + kind + ".json")).read_text(encoding="utf-8"))


class MarkdownReportTest(unittest.TestCase):
    def test_conflict_report_keeps_confirmation_and_never_claims_publish(self) -> None:
        text = render_report(load("02-conflict", "output"), load("02-conflict", "trace"))
        self.assertTrue(text.startswith("# 排期报告 · case02-conflict"))
        self.assertIn("结果：**待用户确认**", text)
        self.assertIn("C-M01-2026-11-12-1900", text)
        self.assertIn("D-M01-2026-11-12-1800", text)
        self.assertIn("时间已调整", text)
        self.assertIn("已发布：否", text)
        self.assertNotIn("已发布：是", text)
        self.assertIn("published 恒为 false", text)

    def test_pending_items_come_with_commands_a_human_can_paste(self) -> None:
        text = render_report(load("02-conflict", "output"), load("02-conflict", "trace"))
        self.assertIn("## 怎么拍板", text)
        self.assertIn("--approve C-M01-2026-11-12-1900", text)
        self.assertIn("--reject C-M01-2026-11-12-1900", text)
        self.assertIn("--replan C-M01-2026-11-12-1900=19:30", text)
        self.assertIn("examples/02-conflict.input.json", text)

    def test_a_clean_run_has_nothing_to_ask_the_human(self) -> None:
        text = render_report(load("01-normal", "output"), load("01-normal", "trace"))
        self.assertNotIn("怎么拍板", text)

    def test_a_custom_run_without_an_input_file_points_at_the_buttons(self) -> None:
        report = load("02-conflict", "output")
        report.pop("input_file", None)
        text = render_report(report, load("02-conflict", "trace"))
        self.assertIn("没有输入文件", text)
        self.assertIn("POST /api/confirm", text)
        self.assertNotIn("--approve", text)

    def test_human_decision_shows_who_when_and_which_version(self) -> None:
        report = load("02-conflict", "output")
        item = report["materials"][0]["pending_confirmations"][0]
        item["status"] = "approved"
        item["decided_by"] = "human"
        item["decided_at"] = "2026-11-12T10:05:00"
        item["replanned_from"] = "C-M01-2026-11-12-1900"
        text = render_report(report, load("02-conflict", "trace"))
        self.assertIn("已同意", text)
        self.assertIn("human", text)
        self.assertIn("2026-11-12T10:05:00", text)
        self.assertIn("改选自 " + item["replanned_from"], text)
        self.assertNotIn("改选自 None", text)

    def test_decision_table_lists_every_tool_call(self) -> None:
        trace = load("02-conflict", "trace")
        text = render_report(load("02-conflict", "output"), trace)
        table = [line for line in text.splitlines() if line.startswith("| ") and "read_material" in line]
        self.assertTrue(table)
        for step in trace["steps"]:
            self.assertIn(step["action"]["tool"], text)
        self.assertIn("## 决策记录", text)

    def test_save_failure_is_reported_as_not_saved(self) -> None:
        text = render_report(load("04-save-failure", "output"), load("04-save-failure", "trace"))
        self.assertIn("保存失败", text)
        self.assertIn("未保存（失败 2 次，未谎报成功）", text)
        self.assertNotIn("draft_ready", text)

    def test_step_limit_report_lists_unfinished_materials(self) -> None:
        text = render_report(load("06-step-limit", "output"), load("06-step-limit", "trace"))
        self.assertIn("步数上限中断", text)
        self.assertIn("步数：3 / 3", text)
        self.assertIn("⚠️ 未完成素材：M02、M05", text)

    def test_cli_writes_markdown_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "report" / "02.md"
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "src.main",
                    "--input",
                    str(EXAMPLES_DIR / "02-conflict.input.json"),
                    "--offline",
                    "--ledger",
                    str(Path(tmp) / "ledger.json"),
                    "--report-md",
                    str(out),
                ],
                cwd=str(REPO_ROOT),
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(out.is_file(), "CLI 应该写出 Markdown 报告")
            text = out.read_text(encoding="utf-8")
            self.assertIn("## 决策记录", text)
            self.assertIn("结果：**待用户确认**", text)


if __name__ == "__main__":
    unittest.main()
