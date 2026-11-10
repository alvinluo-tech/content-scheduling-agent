# 素材审核与发布排期 Agent（小影校招笔试题 · 项目题 1）

一个**模型驱动**的排期助手：读素材 → 按规则审核 → 查时段 → 处理冲突 → 保存**草稿**（绝不发布）。
模型每一步都要基于上一步的工具结果决定下一步动作（ReAct 循环），而不是走固定分支。

- 语言/框架：Python 3.10+ / LangGraph（显式 StateGraph + 工具节点）
- 模型：DeepSeek 官方 API（OpenAI 兼容），默认 `deepseek-v4-pro`；无密钥时可用离线 Mock 跑通全流程
- 交互形态：CLI（JSON 输入 → JSON 结果 + 逐步 trace，可选导出 Markdown 报告）；另附一个可选的 Web 演示界面（同一引擎的薄壳）

## 60 秒上手

```bash
# 1. 建环境（任选其一）
uv venv .venv && uv pip install --python .venv/Scripts/python.exe -r requirements.txt
# 或：python -m venv .venv && .venv/Scripts/python -m pip install -r requirements.txt

# 2. 离线跑一遍（不需要任何密钥，验证流程与守卫）
.venv/Scripts/python.exe -m src.main --input examples/02-conflict.input.json --offline \
  --out examples/02-conflict.output.json --trace examples/02-conflict.trace.json \
  --report-md examples/02-conflict.report.md

# 3. 一次跑完全部 10 个用例（会重置草稿账本）
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

硬性时间被占用时，Agent 只能登记待确认项；确认由人来给（模型没有审批工具）：

```bash
# 02 跑完会打印/写出 C-M01-2026-11-12-1900；人点头后重跑，草稿才变成 draft_ready
.venv/Scripts/python.exe -m src.main --input examples/02-conflict.input.json --offline \
  --approve C-M01-2026-11-12-1900

# 也可以否决：该改期方案连同那份待确认草稿一起作废，重跑后 save_draft 会被
# CONFIRMATION_REJECTED 挡住，不会绕过去再存一遍
.venv/Scripts/python.exe -m src.main --input examples/02-conflict.input.json --offline \
  --reject C-M01-2026-11-12-1900

# 还可以改选：否决模型提的 19:00，但把自己挑的 20:30 记为已同意，
# 重跑时模型先撞 CONFIRMATION_REJECTED，再改存 20:30 直接落 draft_ready
.venv/Scripts/python.exe -m src.main --input examples/02-conflict.input.json --offline \
  --replan C-M01-2026-11-12-1900=20:30
```

模型 id 不是猜的：`LLM_MODEL` 的值取自 `curl https://api.deepseek.com/models`（Bearer 密钥）实测返回的
`deepseek-v4-pro` 与 `deepseek-flash`。想换模型改这一行即可，写错名字会在第一次请求时直接报错，不会悄悄换模型。

## 可选：Web 演示界面

CLI 是主入口，Web 只是一层薄壳：它调用的仍是 `src/main.py` 的 `run_pipeline()`，报告与 trace 与 CLI 完全同构。

```bash
.venv/Scripts/python.exe -m src.server        # 打开 http://127.0.0.1:7860
```

左侧「自定义一次请求」可以不用预置用例：选素材、日期、时间、频道、是否硬性，点「用这条请求跑一次」，
后端（`POST /api/custom`）先用与 CLI 同一个 `validate_run()` 校验，再登记成 `custom-N`，
之后走的还是同一条 SSE + StateGraph 链路——冲突、规则风险仍由工具判定，不因为是从界面上手填的就放宽。
这类请求只活在当前进程的内存里，重启服务即消失。

演示服务默认不设防（本机使用）。想加一道口令就加 `--token`：

```bash
.venv/Scripts/python.exe -m src.server --token 换成你自己的口令
```

开启后所有 `/api/*` 都要带 `X-Demo-Token` 头或 `?token=`，否则 401；页面仍可打开，
地址栏、页面右上角会出现口令输入框，填对后自动补到后续每个请求（存在 `sessionStorage`，刷新还在、关掉标签页即失效）。
这只是共享密钥，不是账号体系；`data/drafts.json` 与 `.runtime/ledger.json` 仍然不设防。

左侧十个场景就是 `examples/` 里的输入，点「运行这个场景」会跑一次并把报告、时段带、决策记录渲染出来；
「离线 Mock」勾选状态与 CLI 的 `--offline` 对应，取消勾选才走真实模型。演示账本写到 `.runtime/ledger.json`，
不污染 `data/drafts.json`。

界面走的是 SSE（`GET /api/stream`）：`TraceRecorder` 每录一步就把快照推给浏览器，所以真实模型那种十几秒的运行
能看到决策一条条长出来，而不是转圈结束才一次性刷出结果。上一轮结论在新一轮跑完前压暗显示，避免被误读成当前结果。

排期支持多日：`data/schedule.json` 的 `days` 里配置每一天的已有排期，请求可以用 `requested_date` 指定日期（不写就用默认日期）。
时段带上方会按天出现切换按钮，跑完自动跳到结果所在的日期。

排期也支持多频道：`data/schedule.json` 的 `channels` 里配置其它频道的排期（示例里是 `shorts`），请求可以用
`requested_channel` 指定。频道之间互不占位——同一时刻主频道空着、短视频频道被占，是两条独立的排期；
草稿 id 与确认项 id 带频道后缀（`D-M02-2026-11-12-1200-shorts`），换频道不会串到别人的草稿上。
时段带上方会先出现频道标签，再看日期。

![时段冲突场景](docs/images/web-02-conflict.png)

