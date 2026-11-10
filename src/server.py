"""可选 Web 演示层：把同一张图、同一套工具包在一个极薄的 HTTP 壳里。

这里没有第二条执行路径 —— /api/run 直接调用 src.main.run_pipeline()，
与 CLI 用的是同一个 StateGraph、同一套工具护栏、同一份草稿账本。
UI 只负责三件事：选场景、展示报告、展示决策记录。
"""
from __future__ import annotations

import argparse
import json
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from .main import load_env, run_pipeline

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
]

RUN_LOCK = threading.Lock()


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
    return {
        "date": schedule.get("date"),
        "channel": schedule.get("channel"),
        "timezone": schedule.get("timezone"),
        "hours": rules.get("operating_hours", {}),
        "rules": [
            {"id": item.get("id"), "name": item.get("name"), "severity": item.get("severity")}
            for item in rules.get("rules", [])
        ],
        "slots": schedule.get("slots", []),
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


def run_case(body: dict[str, Any]) -> dict[str, Any]:
    case = str(body.get("case") or "")
    if case not in {item["file"] for item in CASE_META}:
        raise ValueError("未知场景：" + (case or "(空)"))
    if body.get("reset"):
        LEDGER_PATH.unlink(missing_ok=True)
    source = EXAMPLES_DIR / (case + ".input.json")
    run = json.loads(source.read_text(encoding="utf-8"))
    max_steps = body.get("max_steps")
    report, trace = run_pipeline(
        run,
        data_dir=DATA_DIR,
        ledger=LEDGER_PATH,
        max_steps=int(max_steps) if max_steps not in (None, "") else None,
        offline=bool(body.get("offline", True)),
        run_id=case,
    )
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

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/state":
            self._json(200, {"status": "ok", "state": snapshot()})
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