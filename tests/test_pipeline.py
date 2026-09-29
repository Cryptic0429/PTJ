import tempfile
import unittest
import json
import subprocess
import sys
from pathlib import Path

from ptj.contracts import append_jsonl, save_json, validate_task
from ptj.judge import judge_run
from ptj.ingest import register_video_run
from ptj.probe import run_probe
from ptj.trace import trace_run


def task(**budget):
    return validate_task({
        "case_id": "fixture_C1", "objective": "Probe a visible barrier", "admission": "qualification",
        "protocol": "T1", "phase_order": ["approach", "attempt"],
        "budget": {"max_decisions": 3, "max_control_ticks_per_decision": 2,
                   "base_sample_stride": 2, "event_radius": 1, "max_supplements": 1, **budget},
        "questions": [{"id": "q1", "metric": "C1", "domain": "P3", "subdomain": "P3.1",
                       "dimension": "S1", "required": True, "applicable": True,
                       "premise": "solid barrier is visible", "evidence_required": "before, contact, after",
                       "pass_rule": "body remains blocked", "violation_rule": "body crosses solid material"}],
    })


class FakeWorld:
    allowed_actions = {"forward"}

    def __init__(self):
        self.directory = None
        self.step_count = 0
        self.finalized = False

    def reset(self, run_dir):
        self.directory = run_dir
        return self._frame()

    def _frame(self):
        path = self.directory / f"frame{self.step_count}.ppm"
        path.write_bytes(b"P6\n1 1\n255\n\x00\x00\x00")
        return {"image_path": path.name, "video_time_s": self.step_count * 0.1,
                "actual_actions": ["forward"] if self.step_count else []}

    def step(self, actions, control_ticks):
        self.step_count += 1
        return self._frame()

    def finalize(self, video_path):
        self.finalized = True
        video_path.write_bytes(b"fixture only")


class FakeController:
    def __init__(self):
        self.views = []

    def decide(self, view):
        self.views.append(view)
        if len(self.views) == 1:
            return {"phase": "approach", "command": "HOLD", "actions": ["forward"],
                    "control_ticks": 1, "phase_complete": True,
                    "event_hints": [{"event_type": "possible_contact", "status": "expected"}]}
        return {"phase": "attempt", "command": "WAIT", "actions": [],
                "control_ticks": 1, "phase_complete": True}


class Checker:
    def __init__(self, sufficient=True):
        self.sufficient = sufficient

    def check(self, question, frames, actions):
        return {"sufficient": self.sufficient, "missing_evidence": [] if self.sufficient else ["contact not visible"]}


class Judge:
    def __init__(self, verdict):
        self.verdict = verdict

    def judge(self, question, evidence):
        return {"verdict": self.verdict,
                "opportunity": "absent" if self.verdict == "untriggered" else "present",
                "evidence_frame_ids": [evidence["frames"][0]["frame_id"]] if self.verdict in {"pass", "violation"} else [],
                "reason": "fixture label"}


