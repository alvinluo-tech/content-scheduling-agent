from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Callable

from langchain_core.messages import HumanMessage

from .graph import build_agent_graph
from .markdown import render_report
from .models import build_chat_model, build_system_prompt
from .report import build_report
from .store import Store, atomic_write_json
from .tools import Toolbox
from .trace import TraceRecorder

DEFAULT_MAX_STEPS = 20


def load_env() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(Path.cwd() / ".env")
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")


def resolve_max_steps(cli_value: int | None, run: dict[str, Any]) -> int:
    if cli_value is not None:
        return cli_value
    option = (run.get("options") or {}).get("max_steps")
    if option is not None:
        return int(option)
    return int(os.environ.get("MAX_STEPS", DEFAULT_MAX_STEPS))


def run_pipeline(
    run: dict[str, Any],
    *,
    data_dir: str | Path,
    ledger: str | Path | None,
    max_steps: int | None = None,
    offline: bool = False,
    run_id: str | None = None,
    on_trace_event: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """跑一遍图，返回 (报告, 轨迹)。CLI 与 Web 演示层共用这一条路径，避免两套行为。"""
    store = Store(data_dir, ledger)
    toolbox = Toolbox(store, run)
    steps = resolve_max_steps(max_steps, run)
    mode = "offline" if offline else "deepseek"
    llm = build_chat_model(mode, run)
    model_name = getattr(llm, "model_name", None) or getattr(llm, "mode", mode)
    trace = TraceRecorder(run.get("run_id") or run_id or "run", mode, str(model_name))
    if on_trace_event is not None:
        trace.add_listener(on_trace_event)
    system_text = build_system_prompt(run, store.schedule_date(), store.operating_hours(), store.available_dates())
    graph = build_agent_graph(llm, toolbox, trace, steps, system_text)
    state = graph.invoke(
        {"messages": [HumanMessage(content="请按系统提示处理本次排期请求。")], "steps": 0, "tool_calls": 0}
    )
    limits = {"max_steps": steps, "steps_used": state.get("steps", 0), "tool_calls": state.get("tool_calls", 0)}
    report = build_report(run, store, toolbox, trace, limits)
    return report, trace.as_dict(limits)


def apply_approvals(args: argparse.Namespace) -> None:
    """人工决策通道（模型没有这些工具）：同意落 approved；否决落 rejected 并作废草稿；改选落一条 approved。"""
    approved = getattr(args, "approve", None) or []
    rejected = getattr(args, "reject", None) or []
    replanned = getattr(args, "replan", None) or []
    if not approved and not rejected and not replanned:
        return
    store = Store(args.data_dir, args.ledger)
    decisions = [(cid, "approve") for cid in approved]
    decisions += [(cid, "reject") for cid in rejected]
    decisions += [(item, "replan") for item in replanned]
    for raw, action in decisions:
        record = _apply_decision(store, raw, action)
        print(
            json.dumps(
                {
                    "status": record["status"],
                    "confirmation_id": record["confirmation_id"],
                    "material_id": record["material_id"],
                    "proposed_date": record.get("proposed_date", record.get("date")),
                    "proposed_time": record["proposed_time"],
                    "decided_by": record.get("approved_by") or record.get("rejected_by") or record.get("decided_by"),
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )


def _apply_decision(store: Store, raw: str, action: str) -> dict[str, Any]:
    if action == "approve":
        return store.approve_confirmation(raw)
    if action == "reject":
        return store.reject_confirmation(raw)
    confirmation_id, _, slot = raw.partition("=")
    if not slot:
        raise ValueError(f"--replan 需要写成 C-...=19:30 或 C-...=2026-11-12 19:30，收到 {raw}")
    date: str | None = None
    time_str = slot.strip()
    if " " in time_str:
        date, time_str = time_str.split(None, 1)
    return store.replan_confirmation(confirmation_id.strip(), time_str.strip(), date=date)


def run_once(args: argparse.Namespace) -> dict[str, Any]:
    run: dict[str, Any] = json.loads(Path(args.input).read_text(encoding="utf-8"))
    apply_approvals(args)
    report, trace_dict = run_pipeline(
        run,
        data_dir=args.data_dir,
        ledger=args.ledger,
        max_steps=args.max_steps,
        offline=args.offline,
        run_id=Path(args.input).stem,
    )
    if args.trace:
        atomic_write_json(Path(args.trace), trace_dict)
        report["trace_file"] = str(args.trace)
    if getattr(args, "report_md", None):
        md_path = Path(args.report_md)
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text(render_report(report, trace_dict), encoding="utf-8")
        report["report_md"] = str(args.report_md)
    if args.out:
        atomic_write_json(Path(args.out), report)
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="素材审核与发布排期 Agent（只保存草稿，不发布）")
    parser.add_argument("--input", required=True, help="排期请求 JSON 文件路径")
    parser.add_argument("--data-dir", default="data", help="合成资料目录（materials/rules/schedule）")
    parser.add_argument("--ledger", default=None, help="草稿账本路径，默认 <data-dir>/drafts.json")
    parser.add_argument("--out", default=None, help="把结果 JSON 写入该路径（同时仍打印到 stdout）")
    parser.add_argument("--trace", default=None, help="把执行轨迹写入该路径")
    parser.add_argument("--report-md", dest="report_md", default=None, help="把同一次运行渲染成人读的 Markdown 报告")
    parser.add_argument("--max-steps", type=int, default=None, help="模型-工具往返次数上限，达到即返回未完成状态")
    parser.add_argument("--offline", action="store_true", help="离线 Mock 模式：只验证流程，不代表真实模型决策")
    parser.add_argument(
        "--approve",
        action="append",
        metavar="CONFIRMATION_ID",
        help="人工确认某个待确认项（可重复），随后再跑一次即可落成 draft_ready；模型没有这个能力",
    )
    parser.add_argument(
        "--reject",
        action="append",
        metavar="CONFIRMATION_ID",
        help="人工否决某个待确认项（可重复）：该改期方案连同它的待确认草稿一起作废；模型没有这个能力",
    )
    parser.add_argument(
        "--replan",
        action="append",
        metavar="CONFIRMATION_ID=HH:MM",
        help="人不同意模型提的时段、自己改选一个（可重复）：写成 C-...=19:30，也可带日期 C-...='2026-11-12 19:30'",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    load_env()
    try:
        report = run_once(args)
    except FileNotFoundError as exc:
        print(json.dumps({"status": "error", "error": f"输入文件不存在：{exc}"}, ensure_ascii=False), file=sys.stderr)
        return 2
    except (RuntimeError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
