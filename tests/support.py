from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any, Iterator
from contextlib import contextmanager

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
EXAMPLES_DIR = REPO_ROOT / "examples"


@contextmanager
def temp_ledger() -> Iterator[Path]:
    """每个用例用独立账本，避免测试之间互相污染 data/drafts.json。"""
    with tempfile.TemporaryDirectory() as tmp:
        yield Path(tmp) / "drafts.json"


def load_example(name: str) -> dict[str, Any]:
    return json.loads((EXAMPLES_DIR / f"{name}.input.json").read_text(encoding="utf-8"))


def make_run(
    *material_ids: str,
    requested_time: str = "18:00",
    requested_date: str | None = None,
    requested_channel: str | None = None,
    time_hard: bool = False,
    save_failures: int = 0,
    now: str = "2026-11-12T09:00:00+08:00",
) -> dict[str, Any]:
    """构造最小运行输入；只用本次请求里的素材，符合"只处理待处理素材"的边界。"""
    requests = []
    for mid in material_ids:
        request: dict[str, Any] = {"material_id": mid, "requested_time": requested_time, "time_hard": time_hard}
        if requested_date:
            request["requested_date"] = requested_date
        if requested_channel:
            request["requested_channel"] = requested_channel
        requests.append(request)
    return {
        "run_id": "unit-test",
        "now": now,
        "requests": requests,
        "simulate": {"save_failures": save_failures},
    }


def make_args(input_path: Path, ledger: Path, offline: bool = True, max_steps: int | None = None) -> Any:
    import argparse

    return argparse.Namespace(
        input=str(input_path),
        data_dir=str(DATA_DIR),
        ledger=str(ledger),
        out=None,
        trace=None,
        max_steps=max_steps,
        offline=offline,
    )
