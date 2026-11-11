"""可选 Web 演示层：把同一张图、同一套工具包在一个极薄的 HTTP 壳里。

这里没有第二条执行路径 —— /api/run 直接调用 src.main.run_pipeline()，
与 CLI 用的是同一个 StateGraph、同一套工具护栏、同一份草稿账本。
UI 只负责三件事：选场景、展示报告、展示决策记录。
"""
from __future__ import annotations

import argparse
import hmac
import json
import os
import queue
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, unquote, urlparse

from .main import load_env, run_pipeline
from .markdown import render_report
from .store import Store
from .validation import validate_run

REPO_ROOT = Path(__file__).resolve().parent.parent
WEB_ROOT = REPO_ROOT / "web"
DATA_DIR = REPO_ROOT / "data"
EXAMPLES_DIR = REPO_ROOT / "examples"
RUNTIME_DIR = REPO_ROOT / ".runtime"
LEDGER_PATH = RUNTIME_DIR / "ledger.json"
# 演示口令：留空表示不校验（本地演示默认）。填了之后所有 /api/* 都要带 X-Demo-Token 或 ?token=。
# 这不是账号体系，只是把"本机任何进程都能读改账本"降级成"要拿到口令"。
DEMO_TOKEN: str = ""

CASE_META: list[dict[str, Any]] = [
    {
        "file": "01-normal",
        "title": "正常排期",
        "tag": "正常",
        "note": "M02 要求 11:00，时段空闲；标题超长只报警告，不阻断",
    },
    {
        "file": "02-conflict",
        "title": "时段冲突",
        "tag": "冲突",
        "note": "M01 硬性要求 18:00，已被 M00 占用；19:00 可用，先确认再存草稿",
    },
    {
        "file": "05-dedup",
        "title": "重复提交",
        "tag": "去重",
        "note": "同一请求再提交一次；需先跑过 02，账本里已有同一份草稿",
    },
    {
        "file": "03-rules-and-missing",
        "title": "信息缺失 + 规则风险",
        "tag": "阻断",
        "note": "M03 缺时长且授权未知；M04 命中违禁词；两者都不许存草稿",
    },
    {
        "file": "04-save-failure",
        "title": "保存失败",
        "tag": "失败",
        "note": "M05 保存接口持续失败；重试一次后如实放弃，不谎报成功",
    },
    {
        "file": "06-step-limit",
        "title": "步数上限",
        "tag": "上限",
        "note": "max_steps=3，两条素材处理不完，返回明确的未完成状态",
    },
    {
        "file": "07-multiday",
        "title": "多日排期",
        "tag": "多日",
        "note": "11-13 09:00 被 M02 占用：硬性要求只能提议 09:30 并待确认；另一条素材在 11-13 空白时段落草稿",
    },
    {
        "file": "08-day-overflow",
        "title": "当天排满 · 顺延次日",
        "tag": "顺延",
        "note": "11-14 整日排满，18:00 之后没有位置：工具给出次日 09:30，硬性要求必须登记待确认",
    },
    {
        "file": "09-channel",
        "title": "短视频频道",
        "tag": "频道",
        "note": "shorts 的 12:00 被 M00 占用（主频道同一时刻是空的）：M02 只能提议 12:30 并待确认，M06 13:00 直接落草稿",
    },
    {
        "file": "10-contention",
        "title": "两条抢同一时段",
        "tag": "竞争",
        "note": "M01 先落 13:00 草稿并占住时段，M02 硬性也要 13:00：只能提议 13:30 并待确认，同一次运行内也不双订",
    },
]

RUN_LOCK = threading.Lock()
REPORT_LOCK = threading.Lock()
LAST_REPORT: dict[str, Any] = {}


def remember_report(report: dict[str, Any], trace: dict[str, Any], case: str = "") -> None:
    """记住最近一次运行，供 GET /api/report.md 导出；只存在内存里，不落盘。"""
    preset = [item["file"] for item in CASE_META if item["file"] == case]
    if preset:  # 预置场景的输入文件是归档的那一份；自定义请求没有文件，报告里就不写命令
        report["input_file"] = "examples/" + preset[0] + ".input.json"
    with REPORT_LOCK:
        LAST_REPORT.clear()
        LAST_REPORT.update({"report": report, "trace": trace})


