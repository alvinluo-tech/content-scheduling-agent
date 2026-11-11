from __future__ import annotations

import json
import unittest
from pathlib import Path
from typing import Any

from src.store import LedgerBusy, Store, locked_file

from .support import DATA_DIR, temp_ledger


def _draft_record(material_id: str, scheduled_time: str) -> dict[str, Any]:
    return {
        "draft_id": f"D-{material_id}-2026-11-12-1800",
        "date": "2026-11-12",
        "channel": "main",
        "material_id": material_id,
        "requested_date": "2026-11-12",
        "requested_time": "18:00",
        "scheduled_time": scheduled_time,
        "status": "draft_ready",
        "time_changed": scheduled_time != "18:00",
        "date_changed": False,
        "updated_at": "2026-11-12T09:20:00",
    }


class DraftRevokeTest(unittest.TestCase):
    """已存草稿也要能被撤回：撤回后不再占位，未知 id 直接报错。"""

    def setUp(self) -> None:
        self._ctx = temp_ledger()
        self.ledger: Path = self._ctx.__enter__()
        self.store = Store(DATA_DIR, self.ledger)
        self.store.upsert_draft(_draft_record("M01", "19:00"))

    def tearDown(self) -> None:
        self._ctx.__exit__(None, None, None)

    def test_revoke_frees_the_slot_and_is_idempotent(self) -> None:
        self.assertEqual([slot["time"] for slot in self.store.drafted_slots("2026-11-12", channel="main")], ["19:00"])
        revoked = self.store.revoke_draft("D-M01-2026-11-12-1800")
        self.assertEqual(revoked["status"], "draft_revoked")
        self.assertEqual(revoked["revoked_by"], "human")
        self.assertTrue(revoked["revoked_at"])
        self.assertEqual(self.store.drafted_slots("2026-11-12", channel="main"), [])
        self.assertEqual(self.store.revoke_draft("D-M01-2026-11-12-1800")["status"], "draft_revoked")

    def test_a_recreated_draft_does_not_keep_the_revoke_marks(self) -> None:
        self.store.revoke_draft("D-M01-2026-11-12-1800")
        record, created = self.store.upsert_draft(_draft_record("M01", "19:00"))
        self.assertFalse(created)
        self.assertEqual(record["status"], "draft_ready")
        self.assertEqual(record["submit_count"], 2)
        self.assertNotIn("revoked_at", record)
        self.assertEqual([slot["time"] for slot in self.store.drafted_slots("2026-11-12", channel="main")], ["19:00"])

    def test_unknown_draft_is_refused(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            self.store.revoke_draft("D-M99-2026-11-12-1800")
        self.assertIn("没有草稿", str(ctx.exception))


class LedgerConcurrencyTest(unittest.TestCase):
    """账本是"读-改-写"：两个进程各持一份内存快照时，后来的写不能把前一个的写入抹掉。"""

    def setUp(self) -> None:
        self._ctx = temp_ledger()
        self.ledger: Path = self._ctx.__enter__()

    def tearDown(self) -> None:
        self._ctx.__exit__(None, None, None)

    def test_two_instances_do_not_overwrite_each_others_drafts(self) -> None:
        first = Store(DATA_DIR, self.ledger)
        second = Store(DATA_DIR, self.ledger)  # 相当于另一个进程：内存里看不到 first 的写入
        self.assertEqual(first.drafts, {})

        first.upsert_draft(_draft_record("M01", "19:00"))
        second.upsert_draft(_draft_record("M02", "11:00"))

        reader = Store(DATA_DIR, self.ledger)
        self.assertEqual(len(reader.drafts), 2)
        self.assertIsNotNone(reader.draft("2026-11-12", "M01", "18:00"))
        self.assertIsNotNone(reader.draft("2026-11-12", "M02", "18:00"))

    def test_a_confirmation_written_elsewhere_is_not_lost_by_a_later_draft(self) -> None:
        writer = Store(DATA_DIR, self.ledger)
        writer.add_confirmation(
            {
                "confirmation_id": "C-M01-2026-11-12-1900",
                "date": "2026-11-12",
                "channel": "main",
                "requested_date": "2026-11-12",
                "requested_time": "18:00",
                "material_id": "M01",
                "proposed_date": "2026-11-12",
                "proposed_time": "19:00",
                "status": "pending",
            }
        )
        latecomer = Store(DATA_DIR, self.ledger)  # 载入时还没有那条确认
        latecomer.upsert_draft(_draft_record("M05", "20:00"))

        reader = Store(DATA_DIR, self.ledger)
        self.assertIn("C-M01-2026-11-12-1900", reader.confirmations)
        self.assertEqual(len(reader.drafts), 1)

    def test_waiting_on_a_held_lock_gives_up_loudly_instead_of_writing(self) -> None:
        self.ledger.write_text(json.dumps({"drafts": {}, "confirmations": {}}), encoding="utf-8")
        held = locked_file(self.ledger.with_name(self.ledger.name + ".lock"))
        with held:
            with self.assertRaises(LedgerBusy) as caught:
                with locked_file(self.ledger.with_name(self.ledger.name + ".lock"), timeout=0.2):
                    pass
        self.assertIn("被其它进程占用", str(caught.exception))
        # 锁释放后照常可用
        with locked_file(self.ledger.with_name(self.ledger.name + ".lock"), timeout=1.0):
            pass


if __name__ == "__main__":
    unittest.main()
