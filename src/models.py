from __future__ import annotations

import json
import os
from typing import Any, Sequence

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage

SYSTEM_TEMPLATE = """你是短视频内容团队的「素材审核与发布排期助手」。今天是 {date}，营业时段 {start}-{end}，{granularity} 分钟粒度。
可排日期：{dates}（不带日期的请求默认排 {date}）。

本次排期请求（用户原始要求，按顺序处理，共 {count} 条）：
{requests}

可用工具：read_material、check_rules、query_time_slot、request_confirmation、save_draft。

必须遵守：
1. 每调用一个工具，reason 参数必须写清"看到了什么、为什么这一步"。
2. 只保存草稿，你没有任何发布能力；任何时候都不得说已发布、已上线、已确认。
3. 用户标了 hard=true 的发布时间是硬性要求：要改成别的时段，必须先 request_confirmation 登记待确认项，再 save_draft。
4. 审核未通过（can_draft=false）的素材不得保存草稿：缺信息就说缺什么，命中规则就说命中哪条，交给人处理。
5. 时段被占用时不要抢占用时段，查替代时段并说明冲突。
6. 每项素材处理完（保存成功 / 被阻断）就处理下一条；全部处理完后不要再调用工具，用一段话总结你做了什么、哪些还需要用户拍板。
7. 工具返回 ok=false 时读 error.code 与 error.hint 决定下一步；不要忽略失败，也不要把失败说成成功。
"""


def build_system_prompt(
    run: dict[str, Any], schedule_date: str, hours: dict[str, Any], available_dates: Sequence[str] | None = None
) -> str:
    requests = run.get("requests", [])
    lines = []
    for req in requests:
        hard = "hard=true（不可擅自更改）" if req.get("time_hard") else "hard=false（可协商，但仍需说明）"
        note = f"，备注：{req['note']}" if req.get("note") else ""
        day = req.get("requested_date") or schedule_date
        lines.append(f"- {req['material_id']}：要求 {day} {req['requested_time']} 发布，{hard}{note}")
    dates = list(available_dates or [schedule_date])
    return SYSTEM_TEMPLATE.format(
        date=schedule_date,
        dates="、".join(dates),
        start=hours["start"],
        end=hours["end"],
        granularity=hours["granularity_min"],
        count=len(requests),
        requests="\n".join(lines) if lines else "- 无",
    )


DATE_AWARE_TOOLS = {"query_time_slot", "request_confirmation", "save_draft"}


class ScriptedChatModel:
    """离线 Mock：按确定性策略产出工具调用，只验证流程，不代表真实模型决策质量。"""

    mode = "offline-scripted"

    def __init__(self, run: dict[str, Any]) -> None:
        self.requests = list(run.get("requests", []))
        self._seq = 0

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> "ScriptedChatModel":
        return self

    def _call(self, name: str, args: dict[str, Any]) -> AIMessage:
        self._seq += 1
        return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": f"call_{self._seq:03d}"}])

    def invoke(self, messages: Sequence[BaseMessage], **kwargs: Any) -> AIMessage:
        history = _tool_history(messages)
        for req in self.requests:
            action = _next_action(req, history)
            if action is not None:
                name, args = action
                if name in DATE_AWARE_TOOLS and req.get("requested_date") and "date" not in args:
                    args["date"] = req["requested_date"]
                return self._call(name, args)
        saves = [h for h in history if h["tool"] == "save_draft"]
        stored = sum(1 for h in saves if (h["result"] or {}).get("ok"))
        failed = len(saves) - stored
        tail = f"，其中草稿写入成功 {stored} 次、失败 {failed} 次" if failed else f"，其中草稿保存 {stored} 次"
        return AIMessage(
            content=(
                f"离线 Mock 执行结束：共 {len(history)} 次工具调用{tail}。"
                "本结果只验证流程正确性，不代表真实模型的决策质量。"
            )
        )


def _tool_history(messages: Sequence[BaseMessage]) -> list[dict[str, Any]]:
    pending: dict[str, dict[str, Any]] = {}
    history: list[dict[str, Any]] = []
    for message in messages:
        if isinstance(message, AIMessage):
            for call in message.tool_calls or []:
                pending[call["id"]] = {"tool": call["name"], "args": dict(call.get("args") or {})}
        elif isinstance(message, ToolMessage):
            entry = pending.get(message.tool_call_id, {"tool": message.name or "?", "args": {}})
            try:
                entry["result"] = json.loads(message.content if isinstance(message.content, str) else "{}")
            except json.JSONDecodeError:
                entry["result"] = {"ok": False, "error": {"code": "BAD_PAYLOAD"}}
            history.append(entry)
    return history


