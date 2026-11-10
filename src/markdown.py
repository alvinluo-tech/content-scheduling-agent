from __future__ import annotations

from typing import Any

OUTCOME_LABEL = {
    "scheduled": "已排期（仅草稿）",
    "pending_confirmation": "待用户确认",
    "partial": "部分完成",
    "failed": "存在失败项",
    "incomplete": "步数上限中断",
}

STATUS_LABEL = {
    "completed": "completed（推理正常收尾）",
    "step_limit_reached": "step_limit_reached（达到步数上限）",
    "model_error": "model_error（模型调用失败）",
    "tool_error": "tool_error（工具异常）",
    "aborted": "aborted（被中止）",
}

RESULT_LABEL = {
    "scheduled_draft": "已存草稿",
    "scheduled_pending_confirmation": "待确认草稿",
    "needs_info": "信息缺失",
    "blocked": "已阻断（规则风险）",
    "save_failed": "保存失败",
    "confirmation_rejected": "用户已否决该改期",
    "material_not_found": "素材不存在",
    "unresolved": "未完成",
    "not_processed": "未处理",
}

REVIEW_LABEL = {
    "pass": "通过",
    "pass_with_warnings": "通过（有警告）",
    "risk": "规则风险",
    "missing_info": "信息缺失",
}

DRAFT_LABEL = {
    "draft_ready": "draft_ready（可直接用）",
    "draft_pending_confirmation": "draft_pending_confirmation（待用户点头）",
    "draft_rejected": "draft_rejected（用户否决后作废）",
}

PENDING_LABEL = {"pending": "pending 待拍板", "approved": "approved 已同意", "rejected": "rejected 已否决"}
SEVERITY_MARK = {"blocking": "阻断", "warning": "警告", "manual_review": "人工复核"}
TICK = chr(96)


def _code(text: Any) -> str:
    return TICK + str(text) + TICK


def _yes(value: Any) -> str:
    return "是" if value else "否"


def _material_section(row: dict[str, Any], index: int) -> list[str]:
    title = row.get("title") or "（标题缺失）"
    lines = ["### " + str(index) + ". " + row["material_id"] + " · " + title, ""]
    lines.append("- 结论：**" + RESULT_LABEL.get(row["result_status"], row["result_status"]) + "**")
    lines.append("- 审核：" + REVIEW_LABEL.get(row["review_status"], row["review_status"]))
    if row.get("missing_fields"):
        lines.append("- 缺失字段：" + "、".join(row["missing_fields"]))
    for reason in row.get("risk_reasons") or []:
        mark = SEVERITY_MARK.get(reason.get("severity"), str(reason.get("severity")))
        lines.append("- 风险 " + _code(reason.get("rule_id")) + "（" + mark + "）：" + str(reason.get("reason")))
    lines.append(
        "- 用户要求：" + str(row.get("requested_date")) + " " + str(row.get("requested_time"))
        + "（硬性=" + ("是" if row.get("time_hard") else "否") + "）"
    )
    if row.get("suggested_time"):
        moved = []
        if row.get("date_changed"):
            moved.append("日期已调整")
        if row.get("time_changed"):
            moved.append("时间已调整")
        where = row.get("scheduled_date") or row.get("requested_date")
        suffix = "（" + "、".join(moved) + "）" if moved else "（未改动）"
        lines.append("- 建议：" + str(where) + " " + str(row["suggested_time"]) + suffix)
        lines.append("- 硬性要求保持不变：" + _yes(row.get("hard_time_preserved")))
    for item in row.get("pending_confirmations") or []:
        lines.append(
            "- 待确认 " + _code(item["confirmation_id"]) + "（" + PENDING_LABEL.get(str(item["status"]), str(item["status"])) + "）："
            + str(item["question"]) + " → 建议 " + str(item.get("proposed_date")) + " " + str(item["proposed_time"])
        )
    draft = row.get("draft")
    if draft:
        lines.append(
            "- 草稿 " + _code(draft["draft_id"]) + "：" + DRAFT_LABEL.get(draft["status"], draft["status"])
            + "，" + str(draft.get("scheduled_date")) + " " + str(draft["scheduled_time"])
            + "，提交 " + str(draft.get("submit_count", 1)) + " 次"
        )
    elif row.get("failed_save_attempts"):
        lines.append("- 草稿：未保存（失败 " + str(row["failed_save_attempts"]) + " 次，未谎报成功）")
    elif row["result_status"] in {"needs_info", "blocked"}:
        lines.append("- 草稿：未保存（审核未通过，工具拒绝写入）")
    else:
        lines.append("- 草稿：无")
    lines.append("- 已发布：" + _yes(row.get("published")) + "（工具集里没有发布能力）")
    lines.append("")
    return lines


