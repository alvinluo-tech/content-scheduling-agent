from __future__ import annotations

import json
import unittest
from pathlib import Path

from src.main import run_once
from src.tools import TOOL_SPECS

from .support import EXAMPLES_DIR, make_args, temp_ledger


def run_example(name: str, ledger: Path, max_steps: int | None = None) -> dict:
    return run_once(make_args(EXAMPLES_DIR / f"{name}.input.json", ledger, max_steps=max_steps))


class AgentFlowTest(unittest.TestCase):
    """图级回归：5 条验收路径 + 步数上限 + trace 结构。"""

    def setUp(self) -> None:
        self._ctx = temp_ledger()
        self.ledger = self._ctx.__enter__()

    def tearDown(self) -> None:
        self._ctx.__exit__(None, None, None)

    def trace_of(self, name: str, max_steps: int | None = None) -> dict:
        trace_path = self.ledger.parent / f"{name}.trace.json"
        args = make_args(EXAMPLES_DIR / f"{name}.input.json", self.ledger, max_steps=max_steps)
        args.trace = str(trace_path)
        run_once(args)
        return json.loads(trace_path.read_text(encoding="utf-8"))

    def test_normal_schedule(self) -> None:
        report = run_example("01-normal", self.ledger)
        self.assertEqual(report["status"], "completed")
        self.assertEqual(report["outcome"], "scheduled")
        row = report["materials"][0]
        self.assertEqual(row["review_status"], "pass_with_warnings")
        self.assertEqual(row["suggested_time"], "11:00")
        self.assertEqual(row["draft"]["status"], "draft_ready")
        self.assertFalse(row["published"])
        self.assertFalse(report["published"])

    def test_reference_instance_conflict(self) -> None:
        """题面参考实例：M01 要求 18:00 被占用，19:00 可用 → 只能标记待确认，不能称已发布。"""
        report = run_example("02-conflict", self.ledger)
        self.assertEqual(report["outcome"], "pending_confirmation")
        row = report["materials"][0]
        self.assertEqual(row["requested_time"], "18:00")
        self.assertEqual(row["suggested_time"], "19:00")
        self.assertTrue(row["time_changed"])
        self.assertFalse(row["hard_time_preserved"])
        self.assertTrue(row["requires_user_confirmation"])
        self.assertEqual(row["draft"]["status"], "draft_pending_confirmation")
        self.assertFalse(row["published"])
        self.assertEqual(report["published"], False)

    def test_conflict_prerecorded_confirmation_is_reused(self) -> None:
        run_example("02-conflict", self.ledger)
        report = run_example("05-dedup", self.ledger)
        self.assertEqual(report["status"], "completed")
        row = report["materials"][0]
        self.assertEqual(row["suggested_time"], "19:00")
        self.assertEqual(row["draft"]["submit_count"], 2)
        self.assertEqual(len(row["pending_confirmations"]), 1)
        ledger = json.loads(Path(self.ledger).read_text(encoding="utf-8"))
        self.assertEqual(len(ledger["drafts"]), 1)

    def test_duplicate_submission_creates_one_draft(self) -> None:
        first = run_example("02-conflict", self.ledger)
        second = run_example("05-dedup", self.ledger)
        self.assertEqual(first["materials"][0]["draft"]["draft_id"], second["materials"][0]["draft"]["draft_id"])
        self.assertEqual(second["materials"][0]["draft"]["submit_count"], 2)

    def test_rules_risk_and_missing_info(self) -> None:
        report = run_example("03-rules-and-missing", self.ledger)
        self.assertEqual(report["outcome"], "failed")
        rows = {row["material_id"]: row for row in report["materials"]}
        self.assertEqual(rows["M03"]["result_status"], "needs_info")
        self.assertFalse(rows["M03"]["draft"])
        self.assertTrue(rows["M03"]["missing_fields"])
        self.assertEqual(rows["M04"]["result_status"], "blocked")
        self.assertIn("R03", [risk["rule_id"] for risk in rows["M04"]["risk_reasons"]])
        self.assertFalse(rows["M04"]["draft"])

    def test_save_failure_is_reported_as_failure(self) -> None:
        report = run_example("04-save-failure", self.ledger)
        self.assertEqual(report["outcome"], "failed")
        row = report["materials"][0]
        self.assertEqual(row["result_status"], "save_failed")
        self.assertIsNone(row["draft"])
        self.assertGreaterEqual(row["failed_save_attempts"], 2)
        self.assertFalse(row["published"])

    def test_multi_day_conflict_stays_pending_on_its_own_day(self) -> None:
        """11-13 09:00 被 M07 占用：改到 09:30 只能待确认；另一条素材在 11-13 空白时段直接落草稿。"""
        report = run_example("07-multiday", self.ledger)
        self.assertEqual(report["date"], "2026-11-12")
        self.assertEqual(report["dates"], ["2026-11-12", "2026-11-13"])
        self.assertEqual(report["outcome"], "pending_confirmation")
        rows = {row["material_id"]: row for row in report["materials"]}
        m06 = rows["M06"]
        self.assertEqual(m06["requested_date"], "2026-11-13")
        self.assertEqual(m06["scheduled_date"], "2026-11-13")
        self.assertEqual(m06["suggested_time"], "09:30")
        self.assertTrue(m06["requires_user_confirmation"])
        self.assertEqual(m06["draft"]["status"], "draft_pending_confirmation")
        m07 = rows["M07"]
        self.assertEqual(m07["result_status"], "scheduled_draft")
        self.assertEqual(m07["scheduled_date"], "2026-11-13")
        self.assertFalse(m07["published"])
        self.assertFalse(report["published"])

    def test_step_limit_returns_incomplete_status(self) -> None:
        report = run_example("06-step-limit", self.ledger)
        self.assertEqual(report["status"], "step_limit_reached")
        self.assertEqual(report["outcome"], "incomplete")
        self.assertFalse(report["completed"])
        self.assertEqual(report["limits"]["steps_used"], 3)
        self.assertTrue(report["unfinished_materials"])

    def test_step_limit_enforced_from_cli_flag(self) -> None:
        report = run_example("06-step-limit", self.ledger, max_steps=2)
        self.assertEqual(report["status"], "step_limit_reached")
        self.assertEqual(report["limits"]["max_steps"], 2)
        self.assertEqual(report["limits"]["steps_used"], 2)

    def test_trace_schema(self) -> None:
        trace = self.trace_of("02-conflict")
        self.assertEqual(trace["terminal"], "completed")
        self.assertEqual(trace["mode"], "offline")
        self.assertEqual(set(trace["limits"]), {"max_steps", "steps_used", "tool_calls"})
        self.assertEqual(len(trace["steps"]), trace["limits"]["tool_calls"])
        for step in trace["steps"]:
            self.assertEqual(set(step), {"step", "rationale", "action", "observation", "next_action"})
            self.assertEqual(set(step["action"]), {"tool", "args"})
            self.assertTrue(step["rationale"], "每步都要有决策依据")
            self.assertIn("summary", step["observation"])
            self.assertTrue(step["next_action"])
        self.assertEqual(trace["steps"][-1]["next_action"], "结束并输出排期结果")
        self.assertEqual(trace["steps"][0]["action"]["tool"], "read_material")
        self.assertEqual(
            [step["action"]["tool"] for step in trace["steps"]],
            ["read_material", "check_rules", "query_time_slot", "request_confirmation", "save_draft"],
        )

    def test_missing_api_key_fails_loudly(self) -> None:
        """没有密钥必须直接报错并提示 --offline，不能静默降级成假结果。"""
        from unittest import mock

        from src.models import build_chat_model

        with mock.patch.dict("os.environ", {"DEEPSEEK_API_KEY": "", "OPENAI_API_KEY": ""}, clear=False):
            with self.assertRaises(RuntimeError) as ctx:
                build_chat_model("deepseek", {"run_id": "x"})
        self.assertIn("--offline", str(ctx.exception))

    def test_no_publish_tool_exists(self) -> None:
        names = {spec["function"]["name"] for spec in TOOL_SPECS}
        self.assertEqual(
            names, {"read_material", "check_rules", "query_time_slot", "request_confirmation", "save_draft"}
        )
        self.assertFalse([name for name in names if "publish" in name])


if __name__ == "__main__":
    unittest.main()
