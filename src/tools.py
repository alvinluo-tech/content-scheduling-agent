from __future__ import annotations

import inspect
from datetime import datetime
from typing import Any, Callable

from .policy import evaluate_rules, find_conflicts, is_blank, rank_alternatives, slot_grid, to_minutes
from .store import LedgerBusy, Store

REASON_PROP = {
    "type": "string",
    "description": "本次调用的决策依据：你观察到了什么、为什么现在调用这个工具（写入 trace，必填，一句话）",
}

DATE_PROP = {
    "type": "string",
    "description": "YYYY-MM-DD；省略则用该素材要求的日期。只能填 schedule.json 里配置过的日期",
}

CHANNEL_PROP = {
    "type": "string",
    "description": "发布频道，例如 main / shorts；省略则用该素材要求的频道。只能填 schedule.json 里配置过的频道",
}

TOOL_SPECS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "read_material",
            "description": "读取素材元数据（标题、时长、封面、授权状态、品类等）。用于在审核或排期前确认素材到底有什么信息。",
            "parameters": {
                "type": "object",
                "properties": {"material_id": {"type": "string", "description": "素材 ID，例如 M01"}, "reason": REASON_PROP},
                "required": ["material_id", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_rules",
            "description": "按审核规则检查素材，返回审核状态（pass / pass_with_warnings / risk / missing_info）、违规明细与缺失字段。是否允许保存草稿看 can_draft。",
            "parameters": {
                "type": "object",
                "properties": {"material_id": {"type": "string", "description": "素材 ID"}, "reason": REASON_PROP},
                "required": ["material_id", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_time_slot",
            "description": "查询某频道某天某个时间点是否可排：返回是否被占用、被谁占用，以及该时间点之后最近的 3 个可用时段。营业时段 09:00-21:00，30 分钟粒度；已保存的草稿同样占位；频道之间互不影响。",
            "parameters": {
                "type": "object",
                "properties": {
                    "material_id": {"type": "string", "description": "为哪项素材查询时段（用于归因到该素材）"},
                    "time": {"type": "string", "description": "HH:MM，例如 18:00"},
                    "date": DATE_PROP,
                    "channel": CHANNEL_PROP,
                    "reason": REASON_PROP,
                },
                "required": ["material_id", "time", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "request_confirmation",
            "description": "登记一条待用户确认的事项（例如把硬性发布时间从 18:00 改到 19:00）。登记不等于用户已确认；未确认前不得表述为已排期、已确认或已发布。",
            "parameters": {
                "type": "object",
                "properties": {
                    "material_id": {"type": "string", "description": "素材 ID"},
                    "proposed_time": {"type": "string", "description": "建议的替代时段 HH:MM"},
                    "date": DATE_PROP,
                    "channel": CHANNEL_PROP,
                    "question": {"type": "string", "description": "要用户拍板的问题，一句话"},
                    "reason": REASON_PROP,
                },
                "required": ["material_id", "proposed_time", "question", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_draft",
            "description": "只保存排期草稿，不发布。相同请求（同频道同素材同时段要求）重复保存只会产生一份草稿；把硬性要求的时间改掉前必须先 request_confirmation；审核未通过的素材不允许保存。返回 published 恒为 false。",
            "parameters": {
                "type": "object",
                "properties": {
                    "material_id": {"type": "string", "description": "素材 ID"},
                    "scheduled_time": {"type": "string", "description": "拟排期时段 HH:MM"},
                    "date": DATE_PROP,
                    "channel": CHANNEL_PROP,
                    "reason": REASON_PROP,
                },
                "required": ["material_id", "scheduled_time", "reason"],
            },
        },
    },
]


def _err(code: str, message: str, *, recoverable: bool = True, hint: str | None = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message, "recoverable": recoverable}
    if hint:
        error["hint"] = hint
    return {"ok": False, "error": error}


class Toolbox:
    """5 个业务工具的真实实现：参数校验、业务守卫、调用账本。"""

    def __init__(self, store: Store, run: dict[str, Any]) -> None:
        self.store = store
        self.run = run
        self.date = store.schedule_date()
        self.requests: dict[str, dict[str, Any]] = {r["material_id"]: r for r in run.get("requests", [])}
        self.save_failures_left = int((run.get("simulate") or {}).get("save_failures", 0))
        self.fixed_now = run.get("now")
        self.calls: list[dict[str, Any]] = []
        self.attempts: dict[str, int] = {}
        self.last_query: dict[str, dict[str, Any]] = {}

    def request_date(self, material_id: str) -> str:
        """素材要求的日期；请求没写就用 schedule.json 的默认日期。"""
        request = self.requests.get(material_id) or {}
        return str(request.get("requested_date") or self.date)

    def request_channel(self, material_id: str) -> str:
        """素材要发的频道；请求没写就是主频道。"""
        request = self.requests.get(material_id) or {}
        return str(request.get("requested_channel") or self.store.default_channel())

    def _channel_error(self, channel: str) -> dict[str, Any] | None:
        if self.store.has_channel(channel):
            return None
        return _err(
            "CHANNEL_NOT_AVAILABLE",
            f"{channel} 不在可排频道内：{self.store.channels()}",
            recoverable=False,
            hint="只能排 schedule.json 里配置过的频道；不要臆造频道",
        )

    def _channel_tag(self, channel: str) -> str:
        """主频道沿用原来的 id，其它频道加后缀——两个频道里的同一时段是两个身份。"""
        return "" if channel == self.store.default_channel() else f"-{channel}"

    def _date_error(self, date: str, channel: str | None = None) -> dict[str, Any] | None:
        available = self.store.available_dates(channel)
        if date in available:
            return None
        return _err(
            "DATE_NOT_AVAILABLE",
            f"{date} 不在可排日期内：{available}",
            recoverable=False,
            hint="只能排 schedule.json 里配置的日期；不要臆造日期",
        )

    def clock(self) -> str:
        return self.fixed_now or datetime.now().astimezone().isoformat(timespec="seconds")

    def handlers(self) -> dict[str, Callable[..., dict[str, Any]]]:
        return {
            "read_material": self.read_material,
            "check_rules": self.check_rules,
            "query_time_slot": self.query_time_slot,
            "request_confirmation": self.request_confirmation,
            "save_draft": self.save_draft,
        }

    def call(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        handler = self.handlers().get(name)
        if handler is None:
            result = _err("UNKNOWN_TOOL", f"未注册的工具 {name}，可用工具：{sorted(self.handlers())}", recoverable=False)
        else:
            try:
                inspect.signature(handler).bind(**args)
            except TypeError as exc:
                result = _err("BAD_ARGUMENTS", f"{name} 参数不合法：{exc}", hint="按工具 schema 重新构造参数")
            else:
                try:
                    result = handler(**args)
                except LedgerBusy as exc:
                    result = _err("LEDGER_BUSY", str(exc), hint="另一个进程正在写账本；稍后重试，不要假设已经写入成功")
                except Exception as exc:  # 工具内部异常必须变成可观察结果，而不是中断执行
                    result = _err("TOOL_CRASH", f"{name} 内部异常：{type(exc).__name__}: {exc}", recoverable=False)
        if name == "query_time_slot" and result.get("ok") and args.get("material_id"):
            self.last_query[args["material_id"]] = result  # 报告要用人看过的候选时段，不必翻 trace
        self.calls.append({"tool": name, "args": args, "ok": bool(result.get("ok")), "summary": self.summary_of(name, result)})
        return result

    def summary_of(self, name: str, result: dict[str, Any]) -> str:
        if not result.get("ok"):
            error = result.get("error", {})
            return f"失败[{error.get('code')}] {error.get('message')}"
        if name == "read_material":
            material = result["material"]
            missing = result["missing_required_fields"]
            tail = f"，缺 {missing}" if missing else "，必填字段齐全"
            return f"{material['id']} 标题《{material['title']}》{tail}"
        if name == "check_rules":
            ids = [v["rule_id"] for v in result["violations"]]
            return f"审核 {result['review_status']}，命中规则 {ids or '无'}"
        if name == "query_time_slot":
            ranked = result.get("ranked_alternatives") or []
            best = f"首选 {ranked[0]['time']}：{'；'.join(ranked[0]['reasons'])}" if ranked else "无候选时段"
            next_day = result.get("next_available")
            tail = f"；{next_day['reason']}" if next_day else ""
            if result["available"]:
                return f"{result['time']} 可用，后续可用时段 {result['alternatives']}；{best}"
            return f"{result['time']} 被 {[c['material_id'] for c in result['conflicts']]} 占用，可用替代 {result['alternatives']}；{best}{tail}"
        if name == "request_confirmation":
            return f"待确认 {result['confirmation_id']} 已登记（{result['status']}）"
        if name == "save_draft":
            tag = "（重复提交，已去重）" if result["deduplicated"] else ""
            return f"草稿 {result['draft_id']} {result['status']}{tag}，published={result['published']}"
        return "ok"

    def _material_and_request(self, material_id: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None]:
        material = self.store.material(material_id)
        if material is None:
            return None, None, _err("MATERIAL_NOT_FOUND", f"素材 {material_id} 不存在", recoverable=False, hint="向用户确认素材 ID，不要臆造素材")
        request = self.requests.get(material_id)
        if request is None:
            return material, None, _err("NO_REQUEST", f"素材 {material_id} 不在本次排期请求中", recoverable=False)
        return material, request, None

    def read_material(self, material_id: str, reason: str) -> dict[str, Any]:
        material, _, error = self._material_and_request(material_id)
        if error:
            return error
        evaluation = evaluate_rules(material, self.store.rules_doc)
        return {
            "ok": True,
            "material": material,
            "missing_required_fields": evaluation["missing_fields"],
            "present_fields": sorted(material.keys()),
        }

    def check_rules(self, material_id: str, reason: str) -> dict[str, Any]:
        material, _, error = self._material_and_request(material_id)
        if error:
            return error
        evaluation = evaluate_rules(material, self.store.rules_doc)
        return {"ok": True, "material_id": material_id, **evaluation}

    def query_time_slot(
        self, material_id: str, time: str, reason: str, date: str | None = None, channel: str | None = None
    ) -> dict[str, Any]:
        if self.store.material(material_id) is None:
            return _err("MATERIAL_NOT_FOUND", f"素材 {material_id} 不存在", recoverable=False)
        target_channel = channel or self.request_channel(material_id)
        channel_error = self._channel_error(target_channel)
        if channel_error:
            return channel_error
        target_date = date or self.request_date(material_id)
        date_error = self._date_error(target_date, target_channel)
        if date_error:
            return date_error
        hours = self.store.operating_hours()
        grid = slot_grid(hours)
        if time not in grid:
            return _err(
                "BAD_TIME",
                f"{time} 不在营业时段 {hours['start']}-{hours['end']} 或不在 {hours['granularity_min']} 分钟网格上",
                hint=f"可用网格示例：{grid[:4]}",
            )
        occupied = self.store.occupied_slots(target_date, target_channel) + self.store.drafted_slots(
            target_date, exclude_material=material_id, channel=target_channel
        )
        conflicts = find_conflicts(occupied, time)
        ranked = rank_alternatives(
            hours, occupied, time, preferences=self.store.rules_doc.get("preferences")
        )
        return {
            "ok": True,
            "material_id": material_id,
            "channel": target_channel,
            "date": target_date,
            "time": time,
            "available": not conflicts,
            "conflicts": conflicts,
            "alternatives": [row["time"] for row in ranked],
            "ranked_alternatives": ranked,
            "next_available": None if ranked else self.next_day_slot(target_date, material_id, hours, target_channel),
            "occupied_count": len(occupied),
        }

    def next_day_slot(
        self, target_date: str, material_id: str, hours: dict[str, Any], channel: str | None = None
    ) -> dict[str, Any] | None:
        """当天请求时段之后已经排不下时，去同一频道后面配置过的日期里找第一个可用时段。"""
        for day in self.store.available_dates(channel):
            if day <= target_date:
                continue
            occupied = self.store.occupied_slots(day, channel) + self.store.drafted_slots(
                day, exclude_material=material_id, channel=channel
            )
            ranked = rank_alternatives(hours, occupied, None, preferences=self.store.rules_doc.get("preferences"))
            if not ranked:
                continue
            return {
                "date": day,
                "time": ranked[0]["time"],
                "ranked_alternatives": ranked,
                "reason": f"{target_date} 当天已无更晚的可用时段，顺延到 {day} {ranked[0]['time']}",
            }
        return None

    def request_confirmation(
        self,
        material_id: str,
        proposed_time: str,
        question: str,
        reason: str,
        date: str | None = None,
        channel: str | None = None,
    ) -> dict[str, Any]:
        _, request, error = self._material_and_request(material_id)
        if error:
            return error
        target_channel = channel or self.request_channel(material_id)
        channel_error = self._channel_error(target_channel)
        if channel_error:
            return channel_error
        requested_date = self.request_date(material_id)
        proposed_date = date or requested_date
        date_error = self._date_error(proposed_date, target_channel)
        if date_error:
            return date_error
        confirmation_id = f"C-{material_id}-{proposed_date}-{proposed_time.replace(':', '')}{self._channel_tag(target_channel)}"
        existing = self.store.confirmations.get(confirmation_id)
        if existing:
            return {
                "ok": True,
                "confirmation_id": confirmation_id,
                "status": existing["status"],
                "deduplicated": True,
                "requested_time": existing["requested_time"],
                "proposed_time": existing["proposed_time"],
                "note": "该待确认项已存在；用户确认前不得表述为已确认或已发布",
            }
        record = {
            "confirmation_id": confirmation_id,
            "date": proposed_date,
            "channel": target_channel,
            "requested_date": requested_date,
            "proposed_date": proposed_date,
            "material_id": material_id,
            "requested_time": request["requested_time"],
            "proposed_time": proposed_time,
            "question": question,
            "reason": reason,
            "status": "pending",
            "created_at": self.clock(),
        }
        self.store.add_confirmation(record)
        return {
            "ok": True,
            "confirmation_id": confirmation_id,
            "status": "pending",
            "deduplicated": False,
            "requested_time": request["requested_time"],
            "proposed_time": proposed_time,
            "note": "已登记待确认项；用户确认前该时段只能作为建议，不得表述为已确认或已发布",
        }

    def _save_should_fail(self) -> bool:
        if self.save_failures_left == 0:
            return False
        if self.save_failures_left > 0:
            self.save_failures_left -= 1
        return True

    def save_draft(
        self,
        material_id: str,
        scheduled_time: str,
        reason: str,
        date: str | None = None,
        channel: str | None = None,
    ) -> dict[str, Any]:
        # 查冲突与写账本必须在同一把跨进程锁里：否则两个并发运行都会看到"空位"，各写一份草稿。
        with self.store.transaction():
            return self._save_draft(material_id, scheduled_time, reason, date, channel)

    def _save_draft(
        self,
        material_id: str,
        scheduled_time: str,
        reason: str,
        date: str | None = None,
        channel: str | None = None,
    ) -> dict[str, Any]:
        material, request, error = self._material_and_request(material_id)
        if error:
            return error
        target_channel = channel or self.request_channel(material_id)
        channel_error = self._channel_error(target_channel)
        if channel_error:
            return channel_error
        evaluation = evaluate_rules(material, self.store.rules_doc)
        if not evaluation["can_draft"]:
            return _err(
                "MATERIAL_NOT_READY",
                f"素材 {material_id} 审核状态 {evaluation['review_status']}，命中 {[v['rule_id'] for v in evaluation['violations']]}，不得保存草稿",
                recoverable=False,
                hint="把阻断/缺失原因写进结论，或向用户请求补料；不要为了推进流程跳过审核",
            )
        hours = self.store.operating_hours()
        if scheduled_time not in slot_grid(hours):
            return _err("BAD_TIME", f"{scheduled_time} 不在营业时段或网格上", hint=f"{hours['start']}-{hours['end']}，{hours['granularity_min']} 分钟粒度")
        requested_date = self.request_date(material_id)
        scheduled_date = date or requested_date
        date_error = self._date_error(scheduled_date, target_channel)
        if date_error:
            return date_error
        requested_time = request["requested_time"]
        time_changed = scheduled_time != requested_time
        date_changed = scheduled_date != requested_date
        changed = time_changed or date_changed
        # pending 与 approved 都算"用户已看到这个改动"；只有 pending 才需要继续等用户拍板。
        approvals = [
            c for c in self.store.confirmations_for(requested_date, material_id, requested_time, target_channel)
            if c.get("proposed_date", c.get("date")) == scheduled_date
            and c["proposed_time"] == scheduled_time
            and c["status"] in ("pending", "approved")
        ]
        awaiting_user = any(c["status"] == "pending" for c in approvals)
        rejected = [
            c for c in self.store.confirmations_for(requested_date, material_id, requested_time, target_channel)
            if c.get("proposed_date", c.get("date")) == scheduled_date
            and c["proposed_time"] == scheduled_time
            and c.get("status") == "rejected"
        ]
        if rejected:
            return _err(
                "CONFIRMATION_REJECTED",
                f"用户已经否决了把 {requested_date} {requested_time} 改到 {scheduled_date} {scheduled_time}",
                hint="换一个替代时段重新查，或请用户给出新的要求；不要绕过否决再存同一个方案",
            )
        if changed and request.get("time_hard") and not approvals:
            return _err(
                "HARD_CONSTRAINT_VIOLATION",
                f"{requested_date} {requested_time} 是用户硬性要求，改到 {scheduled_date} {scheduled_time} 必须先获得用户确认",
                hint="先调用 request_confirmation 登记待确认项，再保存草稿",
            )
        occupied = self.store.occupied_slots(scheduled_date, target_channel) + self.store.drafted_slots(
            scheduled_date, exclude_material=material_id, channel=target_channel
        )
        conflicts = find_conflicts(occupied, scheduled_time)
        if conflicts:
            return _err(
                "SLOT_CONFLICT",
                f"{scheduled_time} 已被 {[c['material_id'] for c in conflicts]} 占用",
                hint="用 query_time_slot 查替代时段，不要占用已有排期",
            )
        if self._save_should_fail():
            self.attempts[material_id] = self.attempts.get(material_id, 0) + 1
            return _err(
                "SAVE_FAILED",
                "草稿保存服务返回失败（存储不可用）",
                hint="可以重试；若仍失败必须如实报告保存失败，不得声称保存成功",
            )
        # draft_id 跟“用户要求”走（与去重键一致）：换替代时段重提时 id 不变，改期只体现在 scheduled_time/date。
        draft_id = f"D-{material_id}-{requested_date}-{requested_time.replace(':', '')}{self._channel_tag(target_channel)}"
        record = {
            "draft_id": draft_id,
            "date": scheduled_date,
            "channel": target_channel,
            "requested_date": requested_date,
            "material_id": material_id,
            "requested_time": requested_time,
            "scheduled_time": scheduled_time,
            "time_changed": time_changed,
            "date_changed": date_changed,
            "status": "draft_pending_confirmation" if (changed and request.get("time_hard") and awaiting_user) else "draft_ready",
            "reason": reason,
            "submit_count": 1,
            "updated_at": self.clock(),
            "published": False,
        }
        stored, created = self.store.upsert_draft(record)
        return {
            "ok": True,
            "draft_id": stored["draft_id"],
            "material_id": material_id,
            "channel": stored.get("channel", target_channel),
            "date": stored["date"],
            "requested_time": requested_time,
            "scheduled_time": stored["scheduled_time"],
            "time_changed": stored["time_changed"],
            "date_changed": stored.get("date_changed", False),
            "status": stored["status"],
            "deduplicated": not created,
            "published": False,
            "note": (
                "仅保存草稿，未发布；用户已确认替代时段，草稿可直接使用"
                if stored["status"] == "draft_ready"
                else "仅保存草稿，未发布；待确认项仍需用户拍板"
            ),
        }
