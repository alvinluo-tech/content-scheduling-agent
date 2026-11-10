# 素材审核与发布排期 Agent（小影校招笔试题 · 项目题 1）

一个**模型驱动**的排期助手：读素材 → 按规则审核 → 查时段 → 处理冲突 → 保存**草稿**（绝不发布）。
模型每一步都要基于上一步的工具结果决定下一步动作（ReAct 循环），而不是走固定分支。

- 语言/框架：Python 3.10+ / LangGraph（显式 StateGraph + 工具节点）
- 模型：DeepSeek 官方 API（OpenAI 兼容），默认 `deepseek-v4-pro`；无密钥时可用离线 Mock 跑通全流程
- 交互形态：CLI（JSON 输入 → JSON 结果 + 逐步 trace）

## 60 秒上手

```bash
# 1. 建环境（任选其一）
uv venv .venv && uv pip install --python .venv/Scripts/python.exe -r requirements.txt
# 或：python -m venv .venv && .venv/Scripts/python -m pip install -r requirements.txt

# 2. 离线跑一遍（不需要任何密钥，验证流程与守卫）
.venv/Scripts/python.exe -m src.main --input examples/02-conflict.input.json --offline \
  --out examples/02-conflict.output.json --trace examples/02-conflict.trace.json

# 3. 一次跑完全部 6 个用例（会重置草稿账本）
.venv/Scripts/python.exe scripts/run_examples.py

# 4. 跑测试
.venv/Scripts/python.exe -m unittest discover -s tests -t . -v
```

macOS / Linux 把 `.venv/Scripts/python.exe` 换成 `.venv/bin/python` 即可。

## 接真实模型

```bash
cp .env.example .env         # 填入 DEEPSEEK_API_KEY=sk-...
.venv/Scripts/python.exe -m src.main --input examples/02-conflict.input.json \
  --out examples/02-conflict.live.output.json --trace examples/02-conflict.live.trace.json
```

不加 `--offline` 时走真实模型；缺少密钥会直接报错并提示改用 `--offline`，不会静默降级成假结果。

模型 id 不是猜的：`LLM_MODEL` 的值取自 `curl https://api.deepseek.com/models`（Bearer 密钥）实测返回的
`deepseek-v4-pro` 与 `deepseek-flash`。想换模型改这一行即可，写错名字会在第一次请求时直接报错，不会悄悄换模型。

## 评审入口（按题面验收点）

| 验收点 | 用例 | 看什么 |
| --- | --- | --- |
| 正常排期 | `examples/01-normal.*` | `outcome=scheduled`，草稿 `draft_ready` |
| 时段冲突（参考实例：18:00 占用 → 19:00） | `examples/02-conflict.*` | `suggested_time=19:00`、`requires_user_confirmation=true`、`published=false` |
| 重复提交 | `examples/05-dedup.*`（紧接 02 运行） | 只有 1 份草稿，`submit_count=2`，`draft_id` 不变 |
| 素材信息缺失 | `examples/03-rules-and-missing.*` | M03 `needs_info`，`missing_fields` 非空，无草稿 |
| 规则风险 | 同上 | M04 `blocked`，`risk_reasons` 命中 R03 |
| 保存失败 | `examples/04-save-failure.*` | `result_status=save_failed`，无草稿，不谎报成功 |
| 步数上限 | `examples/06-step-limit.*` | `status=step_limit_reached`、`completed=false`、列出未完成素材 |

每个 `.output.json` 是实际输出，`.trace.json` 是同一次运行的分步记录（动作 / 参数 / 观察摘要 / 下一步与决策依据）。

## 目录

```
src/        store / policy / tools / models / graph / trace / report / main
data/       合成素材、审核规则、已有排期、草稿账本
examples/   6 个用例的输入 + 实际输出 + trace + test-results.txt
tests/      29 个 unittest（规则、工具守卫、图级链路、trace 结构）
scripts/    run_examples.py（一键重跑全部用例）
docs/       01-需求 / 02-方案 / 03-评估 / 04-AI 使用 / 05-复盘
```

## 边界

- **只保存草稿**：工具集里没有发布工具，报告里 `published` 恒为 `false`（结构上不可能越权发布）。
- 草稿账本默认 `data/drafts.json`，可用 `--ledger` 指向别处；重跑前可用 `scripts/run_examples.py` 自动重置。
- 数据全部为合成数据，无真实用户/素材信息，无真实密钥入库。
