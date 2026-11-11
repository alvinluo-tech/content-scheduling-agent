"""验收自查脚本本身要有牙：全套通过，且篡改报告时必须变红。"""

from __future__ import annotations

import copy
import importlib.util
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def load_verifier():
    path = REPO_ROOT / "scripts" / "verify_submission.py"
    spec = importlib.util.spec_from_file_location("verify_submission", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class VerifySubmissionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = load_verifier()
        cls.ctx = cls.mod.collect()

    def test_every_acceptance_point_passes_on_the_archived_examples(self):
        results = self.mod.run_checks(self.ctx)
        failed = [row["name"] + " " + row["title"] + "：" + row["detail"] for row in results if not row["ok"]]
        self.assertEqual(failed, [])
        self.assertEqual(len(results), 11)

    def test_a_claim_of_publishing_is_caught(self):
        ctx = copy.deepcopy(self.ctx)
        ctx["reports"]["01-normal"]["materials"][0]["published"] = True
        results = {row["name"]: row for row in self.mod.run_checks(ctx)}
        self.assertFalse(results["01"]["ok"])
        self.assertIn("published=true", results["01"]["detail"])

    def test_a_draft_after_a_save_failure_is_caught(self):
        ctx = copy.deepcopy(self.ctx)
        row = self.mod.material(ctx["reports"]["04-save-failure"], "M05")
        row["draft"] = {"draft_id": "D-M05-2026-11-12-2000", "status": "draft_ready"}
        results = {row["name"]: row for row in self.mod.run_checks(ctx)}
        self.assertFalse(results["06"]["ok"])
        self.assertIn("保存失败却留下草稿", results["06"]["detail"])


if __name__ == "__main__":
    unittest.main()
