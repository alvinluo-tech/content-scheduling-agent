"""可选 Web 演示层：把同一张图、同一套工具包在一个极薄的 HTTP 壳里。

这里没有第二条执行路径 —— /api/run 直接调用 src.main.run_pipeline()，
与 CLI 用的是同一个 StateGraph、同一套工具护栏、同一份草稿账本。
UI 只负责三件事：选场景、展示报告、展示决策记录。
"""
from __future__ import annotations

import argparse
import json
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

REPO_ROOT = Path(__file__).resolve().parent.parent
WEB_ROOT = REPO_ROOT / "web"
DATA_DIR = REPO_ROOT / "data"
EXAMPLES_DIR = REPO_ROOT / "examples"
RUNTIME_DIR = REPO_ROOT / ".runtime"
LEDGER_PATH = RUNTIME_DIR / "ledger.json"

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
]

RUN_LOCK = threading.Lock()
REPORT_LOCK = threading.Lock()
LAST_REPORT: dict[str, Any] = {}


def remember_report(report: dict[str, Any], trace: dict[str, Any]) -> None:
    """记住最近一次运行，供 GET /api/report.md 导出；只存在内存里，不落盘。"""
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
    return {
        "date": default_date,
        "dates": sorted(days.keys()),
        "days": {day: days.get(day, []) for day in sorted(days.keys())},
        "channel": schedule.get("channel"),
        "timezone": schedule.get("timezone"),
        "hours": rules.get("operating_hours", {}),
        "rules": [
            {"id": item.get("id"), "name": item.get("name"), "severity": item.get("severity")}
            for item in rules.get("rules", [])
        ],
        "slots": days.get(default_date, []),
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
    """把示例输入读成一次运行请求；场景名必须在白名单里。"""
    if case not in {item["file"] for item in CASE_META}:
        raise ValueError("未知场景：" + (case or "(空)"))
    return json.loads((EXAMPLES_DIR / (case + ".input.json")).read_text(encoding="utf-8"))


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
    remember_report(report, trace)
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
                        remember_report(report, trace)
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
    args = parser.parse_args(argv)
    load_env()
    serve(args.host, args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())