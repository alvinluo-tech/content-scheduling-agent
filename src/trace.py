from __future__ import annotations

from typing import Any


class TraceRecorder:
    """记录每一步的动作、观察与后续动作；next_action 由下一步反填，保证链路闭合。"""

    def __init__(self, run_id: str, mode: str, model_name: str) -> None:
        self.run_id = run_id
        self.mode = mode
        self.model_name = model_name
        self.steps: list[dict[str, Any]] = []
        self.final_message: str | None = None
        self.terminal = "running"

    def record(self, step: int, tool: str, args: dict[str, Any], result: dict[str, Any], summary: str) -> None:
        if self.steps:
            self.steps[-1]["next_action"] = f"调用 {tool}"
        self.steps.append(
            {
                "step": step,
                "rationale": args.get("reason", ""),
                "action": {"tool": tool, "args": args},
                "observation": {"ok": bool(result.get("ok")), "summary": summary},
            }
        )

    def close(self, terminal: str, final_message: str | None = None) -> None:
        self.terminal = terminal
        if self.steps:
            self.steps[-1]["next_action"] = "结束并输出排期结果"
        self.final_message = final_message

    def as_dict(self, limits: dict[str, Any]) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "mode": self.mode,
            "model": self.model_name,
            "terminal": self.terminal,
            "limits": limits,
            "steps": self.steps,
            "final_message": self.final_message,
        }
