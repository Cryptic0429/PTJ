"""Command line interface for task validation and post-run evaluation."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .agents import CommandAgent, CommandChecker, CommandJudge, CommandLocator
from .contracts import load_json, task_digest, validate_task
from .judge import judge_run
from .ingest import register_video_run
from .trace import trace_run


def main() -> None:
    parser = argparse.ArgumentParser(prog="ptj")
    sub = parser.add_subparsers(dest="command", required=True)
    validate = sub.add_parser("validate-task", help="validate a frozen task and print its SHA-256")
    validate.add_argument("task")
    ingest = sub.add_parser("ingest", help="register an MP4 and original full-frame manifest")
    ingest.add_argument("task")
    ingest.add_argument("video")
    ingest.add_argument("manifest")
    ingest.add_argument("run_dir")
    evaluate = sub.add_parser("evaluate", help="trace original frames, judge q, and aggregate")
    evaluate.add_argument("task")
    evaluate.add_argument("run_dir")
    evaluate.add_argument("--agents", required=True, help="JSON file containing checker/judge commands")
    args = parser.parse_args()
    task = validate_task(load_json(args.task))
    if args.command == "validate-task":
        print(task_digest(task))
        return
    if args.command == "ingest":
        register_video_run(task, args.video, args.manifest, args.run_dir,
                           metadata={"frame_manifest": str(Path(args.manifest).resolve())})
        print(str(Path(args.run_dir).resolve()))
        return
    run_dir = Path(args.run_dir).resolve()
    settings = load_json(args.agents)
    timeout = settings.get("timeout_s", 120)
    checker = CommandChecker(CommandAgent(settings["checker_command"], run_dir, timeout))
    judge = CommandJudge(CommandAgent(settings["judge_command"], run_dir, timeout))
    locator = CommandLocator(CommandAgent(settings["locator_command"], run_dir, timeout)) if settings.get("locator_command") else None
    trace_run(task, run_dir, checker, locator)
    result = judge_run(task, run_dir, judge)
    print(json.dumps(result["summary"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
