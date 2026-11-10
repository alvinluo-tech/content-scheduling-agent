from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def draft_key(date: str, material_id: str, requested_time: str) -> str:
    return f"{date}|{material_id}|{requested_time}"


class Store:
    """只负责读取合成资料与持久化草稿账本，不含任何业务判断。"""

    def __init__(self, data_dir: Path | str, ledger_path: Path | str | None = None) -> None:
        self.data_dir = Path(data_dir)
        self.ledger_path = Path(ledger_path) if ledger_path else self.data_dir / "drafts.json"
        self.materials: dict[str, dict[str, Any]] = read_json(self.data_dir / "materials.json")
        self.rules_doc: dict[str, Any] = read_json(self.data_dir / "rules.json")
        self.schedule_doc: dict[str, Any] = read_json(self.data_dir / "schedule.json")
        ledger: dict[str, Any] = read_json(self.ledger_path) if self.ledger_path.exists() else {}
        self.drafts: dict[str, dict[str, Any]] = ledger.get("drafts", {})
        self.confirmations: dict[str, dict[str, Any]] = ledger.get("confirmations", {})

    def material(self, material_id: str) -> dict[str, Any] | None:
        return self.materials.get(material_id)

    def rules(self) -> list[dict[str, Any]]:
        return self.rules_doc["rules"]

    def banned_terms(self) -> list[str]:
        return self.rules_doc["banned_terms"]

    def operating_hours(self) -> dict[str, Any]:
        return self.rules_doc["operating_hours"]

    def schedule_date(self) -> str:
        return self.schedule_doc["date"]

    def available_dates(self) -> list[str]:
        """可排日期来自 schedule.json 的 days（默认日期也必须在其中）。"""
        return sorted(self.schedule_doc.get("days", {}).keys())

    def occupied_slots(self, date: str | None = None) -> list[dict[str, Any]]:
        target = date or self.schedule_date()
        return list(self.schedule_doc.get("days", {}).get(target, []))

    def draft(self, date: str, material_id: str, requested_time: str) -> dict[str, Any] | None:
        return self.drafts.get(draft_key(date, material_id, requested_time))

    def drafted_slots(self, date: str, exclude_material: str | None = None) -> list[dict[str, Any]]:
        """草稿同样占位，避免同一时段排进两条素材；素材自己的草稿不算冲突（用于幂等重提）。"""
        return [
            {"time": draft["scheduled_time"], "material_id": draft["material_id"], "source": "draft"}
            for draft in self.drafts.values()
            if draft["date"] == date and draft["material_id"] != exclude_material
        ]

    def upsert_draft(self, record: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        """去重键用“用户要求”而不是“建议结果”：换了替代时段重提仍是同一份草稿。"""
        key = draft_key(record["requested_date"], record["material_id"], record["requested_time"])
        existing = self.drafts.get(key)
        if existing is None:
            self.drafts[key] = record
            self.commit()
            return record, True
        existing["date"] = record["date"]
        existing["scheduled_time"] = record["scheduled_time"]
        existing["status"] = record["status"]
        existing["time_changed"] = record["time_changed"]
        existing["date_changed"] = record["date_changed"]
        existing["updated_at"] = record["updated_at"]
        existing["submit_count"] = existing.get("submit_count", 1) + 1
        self.commit()
        return existing, False

    def add_confirmation(self, record: dict[str, Any]) -> dict[str, Any]:
        self.confirmations[record["confirmation_id"]] = record
        self.commit()
        return record

    def approve_confirmation(self, confirmation_id: str, actor: str = "human") -> dict[str, Any]:
        """人工确认通道：只有人能调用，模型工具集里没有这个动作。"""
        record = self.confirmations.get(confirmation_id)
        if record is None:
            raise ValueError(f"没有待确认项 {confirmation_id}")
        if record.get("status") == "approved":
            return record
        record["status"] = "approved"
        record["approved_by"] = actor
        record["approved_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        self.commit()
        return record

    def confirmations_for(self, requested_date: str, material_id: str, requested_time: str) -> list[dict[str, Any]]:
        """按“用户原本要求的那一天 + 那个时间”找确认项；提议日期在 proposed_date 上。"""
        return [
            c for c in self.confirmations.values()
            if c.get("requested_date", c.get("date")) == requested_date
            and c["material_id"] == material_id
            and c["requested_time"] == requested_time
        ]

    def commit(self) -> None:
        atomic_write_json(self.ledger_path, {"drafts": self.drafts, "confirmations": self.confirmations})
