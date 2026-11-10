# 03 · 评估

## 测试预期与实际结果

```
.venv/Scripts/python.exe -m unittest discover -s tests -t . -v   # 40 passed
.venv/Scripts/python.exe scripts/run_examples.py                 # 重跑 6 个用例
```

原始输出见 `examples/test-results.txt`。测试分三层：

| 层 | 文件 | 覆盖 |
| --- | --- | --- |
| 规则层 | `tests/test_policy.py` | R01-R05 的四种审核结论、时段网格、冲突与替代时段、替代时段的排序（近优先、间隔惩罚、偏好档加分） |
| 工具层 | `tests/test_tools.py` | 硬性时间护栏、审核未过护栏、重复提交去重、自有草稿不冲突、他人草稿占位、保存失败、参数校验、素材不存在、越界读取、未注册工具、替代时段的推荐理由随工具结果返回 |
| 图级 | `tests/test_agent_flow.py` | 8 个用例端到端、多日冲突只在自己那一天待确认、步数上限（含 CLI 覆盖）、trace 结构、无发布工具 |
| 报告层 | `tests/test_markdown.py` | 02 的确认项与草稿 id 原样出现在 Markdown、任何用例都不得出现「已发布：是」、保存失败写成「未保存（失败 2 次，未谎报成功）」、步数上限列出未完成素材、CLI `--report-md` 真的写出文件 |
| 接口层 | `tests/test_server.py` | `/api/state` 字段（含 `dates`/`days` 两天占位）、7 个场景、静态文件与路径穿越、`/api/run` 跑通 02、非法场景与坏 JSON、`/api/reset` 清账本、02→05 只有一份草稿、四场景 `published` 恒 False、SSE 顺序与人工确认回写、页面保留键盘/读屏钩子（`aria-live`、`meta-duration`、箭头键 tabindex） |

## 八个用例的实际结果（离线 Mock）

| 用例 | status | outcome | steps / tools | 关键观察 |
| --- | --- | --- | --- | --- |
| 01-normal | completed | scheduled | 5 / 4 | M02 11:00 可用 → 直接存草稿 `draft_ready`；R04 标题超长只留 warning 不阻断 |
| 02-conflict | completed | pending_confirmation | 6 / 5 | 18:00 被 M00 占用 → 登记确认 C-M01-…-1900 → 存草稿 `draft_pending_confirmation`，`published=false` |
| 05-dedup | completed | pending_confirmation | 6 / 5 | 同一请求重复提交：`draft_id` 不变、`submit_count=2`、确认项仍只有 1 条、账本只有 1 份草稿 |
| 03-rules-and-missing | completed | failed | 6 / 5 | M03 `needs_info`（缺 duration_sec、authorized 未知，R01/R02）；M04 `blocked`（R03 违禁词 + R05 人工复核）；两者都没草稿 |
| 04-save-failure | completed | failed | 6 / 5 | 保存返回 SAVE_FAILED → 重试一次仍失败 → 停止；`result_status=save_failed`，无草稿，不谎报成功 |
| 06-step-limit | step_limit_reached | incomplete | 3 / 3 | 上限 3 步：M02 停在查询后未保存、M05 未处理；`completed=false`、`unfinished_materials=[M02, M05]` |
| 08-day-overflow | completed | pending_confirmation | 6 / 5 | 11-14 整日排满、18:00 之后没有位置 → 工具返回 `next_available=2026-11-15 09:30` → 登记 `C-M01-2026-11-15-0930` → 存草稿 `draft_pending_confirmation`；`date_changed=true`、`published=false`（顺延同样要过确认） |
| 07-multiday | completed | pending_confirmation | 10 / 9 | 两条请求都排 11-13：M06 的 09:00 被 M02 占 → 采纳工具给的首选替代 09:30 并待确认（`scheduled_date=2026-11-13`、`date_changed=false`）；M07 的 20:00 空闲 → `draft_ready`；两条 `published=false` |

每条都可在 `examples/<case>.output.json`（结果）、`examples/<case>.trace.json`（分步动作/参数/观察/下一步）
与 `examples/<case>.report.md`（同一份结果的 Markdown 版）对照。