def _trace_table(trace: dict[str, Any]) -> list[str]:
    steps = (trace or {}).get("steps") or []
    if not steps:
        return ["（没有工具调用记录）", ""]
    lines = ["| 步 | 动作 | 决策依据 | 观察 |", "| --- | --- | --- | --- |"]
    for step in steps:
        action = step.get("action") or {}
        args = action.get("args") or {}
        target = args.get("material_id") or ""
        detail = " ".join(
            part for part in (target, args.get("date") or "", args.get("time") or args.get("scheduled_time") or "") if part
        )
        tool = (_code(action.get("tool")) + " " + detail).strip()
        rationale = str(step.get("rationale") or "").replace("|", "\\|")
        observation = step.get("observation") or {}
        summary = str(observation.get("summary") or "").replace("|", "\\|")
        ok = "OK" if observation.get("ok") else "FAIL"
        lines.append(
            "| " + str(step.get("step")) + " | " + tool + " | " + rationale + " | " + ok + " · " + summary + " |"
        )
    lines.append("")
    return lines


def render_report(report: dict[str, Any], trace: dict[str, Any] | None = None) -> str:
    """把同一份 report 渲染成人读的 Markdown；所有数字都来自报告，不额外推断。"""
    limits = report.get("limits") or {}
    summary = report.get("summary") or {}
    dates = report.get("dates") or [report.get("date")]
    model = report.get("model") or {}
    lines = [
        "# 排期报告 · " + str(report.get("run_id", "run")),
        "",
        "> 只保存草稿，未接真实发布通道；published 恒为 false。",
        "",
        "- 可排日期：" + "、".join(str(d) for d in dates),
        "- 状态：" + STATUS_LABEL.get(report.get("status"), str(report.get("status"))),
        "- 结果：**" + OUTCOME_LABEL.get(report.get("outcome"), str(report.get("outcome"))) + "**",
        "- 步数：" + str(limits.get("steps_used")) + " / " + str(limits.get("max_steps"))
        + "（工具调用 " + str(limits.get("tool_calls")) + " 次）",
        "- 模型：" + str(model.get("name")) + "（" + str(model.get("mode")) + "）",
        "- 已发布：" + _yes(report.get("published")),
        "",
        "| 素材 | 结论 | 草稿 |",
        "| --- | --- | --- |",
    ]
    for row in report.get("materials") or []:
        draft = row.get("draft")
        draft_text = _code(draft["draft_id"]) + " " + draft["status"] if draft else "无"
        lines.append(
            "| " + row["material_id"] + " | " + RESULT_LABEL.get(row["result_status"], row["result_status"])
            + " | " + draft_text + " |"
        )
    counts = "，".join(RESULT_LABEL.get(k, k) + " " + str(v) for k, v in summary.items() if k != "total")
    lines += ["", "合计 " + str(summary.get("total", 0)) + " 项：" + counts, "", "## 素材逐项", ""]
    for index, row in enumerate(report.get("materials") or [], start=1):
        lines += _material_section(row, index)
    if report.get("unfinished_materials"):
        lines += ["⚠️ 未完成素材：" + "、".join(report["unfinished_materials"]), ""]
    summary_text = str(report.get("agent_summary") or "").strip()
    if summary_text:
        lines += ["## 模型自述（不是证据，结论以上面的报告字段为准）", "", summary_text, ""]
    if trace:
        lines += ["## 决策记录", ""] + _trace_table(trace)
    lines += ["---", "", str(report.get("note", "")), ""]
    return chr(10).join(lines)
