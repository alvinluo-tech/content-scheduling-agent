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
| `query_time_slot` | 查某天某时段是否可排 + 后续 3 个可用时段及其排序理由（`date` 可选，缺省用该素材的 `requested_date`；`channel` 可选，缺省用该素材的 `requested_channel`）；当天更晚已无位置时额外返回 `next_available`（该频道后面配置过的日期里第一个可用时段及理由） | 不在营业时段/网格 → `BAD_TIME`；日期不在该频道的 `days` 里 → `DATE_NOT_AVAILABLE`、频道没配置 → `CHANNEL_NOT_AVAILABLE`（均不可恢复）；占用按`频道 + 日期`算，草稿同样占位（但自己的草稿不算冲突） |
| `request_confirmation` | 把"需要人拍板"变成一条可追踪的记录 | 幂等：同一 `(素材, 请求时段, 建议时段)` 只登记一条 |
| `save_draft` | 写草稿账本（`date` 可选） | ① 硬性时间的时间或日期被改动且未确认 → `HARD_CONSTRAINT_VIOLATION`；② `can_draft=false` → `MATERIAL_NOT_READY`；③ 时段被占 → `SLOT_CONFLICT`；④ 注入失败 → `SAVE_FAILED`；⑤ 重复提交 → 同 `draft_id`，`deduplicated=true` |

每个工具强制要求 `reason` 参数，因此"模型的决策依据"天然出现在 trace 里，而不是靠事后猜。

**为什么多一个 `request_confirmation`**：题面要求"请求确认"是模型可选动作之一。只把它写成报告字段，
模型就没有执行这个动作的载体，也无法体现"先登记确认、再保存草稿"的顺序约束。代价是多一个工具需要解释，
收益是"硬性要求不得擅自变更"从文案约束变成可校验的状态机。

## 三条不可越界的红线（代码强制，不靠提示词）
1. **不发布**：工具集里没有发布工具，`build_report` 里 `published` 是常量 `False`。
2. **硬性时间需确认**：`time_hard=true` 且时间或日期变了（`time_changed or date_changed`）时，若没有 pending/approved 的确认记录，直接拒绝保存。
   确认只能由**人**给出：`--approve C-...`（CLI）或 `POST /api/confirm`（Web），模型工具集里没有审批动作；
   `approved` 之后重跑才会落到 `draft_ready`，没点头之前那份草稿始终是 `draft_pending_confirmation`。
   人还可以改选（`--replan C-...=20:30` / `POST /api/reject` 带 `time`）：模型提的那版照样作废，
   人挑的时段另记一条 `approved`（`replanned_from` 指回原方案），重跑时模型只能落到人挑的时段。
   人也可以直接否决（`--reject C-...` / `POST /api/reject`）：该方案连同那份待确认草稿一起标为
   `rejected` / `draft_rejected`，之后 `save_draft` 对同一替代时段直接返回 `CONFIRMATION_REJECTED`——
   否决不是一句提示，而是账本里一条谁都绕不过去的记录。
3. **审核未过不许排**：`can_draft=false`（缺信息或命中阻断规则）时 `save_draft` 一律 `MATERIAL_NOT_READY`。

提示词里同样写了这三条，但**提示词只是提醒，护栏在代码里**——模型说错话不会变成错数据。

## 状态与结果的可信度来源
- 报告里的每个字段都从**工具账本**（成功保存的草稿、登记的确认、失败计数）反查，而不是采信模型的自述。
- 模型的自然语言只进 `agent_summary`，不参与状态判定。
- `completed` 与 `outcome` 是两个维度：`completed` 只表示"推理正常结束且每项素材都有结论"，
  是否真的排上要看 `outcome`（scheduled / pending_confirmation / partial / failed / incomplete）。
  这样"保存失败"既不会被说成成功，也不会被误读成流程崩了。

## 人的拍板要能只读回放