多日的三条口径：① 占位按天算（`data/schedule.json` 的 `days`），09:00 在 11-13 被占不等于 11-12 也被占；
② 请求不带 `requested_date` 就用默认日期，所以旧的 6 个用例 trace 逐字节未变；
③ 去重键跟「用户要求」走（`requested_date|material_id|requested_time`）——换替代时段重提命中同一份草稿，改日期则是一条新排期；
时间或日期任一被改动且 `time_hard=true`，都必须先有确认记录才能保存。
④ 跨日顺延：当天请求时段之后已无位置时，工具再用 `next_available` 指出后面第一个有空位的日期，
但顺延同样算改期（`date_changed=true`），硬性要求照样要先登记确认——08 用例的草稿因此停在 `draft_pending_confirmation`。

## 与题面参考实例的逐条对齐

题面参考：M01 无风险、要求 18:00，但该时段已占用、19:00 可用 → 建议 19:00 并标记待确认，不得称为已发布。

`examples/02-conflict.output.json` 实测：`review_status=pass`、`requested_time=18:00`、`suggested_time=19:00`、
`time_changed=true`、`hard_time_preserved=false`、`requires_user_confirmation=true`、
`draft.status=draft_pending_confirmation`、`published=false`。一致。

## Web 演示界面的验证

界面不是另一条实现路径：`src/server.py` 的 `/api/run` 直接调 `run_pipeline()`，测试断言它跑出的报告与 CLI 同构
（`tests/test_server.py`：02 场景 `steps_used=5`、首个工具 `read_material`、末个 `save_draft`、`published=False`）。
实时推送同样有接口级断言：`test_stream_pushes_steps_before_the_final_report` 读真 SSE 流，确认 `meta` 先行、
中间至少 3 条 `trace` 快照（第一条只含 1 步、末条步号已推进）、最后一条是 `report` 且 `published=False`；
`test_stream_rejects_unknown_case_before_opening_stream` 保证非法场景在开流之前就 400。
十二张截图是同一界面在不同场景下的实际渲染，页面上的数字全部来自那份 `report`：

| 截图 | 场景 | 页面上应看到的 |
| --- | --- | --- |
| `docs/images/web-02-conflict.png` | 时段冲突 | 结论「待用户确认」；M01 待确认 + 确认项 `C-M01-…-1900`；时段带 19:00 标红为建议；`已发布 false` |
| `docs/images/web-03-rules.png` | 信息缺失 + 规则风险 | M03「信息缺失」、M04「已阻断」；风险行带 `R03`/`R05` 而非 `[object Object]`；草稿均「未落库」 |
| `docs/images/web-04-save-failure.png` | 保存失败 | 结论「存在失败项」；M05「保存失败」、草稿「未保存（失败 2 次）」；trace 末两步 `FAIL` |
| `docs/images/web-06-step-limit.png` | 步数上限 | 结论「步数上限中断」；`步数 3/3`；M02「未完成」、M05「未处理」，模型自述写明未收尾 |
| `docs/images/web-02-conflict-live.png` | 02 走真实模型 | 右上角切到「真实模型」；`步数 5/20`、`工具 5`；决策依据栏是模型自己写的理由，结论同为「待用户确认」，`已发布 false` |
| `docs/images/web-02-conflict-approved.png` | 人工确认后重跑 | 点「记为用户已确认」后确认项变 `approved`、结论变「已排期 · 仅草稿」、`requires_user_confirmation=false`、`published` 仍为 false |
| `docs/images/web-02-conflict-rejected.png` | 人工否决后重跑 | 点「不同意这个改期」后：结论「存在失败项」、素材状态「改期被否决」；确认项 `rejected`、草稿「已否决 · 草稿作废」；第 5 步观察是 `CONFIRMATION_REJECTED`（FAIL），`published` 仍为 false |
| `docs/images/web-02-conflict-streaming.png` | 真实模型运行中（第 3 条决策） | 右栏逐条推送（`执行中 · 已产生 3 条决策记录`，朱红呼吸点），中间上一轮结论压暗；跑完后恢复亮度并换成新结果 |
| `docs/images/web-narrow.png` | 884px 窄屏（单列） | 三栏在 ≤1000px 折成一列，场景列表、执行区、结论依次堆叠；顶栏出现「耗时 43 毫秒」，说明计时来自本轮真实运行而不是写死 |
| `docs/images/web-report-md.png` | 结论下方的报告导出 | 结论行下出现「导出 Markdown 报告」；点它下载的就是上面那份报告（同一份 `report` + 决策记录），`已发布 否` |
| `docs/images/web-07-multiday.png` | 多日排期 | 页面顶部「2026-11-12 起 4 天」；时段带上方出现日期切换（11-13 激活）；11-13 明细列出 09:00 M02 / 09:30 草稿 M06 / 14:00 M00 / 20:00 草稿 M07；结论「待用户确认」、`已发布 false` |
| `docs/images/web-08-day-overflow.png` | 当天排满 · 顺延次日 | 11-14 标签页激活、整日都是「已有排期」；M01 行写「要求 2026-11-14 18:00 · 硬性 / 建议 2026-11-15 09:30 / 已改时段 / 已改日期」；确认项问题是「当天更晚的时段已排满，是否同意顺延到 2026-11-15 09:30？」；步骤 3 的观察摘要写明「顺延到 2026-11-15 09:30」 |