def _load_or_default(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


def snapshot() -> dict[str, Any]:
    """给 UI 的静态资料快照：日期、营业时段、已有排期、当前账本。"""
    schedule = _load_or_default(DATA_DIR / "schedule.json", {})
    rules = _load_or_default(DATA_DIR / "rules.json", {})
    ledger = _load_or_default(LEDGER_PATH, {"drafts": {}, "confirmations": {}})
    days = schedule.get("days", {})
    default_date = schedule.get("date")
    default_channel = schedule.get("channel", "main")
    channel_days = {
        default_channel: days,
        **{name: item.get("days", {}) for name, item in schedule.get("channels", {}).items()},
    }
    return {
        "date": default_date,
        "dates": sorted(days.keys()),
        "days": {day: days.get(day, []) for day in sorted(days.keys())},
        "channel": default_channel,
        "channels": [
            {
                "name": name,
                "label": schedule.get("channel_label", "主频道") if name == default_channel else schedule["channels"][name].get("label", name),
                "default": name == default_channel,
                "dates": sorted(channel_days.get(name, {}).keys()),
            }
            for name in [default_channel] + [n for n in schedule.get("channels", {}) if n != default_channel]
        ],
        "channel_days": {name: {day: items for day, items in doc.items()} for name, doc in channel_days.items()},
        "timezone": schedule.get("timezone"),
        "hours": rules.get("operating_hours", {}),
        "rules": [
            {"id": item.get("id"), "name": item.get("name"), "severity": item.get("severity")}
            for item in rules.get("rules", [])
        ],
        "slots": days.get(default_date, []),
        "materials": [
            {
                "material_id": key,
                "title": item.get("title"),
                "duration_sec": item.get("duration_sec"),
                "category": item.get("category"),
            }
            for key, item in sorted(_load_or_default(DATA_DIR / "materials.json", {}).items())
        ],
        "drafts": ledger.get("drafts", {}),
        "confirmations": ledger.get("confirmations", {}),
        "cases": CASE_META,
        "ledger": _ledger_label(),
    }


def _ledger_label() -> str:
    try:
        return str(LEDGER_PATH.relative_to(REPO_ROOT))
    except ValueError:
        return str(LEDGER_PATH)


def read_case_input(case: str) -> dict[str, Any]:
    """把示例输入读成一次运行请求；场景名必须在白名单里，或指向本次会话登记的自定义请求。"""
    with CUSTOM_LOCK:
        if case in CUSTOM_RUNS:
            return json.loads(json.dumps(CUSTOM_RUNS[case]))
    if case not in {item["file"] for item in CASE_META}:
        raise ValueError("未知场景：" + (case or "(空)"))
    return json.loads((EXAMPLES_DIR / (case + ".input.json")).read_text(encoding="utf-8"))


# 界面里填的自定义请求只活在这一次服务里：注册一个编号，之后仍走同一条 /api/stream 链路。
CUSTOM_RUNS: dict[str, dict[str, Any]] = {}
CUSTOM_LOCK = threading.Lock()
CUSTOM_SEQ = [0]


def register_custom(body: dict[str, Any]) -> dict[str, Any]:
    """把界面上填的一条请求变成与示例同形状的 input，校验后登记成可重复运行的编号。"""
    requests = body.get("requests")
    if not isinstance(requests, list) or not requests:
        raise ValueError("自定义请求至少要有一条")
    cleaned: list[dict[str, Any]] = []
    for item in requests:
        if not isinstance(item, dict):
            raise ValueError("自定义请求的每一条都必须是对象")
        entry = {
            "material_id": str(item.get("material_id") or "").strip(),
            "requested_time": str(item.get("requested_time") or "").strip(),
            "time_hard": bool(item.get("time_hard")),
            "note": str(item.get("note") or "界面里手填的请求"),
        }
        for key in ("requested_date", "requested_channel"):
            value = str(item.get(key) or "").strip()
            if value:
                entry[key] = value
        cleaned.append(entry)
    max_steps = body.get("max_steps")
    run: dict[str, Any] = {
        "now": str(body.get("now") or "2026-11-12T09:20:00+08:00"),
        "requests": cleaned,
        "simulate": {"save_failures": 0},
    }
    if max_steps not in (None, ""):
        run["options"] = {"max_steps": int(max_steps)}
    validate_run(run)
    with CUSTOM_LOCK:
        CUSTOM_SEQ[0] += 1
        case = "custom-" + str(CUSTOM_SEQ[0])
        run["run_id"] = case
        CUSTOM_RUNS[case] = run
    return {"status": "ok", "case": case, "run": run}


def run_case(body: dict[str, Any]) -> dict[str, Any]:
    case = str(body.get("case") or "")
    run = read_case_input(case)
    if body.get("reset"):
        LEDGER_PATH.unlink(missing_ok=True)
    max_steps = body.get("max_steps")
    report, trace = run_pipeline(
        run,
        data_dir=DATA_DIR,
        ledger=LEDGER_PATH,
        max_steps=int(max_steps) if max_steps not in (None, "") else None,
        offline=bool(body.get("offline", True)),
        run_id=case,
    )
    remember_report(report, trace, case)
    return {"status": "ok", "case": case, "report": report, "trace": trace, "state": snapshot()}


CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".json": "application/json; charset=utf-8",
    ".ico": "image/x-icon",
}


