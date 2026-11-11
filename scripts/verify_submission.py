"""按题面的六个验收点自查：跑一遍离线用例，逐条核对报告字段。

这是给评审用的单条命令：不用逐个打开 examples/，也不用相信任何一句自述——
每一行断言都读报告里的结构化字段（报告本身又是从工具账本反查出来的）。

    python scripts/verify_submission.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.main import run_pipeline  # noqa: E402

EXAMPLES_DIR = REPO_ROOT / "examples"
DATA_DIR = REPO_ROOT / "data"

CASES = [
    "01-normal",
    "02-conflict",
    "05-dedup",
    "03-rules-and-missing",
    "04-save-failure",
    "06-step-limit",
    "07-multiday",
    "08-day-overflow",
    "09-channel",
    "10-contention",
]


def load_case(name: str) -> dict[str, Any]:
    return json.loads((EXAMPLES_DIR / (name + ".input.json")).read_text(encoding="utf-8"))


def material(report: dict[str, Any], material_id: str) -> dict[str, Any]:
    for row in report["materials"]:
        if row["material_id"] == material_id:
            return row
    raise KeyError(material_id)


def collect() -> dict[str, Any]:
    """每个用例一份临时账本（05 故意复用 02 的账本验证去重），返回报告与 02/05 的最终账本。"""
    with tempfile.TemporaryDirectory() as tmp:
        reports = {}
        for case in CASES:
            shared = "02-conflict" if case == "05-dedup" else case
            ledger = Path(tmp) / (shared + ".json")
            reports[case], _ = run_pipeline(load_case(case), data_dir=DATA_DIR, ledger=ledger, offline=True)
        dedup_ledger = Path(tmp) / "02-conflict.json"
        book = json.loads(dedup_ledger.read_text(encoding="utf-8")) if dedup_ledger.exists() else {}
    return {"reports": reports, "ledger": book}


def check(name: str, title: str, fn: Callable[[dict[str, Any]], str]) -> dict[str, Any]:
    return {"name": name, "title": title, "fn": fn}


def _published_false(ctx):
    bad = [
        case + "/" + row["material_id"]
        for case, report in ctx["reports"].items()
        for row in report["materials"]
        if row["published"] or (row["draft"] or {}).get("published")
    ]
    return "" if not bad else "仍然出现 published=true：" + "、".join(bad)


def _normal(ctx):
    row = material(ctx["reports"]["01-normal"], "M02")
    if ctx["reports"]["01-normal"]["outcome"] != "scheduled":
        return "outcome=" + str(ctx["reports"]["01-normal"]["outcome"])
    if row["result_status"] != "scheduled_draft":
        return "result_status=" + str(row["result_status"])
    if (row["draft"] or {}).get("status") != "draft_ready":
        return "草稿不是 draft_ready"
    return ""


def _missing_info(ctx):
    report = ctx["reports"]["03-rules-and-missing"]
    m3, m4 = material(report, "M03"), material(report, "M04")
    if m3["review_status"] != "missing_info" or m3["draft"]:
        return "M03 应当 missing_info 且无草稿，实际 " + str(m3["review_status"]) + "/" + str(m3["draft"])
    if m4["review_status"] != "risk":
        return "M04 应当 risk，实际 " + str(m4["review_status"])
    rules = {item["rule_id"] for item in m4["risk_reasons"]}
    if not {"R03", "R05"} <= rules:
        return "M04 风险原因缺 R03/R05：" + str(sorted(rules))
    return ""


def _conflict(ctx):
    row = material(ctx["reports"]["02-conflict"], "M01")
    if ctx["reports"]["02-conflict"]["outcome"] != "pending_confirmation":
        return "outcome 不是 pending_confirmation"
    if row["suggested_time"] == row["requested_time"]:
        return "冲突时没有给出替代时段"
    if not (row["requires_user_confirmation"] and row["time_changed"] and not row["hard_time_preserved"]):
        return "硬性时间被改动却没标待确认"
    if (row["draft"] or {}).get("status") != "draft_pending_confirmation":
        return "草稿不是 draft_pending_confirmation"
    return ""


def _dedup(ctx):
    first = material(ctx["reports"]["02-conflict"], "M01")["draft"]
    second = material(ctx["reports"]["05-dedup"], "M01")["draft"]
    if first["draft_id"] != second["draft_id"]:
        return "重复提交换了 draft_id：" + first["draft_id"] + " → " + second["draft_id"]
    if int(second.get("submit_count", 0)) < 2:
        return "submit_count 没累加"
    same = [
        d for d in ctx["ledger"].get("drafts", {}).values()
        if d.get("material_id") == "M01"
        and d.get("date") == "2026-11-12"
        and d.get("requested_time") == "18:00"
    ]
    if len(same) != 1:
        return "账本里同一请求有 " + str(len(same)) + " 份草稿"
    return ""


def _save_failure(ctx):
    row = material(ctx["reports"]["04-save-failure"], "M05")
    if row["result_status"] != "save_failed":
        return "result_status=" + str(row["result_status"])
    if row["draft"]:
        return "保存失败却留下草稿"
    if int(row["failed_save_attempts"]) < 2:
        return "没有重试记录"
    return ""


def _step_limit(ctx):
    report = ctx["reports"]["06-step-limit"]
    if report["status"] != "step_limit_reached" or report["completed"]:
        return "没有明确报出上限：" + str(report["status"])
    if report["outcome"] != "incomplete" or not report["unfinished_materials"]:
        return "未完成状态不明确"
    return ""


def _multiday(ctx):
    report = ctx["reports"]["07-multiday"]
    m6, m7 = material(report, "M06"), material(report, "M07")
    if m6["draft"]["scheduled_date"] != "2026-11-13" or m7["draft"]["scheduled_date"] != "2026-11-13":
        return "没有按 requested_date 排到 11-13"
    if not m6["requires_user_confirmation"]:
        return "硬性 09:00 被占却没有待确认"
    return ""


def _overflow(ctx):
    row = material(ctx["reports"]["08-day-overflow"], "M01")
    if row["draft"]["scheduled_date"] != "2026-11-15":
        return "没有顺延到后一个可用日期"
    if not (row["date_changed"] and row["requires_user_confirmation"]):
        return "跨日改期没有登记待确认"
    return ""


def _channel(ctx):
    report = ctx["reports"]["09-channel"]
    rows = [material(report, mid) for mid in ("M02", "M06")]
    if any(row["channel"] != "shorts" for row in rows):
        return "素材没有被 scoped 到 shorts"
    if not all(row["draft"]["draft_id"].endswith("-shorts") for row in rows):
        return "草稿 id 没有带频道后缀"
    main_rows = [row for row in ctx["reports"]["01-normal"]["materials"] if row["channel"] != "shorts"]
    if len(main_rows) != len(ctx["reports"]["01-normal"]["materials"]):
        return "主频道用例被频道污染"
    return ""


def _contention(ctx):
    report = ctx["reports"]["10-contention"]
    booked = [(row["scheduled_date"], row["suggested_time"]) for row in report["materials"]]
    if len(booked) != len(set(booked)):
        return "同一次运行把同一时段排给了两条素材：" + str(booked)
    m2 = material(report, "M02")
    if not (m2["time_changed"] and m2["requires_user_confirmation"]):
        return "后到的硬性请求没有被改成待确认"
    return ""


def _next_action(ctx):
    """每行素材都要能直接告诉人下一步做什么，而不是只留一个状态码。"""
    for case, report in ctx["reports"].items():
        for row in report["materials"]:
            if not str(row.get("next_action") or "").strip():
                return case + "/" + row["material_id"] + " 没有 next_action"
    limited = ctx["reports"]["06-step-limit"]["materials"]
    if not all("max-steps" in str(row.get("next_action")) for row in limited):
        return "步数上限的素材没有写清怎么继续"
    return ""


CHECKS = [
    check("01", "任何情况下都不发布（published 恒为 false）", _published_false),
    check("02", "正常排期：01 落 draft_ready", _normal),
    check("03", "信息缺失与规则风险：03 的 M03 不写草稿、M04 命中 R03/R05", _missing_info),
    check("04", "时段冲突：02 给出替代时段并登记待确认", _conflict),
    check("05", "重复提交：05 复用同一份草稿", _dedup),
    check("06", "保存失败：04 如实报失败且不留草稿", _save_failure),
    check("07", "步数上限：06 返回明确的未完成状态", _step_limit),
    check("08", "多日：07 按 requested_date 排到次日", _multiday),
    check("09", "顺延：08 当天排满后跨日并待确认", _overflow),
    check("10", "频道隔离：09 只在 shorts 占位", _channel),
    check("11", "竞争：10 不对同一时段双订", _contention),
    check("12", "每行素材都带下一步动作", _next_action),
]


def run_checks(ctx: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    ctx = ctx or collect()
    results = []
    for item in CHECKS:
        try:
            detail = item["fn"](ctx)
        except Exception as exc:  # 断言自己崩了也算不通过
            detail = "自查出错：" + repr(exc)
        results.append({"name": item["name"], "title": item["title"], "ok": not detail, "detail": detail})
    return results


def main() -> int:
    results = run_checks()
    print("验收自查（离线 Mock，同一份临时账本）")
    print("")
    for row in results:
        mark = "✔" if row["ok"] else "✘"
        print(" " + mark + " " + row["name"] + " " + row["title"])
        if not row["ok"]:
            print("     " + row["detail"])
    passed = sum(1 for row in results if row["ok"])
    print("")
    print(str(passed) + "/" + str(len(results)) + " 通过")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
