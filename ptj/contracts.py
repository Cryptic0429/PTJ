"""Frozen task definitions and record validation."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


OUTCOMES = {"pass", "violation", "undetermined", "untriggered"}
ADMISSION = {"formal", "qualification", "pilot"}


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def task_digest(task: dict) -> str:
    return hashlib.sha256(canonical_json(task).encode("utf-8")).hexdigest()


def load_json(path: str | Path) -> dict:
    with Path(path).open(encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError("JSON root must be an object")
    return value


def save_json(path: str | Path, value: Any) -> None:
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read_jsonl(path: str | Path) -> list[dict]:
    result = []
    with Path(path).open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            if line.strip():
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError(f"{path}:{number}: expected object")
                result.append(value)
    return result


def append_jsonl(path: str | Path, value: dict) -> None:
    with Path(path).open("a", encoding="utf-8") as handle:
        handle.write(canonical_json(value) + "\n")


def _required_string(obj: dict, key: str) -> None:
    if not isinstance(obj.get(key), str) or not obj[key].strip():
        raise ValueError(f"{key} must be a nonempty string")


def validate_task(task: dict) -> dict:
    for field in ("case_id", "objective", "protocol"):
        _required_string(task, field)
    if task.get("admission") not in ADMISSION:
        raise ValueError(f"admission must be one of {sorted(ADMISSION)}")
    phases = task.get("phase_order")
    if not isinstance(phases, list) or not phases or any(not isinstance(p, str) or not p for p in phases):
        raise ValueError("phase_order must be a nonempty string list")
    if len(phases) != len(set(phases)):
        raise ValueError("phase_order contains duplicates")
    budget = task.get("budget")
    if not isinstance(budget, dict):
        raise ValueError("budget is required")
    for key in ("max_decisions", "max_control_ticks_per_decision", "base_sample_stride", "event_radius", "max_supplements"):
        value = budget.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < (0 if key == "max_supplements" else 1):
            raise ValueError(f"budget.{key} must be a valid integer")
    questions = task.get("questions")
    if not isinstance(questions, list) or not questions:
        raise ValueError("questions must be nonempty")
    ids = []
    for q in questions:
        if not isinstance(q, dict):
            raise ValueError("question must be an object")
        for key in ("id", "metric", "domain", "subdomain", "dimension", "premise", "evidence_required", "pass_rule", "violation_rule"):
            _required_string(q, key)
        if not isinstance(q.get("required"), bool) or not isinstance(q.get("applicable"), bool):
            raise ValueError(f"{q['id']}: required and applicable must be booleans")
        if q["dimension"] == "S4":
            raise ValueError(f"{q['id']}: S4 paired-run judgment is not implemented in PTJ v0.1")
        ids.append(q["id"])
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate question id")
    return task


def validate_frames(frames: list[dict], run_dir: Path) -> None:
    if not frames:
        raise ValueError("run has no frames")
    ids = set()
    previous_time = -1.0
    for frame in frames:
        frame_id = frame.get("frame_id")
        stamp = frame.get("video_time_s")
        image = frame.get("image_path")
        if not isinstance(frame_id, str) or not frame_id or frame_id in ids:
            raise ValueError("frame IDs must be unique nonempty strings")
        if not isinstance(stamp, (int, float)) or isinstance(stamp, bool) or stamp < previous_time:
            raise ValueError(f"{frame_id}: non-monotonic video timestamp")
        if not isinstance(image, str) or not image:
            raise ValueError(f"{frame_id}: missing original frame image path")
        resolved = (run_dir / image).resolve()
        if not resolved.is_relative_to(run_dir.resolve()) or not resolved.is_file():
            raise ValueError(f"{frame_id}: original frame image missing or outside run")
        ids.add(frame_id)
        previous_time = float(stamp)
