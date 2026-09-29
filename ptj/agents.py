"""Provider-neutral JSON stdin/stdout process bridge for model-backed roles."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path


class CommandAgent:
    """External processes may use different VLMs per role.

    A process receives one JSON object on stdin and must return one JSON object on
    stdout. Images are passed as original absolute paths; the process must load
    them as image inputs to its model rather than judging metadata alone.
    """

    def __init__(self, command: list[str], run_dir: str | Path, timeout_s: int = 120):
        if not isinstance(command, list) or not command or any(not isinstance(s, str) or not s for s in command):
            raise ValueError("agent command must be a nonempty string list")
        self.command = command
        self.run_dir = Path(run_dir).resolve()
        self.timeout_s = timeout_s

    def call(self, role: str, payload: dict) -> dict:
        data = {"role": role, "run_dir": str(self.run_dir), **payload}
        completed = subprocess.run(
            self.command,
            input=json.dumps(data, ensure_ascii=False),
            text=True,
            capture_output=True,
            timeout=self.timeout_s,
            check=False,
            shell=False,
        )
        if completed.returncode:
            raise RuntimeError(f"{role} process exited {completed.returncode}: {completed.stderr[-1000:]}")
        try:
            result = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{role} process did not return JSON") from exc
        if not isinstance(result, dict):
            raise ValueError(f"{role} process must return a JSON object")
        return result


class CommandChecker:
    def __init__(self, agent: CommandAgent):
        self.agent = agent

    def check(self, question: dict, frames: list[dict], actions: list[dict]) -> dict:
        return self.agent.call("evidence_checker", {
            "question": question,
            "frames": _absolute_frames(frames, self.agent.run_dir),
            "actual_actions": actions,
        })


class CommandController:
    def __init__(self, agent: CommandAgent):
        self.agent = agent

    def decide(self, view: dict) -> dict:
        return self.agent.call("controller", {
            **view,
            "latest_frame": _absolute_frames([view["latest_frame"]], self.agent.run_dir)[0],
        })


class CommandLocator:
    def __init__(self, agent: CommandAgent):
        self.agent = agent

    def locate(self, question: dict, frames: list[dict], actions: list[dict]) -> list[str]:
        result = self.agent.call("event_locator", {
            "question": question,
            "frames": _absolute_frames(frames, self.agent.run_dir),
            "actual_actions": actions,
        })
        ids = result.get("frame_ids")
        if not isinstance(ids, list) or any(not isinstance(fid, str) for fid in ids):
            raise ValueError("locator must return frame_ids list")
        return ids


class CommandJudge:
    def __init__(self, agent: CommandAgent):
        self.agent = agent

    def judge(self, question: dict, evidence: dict) -> dict:
        return self.agent.call("rule_judge", {
            "question": question,
            "evidence": {**evidence, "frames": _absolute_frames(evidence["frames"], self.agent.run_dir)},
        })


def _absolute_frames(frames: list[dict], run_dir: Path) -> list[dict]:
    return [{**frame, "image_path": str((run_dir / frame["image_path"]).resolve())} for frame in frames]