## 失败与限制

1. **真实模型路径不进 CI**：离线 Mock 用确定性状态机产动作，能证明"流程 + 护栏 + 报告"正确，
   不能证明真实 LLM 的决策质量。真实模型的两条路径已在下节留下 trace，但每次运行依赖外部 API 与配额，因此不进 CI。
2. **规则是合成规则**：R01-R05 只覆盖题面点名的四类风险（缺字段、未授权、违禁词、敏感品类），
   真实内容审核还需要语义模型或人工。
3. **排期不是最优化**：只保证不冲突 + 尊重用户要求，不做频道/流量/时长组合优化。
4. **单频道**：多日已支持（`schedule.json` 的 `days`），但仍只有 `channel=main` 一个频道维度；真正的多渠道排期需要把频道加进占位键与工具入参。
5. ~~确认无回写通道~~ 已补：人可通过 `--approve C-...` 或 `POST /api/confirm` 把 pending 改成 approved，
   重跑即落 `draft_ready`（接口测试 `test_human_approval_turns_pending_draft_into_ready`）；
   `--reject C-...` / `POST /api/reject` 把同一方案连同草稿作废，`save_draft` 之后返回
   `CONFIRMATION_REJECTED`（`test_human_rejection_voids_the_draft_and_stops_the_same_plan`）。
   仍是单人本地演示，没有账号、没有双人复核与审批留痕。
6. **步数上限是硬截断**：达到上限的素材保持 `unresolved`/`not_processed`，不做部分保存的补偿逻辑。
7. **顺延只到"后面配置过的日期"**：`next_available` 不会凭空造日期，也不考虑跨日的节假日/流量策略；
   真实排期要接业务日历，这一层现在由 `schedule.json` 的 `days` 近似。

## 真实模型（DeepSeek）验证

状态：🟢 已完成（2026-11-10）。模型 id 取自 `GET https://api.deepseek.com/models` 的实际返回：
`deepseek-v4-pro`（默认）、`deepseek-flash`，仓库里不再出现未经核对的 `deepseek-chat`。密钥只在本机 `.env`，未入库。

```bash
.venv/Scripts/python.exe -m src.main --input examples/02-conflict.input.json --out examples/02-conflict.live.output.json --trace examples/02-conflict.live.trace.json
.venv/Scripts/python.exe -m src.main --input examples/04-save-failure.input.json --out examples/04-save-failure.live.output.json --trace examples/04-save-failure.live.trace.json
.venv/Scripts/python.exe -m src.main --input examples/07-multiday.input.json --out examples/07-multiday.live.output.json --trace examples/07-multiday.live.trace.json
.venv/Scripts/python.exe -m src.main --input examples/08-day-overflow.input.json --out examples/08-day-overflow.live.output.json --trace examples/08-day-overflow.live.trace.json --report-md examples/08-day-overflow.live.report.md
# 先跑一次留下待确认项，再否决它并用真实模型重跑：模型不会绕过否决
.venv/Scripts/python.exe -m src.main --input examples/02-conflict.input.json --offline --ledger .runtime/rejected.json
.venv/Scripts/python.exe -m src.main --input examples/02-conflict.input.json --ledger .runtime/rejected.json --reject C-M01-2026-11-12-1900 --out examples/02-conflict-rejected.live.output.json --trace examples/02-conflict-rejected.live.trace.json --report-md examples/02-conflict-rejected.live.report.md
```