class Handler(BaseHTTPRequestHandler):
    server_version = "SchedulingDesk/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        pass

    def _send(self, code: int, payload: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _download(self, filename: str, payload: bytes, content_type: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Content-Disposition", 'attachment; filename="' + filename + '"')
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _json(self, code: int, payload: dict[str, Any]) -> None:
        self._send(code, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _static(self, path: str) -> None:
        rel = unquote(path).lstrip("/") or "index.html"
        target = (WEB_ROOT / rel).resolve()
        if not str(target).startswith(str(WEB_ROOT)) or not target.is_file():
            self._json(404, {"status": "error", "error": "文件不存在"})
            return
        ctype = CONTENT_TYPES.get(target.suffix.lower(), "application/octet-stream")
        self._send(200, target.read_bytes(), ctype)

    def _token_ok(self) -> bool:
        if not DEMO_TOKEN:
            return True
        given = self.headers.get("X-Demo-Token") or ""
        if not given:
            given = dict(parse_qsl(urlparse(self.path).query)).get("token") or ""
        return hmac.compare_digest(given, DEMO_TOKEN)

    def _require_token(self) -> bool:
        """返回 True 表示这次请求可以继续；否则已经回好 401。"""
        if self._token_ok():
            return True
        self._json(401, {"status": "error", "error": "需要演示口令", "auth_required": True})
        return False

    def _sse(self, event: str, payload: dict[str, Any]) -> None:
        chunk = "event: " + event + "\ndata: " + json.dumps(payload, ensure_ascii=False) + "\n\n"
        self.wfile.write(chunk.encode("utf-8"))
        self.wfile.flush()

    def stream_case(self, params: dict[str, str]) -> None:
        """边跑边推：每录完一步发一条 trace 事件，最后发 report。"""
        case = str(params.get("case") or "")
        try:
            run = read_case_input(case)
        except ValueError as exc:
            self._json(400, {"status": "error", "error": str(exc)})
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        events: queue.Queue[dict[str, Any]] = queue.Queue()
        try:
            self._sse("meta", {"case": case})
            with RUN_LOCK:
                if params.get("reset") in ("1", "true", "yes"):
                    LEDGER_PATH.unlink(missing_ok=True)
                max_steps = params.get("max_steps") or None

                def worker() -> None:
                    try:
                        report, trace = run_pipeline(
                            run,
                            data_dir=DATA_DIR,
                            ledger=LEDGER_PATH,
                            max_steps=int(max_steps) if max_steps else None,
                            offline=params.get("offline") in (None, "", "1", "true", "yes"),
                            run_id=case,
                            on_trace_event=events.put,
                        )
                        remember_report(report, trace, case)
                        events.put({"type": "report", "case": case, "report": report, "trace": trace, "state": snapshot()})
                    except Exception as exc:
                        traceback.print_exc()
                        events.put({"type": "failed", "error": str(exc) or exc.__class__.__name__})

                thread = threading.Thread(target=worker, daemon=True)
                thread.start()
                while True:
                    event = events.get()
                    if event.get("type") == "report":
                        self._sse("report", event)
                        break
                    if event.get("type") == "failed":
                        self._sse("failed", event)
                        break
                    self._sse("trace", event)
                thread.join(timeout=5)
        except (BrokenPipeError, ConnectionResetError):
            return

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path.startswith("/api/") and not self._require_token():
            return
        if path == "/api/stream":
            self.stream_case(dict(parse_qsl(urlparse(self.path).query)))
            return
        if path == "/api/state":
            self._json(200, {"status": "ok", "state": snapshot()})
            return
        if path == "/api/report.md":
            with REPORT_LOCK:
                payload = dict(LAST_REPORT)
            if not payload:
                self._json(404, {"status": "error", "error": "还没有运行记录，先跑一个场景"})
                return
            text = render_report(payload["report"], payload["trace"])
            run_id = str((payload.get("report") or {}).get("run_id") or "run")
            self._download(run_id + ".report.md", text.encode("utf-8"), "text/markdown; charset=utf-8")
            return
        if path.startswith("/api/"):
            self._json(404, {"status": "error", "error": "未知接口"})
            return
        self._static(path)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path.startswith("/api/") and not self._require_token():
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            self._json(400, {"status": "error", "error": "请求体不是合法 JSON"})
            return
        if path == "/api/reset":
            LEDGER_PATH.unlink(missing_ok=True)
            self._json(200, {"status": "ok", "state": snapshot()})
            return
        if path == "/api/confirm":
            with RUN_LOCK:
                try:
                    store = Store(DATA_DIR, LEDGER_PATH)
                    record = store.approve_confirmation(str(body.get("confirmation_id") or ""))
                except ValueError as exc:
                    self._json(400, {"status": "error", "error": str(exc)})
                    return
                self._json(200, {"status": "ok", "confirmation": record, "state": snapshot()})
            return
        if path == "/api/reject":
            with RUN_LOCK:
                try:
                    store = Store(DATA_DIR, LEDGER_PATH)
                    confirmation_id = str(body.get("confirmation_id") or "")
                    chosen = str(body.get("time") or "").strip()
                    # 人可以直接否决，也可以"否决这个、改到我自己挑的时段"：后者登记一条 approved。
                    record = (
                        store.replan_confirmation(
                            confirmation_id, chosen, date=str(body.get("date") or "").strip() or None
                        )
                        if chosen
                        else store.reject_confirmation(confirmation_id, note=str(body.get("note") or ""))
                    )
                except ValueError as exc:
                    self._json(400, {"status": "error", "error": str(exc)})
                    return
                self._json(200, {"status": "ok", "confirmation": record, "state": snapshot()})
            return
        if path == "/api/revoke":
            with RUN_LOCK:
                try:
                    store = Store(DATA_DIR, LEDGER_PATH)
                    draft = store.revoke_draft(str(body.get("draft_id") or ""), note=str(body.get("note") or ""))
                except ValueError as exc:
                    self._json(400, {"status": "error", "error": str(exc)})
                    return
                self._json(200, {"status": "ok", "draft": draft, "state": snapshot()})
            return
        if path == "/api/custom":
            with RUN_LOCK:
                try:
                    self._json(200, register_custom(body))
                except (ValueError, TypeError) as exc:
                    self._json(400, {"status": "error", "error": str(exc)})
            return
        if path == "/api/run":
            with RUN_LOCK:
                try:
                    self._json(200, run_case(body))
                except ValueError as exc:
                    self._json(400, {"status": "error", "error": str(exc)})
                except Exception as exc:
                    traceback.print_exc()
                    self._json(500, {"status": "error", "error": str(exc) or exc.__class__.__name__})
            return
        self._json(404, {"status": "error", "error": "未知接口"})


def serve(host: str, port: int) -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    httpd = ThreadingHTTPServer((host, port), Handler)
    print("排期工作台已启动：http://" + host + ":" + str(port) + "   (Ctrl+C 退出)")
    print("草稿账本：" + str(LEDGER_PATH.relative_to(REPO_ROOT)) + "（与 data/drafts.json 隔离，演示不会污染归档产物）")
    if DEMO_TOKEN:
        print("已开启演示口令：/api/* 需要 X-Demo-Token 头或 ?token= 参数（口令本身不打印）")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("")
    finally:
        httpd.server_close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="排期工作台（Web 演示层，只保存草稿）")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7860)
    parser.add_argument(
        "--token",
        default="",
        help="开启演示口令：/api/* 需要 X-Demo-Token 头或 ?token= 参数；也可以用环境变量 DEMO_TOKEN",
    )
    args = parser.parse_args(argv)
    load_env()
    global DEMO_TOKEN
    DEMO_TOKEN = args.token or os.environ.get("DEMO_TOKEN", "")
    serve(args.host, args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())