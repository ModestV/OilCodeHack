from __future__ import annotations

from typing import Any

from .common import clone_state


class Blackboard:
    def __init__(self, process_state: dict[str, Any]):
        self.state = clone_state(process_state)
        self.trace: list[dict[str, Any]] = []
        self.state.setdefault("agent_results", {})

    def add_agent_result(self, key: str, result: dict[str, Any]) -> None:
        self.state["agent_results"][key] = result["output"]
        self.trace.append(result)

    def add_step(self, title: str, payload: dict[str, Any]) -> None:
        self.trace.append(
            {
                "agent": "orchestrator",
                "title": title,
                "status": "completed",
                "input_summary": {},
                "analysis_steps": [payload.get("reason", "step completed")],
                "output": payload,
                "warnings": [],
            }
        )

    def snapshot(self) -> dict[str, Any]:
        return clone_state(self.state)
