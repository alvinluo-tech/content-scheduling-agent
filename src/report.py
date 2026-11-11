from __future__ import annotations

from typing import Any

from .policy import evaluate_rules
from .store import Store
from .tools import Toolbox
from .trace import TraceRecorder

CONCLUDED = {
    "scheduled_draft",
    "scheduled_pending_confirmation",
    "needs_info",
    "blocked",
    "save_failed",
    "confirmation_rejected",
    "draft_revoked",
    "material_not_found",
}
FAILED_STATUSES = {"save_failed", "blocked", "needs_info", "confirmation_rejected", "draft_revoked", "material_not_found"}


def _outcome(statuses: list[str], terminal: str) -> str:
    """completed 只说明"推理正常结束且每项素材都有结论"，是否真的排上要用 outcome 表达。"""
    if terminal != "completed" or not statuses:
        return "incomplete"
    if any(s == "scheduled_pending_confirmation" for s in statuses):
        return "pending_confirmation"
    if all(s == "scheduled_draft" for s in statuses):
        return "scheduled"
    if all(s in FAILED_STATUSES for s in statuses):
        return "failed"
    return "partial"


def _material_status(
    request: dict[str, Any],
    review: dict[str, Any],
    draft: dict[str, Any] | None,
    touched: bool,
    failed_save_attempts: int,
) -> str:
    if not review["can_draft"]:
        return "needs_info" if review["review_status"] == "missing_info" else "blocked"
    if draft is not None and draft["status"] == "draft_rejected":
        return "confirmation_rejected"
    if draft is not None and draft["status"] == "draft_revoked":
        return "draft_revoked"
    if draft is not None:
        return "scheduled_pending_confirmation" if draft["status"] == "draft_pending_confirmation" else "scheduled_draft"
    if failed_save_attempts:
        return "save_failed"
    if touched:
        return "unresolved"
    return "not_processed"


NEXT_ACTION = {
    "scheduled_draft": "无（草稿可直接进入人工发布流程）",
    "needs_info": "补齐缺失字段后重跑",
    "blocked": "按风险原因修改素材后重跑",
    "save_failed": "保存接口失败；确认磁盘与权限后重跑（不会谎报成功）",
    "material_not_found": "核对素材 ID 是否在素材库里",
    "confirmation_rejected": "该改期已被否决；换一个时段或给出新的要求",
    "draft_revoked": "草稿已被撤回；需要时重跑同一请求重新起草",
    "unresolved": "步数上限中断；调高 --max-steps 后重跑",
    "not_processed": "本次未处理；调高 --max-steps 或单独提交这条素材",
}


def _next_action(status: str, review: dict[str, Any], draft: dict[str, Any] | None) -> str:
    """报告要能直接告诉人下一步做什么，而不是只给一个状态码。"""
    if status == "scheduled_pending_confirmation" and draft is not None:
        return "等人确认 " + str(draft["date"]) + " " + str(draft["scheduled_time"]) + " 这个替代时段；确认后重跑即落 draft_ready"
    text = NEXT_ACTION.get(status, "")
    if status == "needs_info" and review.get("missing_fields"):
        text += "：" + "、".join(review["missing_fields"])
    if status == "blocked":
        rules = [v["rule_id"] for v in review.get("violations", []) if v.get("severity") == "blocking"]
        if rules:
            text += "（" + "、".join(rules) + "）"
    return text


