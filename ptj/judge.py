"""Per-question verdict validation and deterministic aggregation."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from .contracts import OUTCOMES, load_json, save_json, task_digest, validate_task


class RuleJudge(Protocol):
    def judge(self, question: dict, evidence: dict) -> dict: ...


def _validate_verdict(q: dict, entry: dict, result: dict) -> dict:
    if not isinstance(result, dict) or result.get("verdict") not in OUTCOMES:
        raise ValueError(f"{q['id']}: invalid verdict")
    verdict = result["verdict"]
    opportunity = result.get("opportunity")
    if opportunity not in {"present", "absent", "unknown"}:
        raise ValueError(f"{q['id']}: invalid opportunity")
    frame_ids = result.get("evidence_frame_ids", [])
    allowed = {f["frame_id"] for f in entry["frames"]}
    if not isinstance(frame_ids, list) or any(fid not in allowed for fid in frame_ids):
        raise ValueError(f"{q['id']}: verdict cites a frame outside original evidence")
    sufficient = entry["checker"]["sufficient"]
    if verdict in {"pass", "violation"} and (not sufficient or opportunity != "present" or not frame_ids):
        raise ValueError(f"{q['id']}: decisive verdict lacks trigger, sufficient evidence or frame references")
    if verdict == "untriggered" and (not sufficient or opportunity != "absent" or not entry["full_timeline_covered"]):
        raise ValueError(f"{q['id']}: untriggered requires sufficient full-timeline evidence")
    if verdict == "undetermined" and not result.get("reason"):
        raise ValueError(f"{q['id']}: undetermined needs a reason")
    return {
        "question_id": q["id"],
        "verdict": verdict,
        "opportunity": opportunity,
        "evidence_frame_ids": frame_ids,
        "observations": result.get("observations", []),
        "reason": str(result.get("reason", "")),
    }


def aggregate(task: dict, verdicts: dict[str, dict]) -> dict:
    counts = {key: 0 for key in sorted(OUTCOMES)}
    counts["N/A"] = 0
    by_domain: dict[str, dict] = {}
    by_dimension: dict[str, dict] = {}
    for q in task["questions"]:
        verdict = verdicts[q["id"]]["verdict"]
        counts[verdict] += 1
        if verdict == "N/A":
            continue
        for groups, key in ((by_domain, q["domain"]), (by_dimension, q["dimension"])):
            bucket = groups.setdefault(key, {"pass": 0, "violation": 0, "undetermined": 0, "untriggered": 0})
            bucket[verdict] += 1
    evaluable = counts["pass"] + counts["violation"]
    applicable = sum(counts[k] for k in OUTCOMES)
    required = [verdicts[q["id"]]["verdict"] for q in task["questions"] if q["required"] and q["applicable"]]
    task_result = "fail" if "violation" in required else "pass" if required and all(v == "pass" for v in required) else "undetermined"
    return {
        "admission": task["admission"],
        "official_score_eligible": task["admission"] == "formal",
        "counts": counts,
        "evaluable_denominator": evaluable,
        "pass_rate": counts["pass"] / evaluable if evaluable else None,
        "applicable_denominator": applicable,
        "decisive_coverage": evaluable / applicable if applicable else None,
        "task_result": task_result,
        "by_domain": by_domain,
        "by_dimension": by_dimension,
    }


def judge_run(task: dict, run_dir: str | Path, judge: RuleJudge) -> dict:
    task = validate_task(task)
    run_dir = Path(run_dir).resolve()
    evidence = load_json(run_dir / "evidence.json")
    if evidence.get("task_sha256") != task_digest(task):
        raise ValueError("evidence was created for a different frozen task")
    verdicts: dict[str, dict] = {}
    for q in task["questions"]:
        entry = evidence["questions"][q["id"]]
        if not q["applicable"]:
            verdicts[q["id"]] = {"question_id": q["id"], "verdict": "N/A", "reason": "preconfigured in frozen task"}
            continue
        if not entry["checker"]["sufficient"]:
            verdicts[q["id"]] = {
                "question_id": q["id"], "verdict": "undetermined", "opportunity": "unknown",
                "evidence_frame_ids": [], "observations": [], "reason": "evidence checker found missing evidence",
            }
            continue
        verdicts[q["id"]] = _validate_verdict(q, entry, judge.judge(q, entry))
    output = {"task_sha256": task_digest(task), "verdicts": verdicts, "summary": aggregate(task, verdicts)}
    save_json(run_dir / "scores.json", output)
    return output