class PipelineTests(unittest.TestCase):
    def test_probe_records_actual_actions_and_restricts_controller_view(self):
        with tempfile.TemporaryDirectory() as tmp:
            world, controller = FakeWorld(), FakeController()
            status = run_probe(task(), controller, world, Path(tmp) / "run", metadata={"fixture": True})
            self.assertTrue(status["probe_finished"])
            self.assertTrue(world.finalized)
            self.assertEqual(controller.views[1]["actual_action_history"][0]["actual_actions"], ["forward"])
            self.assertFalse(any("verdict" in view or "score" in view for view in controller.views))
            hints = (Path(tmp) / "run" / "event_hints.jsonl").read_text(encoding="utf-8")
            self.assertIn('"status":"expected"', hints)
            self.assertIn('"frame_id":"f000001"', hints)

    def test_four_states_and_denominators(self):
        for verdict in ("pass", "violation", "undetermined", "untriggered"):
            with self.subTest(verdict=verdict), tempfile.TemporaryDirectory() as tmp:
                run_dir = Path(tmp) / "run"
                run_probe(task(base_sample_stride=1), FakeController(), FakeWorld(), run_dir, metadata={"fixture": True})
                trace_run(task(base_sample_stride=1), run_dir, Checker())
                score = judge_run(task(base_sample_stride=1), run_dir, Judge(verdict))
                self.assertEqual(score["verdicts"]["q1"]["verdict"], verdict)
                self.assertEqual(score["summary"]["evaluable_denominator"], int(verdict in {"pass", "violation"}))
                self.assertEqual(score["summary"]["task_result"],
                                 "pass" if verdict == "pass" else "fail" if verdict == "violation" else "undetermined")
                self.assertFalse(score["summary"]["official_score_eligible"])

    def test_insufficient_evidence_cannot_become_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            run_probe(task(), FakeController(), FakeWorld(), run_dir, metadata={"fixture": True})
            trace_run(task(), run_dir, Checker(sufficient=False))
            result = judge_run(task(), run_dir, Judge("pass"))
            self.assertEqual(result["verdicts"]["q1"]["verdict"], "undetermined")

    def test_untriggered_requires_full_timeline(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            run_probe(task(base_sample_stride=3, event_radius=1), FakeController(), FakeWorld(), run_dir, metadata={"fixture": True})
            # A small run is entirely covered by the event radius. Extend it to create a gap.
            for i in range(3, 6):
                path = run_dir / f"extra{i}.ppm"
                path.write_bytes(b"P6\n1 1\n255\n\x00\x00\x00")
                append_jsonl(run_dir / "frames.jsonl", {"frame_id": f"f{i:06d}", "video_time_s": i / 10,
                    "image_path": path.name, "after_action_id": None, "phase": "attempt"})
            evidence = trace_run(task(base_sample_stride=3, event_radius=1), run_dir, Checker())
            self.assertFalse(evidence["questions"]["q1"]["full_timeline_covered"])
            with self.assertRaisesRegex(ValueError, "full-timeline"):
                judge_run(task(base_sample_stride=3, event_radius=1), run_dir, Judge("untriggered"))

    def test_rejects_changed_task_and_unverified_action(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            run_probe(task(), FakeController(), FakeWorld(), run_dir, metadata={"fixture": True})
            with self.assertRaisesRegex(ValueError, "different frozen task"):
                trace_run(task(event_radius=2), run_dir, Checker())

            class BadController:
                def decide(self, view):
                    return {"phase": view["phase"], "command": "HOLD", "actions": ["teleport"], "control_ticks": 1}

            world = FakeWorld()
            with self.assertRaisesRegex(ValueError, "unverified"):
                run_probe(task(), BadController(), world, Path(tmp) / "bad", metadata={"fixture": True})
            self.assertTrue(world.finalized)

    def test_ingest_and_command_agent_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video = root / "source.mp4"
            video.write_bytes(b"fixture only")
            image = root / "frame.ppm"
            image.write_bytes(b"P6\n1 1\n255\n\x00\x00\x00")
            manifest = root / "manifest.json"
            save_json(manifest, {"frames": [{"source_image": "frame.ppm", "video_time_s": 0.0}]})
            chosen_task = task(base_sample_stride=1)
            task_file = root / "task.json"
            save_json(task_file, chosen_task)
            run_dir = root / "run"
            register_video_run(chosen_task, video, manifest, run_dir, metadata={"fixture": True})
            script = root / "agent.py"
            script.write_text(
                "import json,sys\n"
                "x=json.load(sys.stdin)\n"
                "if x['role']=='evidence_checker': print(json.dumps({'sufficient':True}))\n"
                "else: print(json.dumps({'verdict':'pass','opportunity':'present','evidence_frame_ids':[x['evidence']['frames'][0]['frame_id']],'reason':'fixture'}))\n",
                encoding="utf-8",
            )
            settings = root / "agents.json"
            save_json(settings, {"checker_command": [sys.executable, str(script)],
                                 "judge_command": [sys.executable, str(script)]})
            proc = subprocess.run([sys.executable, "-m", "ptj", "evaluate", str(task_file), str(run_dir),
                                   "--agents", str(settings)], capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(json.loads(proc.stdout)["pass_rate"], 1.0)
            self.assertTrue((run_dir / "scores.json").is_file())


if __name__ == "__main__":
    unittest.main()
