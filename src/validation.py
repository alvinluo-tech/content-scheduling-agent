"""排期请求的结构校验：只拦"模型也修不了的输入"，不拦该由工具回报的业务错误。

时间不在网格、日期没配置、频道不存在这些属于业务判断，必须留给工具返回给模型（`BAD_TIME` /
`DATE_NOT_AVAILABLE` / `CHANNEL_NOT_AVAILABLE`）；而"两条请求指向同一素材"这种结构性错误，
模型无论查什么工具都修不了，只能在入口就停下来说清楚。
"""

from __future__ import annotations

from typing import Any


def validate_run(run: Any) -> None:
    if not isinstance(run, dict):
        raise ValueError("输入必须是 JSON 对象，例如 {\"requests\": [...]}")
    requests = run.get("requests")
    if not isinstance(requests, list) or not requests:
        raise ValueError("输入缺少 requests 列表（至少一条素材请求）")
    seen: dict[str, int] = {}
    for index, request in enumerate(requests, start=1):
        if not isinstance(request, dict):
            raise ValueError(f"第 {index} 条请求不是对象")
        material_id = request.get("material_id")
        if not isinstance(material_id, str) or not material_id.strip():
            raise ValueError(f"第 {index} 条请求缺少 material_id")
        if not isinstance(request.get("requested_time"), str) or not request["requested_time"].strip():
            raise ValueError(
                f"第 {index} 条请求（{material_id}）缺少 requested_time；没有发布时间要求就无法排期，"
                "请把用户的原始要求写进来，不要替用户猜一个"
            )
        if material_id in seen:
            raise ValueError(
                f"素材 {material_id} 同时出现在第 {seen[material_id]} 条和第 {index} 条请求里："
                "一条素材一次只能有一个发布时间要求，请合并成一条（或拆成两次运行）"
            )
        seen[material_id] = index
    max_steps = (run.get("options") or {}).get("max_steps")
    if max_steps is not None and (not isinstance(max_steps, int) or max_steps <= 0):
        raise ValueError(f"options.max_steps 必须是正整数，收到 {max_steps!r}")
