from __future__ import annotations

import contextlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Iterator

if os.name == "nt":  # pragma: no cover - 平台分支，CI 上只跑其中一支
    import msvcrt
else:  # pragma: no cover
    import fcntl


class LedgerBusy(RuntimeError):
    """另一个进程正持有账本锁，等到超时仍拿不到。宁可报忙，也不要覆盖别人的写入。"""


@contextlib.contextmanager
def locked_file(path: Path, timeout: float = 10.0) -> Iterator[None]:
    """跨进程互斥：账本是"读-改-写"，只有原子替换挡不住两个进程同时写丢更新。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+b") as fh:
        fh.seek(0, os.SEEK_END)
        if fh.tell() == 0:
            fh.write(b"\0")
            fh.flush()
        deadline = time.monotonic() + timeout
        while True:
            try:
                fh.seek(0)
                if os.name == "nt":
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise LedgerBusy(f"账本 {path.name} 被其它进程占用，等待 {timeout:.0f} 秒后放弃") from None
                time.sleep(0.02)
        try:
            yield
        finally:
            fh.seek(0)
            if os.name == "nt":
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


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


def draft_key(channel: str, date: str, material_id: str, requested_time: str) -> str:
    """频道是身份的一部分：同一素材在短片频道和主频道是两份不同的草稿。"""
    return f"{channel}|{date}|{material_id}|{requested_time}"


class Store:
    """只负责读取合成资料与持久化草稿账本，不含任何业务判断。"""

    def __init__(self, data_dir: Path | str, ledger_path: Path | str | None = None) -> None:
        self.data_dir = Path(data_dir)
        self.ledger_path = Path(ledger_path) if ledger_path else self.data_dir / "drafts.json"
        self.materials: dict[str, dict[str, Any]] = read_json(self.data_dir / "materials.json")
        self.rules_doc: dict[str, Any] = read_json(self.data_dir / "rules.json")
        self.schedule_doc: dict[str, Any] = read_json(self.data_dir / "schedule.json")
        self.drafts: dict[str, dict[str, Any]] = {}
        self.confirmations: dict[str, dict[str, Any]] = {}
        self._lock_depth = 0
        self._reload()

    @property
    def lock_path(self) -> Path:
        return self.ledger_path.with_name(self.ledger_path.name + ".lock")

    def _reload(self) -> None:
        ledger: dict[str, Any] = read_json(self.ledger_path) if self.ledger_path.exists() else {}
        self.drafts = ledger.get("drafts", {})
        self.confirmations = ledger.get("confirmations", {})

    @contextlib.contextmanager
    def transaction(self) -> Iterator["Store"]:
        """写入前先拿锁、重读磁盘，再改再写：另一个进程这期间的写入不会被覆盖。

        可重入：replan_confirmation 会先否决再登记新方案，只在最外层进出一次锁。
        """
        if self._lock_depth:
            yield self
            return
        with locked_file(self.lock_path):
            self._lock_depth = 1
            try:
                self._reload()
                yield self
            finally:
                self._lock_depth = 0

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

    def default_channel(self) -> str:
        return self.schedule_doc.get("channel", "main")

    def channels(self) -> list[str]:
        """主频道永远在第一位，其余按名字排序。"""
        extra = sorted(self.schedule_doc.get("channels", {}).keys())
        return [self.default_channel()] + [name for name in extra if name != self.default_channel()]

    def channel_label(self, channel: str | None = None) -> str:
        name = channel or self.default_channel()
        if name == self.default_channel():
            return self.schedule_doc.get("channel_label", name)
        return self.schedule_doc.get("channels", {}).get(name, {}).get("label", name)

    def channel_catalog(self) -> list[dict[str, Any]]:
        return [
            {"name": name, "label": self.channel_label(name), "default": name == self.default_channel()}
            for name in self.channels()
        ]

    def has_channel(self, channel: str) -> bool:
        return channel in self.channels()

    def channel_days(self, channel: str | None = None) -> dict[str, list[dict[str, Any]]]:
        name = channel or self.default_channel()
        if name == self.default_channel():
            return self.schedule_doc.get("days", {})
        return self.schedule_doc.get("channels", {}).get(name, {}).get("days", {})

    def available_dates(self, channel: str | None = None) -> list[str]:
        """可排日期来自该频道的 days（主频道的默认日期必须在其中）。"""
        return sorted(self.channel_days(channel).keys())

    def occupied_slots(self, date: str | None = None, channel: str | None = None) -> list[dict[str, Any]]:
        target = date or self.schedule_date()
        return list(self.channel_days(channel).get(target, []))

    def draft(
        self, date: str, material_id: str, requested_time: str, channel: str | None = None
    ) -> dict[str, Any] | None:
        return self.drafts.get(draft_key(self._channel(channel), date, material_id, requested_time))

    def _channel(self, channel: str | None) -> str:
        return channel or self.default_channel()

    def drafted_slots(
        self, date: str, exclude_material: str | None = None, channel: str | None = None
    ) -> list[dict[str, Any]]:
        """草稿同样占位，避免同一时段排进两条素材；素材自己的草稿不算冲突（用于幂等重提）。"""
        name = self._channel(channel)
        return [
            {"time": draft["scheduled_time"], "material_id": draft["material_id"], "source": "draft"}
            for draft in self.drafts.values()
            if draft["date"] == date
            and draft.get("channel", self.default_channel()) == name
            and draft["material_id"] != exclude_material
            and draft.get("status") != "draft_rejected"  # 被否决的草稿已作废，不该继续占位
        ]

    def upsert_draft(self, record: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        """去重键用“用户要求”而不是“建议结果”：换了替代时段重提仍是同一份草稿。"""
        key = draft_key(
            self._channel(record.get("channel")),
            record["requested_date"],
            record["material_id"],
            record["requested_time"],
        )
        with self.transaction():
            existing = self.drafts.get(key)
            if existing is None:
                self.drafts[key] = record
                self.commit()
                return record, True
            existing["date"] = record["date"]
            existing["channel"] = record.get("channel", self.default_channel())
            existing["scheduled_time"] = record["scheduled_time"]
            existing["status"] = record["status"]
            existing["time_changed"] = record["time_changed"]
            existing["date_changed"] = record["date_changed"]
            existing["updated_at"] = record["updated_at"]
            existing["submit_count"] = existing.get("submit_count", 1) + 1
            self.commit()
            return existing, False

    def add_confirmation(self, record: dict[str, Any]) -> dict[str, Any]:
        with self.transaction():
            self.confirmations[record["confirmation_id"]] = record
            self.commit()
            return record

    def approve_confirmation(self, confirmation_id: str, actor: str = "human") -> dict[str, Any]:
        """人工确认通道：只有人能调用，模型工具集里没有这个动作。"""
        with self.transaction():
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

    def reject_confirmation(self, confirmation_id: str, actor: str = "human", note: str = "") -> dict[str, Any]:
        """人工否决：同样只有人能调用。被否决的替代方案连同它那份待确认草稿一起作废。"""
        with self.transaction():
            record = self.confirmations.get(confirmation_id)
            if record is None:
                raise ValueError(f"没有待确认项 {confirmation_id}")
            if record.get("status") == "rejected":
                return record
            record["status"] = "rejected"
            record["rejected_by"] = actor
            record["rejected_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            if note:
                record["reject_note"] = note
            for draft in self.drafts.values():
                if (
                    draft["material_id"] == record["material_id"]
                    and draft.get("channel", self.default_channel()) == record.get("channel", self.default_channel())
                    and draft["status"] == "draft_pending_confirmation"
                    and draft["date"] == record.get("proposed_date", record.get("date"))
                    and draft["scheduled_time"] == record["proposed_time"]
                ):
                    draft["status"] = "draft_rejected"
            self.commit()
            return record

    def replan_confirmation(
        self,
        confirmation_id: str,
        time_str: str,
        date: str | None = None,
        actor: str = "human",
        note: str = "",
    ) -> dict[str, Any]:
        """人工改选：否决模型提的时段，同时把自己选的那个时段记为 approved。

        否决与否决是同一件事——模型那版方案作废；区别只在于人给了新的落点，
        所以额外登记一条 approved 记录，模型重跑时 save_draft 才能落到 draft_ready。
        """
        with self.transaction():
            return self._replan_locked(confirmation_id, time_str, date, actor, note)

    def _replan_locked(
        self, confirmation_id: str, time_str: str, date: str | None, actor: str, note: str
    ) -> dict[str, Any]:
        record = self.confirmations.get(confirmation_id)
        if record is None:
            raise ValueError(f"没有待确认项 {confirmation_id}")
        self.reject_confirmation(confirmation_id, actor, note or "用户改选了其它时段")
        record = self.confirmations[confirmation_id]
        counter_date = date or record.get("proposed_date") or record.get("date")
        counter_id = f"C-{record['material_id']}-{counter_date}-{time_str.replace(':', '')}"
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
        counter = self.confirmations.get(counter_id)
        if counter is None:
            counter = {
                "confirmation_id": counter_id,
                "date": counter_date,
                "channel": record.get("channel", self.default_channel()),
                "requested_date": record.get("requested_date", record.get("date")),
                "material_id": record["material_id"],
                "requested_time": record["requested_time"],
                "proposed_date": counter_date,
                "proposed_time": time_str,
                "question": f"用户改选为 {counter_date} {time_str}",
            }
            self.confirmations[counter_id] = counter
        counter.update(
            {
                "status": "approved",
                "decided_by": actor,
                "decided_at": stamp,
                "replanned_from": confirmation_id,
            }
        )
        self.commit()
        return counter

    def confirmations_for(
        self, requested_date: str, material_id: str, requested_time: str, channel: str | None = None
    ) -> list[dict[str, Any]]:
        """按“频道 + 用户原本要求的那一天 + 那个时间”找确认项；提议日期在 proposed_date 上。"""
        name = self._channel(channel)
        return [
            c for c in self.confirmations.values()
            if c.get("requested_date", c.get("date")) == requested_date
            and c.get("channel", self.default_channel()) == name
            and c["material_id"] == material_id
            and c["requested_time"] == requested_time
        ]

    def commit(self) -> None:
        atomic_write_json(self.ledger_path, {"drafts": self.drafts, "confirmations": self.confirmations})