确认项不是布尔开关，而是一条记录：`status`（pending / approved / rejected）、`decided_by`、`decided_at`，
改选时还有 `replanned_from` 指回被否决的那一版。报告的 `pending_confirmations` 把这些字段一并带出来，
所以「谁在什么时候批准了哪一版」不用去翻账本、更不用问模型——Markdown 报告与界面的「确认项」读的是同一份数据。
字段名沿用 `pending_confirmations` 是历史包袱（它装的其实是该请求的全部确认项），改名的收益不如保持旧产物可读。

## 步数上限
`agent` 节点入口先判 `steps >= max_steps`，达到上限直接返回 `terminal=step_limit_reached` 且**不再调用模型**。
上限优先级：`--max-steps` > 输入 `options.max_steps` > 环境变量 `MAX_STEPS` > 20。

## 输入校验只拦"模型也修不了的输入"
`src/validation.py` 在入口拒三类结构性错误：`requests` 不是非空列表、请求缺 `material_id` 或 `requested_time`、
同一素材出现在两条请求里（一次性要求两个发布时间要求，只能由人合并）。这三种模型查什么工具都补不出来，
早停比让它绕 20 步更诚实（CLI 退出码 2，stderr 是 `{"status":"error",...}`）。
反过来，`25:99` 这种时间、没配置过的日期与频道**不在这里拦**——它们要成为 `BAD_TIME` / `DATE_NOT_AVAILABLE` /
`CHANNEL_NOT_AVAILABLE` 回到模型眼前，因为"按用户要求查了、发现不可行、然后请求确认"正是这个 Agent 该做的动作。

## 账本为什么需要跨进程锁
原子替换（`mkstemp` + `os.replace`）只保证文件不会写坏，挡不住丢更新：Web 演示层与 CLI 各持一份内存快照，
谁先读谁后写，后写的那次会整份覆盖前一次。所以 `Store` 把写入收进 `transaction()`：先对 `<ledger>.json.lock`
拿跨进程锁（Windows `msvcrt.locking` / POSIX `flock`），再重读磁盘，再改，再原子替换。三个性质：
- **可重入**：`replan_confirmation` 会先否决再登记新方案，只在最外层进出一次锁。
- **检查与写入同锁**：`save_draft` 把"查冲突 + 写账本"整段包进同一把锁，否则两个并发运行会都看到空位、各写一份。
- **等不到就报错**：锁等满 10 秒抛 `LedgerBusy`，工具层转成 `LEDGER_BUSY`（可恢复），不假装写成功。

边界：这是单文件互斥，不是数据库事务——没有多副本、没有行级并发、锁粒度是整个账本。

## 离线 Mock 的边界
`ScriptedChatModel` 用确定性状态机模拟"看工具结果决定下一步"，只用于验证流程、护栏与报告正确性。
它**不证明**真实模型的决策质量——真实模型的 trace 见 `examples/*.live.*`（见 03 文档）。

## Web 演示层（可选，不是第二套逻辑）
`src/server.py` + `web/` 只是同一张图的展示壳：HTTP 处理函数校验场景名后调用 `src/main.py` 的 `run_pipeline()`，
拿到的 `report` 与 `trace` 与 CLI 完全同构，三条红线一行都没有绕过。
- 演示账本固定写到 `.runtime/ledger.json`（已 gitignore），不污染 `data/drafts.json`，避免"点一下演示就改了提交记录"。
- 实时推送走观察者而不是轮询：`TraceRecorder` 每 `record()` / `close()` 一次就把 `deepcopy(steps)` 交给监听器，
  `GET /api/stream` 把监听器塞进队列、用 SSE 逐条写出，最后补一条 `report` 事件。图本身不知道有 Web 这回事。
- 前端只做渲染与状态映射：`review_status` / `outcome` / `draft.status` 逐个映射成中文标签，未映射的值不做猜测。
- 边界：本地单机、无鉴权，只用于演示；CLI 仍是主入口，Web 不新增任何业务决策。

