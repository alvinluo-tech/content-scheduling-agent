# 02 · 方案

## 整体链路

```
输入 JSON ──► Store（合成资料 + 草稿账本）
                │
                ▼
        LangGraph StateGraph
   ┌────────────────────────────────┐
   │  agent 节点（LLM + 5 个工具）  │◄──┐
   │   ↓ 产出 tool_calls / 结束     │   │ 观察结果作为 ToolMessage 回填
   │  tools 节点（真实执行 + 记账） │───┘
   └────────────────────────────────┘
                │
                ▼
   TraceRecorder（逐步动作/观察/下一步）+ build_report（从工具账本汇总）→ 结果 JSON
```

## 为什么用显式 StateGraph 而不是一行式 create_react_agent
题面明确"框架名称不加分，能解释取舍才有价值"。一行式封装会把决策循环藏起来，导致
① 步数上限无处插入，② trace 拿不到"模型为什么这一步选这个工具"，③ 工具结果的处理策略不可控。
所以这里自己写 `agent` 与 `tools` 两个节点 + 条件边，模型只负责决策，护栏全部在代码里。

## 五个工具与护栏

| 工具 | 作用 | 关键守卫 |
| --- | --- | --- |
| `read_material` | 读元数据，返回缺失的必填字段 | 素材必须在本次请求范围内（`NO_REQUEST`）；不存在 → `MATERIAL_NOT_FOUND` |
| `check_rules` | 按 R01-R05 审核 | 返回 `review_status`（pass / pass_with_warnings / missing_info / risk）与 `can_draft` |
| `query_time_slot` | 查某时段是否可排 + 后续 3 个可用时段 | 不在营业时段/网格 → `BAD_TIME`；草稿同样占位（但自己的草稿不算冲突） |
| `request_confirmation` | 把"需要人拍板"变成一条可追踪的记录 | 幂等：同一 `(素材, 请求时段, 建议时段)` 只登记一条 |
| `save_draft` | 写草稿账本 | ① 硬性时间改期未确认 → `HARD_CONSTRAINT_VIOLATION`；② `can_draft=false` → `MATERIAL_NOT_READY`；③ 时段被占 → `SLOT_CONFLICT`；④ 注入失败 → `SAVE_FAILED`；⑤ 重复提交 → 同 `draft_id`，`deduplicated=true` |

每个工具强制要求 `reason` 参数，因此"模型的决策依据"天然出现在 trace 里，而不是靠事后猜。

**为什么多一个 `request_confirmation`**：题面要求"请求确认"是模型可选动作之一。只把它写成报告字段，
模型就没有执行这个动作的载体，也无法体现"先登记确认、再保存草稿"的顺序约束。代价是多一个工具需要解释，
收益是"硬性要求不得擅自变更"从文案约束变成可校验的状态机。

## 三条不可越界的红线（代码强制，不靠提示词）
1. **不发布**：工具集里没有发布工具，`build_report` 里 `published` 是常量 `False`。
2. **硬性时间需确认**：`time_hard=true` 且 `scheduled_time != requested_time` 时，若没有 pending/approved 的确认记录，直接拒绝保存。
   确认只能由**人**给出：`--approve C-...`（CLI）或 `POST /api/confirm`（Web），模型工具集里没有审批动作；
   `approved` 之后重跑才会落到 `draft_ready`，没点头之前那份草稿始终是 `draft_pending_confirmation`。
3. **审核未过不许排**：`can_draft=false`（缺信息或命中阻断规则）时 `save_draft` 一律 `MATERIAL_NOT_READY`。

提示词里同样写了这三条，但**提示词只是提醒，护栏在代码里**——模型说错话不会变成错数据。

## 状态与结果的可信度来源
- 报告里的每个字段都从**工具账本**（成功保存的草稿、登记的确认、失败计数）反查，而不是采信模型的自述。
- 模型的自然语言只进 `agent_summary`，不参与状态判定。
- `completed` 与 `outcome` 是两个维度：`completed` 只表示"推理正常结束且每项素材都有结论"，
  是否真的排上要看 `outcome`（scheduled / pending_confirmation / partial / failed / incomplete）。
  这样"保存失败"既不会被说成成功，也不会被误读成流程崩了。

## 步数上限
`agent` 节点入口先判 `steps >= max_steps`，达到上限直接返回 `terminal=step_limit_reached` 且**不再调用模型**。
上限优先级：`--max-steps` > 输入 `options.max_steps` > 环境变量 `MAX_STEPS` > 20。

## 离线 Mock 的边界
`ScriptedChatModel` 用确定性状态机模拟"看工具结果决定下一步"，只用于验证流程、护栏与报告正确性。
它**不证明**真实模型的决策质量——真实模型的 trace 见 `examples/*.live.*`（见 03 文档）。

## Web 演示层（可选，不是第二套逻辑）
`src/server.py` + `web/` 只是同一张图的展示壳：HTTP 处理函数校验场景名后调用 `src/main.py` 的 `run_pipeline()`，
拿到的 `report` 与 `trace` 与 CLI 完全同构，三条红线一行都没有绕过。
- 演示账本固定写到 `.runtime/ledger.json`（已 gitignore），不污染 `data/drafts.json`，避免"点一下演示就改了提交记录"。
- 前端只做渲染与状态映射：`review_status` / `outcome` / `draft.status` 逐个映射成中文标签，未映射的值不做猜测。
- 边界：本地单机、无鉴权，只用于演示；CLI 仍是主入口，Web 不新增任何业务决策。

## 取舍记录
| 取舍 | 选择 | 理由 |
| --- | --- | --- |
| 交互形态 | CLI | 评审可复现、无端口/环境依赖；HTTP 与 Notebook 都会引入与考核目标无关的复杂度 |
| 框架 | LangGraph（显式图） | 用户指定；显式节点/边保留对循环与上限的控制权 |
| 账本 | JSON 文件 + 原子写 | 3 小时范围内不引入数据库；原子替换保证不会写坏 |
| 去重键 | `date|material_id|requested_time` | 同一素材同一原始请求重复提交应落回同一份草稿；换请求时段视为新排期 |
| 为确认单独建工具 | 是 | 让"请求确认"可观测、可校验顺序 |
