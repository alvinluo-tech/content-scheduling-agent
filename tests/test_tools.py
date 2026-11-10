from __future__ import annotations

import unittest

from src.store import Store
from src.tools import Toolbox

from .support import DATA_DIR, make_run, temp_ledger


class ToolboxTest(unittest.TestCase):
    """工具层守卫：硬性时间、审核未通过、重复提交、保存失败、参数与素材不存在。"""

    def setUp(self) -> None:
        self._ctx = temp_ledger()
        self.ledger = self._ctx.__enter__()
        self.store = Store(DATA_DIR, self.ledger)
        self.toolbox = Toolbox(self.store, make_run("M01", "M02", "M03", "M04", "M05", requested_time="18:00", time_hard=True))

    def tearDown(self) -> None:
        self._ctx.__exit__(None, None, None)

    def read_and_check(self, material_id: str) -> None:
        self.assertTrue(self.toolbox.call("read_material", {"material_id": material_id, "reason": "测试读取"})["ok"])
        self.assertTrue(self.toolbox.call("check_rules", {"material_id": material_id, "reason": "测试校验"})["ok"])

    def test_hard_time_change_requires_confirmation(self) -> None:
        self.read_and_check("M01")
        result = self.toolbox.call(
            "save_draft",
            {"material_id": "M01", "scheduled_time": "19:00", "reason": "冲突改用 19:00"},
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "HARD_CONSTRAINT_VIOLATION")

    def test_confirmed_hard_time_change_saves_draft_without_publishing(self) -> None:
        self.read_and_check("M01")
        self.toolbox.call(
            "request_confirmation",
            {
                "material_id": "M01",
                "proposed_time": "19:00",
                "question": "18:00 硬性要求但被占用，是否改到 19:00？",
                "reason": "硬性要求不能擅自改",
            },
        )
        result = self.toolbox.call(
            "save_draft",
            {"material_id": "M01", "scheduled_time": "19:00", "reason": "已登记待确认项"},
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "draft_pending_confirmation")
        self.assertFalse(result["published"])
        self.assertFalse(result["deduplicated"])

    def test_rejected_confirmation_voids_the_draft_and_blocks_the_same_plan(self) -> None:
        self.read_and_check("M01")
        self.toolbox.call(
            "request_confirmation",
            {
                "material_id": "M01",
                "proposed_time": "19:00",
                "question": "18:00 硬性要求但被占用，是否改到 19:00？",
                "reason": "硬性要求不能擅自改",
            },
        )
        self.assertTrue(
            self.toolbox.call("save_draft", {"material_id": "M01", "scheduled_time": "19:00", "reason": "先存待确认草稿"})["ok"]
        )
        record = self.store.reject_confirmation("C-M01-2026-11-12-1900")
        self.assertEqual(record["status"], "rejected")
        self.assertEqual(self.store.draft("2026-11-12", "M01", "18:00")["status"], "draft_rejected")

        again = self.toolbox.call("save_draft", {"material_id": "M01", "scheduled_time": "19:00", "reason": "再存一次同一方案"})
        self.assertFalse(again["ok"])
        self.assertEqual(again["error"]["code"], "CONFIRMATION_REJECTED")
        self.assertTrue(again["error"]["recoverable"])
        # 作废的草稿不再占位：19:00 应重新回到可用替代时段里
        slot = self.toolbox.call("query_time_slot", {"material_id": "M01", "time": "18:00", "reason": "否决后重查替代"})
        self.assertIn("19:00", slot["alternatives"])

    def test_slot_query_explains_why_a_slot_is_recommended(self) -> None:
        slot = self.toolbox.call("query_time_slot", {"material_id": "M01", "time": "18:00", "reason": "查冲突与替代"})
        ranked = slot["ranked_alternatives"]
        self.assertEqual([row["time"] for row in ranked], slot["alternatives"])
        self.assertEqual(ranked[0]["time"], "19:00")
        self.assertTrue(ranked[0]["recommended"])
        self.assertTrue(any("偏好时段" in reason for reason in ranked[0]["reasons"]))
        self.assertIn("首选 19:00", self.toolbox.summary_of("query_time_slot", slot))

    def test_duplicate_submission_is_deduplicated(self) -> None:
        self.read_and_check("M01")
        self.toolbox.call(
            "request_confirmation",
            {
                "material_id": "M01",
                "proposed_time": "19:00",
                "question": "18:00 硬性要求但被占用，是否改到 19:00？",
                "reason": "硬性要求不能擅自改",
            },
        )
        first = self.toolbox.call("save_draft", {"material_id": "M01", "scheduled_time": "19:00", "reason": "首次保存"})
        second = self.toolbox.call("save_draft", {"material_id": "M01", "scheduled_time": "19:00", "reason": "重复提交"})
        self.assertTrue(second["deduplicated"])
        self.assertEqual(first["draft_id"], second["draft_id"])
        self.assertEqual(len(self.store.drafts), 1)

    def test_own_draft_does_not_conflict_with_itself(self) -> None:
        self.read_and_check("M01")
        self.toolbox.call(
            "request_confirmation",
            {"material_id": "M01", "proposed_time": "19:00", "question": "改到 19:00？", "reason": "硬性要求"},
        )
        self.toolbox.call("save_draft", {"material_id": "M01", "scheduled_time": "19:00", "reason": "首次保存"})
        slot = self.toolbox.call("query_time_slot", {"material_id": "M01", "time": "19:00", "reason": "复查"})
        self.assertTrue(slot["available"])

    def test_other_material_draft_occupies_slot(self) -> None:
        box = Toolbox(self.store, make_run("M01", "M02", requested_time="11:00"))
        box.call("read_material", {"material_id": "M02", "reason": "测试读取"})
        box.call("check_rules", {"material_id": "M02", "reason": "测试校验"})
        saved = box.call("save_draft", {"material_id": "M02", "scheduled_time": "11:00", "reason": "可用就保存"})
        self.assertTrue(saved["ok"])
        slot = box.call("query_time_slot", {"material_id": "M01", "time": "11:00", "reason": "查位"})
        self.assertFalse(slot["available"])
        self.assertEqual([c["material_id"] for c in slot["conflicts"]], ["M02"])

    def test_save_failure_is_reported_not_hidden(self) -> None:
        failing = Toolbox(self.store, make_run("M02", requested_time="11:00", save_failures=-1))
        failing.call("read_material", {"material_id": "M02", "reason": "测试读取"})
        failing.call("check_rules", {"material_id": "M02", "reason": "测试校验"})
        result = failing.call("save_draft", {"material_id": "M02", "scheduled_time": "11:00", "reason": "保存"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "SAVE_FAILED")
        self.assertEqual(self.store.drafts, {})
        self.assertEqual(failing.attempts["M02"], 1)

    def test_review_not_passed_blocks_saving(self) -> None:
        self.read_and_check("M04")  # M04 命中违禁词，规则判定 can_draft=false
        result = self.toolbox.call("save_draft", {"material_id": "M04", "scheduled_time": "10:00", "reason": "尝试保存"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "MATERIAL_NOT_READY")

    def test_unknown_material_is_reported(self) -> None:
        result = self.toolbox.call("read_material", {"material_id": "M99", "reason": "测试"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "MATERIAL_NOT_FOUND")

    def test_missing_reason_is_rejected(self) -> None:
        result = self.toolbox.call("read_material", {"material_id": "M01"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "BAD_ARGUMENTS")

    def test_occupied_slot_is_rejected(self) -> None:
        self.read_and_check("M01")
        result = self.toolbox.call("save_draft", {"material_id": "M01", "scheduled_time": "18:00", "reason": "原时段"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "SLOT_CONFLICT")

    def test_material_outside_request_scope_is_rejected(self) -> None:
        outside = Toolbox(self.store, make_run("M01"))
        result = outside.call("read_material", {"material_id": "M02", "reason": "测试越界读取"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "NO_REQUEST")

    def test_tool_not_registered(self) -> None:
        result = self.toolbox.call("publish_now", {"material_id": "M01", "reason": "越权发布"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "UNKNOWN_TOOL")


class MultiDayToolboxTest(unittest.TestCase):
    """多日排期：占位按天算，跨日改期同样算硬性变更。"""

    def setUp(self) -> None:
        self._ctx = temp_ledger()
        self.ledger = self._ctx.__enter__()
        self.store = Store(DATA_DIR, self.ledger)
        self.toolbox = Toolbox(
            self.store,
            make_run("M06", requested_date="2026-11-13", requested_time="09:00", time_hard=True),
        )

    def tearDown(self) -> None:
        self._ctx.__exit__(None, None, None)

    def test_slot_query_uses_the_requested_day(self) -> None:
        self.toolbox.call("read_material", {"material_id": "M06", "reason": "测试读取"})
        self.toolbox.call("check_rules", {"material_id": "M06", "reason": "测试校验"})
        day13 = self.toolbox.call("query_time_slot", {"material_id": "M06", "time": "09:00", "reason": "查 11-13"})
        day12 = self.toolbox.call(
            "query_time_slot", {"material_id": "M06", "time": "09:00", "date": "2026-11-12", "reason": "查 11-12"},
        )
        self.assertEqual(day13["date"], "2026-11-13")
        self.assertFalse(day13["available"])
        self.assertEqual([c["material_id"] for c in day13["conflicts"]], ["M02"])
        self.assertTrue(day12["available"])

    def test_unknown_date_is_rejected(self) -> None:
        result = self.toolbox.call(
            "query_time_slot", {"material_id": "M06", "time": "09:00", "date": "2026-12-24", "reason": "臆造日期"},
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "DATE_NOT_AVAILABLE")
        self.assertFalse(result["error"]["recoverable"])

    def test_cross_day_change_needs_confirmation_and_records_both_days(self) -> None:
        self.toolbox.call("read_material", {"material_id": "M06", "reason": "测试读取"})
        self.toolbox.call("check_rules", {"material_id": "M06", "reason": "测试校验"})
        blocked = self.toolbox.call(
            "save_draft", {"material_id": "M06", "scheduled_time": "09:30", "reason": "09:00 被占，改 09:30"},
        )
        self.assertFalse(blocked["ok"])
        self.assertEqual(blocked["error"]["code"], "HARD_CONSTRAINT_VIOLATION")

        self.toolbox.call(
            "request_confirmation",
            {"material_id": "M06", "proposed_time": "09:30", "question": "11-13 09:00 被占，改 09:30？", "reason": "硬性要求"},
        )
        saved = self.toolbox.call(
            "save_draft", {"material_id": "M06", "scheduled_time": "09:30", "reason": "已登记待确认"},
        )
        self.assertTrue(saved["ok"])
        self.assertEqual(saved["status"], "draft_pending_confirmation")
        self.assertEqual(saved["date"], "2026-11-13")
        self.assertTrue(saved["time_changed"])
        self.assertFalse(saved["date_changed"])
        self.assertFalse(saved["published"])
        stored = self.store.draft("2026-11-13", "M06", "09:00")
        self.assertIsNotNone(stored)
        self.assertEqual(stored["requested_date"], "2026-11-13")

    def test_same_time_on_another_day_does_not_conflict(self) -> None:
        box = Toolbox(self.store, make_run("M06", requested_date="2026-11-13", requested_time="09:00"))
        box.call("read_material", {"material_id": "M06", "reason": "测试读取"})
        box.call("check_rules", {"material_id": "M06", "reason": "测试校验"})
        other_day = box.call(
            "save_draft",
            {"material_id": "M06", "scheduled_time": "09:00", "date": "2026-11-12", "reason": "挪到 11-12 同一时刻"},
        )
        self.assertTrue(other_day["ok"], other_day.get("error"))
        self.assertTrue(other_day["date_changed"])
        self.assertEqual(other_day["date"], "2026-11-12")

    def test_full_day_falls_through_to_the_next_configured_day(self) -> None:
        box = Toolbox(self.store, make_run("M01", requested_date="2026-11-14", requested_time="18:00"))
        result = box.call(
            "query_time_slot",
            {"material_id": "M01", "time": "18:00", "date": "2026-11-14", "reason": "看看上线日还有没有位置"},
        )
        self.assertTrue(result["ok"])
        self.assertFalse(result["available"])
        self.assertEqual(result["alternatives"], [])
        next_day = result["next_available"]
        self.assertIsNotNone(next_day)
        self.assertEqual(next_day["date"], "2026-11-15")
        self.assertEqual(next_day["time"], "09:30")
        self.assertIn("顺延到 2026-11-15 09:30", next_day["reason"])

    def test_next_day_hint_disappears_when_the_same_day_still_has_room(self) -> None:
        box = Toolbox(self.store, make_run("M01", requested_date="2026-11-15", requested_time="09:00"))
        result = box.call(
            "query_time_slot",
            {"material_id": "M01", "time": "09:00", "date": "2026-11-15", "reason": "看看 11-15 早间"},
        )
        self.assertFalse(result["available"])
        self.assertEqual(result["alternatives"][0], "09:30")
        self.assertIsNone(result["next_available"])


if __name__ == "__main__":
    unittest.main()