| 用例 | 真实模型的实际动作（按 trace 顺序） | 结果 | 与离线 Mock 的差异 |
| --- | --- | --- | --- |
| 02-conflict | 第 1 轮并行 `read_material` + `check_rules` → 第 2 轮 `query_time_slot`(18:00) 看到被 M00 占用 → 第 3 轮 `request_confirmation`(C-M01-2026-11-12-1900) → 第 4 轮 `save_draft`(19:00) | completed / pending_confirmation，5 次工具调用 / 5 轮；`draft_pending_confirmation`、`published=false` | Mock 串行 6 轮；真实模型把两个无依赖的读取并行发出，少一轮，决策顺序与结论一致 |
| 04-save-failure | 第 1 轮并行 `read_material` + `check_rules` + `query_time_slot`(15:00) → 第 2 轮 `save_draft` 收 `SAVE_FAILED` → 第 3 轮重试一次仍失败 → 收尾 | completed / failed，5 次工具调用 / 4 轮；`failed_save_attempts=2`、无草稿 | Mock 串行 5 轮；结论相同（重试一次后如实放弃） |
| 07-multiday | 并行读两条素材 → 分别 `query_time_slot`(11-13 09:00 / 20:00) → 对 09:00 的冲突 `request_confirmation`(C-M06-2026-11-13-0930) → 两条 `save_draft`(09:30 待确认 / 20:00 直接存) | completed / pending_confirmation，9 次工具调用 / 8 轮；M06 `scheduled_date=2026-11-13`、09:30、`draft_pending_confirmation`；M07 20:00 `draft_ready`；`published=false` | Mock 10 轮；真实模型同样按天归因、只对冲突那条请求确认，另一条不打扰用户 |
| 08-day-overflow | `read_material` → `check_rules` → `query_time_slot`(11-14 18:00) 看到整日 24 格已满、收 `next_available=2026-11-15 09:30` → `request_confirmation`(C-M01-2026-11-15-0930) → `save_draft`(11-15 09:30) | completed / pending_confirmation，5 次工具调用 / 5 轮；`date_changed=true`、`draft_pending_confirmation`、`published=false`；总结里写「没有擅自抢 M05 的时段，也没有直接改期」 | 与 Mock 的 5 次调用 / 6 轮几乎同构；真实模型额外把「或由人工协调释放 18:00」作为第二个选项交回用户 |

| 02-conflict（先被否决） | 并行 `read_material` + `check_rules` → `query_time_slot`(18:00) 看到替代 `[19:00, 19:30, 20:30]` → `request_confirmation`(19:00) 收到「已登记（rejected）」→ 直接收尾 | completed / failed，4 次工具调用 / 3 轮；`result_status=confirmation_rejected`、`rejected_confirmations=[C-M01-2026-11-12-1900]`、草稿 `draft_rejected`、`published=false` | 真实模型看到否决后**没有**再存 19:00，也没有偷偷换成 19:30 直接落库，而是把 19:00/19:30/20:30 三选一交回用户；Mock 在同一步直接收尾 |

这次真实运行验证到的四件事：

1. **「先确认、再改硬性时间」不是提示词祈使句**：模型确实先调 `request_confirmation` 再 `save_draft`，且 `save_draft` 的硬性时间护栏没有触发 `HARD_CONSTRAINT_VIOLATION`——护栏与模型行为互为验证。
2. **没有谎报**：`agent_summary` 明确写「M05 的排期草稿目前未保存成功」「未发布」，与 `published=false`、`draft=null` 一致。
3. **否决不是提示词约束**：把 19:00 的确认项标成 rejected 之后，模型读到的观察就是「已登记（rejected）」，它自己选择不存草稿并把可选时段交回用户——护栏与模型行为再一次互为验证。
4. **并行工具调用会改写「步」的口径**：模型在同一轮里并行发出无依赖的调用，于是 `limits.steps_used`（模型轮次）小于 trace 条目数。trace 的 `step` 现在按模型轮次编号（同轮共享步号），报告与 trace 口径一致。

5. **多日同样成立**：07 的真实 trace 里，模型对 11-13 的两条请求分别按天查询占位，
   只对"硬性时间被占"的那条登记确认、另一条直接落草稿——没有把两条请求的时间口径串在一起。

边界：每条路径只跑一次，样本量不足以证明稳定的决策质量；真实模型 trace 不进 CI（依赖外部 API 与配额）。