报告还能导出：界面上的「导出 Markdown 报告」按钮（`GET /api/report.md`，服务端只保留最近一次运行，不落盘），
CLI 对应 `--report-md`。两者都复用 `src/markdown.py` 的同一份渲染，内容就是那份 `report` + 决策记录。

截图：`docs/images/web-02-conflict.png`（冲突→待确认）、`web-03-rules.png`（信息缺失 + 规则风险）、
`web-04-save-failure.png`（保存失败如实返回）、`web-06-step-limit.png`（步数上限中断）、
`web-02-conflict-live.png`（同一场景走真实 DeepSeek：右上角切到「真实模型」，决策依据由模型自己写）、
`web-02-conflict-streaming.png`（真实模型运行中的一帧：右栏逐条推送，中间上一轮结论压暗）、
`web-07-multiday.png`（多日排期：11-13 的占用与两份草稿、按天切换）、`web-08-day-overflow.png`（当天排满：11-14 整日有排期，改期到 11-15 09:30 待确认）、`web-09-channel.png`（短视频频道：12:00 被占，主频道同一时刻是空的）、`web-10-contention.png`（两条请求抢 13:00：M01 已存草稿、M02 改到 13:30 待确认）、`web-token-prompt.png` / `web-token-unlocked.png`（口令开启时：先只看到输入框与「需要演示口令」，填对后正常载入 10 个场景）、
`web-02-conflict-replan.png`（待确认项下面多了「工具给过的候选 + 改到所选时段」）、`web-02-conflict-replanned.png`（改选 20:30 之后：确认项显示「已否决」与「已同意 · 改选自 C-M01-2026-11-12-1900 · 由 human 于 … 决定」，结论「已排期 · 仅草稿」、草稿 `draft_ready`、`published` 仍为 false）、`web-02-conflict-rejected.png`（人点了「不同意这个改期」之后：结论变「存在失败项」、草稿标为「已否决 · 草稿作废」）、`web-report-md.png`（结论下方多出「导出 Markdown 报告」）、`web-custom-request.png`（左栏展开自定义表单：素材 M01 要求 11-12 18:00，跑出来仍是「待用户确认」+ 建议 19:00）、`web-narrow.png`（884px 窄屏：单列堆叠，顶栏显示本轮耗时）。
键盘操作：`↑`/`↓`（或 `←`/`→`）在场景间移动、`Ctrl/⌘ + Enter` 运行，焦点有可见描边。

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
| 当天排满 · 顺延 | `examples/08-day-overflow.*` | 11-14 整日排满、18:00 之后无位置 → 工具给出次日 09:30，模型先登记 `C-M01-2026-11-15-0930` 再存草稿：`scheduled_date=2026-11-15`、`date_changed=true`、`draft_pending_confirmation`、`published=false` |
| 竞争 | `examples/10-contention.*` | 两条硬性请求都要 13:00：M01 先占住，M02 自己查到时已被占 → 提议 13:30 待确认。同一次运行内也不双订 |
| 多频道 | `examples/09-channel.*` | 两条都在 `shorts`：12:00 被 M00 占 → 提议 12:30 待确认；13:00 空着直接落草稿。主频道同一时刻仍是空的（不串道），草稿 id 带 `-shorts` |
| 多日排期 | `examples/07-multiday.*` | M06 在 11-13 09:00 被 M02 占 → `scheduled_date=2026-11-13`、`suggested_time=09:30`、待确认；M07 同一天 20:00 直接落草稿；`published=false` |

每个 `.output.json` 是实际输出，`.trace.json` 是同一次运行的分步记录（动作 / 参数 / 观察摘要 / 下一步与决策依据）。

## 目录

```
src/        store / policy / tools / models / graph / trace / report / markdown / main / server（Web 演示用，标准库 http.server）
data/       合成素材、审核规则、已有排期、草稿账本
examples/   10 个用例的输入 + 实际输出 + trace + Markdown 报告（*.report.md）+ test-results.txt
web/        index.html / app.css / app.js（可选演示界面，无构建步骤）
tests/      87 个 unittest（规则、工具守卫、图级链路、trace 结构、多日排期、频道隔离、账本并发、替代时段排序、Markdown 报告、人工同意 / 否决 / 改选、HTTP 接口 / SSE 推送、键盘与读屏钩子）
scripts/    run_examples.py（一键重跑全部用例）
docs/       01-需求 / 02-方案 / 03-评估 / 04-AI 使用 / 05-复盘 + images/（界面截图）
```

## 边界

- **只保存草稿**：工具集里没有发布工具，报告里 `published` 恒为 `false`（结构上不可能越权发布）。
- 草稿账本默认 `data/drafts.json`，可用 `--ledger` 指向别处；重跑前可用 `scripts/run_examples.py` 自动重置。
- 账本写入是「锁 → 重读 → 改 → 原子替换」（锁文件 `<ledger>.json.lock`），CLI 与 Web 同时跑也不会丢更新；锁等不到 10 秒会明确报 `LEDGER_BUSY`，不假装写成功。
- 数据全部为合成数据，无真实用户/素材信息，无真实密钥入库。
- 入口只拦结构性错误的输入（重复素材、缺时间要求），业务性的不可行（时间不在网格、日期/频道没配置）交给工具回报给模型；错误输入退出码为 2。
- 改期只能由人拍板：`--approve` / `--reject` / `--replan`（或 `/api/confirm` / `/api/reject`，`/api/reject` 带 `time` 即为改选）；否决后同一方案会被 `CONFIRMATION_REJECTED` 挡住。
- 日期与频道都只覆盖 `schedule.json` 里配置过的（示例给到 2026-11-15 与 `main` / `shorts`）；没有频道级的规则差异与流量/负载优化。
