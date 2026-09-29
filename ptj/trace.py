"""Deterministic frame indexing and bounded evidence review."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from .contracts import load_json, read_jsonl, save_json, task_digest, validate_frames, validate_task


class EvidenceChecker(Protocol):
    def check(self, question: dict, frames: list[dict], actions: list[dict]) -> dict: ...


class EventLocator(Protocol):
    def locate(self, question: dict, frames: list[dict], actions: list[dict]) -> list[str]: ...


def _load_run(task: dict, run_dir: Path) -> tuple[list[dict], list[dict], list[dict]]:
    config = load_json(run_dir / "config.json")
    if config.get("task_sha256") != task_digest(task):
        raise ValueError("run was created with a different frozen task")
    video = run_dir / "video.mp4"
    if not video.is_file() or video.stat().st_size == 0:
        raise ValueError("run is missing its original video.mp4")
    frames = read_jsonl(run_dir / "frames.jsonl")
    validate_frames(frames, run_dir)
    actions = read_jsonl(run_dir / "actions.jsonl")
    hints_path = run_dir / "event_hints.jsonl"
    hints = read_jsonl(hints_path) if hints_path.exists() else []
    return frames, actions, hints


def _initial_indices(task: dict, frames: list[dict], hints: list[dict]) -> set[int]:
    budget = task["budget"]
    indices = {0, len(frames) - 1}
    indices.update(range(0, len(frames), budget["base_sample_stride"]))
    by_id = {f["frame_id"]: i for i, f in enumerate(frames)}
    for i in range(1, len(frames)):
        if frames[i]["phase"] != frames[i - 1]["phase"]:
            indices.update((i - 1, i))
    for hint in hints:
        anchor = by_id.get(hint.get("frame_id"))
        if anchor is None:
            raise ValueError("event hint references an unknown frame")
        radius = budget["event_radius"]
        indices.update(range(max(0, anchor - radius), min(len(frames), anchor + radius + 1)))
    return indices


def trace_run(task: dict, run_dir: str | Path, checker: EvidenceChecker, locator: EventLocator | None = None) -> dict:
    task = validate_task(task)
    run_dir = Path(run_dir).resolve()
    frames, actions, hints = _load_run(task, run_dir)
    by_id = {f["frame_id"]: i for i, f in enumerate(frames)}
    initial = _initial_indices(task, frames, hints)
    evidence: dict = {"task_sha256": task_digest(task), "questions": {}}
    for q in task["questions"]:
        if not q["applicable"]:
            evidence["questions"][q["id"]] = {"status": "N/A", "reason": "preconfigured in frozen task"}
            continue
        indices = set(initial)
        review = None
        attempts = 0
        while True:
            selected = [frames[i] for i in sorted(indices)]
            review = checker.check(q, selected, actions)
            if not isinstance(review, dict) or not isinstance(review.get("sufficient"), bool):
                raise ValueError(f"{q['id']}: checker must return sufficient boolean")
            if review["sufficient"] or attempts >= task["budget"]["max_supplements"]:
                break
            requested = review.get("supplement_frame_ids", [])
            if not isinstance(requested, list):
                raise ValueError("supplement_frame_ids must be a list")
            if locator is not None:
                requested.extend(locator.locate(q, frames, actions))
            if any(frame_id not in by_id for frame_id in requested):
                raise ValueError("checker or locator returned an unknown frame ID")
            before = len(indices)
            indices.update(by_id[frame_id] for frame_id in requested)
            # Bounded full-timeline fallback catches events omitted by control hints.
            if len(indices) == before:
                indices.update(range(len(frames)))
            attempts += 1
        selected = [frames[i] for i in sorted(indices)]
        evidence["questions"][q["id"]] = {
            "status": "reviewed",
            "frames": selected,
            "checker": {
                "sufficient": review["sufficient"],
                "missing_evidence": review.get("missing_evidence", []),
                "notes": review.get("notes", ""),
            },
            "full_timeline_covered": len(indices) == len(frames),
            "supplement_rounds": attempts,
        }
    save_json(run_dir / "evidence.json", evidence)
    return evidence
