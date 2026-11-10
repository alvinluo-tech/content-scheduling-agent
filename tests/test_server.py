"""Web 演示层的接口测试：线程里起一个真服务器，走真实 HTTP。"""
from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from src import server


class ServerTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.original_ledger = server.LEDGER_PATH
        server.LEDGER_PATH = Path(cls.tmp.name) / "ledger.json"
        cls.httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.base = "http://127.0.0.1:%d" % cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=5)
        server.LEDGER_PATH = cls.original_ledger
        cls.tmp.cleanup()

    def setUp(self) -> None:
        server.LEDGER_PATH.unlink(missing_ok=True)

    def get(self, path: str):
        try:
            with urlopen(self.base + path, timeout=30) as resp:
                return resp.status, resp.read()
        except HTTPError as exc:
            return exc.code, exc.read()

    def post(self, path: str, payload):
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
        request = Request(self.base + path, data=data, headers={"Content-Type": "application/json"})
        try:
            with urlopen(request, timeout=60) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_state_endpoint_describes_the_workbench(self) -> None:
        status, body = self.get("/api/state")
        self.assertEqual(status, 200)
        state = json.loads(body.decode("utf-8"))["state"]
        self.assertEqual(state["date"], "2026-11-12")
        self.assertEqual(state["dates"], ["2026-11-12", "2026-11-13"])
        self.assertEqual(sorted(state["days"].keys()), state["dates"])
        self.assertEqual([slot["time"] for slot in state["slots"]], ["10:00", "18:00", "18:30", "20:00"])
        self.assertEqual([slot["time"] for slot in state["days"]["2026-11-13"]], ["09:00", "14:00"])
        self.assertEqual(state["hours"]["start"], "09:00")
        self.assertEqual(len(state["rules"]), 5)
        self.assertEqual(len(state["cases"]), 7)
        self.assertEqual(state["drafts"], {})

    def test_index_is_served(self) -> None:
        status, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("排期台", body.decode("utf-8"))

    def test_static_css_is_served_with_right_content_type(self) -> None:
        status, _ = self.get("/app.css")
        self.assertEqual(status, 200)

    def test_path_traversal_is_refused(self) -> None:
        status, body = self.get("/../src/main.py")
        self.assertEqual(status, 404)
        self.assertIn("error", json.loads(body.decode("utf-8")))

    def test_unknown_api_is_404(self) -> None:
        status, _ = self.get("/api/nope")
        self.assertEqual(status, 404)

    def test_run_conflict_case_saves_draft_without_publishing(self) -> None:
        status, body = self.post("/api/run", {"case": "02-conflict", "offline": True, "reset": True})
        self.assertEqual(status, 200)
        report = body["report"]
        self.assertEqual(report["outcome"], "pending_confirmation")
        self.assertFalse(report["published"])
        material = report["materials"][0]
        self.assertEqual(material["suggested_time"], "19:00")
        self.assertIsNotNone(material["draft"])
        self.assertTrue(server.LEDGER_PATH.exists())
        steps = body["trace"]["steps"]
        self.assertEqual(len(steps), 5)
        self.assertEqual(steps[0]["action"]["tool"], "read_material")
        self.assertEqual(steps[-1]["action"]["tool"], "save_draft")
        for step in steps:
            self.assertTrue(step["rationale"])
            self.assertIn("ok", step["observation"])

    def stream(self, path: str):
        """把 SSE 响应读成 [(event, payload)]，读完即连接关闭。"""
        events = []
        with urlopen(self.base + path, timeout=60) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("text/event-stream", resp.headers.get("Content-Type", ""))
            name, payload = None, None
            for raw in resp:
                line = raw.decode("utf-8").rstrip("\r\n")
                if line.startswith("event: "):
                    name = line[len("event: "):]
                elif line.startswith("data: "):
                    payload = json.loads(line[len("data: "):])
                elif line == "" and name:
                    events.append((name, payload))
                    name, payload = None, None
        return events

    def test_stream_pushes_steps_before_the_final_report(self) -> None:
        events = self.stream("/api/stream?case=02-conflict&offline=1&reset=1")
        names = [name for name, _ in events]
        self.assertEqual(names[0], "meta")
        self.assertEqual(names[-1], "report")
        snapshots = [payload for name, payload in events if name == "trace"]
        self.assertGreaterEqual(len(snapshots), 3)
        self.assertEqual(len(snapshots[0]["steps"]), 1)
        self.assertGreater(snapshots[-1]["steps"][-1]["step"], 1)
        report_event = events[-1][1]
        self.assertEqual(report_event["report"]["outcome"], "pending_confirmation")
        self.assertFalse(report_event["report"]["published"])
        self.assertIn("drafts", report_event["state"])

    def test_stream_rejects_unknown_case_before_opening_stream(self) -> None:
        status, body = self.get("/api/stream?case=99-nope")
        self.assertEqual(status, 400)
        self.assertIn("未知场景", json.loads(body.decode("utf-8"))["error"])

    def test_unknown_case_is_rejected(self) -> None:
        status, body = self.post("/api/run", {"case": "99-nope"})
        self.assertEqual(status, 400)
        self.assertIn("未知场景", body["error"])

    def test_broken_json_is_rejected(self) -> None:
        status, body = self.post("/api/run", b"not-json")
        self.assertEqual(status, 400)
        self.assertIn("JSON", body["error"])

    def test_reset_clears_ledger(self) -> None:
        self.post("/api/run", {"case": "02-conflict", "offline": True, "reset": True})
        self.assertTrue(server.LEDGER_PATH.exists())
        status, body = self.post("/api/reset", {})
        self.assertEqual(status, 200)
        self.assertFalse(server.LEDGER_PATH.exists())
        self.assertEqual(body["state"]["drafts"], {})

    def test_repeated_submission_keeps_one_draft(self) -> None:
        self.post("/api/run", {"case": "02-conflict", "offline": True, "reset": True})
        status, body = self.post("/api/run", {"case": "05-dedup", "offline": True, "reset": False})
        self.assertEqual(status, 200)
        ledger = json.loads(server.LEDGER_PATH.read_text(encoding="utf-8"))
        self.assertEqual(len(ledger["drafts"]), 1)
        draft = list(ledger["drafts"].values())[0]
        self.assertEqual(draft["submit_count"], 2)
        self.assertFalse(body["report"]["published"])

    def test_report_never_claims_publication(self) -> None:
        for case in ("01-normal", "03-rules-and-missing", "04-save-failure", "06-step-limit"):
            status, body = self.post("/api/run", {"case": case, "offline": True, "reset": True})
            self.assertEqual(status, 200, case)
            self.assertFalse(body["report"]["published"], case)
            for material in body["report"]["materials"]:
                self.assertFalse(material.get("published", False), case)

    def test_human_approval_turns_pending_draft_into_ready(self) -> None:
        status, body = self.post("/api/run", {"case": "02-conflict", "offline": True, "reset": True})
        self.assertEqual(status, 200)
        material = body["report"]["materials"][0]
        self.assertEqual(body["report"]["outcome"], "pending_confirmation")
        confirmation_id = material["pending_confirmations"][0]["confirmation_id"]

        status, confirmed = self.post("/api/confirm", {"confirmation_id": confirmation_id})
        self.assertEqual(status, 200)
        self.assertEqual(confirmed["confirmation"]["status"], "approved")

        status, again = self.post("/api/run", {"case": "02-conflict", "offline": True, "reset": False})
        self.assertEqual(status, 200)
        report = again["report"]
        after = report["materials"][0]
        self.assertEqual(report["outcome"], "scheduled")
        self.assertFalse(after["requires_user_confirmation"])
        self.assertEqual(after["draft"]["status"], "draft_ready")
        self.assertFalse(report["published"])

    def test_markdown_export_needs_a_run_then_serves_the_last_one(self) -> None:
        with server.REPORT_LOCK:
            server.LAST_REPORT.clear()
        status, body = self.get("/api/report.md")
        self.assertEqual(status, 404)
        self.assertIn("还没有运行记录", json.loads(body.decode("utf-8"))["error"])

        status, _ = self.post("/api/run", {"case": "02-conflict", "offline": True, "reset": True})
        self.assertEqual(status, 200)
        status, body = self.get("/api/report.md")
        self.assertEqual(status, 200)
        text = body.decode("utf-8")
        self.assertIn("# 排期报告", text)
        self.assertIn("## 决策记录", text)
        self.assertIn("已发布：否", text)
        self.assertIn("待用户确认", text)

    def test_unknown_confirmation_is_rejected(self) -> None:
        status, body = self.post("/api/confirm", {"confirmation_id": "C-NOPE"})
        self.assertEqual(status, 400)
        self.assertIn("没有待确认项", body["error"])


if __name__ == "__main__":
    unittest.main()
