"""按顺序重跑 examples/ 下全部用例，生成可评审的输出与 trace。

用法（仓库根目录）：
    python scripts/run_examples.py

行为：每个用例前重置草稿账本（data/drafts.json），但 05-dedup 必须跟在 02-conflict 之后，
因为它验证的是同一请求重复提交只产生一份草稿。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples"
LEDGER = ROOT / "data" / "drafts.json"
EMPTY_LEDGER = {"drafts": {}, "confirmations": {}}


def reset_ledger() -> None:
    LEDGER.write_text(json.dumps(EMPTY_LEDGER, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def run(case: str, reset: bool = True) -> dict:
    if reset:
        reset_ledger()
    output = EXAMPLES / f"{case}.output.json"
    trace = EXAMPLES / f"{case}.trace.json"
    cmd = [
        sys.executable,
        "-m",
        "src.main",
        "--input",
        f"examples/{case}.input.json",
        "--offline",
        "--out",
        f"examples/{case}.output.json",
        "--trace",
        f"examples/{case}.trace.json",
        "--report-md",
        f"examples/{case}.report.md",
    ]
    completed = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
    if completed.returncode != 0:
        raise SystemExit(f"{case} 执行失败（退出码 {completed.returncode}）：\n{completed.stderr}")
    return json.loads(output.read_text(encoding="utf-8"))


def main() -> int:
    plan = [
        ("01-normal", True),
        ("02-conflict", True),
        ("05-dedup", False),  # 复用 02 的账本，验证去重
        ("03-rules-and-missing", True),
        ("04-save-failure", True),
        ("06-step-limit", True),
        ("07-multiday", True),
        ("08-day-overflow", True),
        ("09-channel", True),
        ("10-contention", True),
    ]
    rows = []
    for case, reset in plan:
        report = run(case, reset)
        rows.append(
            (
                case,
                report["status"],
                report["outcome"],
                report["limits"]["steps_used"],
                report["limits"]["tool_calls"],
                json.dumps(report["summary"], ensure_ascii=False),
            )
        )
    reset_ledger()
    header = ("case", "status", "outcome", "steps", "tools", "summary")
    widths = [max(len(str(row[i])) for row in [header, *rows]) for i in range(len(header))]
    for row in [header, *rows]:
        print("  ".join(str(cell).ljust(widths[i]) for i, cell in enumerate(row)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
