from __future__ import annotations

import unittest

from src.policy import (
    STATUS_MISSING,
    STATUS_PASS,
    STATUS_RISK,
    STATUS_WARN,
    evaluate_rules,
    find_conflicts,
    next_free_slots,
    rank_alternatives,
    slot_grid,
)
from src.store import Store

from .support import DATA_DIR, temp_ledger


class PolicyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        with temp_ledger() as ledger:
            cls.store = Store(DATA_DIR, ledger)
        cls.rules = cls.store.rules_doc

    def review(self, material_id: str) -> dict:
        return evaluate_rules(self.store.material(material_id), self.rules)

    def test_pass_material_is_draftable(self) -> None:
        review = self.review("M01")
        self.assertEqual(review["review_status"], STATUS_PASS)
        self.assertTrue(review["can_draft"])
        self.assertEqual(review["violations"], [])

    def test_warning_does_not_block_drafting(self) -> None:
        review = self.review("M02")
        self.assertEqual(review["review_status"], STATUS_WARN)
        self.assertTrue(review["can_draft"])
        self.assertIn("R04", [v["rule_id"] for v in review["violations"]])

    def test_missing_info_blocks_drafting(self) -> None:
        review = self.review("M03")
        self.assertEqual(review["review_status"], STATUS_MISSING)
        self.assertFalse(review["can_draft"])
        self.assertIn("duration_sec", review["missing_fields"])
        self.assertIn("authorized", review["missing_fields"])

    def test_banned_term_is_risk(self) -> None:
        review = self.review("M04")
        self.assertEqual(review["review_status"], STATUS_RISK)
        self.assertFalse(review["can_draft"])
        self.assertIn("R03", [v["rule_id"] for v in review["violations"]])

    def test_slot_grid_matches_operating_hours(self) -> None:
        grid = slot_grid(self.store.operating_hours())
        self.assertEqual(grid[0], "09:00")
        self.assertEqual(grid[-1], "20:30")
        self.assertEqual(len(grid), 24)

    def test_conflict_and_alternatives(self) -> None:
        occupied = self.store.occupied_slots()
        self.assertEqual([c["material_id"] for c in find_conflicts(occupied, "18:00")], ["M00"])
        self.assertEqual(find_conflicts(occupied, "19:00"), [])
        self.assertEqual(next_free_slots(self.store.operating_hours(), occupied, "18:00")[0], "19:00")

    def test_ranked_alternatives_prefer_the_closest_free_slot(self) -> None:
        hours = self.store.operating_hours()
        occupied = self.store.occupied_slots()
        ranked = rank_alternatives(hours, occupied, "18:00", preferences={"prefer_windows": [["18:00", "21:00"]]})
        self.assertEqual(ranked[0]["time"], "19:00")
        self.assertTrue(ranked[0]["recommended"])
        self.assertTrue(all(row["reasons"][0].startswith("距请求时段 +") for row in ranked))
        self.assertEqual([row["recommended"] for row in ranked].count(True), 1)
        self.assertTrue(any("偏好时段 18:00–21:00" in reason for reason in ranked[0]["reasons"]))

    def test_min_gap_penalty_pushes_the_candidate_away_from_a_neighbour(self) -> None:
        hours = self.store.operating_hours()
        occupied = [{"time": "09:00", "material_id": "M02"}]
        tight = rank_alternatives(hours, occupied, "09:00", preferences={"min_gap_minutes": 60})
        loose = rank_alternatives(hours, occupied, "09:00", preferences={})
        self.assertEqual(loose[0]["time"], "09:30")
        self.assertEqual(tight[0]["time"], "10:00")
        self.assertTrue(any("只隔 30 分钟" in reason for reason in tight[1]["reasons"]))


if __name__ == "__main__":
    unittest.main()