def _next_action(req: dict[str, Any], history: list[dict[str, Any]]) -> tuple[str, dict[str, Any]] | None:
    material_id = req["material_id"]
    calls = [h for h in history if (h.get("args") or {}).get("material_id") == material_id]

    def latest(name: str) -> dict[str, Any] | None:
        hits = [c for c in calls if c["tool"] == name]
        return hits[-1] if hits else None

    read, check = latest("read_material"), latest("check_rules")
    query, confirm, save = latest("query_time_slot"), latest("request_confirmation"), latest("save_draft")

    if read is None:
        return "read_material", {"material_id": material_id, "reason": f"先读取 {material_id} 的元数据，确认信息是否齐全"}
    if read["result"].get("ok") is False:
        return None
    if check is None:
        return "check_rules", {"material_id": material_id, "reason": f"{material_id} 元数据已读取，按审核规则校验风险"}
    check_result = check["result"]
    if check_result.get("ok") is False:
        return None
    if not check_result.get("can_draft"):
        refetched = len([c for c in calls if c["tool"] == "read_material"]) > 1
        if read["result"].get("missing_required_fields") and not refetched:
            return "read_material", {"material_id": material_id, "reason": "规则判定信息缺失，补查一次素材元数据"}
        return None
    if save is not None and save["result"].get("ok"):
        return None
    if save is not None:
        code = (save["result"].get("error") or {}).get("code")
        attempts = len([c for c in calls if c["tool"] == "save_draft"])
        if code == "SAVE_FAILED":
            if attempts < 2:
                return "save_draft", {
                    "material_id": material_id,
                    "scheduled_time": save["args"].get("scheduled_time", req["requested_time"]),
                    "reason": "保存返回失败，重试一次确认是偶发还是持续失败",
                }
            return None
        if code == "CONFIRMATION_REJECTED":
            return None
        if code == "HARD_CONSTRAINT_VIOLATION" and confirm is None:
            proposal = _proposal(query, req)
            return "request_confirmation", {
                **_proposal_args(material_id, proposal),
                "proposed_time": proposal["time"],
                "question": _proposal_question(req, proposal),
                "reason": "保存被硬性约束拦截，先登记待确认项",
            }
    if query is None:
        return "query_time_slot", {
            "material_id": material_id,
            "time": req["requested_time"],
            "reason": f"检查用户要求的 {req['requested_time']} 是否可排",
        }
    query_result = query["result"]
    if query_result.get("ok") and query_result.get("available"):
        return "save_draft", {"material_id": material_id, "scheduled_time": req["requested_time"], "reason": "要求的时段可用且审核通过，保存草稿"}
    proposal = _proposal(query, req)
    if req.get("time_hard") and confirm is None:
        return "request_confirmation", {
            **_proposal_args(material_id, proposal),
            "proposed_time": proposal["time"],
            "question": _proposal_question(req, proposal),
            "reason": "冲突 + 硬性要求，不能擅自改时间，先登记待确认项",
        }
    if confirm is not None and not confirm["result"].get("ok"):
        return None
    return "save_draft", {
        **_proposal_args(material_id, proposal),
        "scheduled_time": proposal["time"],
        "reason": f"{req['requested_time']} 冲突，改用替代时段 {proposal['time']} 保存草稿",
    }


def _proposal(query: dict[str, Any] | None, req: dict[str, Any]) -> dict[str, Any]:
    """冲突时该把草稿放到哪个（日期，时段）：先用当天替代时段，当天排不下才顺延到后面配置过的日期。"""
    requested_date = req.get("requested_date")
    fallback = {"time": req["requested_time"], "date": requested_date, "next_day": False}
    if not query:
        return fallback
    result = query["result"]
    alternatives = result.get("alternatives") or []
    if alternatives:
        return {"time": alternatives[0], "date": requested_date, "next_day": False}
    next_day = result.get("next_available")
    if next_day:
        return {"time": next_day["time"], "date": next_day["date"], "next_day": True}
    return fallback


def _proposal_args(material_id: str, proposal: dict[str, Any]) -> dict[str, Any]:
    args: dict[str, Any] = {"material_id": material_id}
    if proposal.get("date"):
        args["date"] = proposal["date"]
    return args


def _proposal_question(req: dict[str, Any], proposal: dict[str, Any]) -> str:
    day = f"{req['requested_date']} " if req.get("requested_date") else ""
    if proposal["next_day"]:
        return f"{day}{req['requested_time']} 是硬性要求，但当天更晚的时段已排满，是否同意顺延到 {proposal['date']} {proposal['time']}？"
    return f"{day}{req['requested_time']} 是硬性要求但已被占用，是否同意改到 {proposal['time']}？"


def build_chat_model(mode: str, run: dict[str, Any]) -> Any:
    if mode == "offline":
        return ScriptedChatModel(run)
    from langchain_openai import ChatOpenAI

    api_key = os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("缺少 DEEPSEEK_API_KEY；离线演示请加 --offline（Mock 只验证流程）")
    model = ChatOpenAI(
        model=os.environ.get("LLM_MODEL", "deepseek-v4-pro"),
        api_key=api_key,
        base_url=os.environ.get("LLM_BASE_URL", "https://api.deepseek.com"),
        temperature=float(os.environ.get("LLM_TEMPERATURE", "0")),
        timeout=60,
        max_retries=2,
    )
    return model