## 频道之间为什么互不占位
`schedule.json` 里主频道的排期放在顶层 `days`，其它频道放在 `channels.<name>.days`。`Store` 的每个读接口都多收一个
`channel` 参数，占位、草稿、确认项三份数据都按频道过滤，所以"同一时刻主频道空着、短视频频道被占"是两条独立事实，
不是同一份排期的一个标签。代价是 `draft_id` / `confirmation_id` 必须带频道后缀（`-shorts`），否则两个频道会抢同一个 id；
收益是"不串道"由数据形状保证，而不是靠模型记得切换频道。
未知频道一律 `CHANNEL_NOT_AVAILABLE`（不可恢复）：宁可让模型停下来问人，也不要它自己发明一个频道名。

## 替代时段为什么是这个顺序

`policy.rank_alternatives()` 只排序、不新增候选：候选集合仍是营业时段网格里请求之后未被占用的格子，
评分只有三项，每项都写进 `reasons` 随工具结果一起返回：

| 因子 | 作用 | 理由文案 |
| --- | --- | --- |
| 距离 | 每远一格 −1 分，越近越优 | `距请求时段 +60 分钟` |
| 间隔 | 与最近已有排期贴得太近 −2 分（阈值 `preferences.min_gap_minutes`） | `与已有排期只隔 30 分钟（偏好 ≥60）` / `与已有排期间隔 60 分钟` |
| 偏好档 | 落在 `preferences.prefer_windows` 内 +1.5 分（默认 18:00–21:00 晚间档） | `落在偏好时段 18:00–21:00` |

数据里 `min_gap_minutes` 取值 30（等于一格），所以它只在真的"贴格子"时才生效；把它调大，
排序会自己躲开拥挤时段——这一条由 `tests/test_policy.py` 用 60 分钟显式验证，避免"配置写了但从不生效"。

## 当天排不下时，谁决定顺延

替代时段只在**同一天**里找。如果请求时段之后当天已经排满，工具不会编一个"再往后一点"的时间，
而是再算一个 `next_available`：按 `schedule.json` 里配置的日期顺序，找后面第一个还有空位的日子，
连理由一起返回（`2026-11-14 当天已无更晚的可用时段，顺延到 2026-11-15 09:30`）。

顺延**仍然是改期**，所以走的是同一条护栏：`time_hard=true` 的请求要先把确认项登记出来，
用户同意前草稿状态只能是 `draft_pending_confirmation`。工具只回答"哪天有位置"，
"能不能把硬性要求推迟一天"这个判断始终留给用户——这是 08 用例要演示的那条路径。
排序结果不改变工具的判定：`available` 仍只看冲突，模型看到的第一个候选是"最推荐的"，不是"唯一合法的"。

## 报告导出（渲染层，不是第二条链路）
`src/markdown.py` 是纯函数：输入同一份 `report`（可选带上 `trace`），输出人读的 Markdown。
CLI 用 `--report-md` 写文件，Web 用 `GET /api/report.md` 下载最近一次运行，两边渲染同一份文本。
它不改任何数据、不重新计算结论——所以"报告里写的"与"JSON 里写的"不可能出现两套事实；
数值全部来自 `report`（步数、上限、草稿状态、`published=false`），模型自述单独成节并显式标注"不是证据"。

## 取舍记录
| 取舍 | 选择 | 理由 |
| --- | --- | --- |
| 交互形态 | CLI | 评审可复现、无端口/环境依赖；HTTP 与 Notebook 都会引入与考核目标无关的复杂度 |
| 框架 | LangGraph（显式图） | 用户指定；显式节点/边保留对循环与上限的控制权 |
| 账本 | JSON 文件 + 原子写 | 3 小时范围内不引入数据库；原子替换保证不会写坏 |
| 去重键 | `(channel, requested_date, material_id, requested_time)` | 同一素材同一原始请求重复提交应落回同一份草稿；换请求时段视为新排期；换频道是另一条排期（同一素材可在两个频道各排一份） |
| 为确认单独建工具 | 是 | 让"请求确认"可观测、可校验顺序 |
