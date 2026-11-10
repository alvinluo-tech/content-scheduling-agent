# 03 · 评估

## 测试预期与实际结果

```
.venv/Scripts/python.exe -m unittest discover -s tests -t . -v   # 40 passed
.venv/Scripts/python.exe scripts/run_examples.py                 # 重跑 6 个用例
```

原始输出见 `examples/test-results.txt`。测试分三层：

| 层 | 文件 | 覆盖 |
| --- | --- | --- |
| 规则层 | `tests/test_policy.py` | R01-R05 的四种审核结论、时段网格、冲突与替代时段 |
| 工具层 | `tests/test_tools.py` | 硬性时间护栏、审核未过护栏、重复提交去重、自有草稿不冲突、他人草稿占位、保存失败、参数校验、素材不存在、越界读取、未注册工具 |
| 图级 | `tests/test_agent_flow.py` | 6 个用例端到端、步数上限（含 CLI 覆盖）、trace 结构、无发布工具 |
| 接口层 | `tests/test_server.py` | `/api/state` 字段、静态文件与路径穿越、`/api/run` 跑通 02、非法场景与坏 JSON、`/api/reset` 清账本、02→05 只有一份草稿、四场景 `published` 恒 False |

## 六个用例的实际结果（离线 Mock）

| 用例 | status | outcome | steps / tools | 关键观察 |
| --- | --- | --- | --- | --- |
| 01-normal | completed | scheduled | 5 / 4 | M02 11:00 可用 → 直接存草稿 `draft_ready`；R04 标题超长只留 warning 不阻断 |
| 02-conflict | completed | pending_confirmation | 6 / 5 | 18:00 被 M00 占用 → 登记确认 C-M01-…-1900 → 存草稿 `draft_pending_confirmation`，`published=false` |
| 05-dedup | completed | pending_confirmation | 6 / 5 | 同一请求重复提交：`draft_id` 不变、`submit_count=2`、确认项仍只有 1 条、账本只有 1 份草稿 |
| 03-rules-and-missing | completed | failed | 6 / 5 | M03 `needs_info`（缺 duration_sec、authorized 未知，R01/R02）；M04 `blocked`（R03 违禁词 + R05 人工复核）；两者都没草稿 |
| 04-save-failure | completed | failed | 6 / 5 | 保存返回 SAVE_FAILED → 重试一次仍失败 → 停止；`result_status=save_failed`，无草稿，不谎报成功 |
| 06-step-limit | step_limit_reached | incomplete | 3 / 3 | 上限 3 步：M02 停在查询后未保存、M05 未处理；`completed=false`、`unfinished_materials=[M02, M05]` |

每条都可在 `examples/<case>.output.json`（结果）与 `examples/<case>.trace.json`（分步动作/参数/观察/下一步）对照。

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
七张截图是同一界面在不同场景下的实际渲染，页面上的数字全部来自那份 `report`：

| 截图 | 场景 | 页面上应看到的 |
| --- | --- | --- |
| `docs/images/web-02-conflict.png` | 时段冲突 | 结论「待用户确认」；M01 待确认 + 确认项 `C-M01-…-1900`；时段带 19:00 标红为建议；`已发布 false` |
| `docs/images/web-03-rules.png` | 信息缺失 + 规则风险 | M03「信息缺失」、M04「已阻断」；风险行带 `R03`/`R05` 而非 `[object Object]`；草稿均「未落库」 |
| `docs/images/web-04-save-failure.png` | 保存失败 | 结论「存在失败项」；M05「保存失败」、草稿「未保存（失败 2 次）」；trace 末两步 `FAIL` |
| `docs/images/web-06-step-limit.png` | 步数上限 | 结论「步数上限中断」；`步数 3/3`；M02「未完成」、M05「未处理」，模型自述写明未收尾 |
| `docs/images/web-02-conflict-live.png` | 02 走真实模型 | 右上角切到「真实模型」；`步数 5/20`、`工具 5`；决策依据栏是模型自己写的理由，结论同为「待用户确认」，`已发布 false` |
| `docs/images/web-02-conflict-approved.png` | 人工确认后重跑 | 点「记为用户已确认」后确认项变 `approved`、结论变「已排期 · 仅草稿」、`requires_user_confirmation=false`、`published` 仍为 false |
| `docs/images/web-02-conflict-streaming.png` | 真实模型运行中（第 3 条决策） | 右栏逐条推送（`执行中 · 已产生 3 条决策记录`，朱红呼吸点），中间上一轮结论压暗；跑完后恢复亮度并换成新结果 |

