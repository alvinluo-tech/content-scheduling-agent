from __future__ import annotations

import unittest

from src.validation import validate_run

from .support import load_example


class ValidationTest(unittest.TestCase):
    """只拦模型也修不了的结构性错误；业务错误留给工具回报。"""

    def test_example_inputs_pass(self) -> None:
        for name in ("01-normal", "02-conflict", "07-multiday", "09-channel"):
            with self.subTest(name=name):
                validate_run(load_example(name))

    def test_duplicate_material_is_refused_with_both_positions(self) -> None:
        run = {
            "requests": [
                {"material_id": "M01", "requested_time": "18:00"},
                {"material_id": "M02", "requested_time": "11:00"},
                {"material_id": "M01", "requested_time": "19:00"},
            ]
        }
        with self.assertRaises(ValueError) as caught:
            validate_run(run)
        message = str(caught.exception)
        self.assertIn("M01", message)
        self.assertIn("第 1 条和第 3 条", message)

    def test_missing_time_says_it_will_not_guess(self) -> None:
        with self.assertRaises(ValueError) as caught:
            validate_run({"requests": [{"material_id": "M01"}]})
        self.assertIn("没有发布时间要求就无法排期", str(caught.exception))

    def test_business_errors_are_not_rejected_here(self) -> None:
        # 时间不在网格、日期/频道没配置都交给工具返回给模型，而不是在入口拦掉
        validate_run(
            {
                "requests": [
                    {"material_id": "M01", "requested_time": "25:99", "requested_date": "2030-01-01", "requested_channel": "douyin"}
                ]
            }
        )

    def test_bad_max_steps_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            validate_run({"requests": [{"material_id": "M01", "requested_time": "18:00"}], "options": {"max_steps": 0}})


if __name__ == "__main__":
    unittest.main()