def build_report(run: dict[str, Any], store: Store, toolbox: Toolbox, trace: TraceRecorder, limits: dict[str, Any]) -> dict[str, Any]:
    date = store.schedule_date()
    default_channel = store.default_channel()
    rows: list[dict[str, Any]] = []
    for request in run.get("requests", []):
        material_id = request["material_id"]
        channel = request.get("requested_channel") or default_channel
        requested_date = request.get("requested_date") or date
        material = store.material(material_id)
        touched = any((call.get("args") or {}).get("material_id") == material_id for call in toolbox.calls)
        if material is None:
            rows.append(
                {
                    "material_id": material_id,
                    "channel": channel,
                    "requested_date": requested_date,
                    "requested_time": request["requested_time"],
                    "time_hard": bool(request.get("time_hard")),
                    "review_status": "not_found",
                    "risk_reasons": [],
                    "missing_fields": [],
                    "suggested_time": None,
                    "hard_time_preserved": True,
                    "requires_user_confirmation": False,
                    "pending_confirmations": [],
                    "slot_options": [],
                    "draft": None,
                    "published": False,
                    "result_status": "material_not_found",
                }
            )
            continue
        review = evaluate_rules(material, store.rules_doc)
        draft = store.draft(requested_date, material_id, request["requested_time"], channel)
        confirmations = [
            c for c in store.confirmations_for(requested_date, material_id, request["requested_time"], channel)
        ]
        date_changed = bool((draft or {}).get("date_changed"))
        # 工具查过哪些替代时段：人改选时得有据可依，不用自己瞎填时间。
        last_query = toolbox.last_query.get(material_id, {})
        slot_options = [
            {"date": requested_date, "time": item, "kind": "same_day"}
            for item in (last_query.get("alternatives") or [])
        ]
        next_day = last_query.get("next_available")
        if next_day:
            slot_options.append({"date": next_day["date"], "time": next_day["time"], "kind": "next_day"})
        status = _material_status(request, review, draft, touched, toolbox.attempts.get(material_id, 0))
        rows.append(
            {
                "material_id": material_id,
                "title": material.get("title"),
                "channel": channel,
                "channel_label": store.channel_label(channel),
                "requested_date": requested_date,
                "requested_time": request["requested_time"],
                "time_hard": bool(request.get("time_hard")),
                "review_status": review["review_status"],
                "risk_reasons": [
                    {"rule_id": v["rule_id"], "severity": v["severity"], "reason": v["reason"]} for v in review["violations"]
                ],
                "missing_fields": review["missing_fields"],
                "scheduled_date": (draft or {}).get("date"),
                "suggested_time": (draft or {}).get("scheduled_time"),
                "time_changed": (draft or {}).get("time_changed", False),
                "date_changed": date_changed,
                "hard_time_preserved": not ((draft or {}).get("time_changed", False) or date_changed),
                "requires_user_confirmation": any(c["status"] == "pending" for c in confirmations),
                "rejected_confirmations": [c["confirmation_id"] for c in confirmations if c.get("status") == "rejected"],
                "pending_confirmations": [
                    {
                        "confirmation_id": c["confirmation_id"],
                        "question": c.get("question", ""),
                        "proposed_date": c.get("proposed_date", c.get("date")),
                        "proposed_time": c["proposed_time"],
                        "status": c["status"],
                        # 人的拍板要留痕：谁、什么时候、是同意这一版还是改选了别的时段。
                        "decided_by": c.get("approved_by") or c.get("rejected_by") or c.get("decided_by"),
                        "decided_at": c.get("approved_at") or c.get("rejected_at") or c.get("decided_at"),
                        "replanned_from": c.get("replanned_from"),
                    }
                    for c in confirmations
                ],
                "slot_options": slot_options,
                "draft": None if draft is None else {
                    "draft_id": draft["draft_id"],
                    "channel": draft.get("channel", default_channel),
                    "scheduled_date": draft["date"],
                    "scheduled_time": draft["scheduled_time"],
                    "status": draft["status"],
                    "submit_count": draft.get("submit_count", 1),
                    "published": False,
                },
                "failed_save_attempts": toolbox.attempts.get(material_id, 0),
                "published": False,
                "result_status": status,
                "next_action": _next_action(status, review, draft),
            }
        )

    unfinished = [row["material_id"] for row in rows if row["result_status"] not in CONCLUDED]
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["result_status"]] = counts.get(row["result_status"], 0) + 1

    statuses = [row["result_status"] for row in rows]
    completed = trace.terminal == "completed" and not unfinished
    return {
        "run_id": run.get("run_id", "run"),
        "date": date,
        "channel": default_channel,
        "channels": store.channels(),
        "dates": store.available_dates(),
        "status": trace.terminal,
        "completed": completed,
        "outcome": _outcome(statuses, trace.terminal),
        "model": {"mode": trace.mode, "name": trace.model_name},
        "limits": limits,
        "summary": {"total": len(rows), **counts},
        "materials": rows,
        "unfinished_materials": unfinished,
        "published": False,
        "agent_summary": trace.final_message,
        "note": "仅保存排期草稿，未接真实发布通道；published 恒为 false。",
    }