## 失败与限制

1. **真实模型路径不进 CI**：离线 Mock 用确定性状态机产动作，能证明"流程 + 护栏 + 报告"正确，
   不能证明真实 LLM 的决策质量。真实模型的两条路径已在下节留下 trace，但每次运行依赖外部 API 与配额，因此不进 CI。
2. **规则是合成规则**：R01-R05 只覆盖题面点名的四类风险（缺字段、未授权、违禁词、敏感品类），
   真实内容审核还需要语义模型或人工。
3. **排期不是最优化**：只保证不冲突 + 尊重用户要求，不做频道/流量/时长组合优化。
4. **单日单频道**：`schedule.json` 固定一天一个频道；跨天/多频道需要扩展 `Store.occupied_slots`。
5. ~~确认无回写通道~~ 已补：人可通过 `--approve C-...` 或 `POST /api/confirm` 把 pending 改成 approved，
   重跑即落 `draft_ready`（接口测试 `test_human_approval_turns_pending_draft_into_ready`）。仍是单人本地演示，没有账号与审批留痕。
6. **步数上限是硬截断**：达到上限的素材保持 `unresolved`/`not_processed`，不做部分保存的补偿逻辑。

## 真实模型（DeepSeek）验证

状态：🟢 已完成（2026-11-10）。模型 id 取自 `GET https://api.deepseek.com/models` 的实际返回：
`deepseek-v4-pro`（默认）、`deepseek-flash`，仓库里不再出现未经核对的 `deepseek-chat`。密钥只在本机 `.env`，未入库。

```bash
.venv/Scripts/python.exe -m src.main --input examples/02-conflict.input.json --out examples/02-conflict.live.output.json --trace examples/02-conflict.live.trace.json
.venv/Scripts/python.exe -m src.main --input examples/04-save-failure.input.json --out examples/04-save-failure.live.output.json --trace examples/04-save-failure.live.trace.json
```

| 用例 | 真实模型的实际动作（按 trace 顺序） | 结果 | 与离线 Mock 的差异 |
| --- | --- | --- | --- |
| 02-conflict | 第 1 轮并行 `read_material` + `check_rules` → 第 2 轮 `query_time_slot`(18:00) 看到被 M00 占用 → 第 3 轮 `request_confirmation`(C-M01-2026-11-12-1900) → 第 4 轮 `save_draft`(19:00) | completed / pending_confirmation，5 次工具调用 / 5 轮；`draft_pending_confirmation`、`published=false` | Mock 串行 6 轮；真实模型把两个无依赖的读取并行发出，少一轮，决策顺序与结论一致 |
| 04-save-failure | 第 1 轮并行 `read_material` + `check_rules` + `query_time_slot`(15:00) → 第 2 轮 `save_draft` 收 `SAVE_FAILED` → 第 3 轮重试一次仍失败 → 收尾 | completed / failed，5 次工具调用 / 4 轮；`failed_save_attempts=2`、无草稿 | Mock 串行 5 轮；结论相同（重试一次后如实放弃） |

这次真实运行验证到的三件事：

1. **「先确认、再改硬性时间」不是提示词祈使句**：模型确实先调 `request_confirmation` 再 `save_draft`，且 `save_draft` 的硬性时间护栏没有触发 `HARD_CONSTRAINT_VIOLATION`——护栏与模型行为互为验证。
2. **没有谎报**：`agent_summary` 明确写「M05 的排期草稿目前未保存成功」「未发布」，与 `published=false`、`draft=null` 一致。
3. **并行工具调用会改写「步」的口径**：模型在同一轮里并行发出无依赖的调用，于是 `limits.steps_used`（模型轮次）小于 trace 条目数。trace 的 `step` 现在按模型轮次编号（同轮共享步号），报告与 trace 口径一致。

边界：每条路径只跑一次，样本量不足以证明稳定的决策质量；真实模型 trace 不进 CI（依赖外部 API 与配额）。
