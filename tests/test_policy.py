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


if __name__ == "__main__":
    unittest.main()
