"""Environment failures are retried by the next command; processing failures need --retry-failed."""
import contextlib
import io
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from askap_auto import archive, controller, science
from askap_auto.common import read_json
from askap_auto.worker import run_job
import test_auto as fixtures


class UnavailableCasda(fixtures.Casda):
    def stage_data(self, table):
        raise RuntimeError("CASDA staging service unavailable")


class DamagedCasda(fixtures.Casda):
    def download_files(self, urls, savedir):
        for name in (self.filename, self.filename + ".checksum"):
            (Path(savedir) / name).write_bytes(b"not a tar archive")


class EnvironmentFailureTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.Tests("runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.state, self.s = self.fixture.state, self.fixture.s

    def statuses(self, run):
        return {self.state.attempt(t["attempt_id"])["status"] for task in run["tasks"] for t in task["targets"]}

    def prepare(self, run, casda):
        job = controller.pack_job(self.state, run)
        controller.prepare_job(self.state, job, client_factory=lambda _: casda)
        return job

    def scheduler(self, slurm_state=None, executor=None, preflight=lambda _: {}):
        """sbatch runs the worker in-process (or not at all); sacct reports slurm_state."""
        def run(command, **kwargs):
            if command[0] == "sbatch":
                manifest = Path(command[-1]).with_suffix(".json")
                if slurm_state is None:
                    run_job(read_json(manifest), manifest, preflight_fn=preflight,
                            executor_factory=executor or (lambda f, s, d: science.Executor(f, s, d, self.fixture.science)))
                return SimpleNamespace(stdout="4242\n")
            return SimpleNamespace(stdout=f"4242|{slurm_state}\n", returncode=0)
        return run

    def control(self, run, runner):
        with patch("askap_auto.controller.time.sleep"), contextlib.redirect_stdout(io.StringIO()):
            return controller.run_controller(self.state, run, True, runner=runner, client_factory=lambda _: self.fixture.casda)

    def test_download_failures_are_classified(self):
        run = self.fixture.plan()
        self.prepare(run, UnavailableCasda(self.fixture.root))
        self.assertEqual(self.statuses(run), {"env_failed"})
        run = self.fixture.plan()  # The same command plans them again, retry count unchanged.
        self.assertEqual({t["retry_number"] for task in run["tasks"] for t in task["targets"]}, {0})
        self.prepare(run, DamagedCasda(self.fixture.root))
        self.assertEqual(self.statuses(run), {"failed"})
        self.assertFalse(self.fixture.plan()["tasks"], "Processing failures need --retry-failed")
        self.assertTrue(self.fixture.plan(mode="retry")["tasks"])

    def test_exhausted_staging_budget_stops_preparation(self):
        self.s["staging_budget_bytes"] = 1
        run = self.fixture.plan()
        job = controller.pack_job(self.state, run)
        with self.assertRaisesRegex(archive.SpaceError, "Staging budget exceeded.*run the command again"):
            controller.prepare_job(self.state, job, client_factory=lambda _: self.fixture.casda)
        self.assertEqual(self.state.jobs(run["id"])[-1]["status"], "planned")
        statuses = [self.state.attempt(t["attempt_id"])["status"] for task in run["tasks"] for t in task["targets"]]
        self.assertEqual(statuses.count("env_failed"), 1, "Stops at the first dataset that does not fit")

    def test_explicit_retry_stopped_by_the_environment_does_not_use_up_a_retry(self):
        self.fixture.science.fail = True
        self.fixture.execute(self.fixture.plan())
        retry = self.fixture.plan(mode="retry")
        with patch("askap_auto.archive.download", side_effect=RuntimeError("CASDA staging service unavailable")):
            self.prepare(retry, self.fixture.casda)
        self.assertEqual(self.statuses(retry), {"env_failed"})
        self.assertFalse(self.fixture.plan()["tasks"], "An ordinary run does not repeat an explicit retry")
        again = self.fixture.plan(mode="retry")
        self.assertEqual({t["retry_number"] for task in again["tasks"] for t in task["targets"]}, {1})

    def test_space_exhausted_on_compute_node_stops_and_next_command_retries(self):
        run = self.fixture.plan()
        with patch.object(science.Executor, "copy", side_effect=archive.SpaceError("Staging budget exceeded: test")):
            with self.assertRaisesRegex(RuntimeError, "stopped: Staging budget exceeded: test"):
                self.control(run, self.scheduler())
        self.assertEqual(self.state.active_run()["id"], run["id"])
        self.assertIn("env_failed", self.statuses(run))
        self.control(self.state.active_run(), self.scheduler())
        self.assertIsNone(self.state.active_run())
        self.assertEqual(self.state.export()["status_counts"], {"complete": 2})

    def test_image_check_failure_stops_before_processing(self):
        run = self.fixture.plan()
        def unsupported(_):
            raise RuntimeError("Deployed SIF does not support dstools-create-model options: ['--robust']")
        with self.assertRaisesRegex(RuntimeError, "Image check failed: Deployed SIF does not support"):
            self.control(run, self.scheduler(preflight=unsupported))
        self.assertEqual(self.statuses(run), {"env_failed"})
        self.assertFalse([c for c in self.fixture.science.calls if "dstools-create-model" in c])

    def test_node_failure_continues_but_other_job_failures_stop(self):
        run = self.fixture.plan()
        self.control(run, self.scheduler("NODE_FAIL"))
        self.assertIsNone(self.state.active_run())
        self.assertEqual(self.state.export()["status_counts"], {"env_failed": 2})
        again = self.fixture.plan()
        self.assertEqual({t["retry_number"] for task in again["tasks"] for t in task["targets"]}, {0})
        with self.assertRaisesRegex(RuntimeError, r"ended \(FAILED\) without a completion record.*slurm-4242.err"):
            self.control(again, self.scheduler("FAILED"))
        self.assertEqual(self.state.active_run()["id"], again["id"])


if __name__ == "__main__":
    unittest.main()
