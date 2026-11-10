# 03 · 评估

## 测试预期与实际结果

```
.venv/Scripts/python.exe -m unittest discover -s tests -t . -v   # 28 passed
.venv/Scripts/python.exe scripts/run_examples.py                 # 重跑 6 个用例
```

原始输出见 `examples/test-results.txt`。测试分三层：

| 层 | 文件 | 覆盖 |
| --- | --- | --- |
| 规则层 | `tests/test_policy.py` | R01-R05 的四种审核结论、时段网格、冲突与替代时段 |
| 工具层 | `tests/test_tools.py` | 硬性时间护栏、审核未过护栏、重复提交去重、自有草稿不冲突、他人草稿占位、保存失败、参数校验、素材不存在、越界读取、未注册工具 |
| 图级 | `tests/test_agent_flow.py` | 6 个用例端到端、步数上限（含 CLI 覆盖）、trace 结构、无发布工具 |

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

## 失败与限制

1. **真实模型路径尚未进入回归**：离线 Mock 用确定性状态机产动作，能证明"流程 + 护栏 + 报告"正确，
   不能证明真实 LLM 的决策质量。真实模型 trace 见下节，并且不进 CI（依赖外部 API 与配额）。
2. **规则是合成规则**：R01-R05 只覆盖题面点名的四类风险（缺字段、未授权、违禁词、敏感品类），
   真实内容审核还需要语义模型或人工。
3. **排期不是最优化**：只保证不冲突 + 尊重用户要求，不做频道/流量/时长组合优化。
4. **单日单频道**：`schedule.json` 固定一天一个频道；跨天/多频道需要扩展 `Store.occupied_slots`。
5. **确认无回写通道**：`request_confirmation` 只登记待确认，本版本没有"人确认后再次运行"的入口，
   真实产品需要一个确认界面把状态改成 approved 再触发保存。
6. **步数上限是硬截断**：达到上限的素材保持 `unresolved`/`not_processed`，不做部分保存的补偿逻辑。

## 真实模型（DeepSeek）验证

状态：🔴 未完成 —— 需要 `DEEPSEEK_API_KEY`。补全方式：

```bash
.venv/Scripts/python.exe -m src.main --input examples/02-conflict.input.json \
  --out examples/02-conflict.live.output.json --trace examples/02-conflict.live.trace.json
.venv/Scripts/python.exe -m src.main --input examples/04-save-failure.input.json \
  --out examples/04-save-failure.live.output.json --trace examples/04-save-failure.live.trace.json
```

拿到密钥后再补：两条路径的真实 trace、与 Mock 结果的差异对比（模型是否自己想到"先确认再保存"）、
以及失败模式（是否谎报保存成功、是否跳过规则检查）。
