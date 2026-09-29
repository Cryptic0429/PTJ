"""Closed-loop probe with a persistent adapter session and restricted controller view."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from .contracts import append_jsonl, save_json, task_digest, validate_frames, validate_task


class Controller(Protocol):
    def decide(self, view: dict) -> dict: ...


class WorldAdapter(Protocol):
    """One instance represents one persistent world-model session.

    reset/step return {image_path, video_time_s}. The image must be the unaltered
    full frame, written inside run_dir. step additionally returns actual_actions.
    finalize writes the original complete recording to video.mp4 and releases input.
    """

    allowed_actions: set[str]

    def reset(self, run_dir: Path) -> dict: ...
    def step(self, actions: list[str], control_ticks: int) -> dict: ...
    def finalize(self, video_path: Path) -> None: ...


def run_probe(task: dict, controller: Controller, adapter: WorldAdapter, run_dir: str | Path, *, metadata: dict) -> dict:
    task = validate_task(task)
    run_dir = Path(run_dir).resolve()
    if run_dir.exists() and any(run_dir.iterdir()):
        raise FileExistsError(f"run directory is not empty: {run_dir}")
    run_dir.mkdir(parents=True, exist_ok=True)
    save_json(run_dir / "config.json", {"schema_version": "0.1", "task": task, "task_sha256": task_digest(task), "metadata": metadata})
    actions_file = run_dir / "actions.jsonl"
    frames_file = run_dir / "frames.jsonl"
    hints_file = run_dir / "event_hints.jsonl"
    for path in (actions_file, frames_file, hints_file):
        path.touch(exist_ok=True)
    frames: list[dict] = []
    actual_history: list[dict] = []
    phases = task["phase_order"]
    phase_index = 0
    finished = False
    failure: BaseException | None = None

    def record_frame(observation: dict, action_id: str | None) -> dict:
        frame = {
            "frame_id": f"f{len(frames):06d}",
            "video_time_s": observation["video_time_s"],
            "image_path": observation["image_path"],
            "after_action_id": action_id,
            "phase": phases[phase_index],
        }
        frames.append(frame)
        validate_frames(frames, run_dir)
        append_jsonl(frames_file, frame)
        return frame

    try:
        current = record_frame(adapter.reset(run_dir), None)
        for decision_index in range(task["budget"]["max_decisions"]):
            # No reference future, question verdict, score or evidence record enters this view.
            view = {
                "objective": task["objective"],
                "phase": phases[phase_index],
                "phase_order": phases,
                "remaining_decisions": task["budget"]["max_decisions"] - decision_index,
                "latest_frame": current,
                "actual_action_history": actual_history.copy(),
                "allowed_actions": sorted(adapter.allowed_actions),
            }
            decision = controller.decide(view)
            if not isinstance(decision, dict) or decision.get("phase") != phases[phase_index]:
                raise ValueError("controller returned invalid phase")
            command = decision.get("command")
            if command not in {"HOLD", "WAIT", "FINISH"}:
                raise ValueError("command must be HOLD, WAIT or FINISH")
            requested = decision.get("actions", [])
            if not isinstance(requested, list) or any(not isinstance(a, str) for a in requested):
                raise ValueError("actions must be a string list")
            if command == "HOLD" and (not requested or set(requested) - adapter.allowed_actions):
                raise ValueError("unverified or empty HOLD action")
            if command != "HOLD" and requested:
                raise ValueError("WAIT and FINISH cannot request actions")
            ticks = decision.get("control_ticks", 1)
            if not isinstance(ticks, int) or isinstance(ticks, bool) or not 1 <= ticks <= task["budget"]["max_control_ticks_per_decision"]:
                raise ValueError("invalid control_ticks")
            if command == "FINISH":
                finished = True
                break
            action_id = f"a{decision_index:06d}"
            observation = adapter.step(requested if command == "HOLD" else [], ticks)
            actual = observation.get("actual_actions")
            if not isinstance(actual, list) or any(not isinstance(a, str) for a in actual):
                raise ValueError("adapter must report actual_actions")
            action = {
                "action_id": action_id,
                "phase": phases[phase_index],
                "command": command,
                "requested_actions": requested,
                "actual_actions": actual,
                "control_ticks": ticks,
                "from_frame_id": current["frame_id"],
            }
            from_frame = current
            current = record_frame(observation, action_id)
            action["to_frame_id"] = current["frame_id"]
            append_jsonl(actions_file, action)
            actual_history.append(action)
            hints = decision.get("event_hints", [])
            if not isinstance(hints, list):
                raise ValueError("event_hints must be a list")
            for hint in hints:
                if not isinstance(hint, dict) or hint.get("status") not in {"observed", "expected", "unknown"}:
                    raise ValueError("invalid event hint")
                event_type = hint.get("event_type")
                if not isinstance(event_type, str) or not event_type:
                    raise ValueError("event_type required")
                append_jsonl(hints_file, {
                    "event_type": event_type,
                    "status": hint["status"],
                    "note": str(hint.get("note", "")),
                    "source": "controller",
                    "phase": action["phase"],
                    "action_id": action_id,
                    "from_frame_id": from_frame["frame_id"],
                    "to_frame_id": current["frame_id"],
                    "frame_id": from_frame["frame_id"] if hint["status"] == "observed" else current["frame_id"],
                    "video_time_s": from_frame["video_time_s"] if hint["status"] == "observed" else current["video_time_s"],
                })
            if decision.get("phase_complete", False):
                if phase_index + 1 < len(phases):
                    phase_index += 1
                else:
                    finished = True
                    break
    except BaseException as exc:
        failure = exc
    finally:
        try:
            adapter.finalize(run_dir / "video.mp4")
        except BaseException as exc:
            if failure is None:
                failure = exc
    status = {"probe_finished": finished, "decisions": len(actual_history), "failed": failure is not None}
    save_json(run_dir / "probe_status.json", status)
    if failure is not None:
        raise failure
    if not (run_dir / "video.mp4").is_file():
        raise ValueError("adapter did not save video.mp4")
    return status
