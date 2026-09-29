"""Register an existing video and its unmodified full-frame extracts for offline PTJ."""

from __future__ import annotations

import shutil
from pathlib import Path

from .contracts import append_jsonl, load_json, save_json, task_digest, validate_frames, validate_task


def register_video_run(task: dict, video: str | Path, manifest: str | Path, run_dir: str | Path, *, metadata: dict) -> None:
    task = validate_task(task)
    video = Path(video).resolve()
    manifest = Path(manifest).resolve()
    run_dir = Path(run_dir).resolve()
    if not video.is_file() or video.suffix.lower() != ".mp4":
        raise ValueError("an original MP4 video is required")
    entries = load_json(manifest).get("frames")
    if not isinstance(entries, list) or not entries:
        raise ValueError("manifest.frames must be a nonempty list")
    validated = []
    last_stamp = -1.0
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("source_image"), str):
            raise ValueError("each frame requires source_image")
        source = (manifest.parent / entry["source_image"]).resolve()
        if not source.is_file() or source.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp", ".ppm"}:
            raise ValueError(f"frame source is missing or unsupported: {source}")
        stamp = entry.get("video_time_s")
        if not isinstance(stamp, (int, float)) or isinstance(stamp, bool) or stamp < last_stamp:
            raise ValueError("frame manifest has invalid or non-monotonic video time")
        phase = entry.get("phase", task["phase_order"][0])
        if phase not in task["phase_order"]:
            raise ValueError(f"unknown phase: {phase}")
        validated.append((source, float(stamp), phase))
        last_stamp = float(stamp)
    if run_dir.exists() and any(run_dir.iterdir()):
        raise FileExistsError(f"run directory is not empty: {run_dir}")
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "frames").mkdir()
    shutil.copy2(video, run_dir / "video.mp4")
    save_json(run_dir / "config.json", {"schema_version": "0.1", "task": task,
              "task_sha256": task_digest(task), "metadata": {**metadata, "source": "registered_video"}})
    frames = []
    for i, (source, stamp, phase) in enumerate(validated):
        name = f"f{i:06d}{source.suffix.lower()}"
        shutil.copy2(source, run_dir / "frames" / name)
        frames.append({"frame_id": f"f{i:06d}", "video_time_s": stamp,
                       "image_path": f"frames/{name}", "after_action_id": None, "phase": phase})
    validate_frames(frames, run_dir)
    for frame in frames:
        append_jsonl(run_dir / "frames.jsonl", frame)
    (run_dir / "actions.jsonl").touch()
    (run_dir / "event_hints.jsonl").touch()
