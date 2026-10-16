"""--retry-exhausted gives exhausted targets a new round of the two retries, and nothing else."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from askap_auto import catalogue, controller, settings
import test_auto as fixtures


class RetryExhaustedTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.Tests("runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.state, self.s = self.fixture.state, self.fixture.s

    def planned(self, run):
        return {(t["source"]["name"], task["observation"]["key"]): t["retry_number"]
                for task in run["tasks"] for t in task["targets"]}

    def test_each_retry_option_selects_only_its_own_targets(self):
        other = catalogue.make_source("Other", 180, -28, self.s, index=2)
        index = {"revision": "r", "sources": [self.fixture.snapshot(), self.fixture.snapshot(source=other)]}
        self.state.sync_index(index)
        frozen = settings.freeze(self.s, {})
        first = controller.plan(self.state, index, frozen)
        targets = {(t["source"]["name"], task["observation"]["key"]): t for task in first["tasks"] for t in task["targets"]}
        (exhausted, failed), (env, untouched) = [sorted(k for k in targets if k[0] == name) for name in ("Star_A", "Other")]
        self.state.update(targets[exhausted]["attempt_id"], status="exhausted")
        self.state.update(targets[failed]["attempt_id"], status="failed")
        self.state.update(targets[env]["attempt_id"], status="env_failed")
        with self.state.connect() as db:
            db.execute("UPDATE targets SET latest_attempt=NULL WHERE source_id=? AND observation_key=?",
                       (other["source_id"], untouched[1]))
        self.state.store_run(first, "complete")
        run = controller.plan(self.state, index, frozen, mode="retry_exhausted")
        self.assertEqual(self.planned(run), {exhausted: 1})
        self.assertFalse(any(task["shared"] for task in run["tasks"]), "Retries use private models")
        self.assertEqual(self.state.attempt(run["tasks"][0]["targets"][0]["attempt_id"])["kind"], "retry_exhausted")
        self.state.supersede_run(run)
        self.assertEqual(self.planned(controller.plan(self.state, index, frozen, mode="retry")), {failed: 1, env: 0})

    def test_new_round_reuses_the_download_then_downloads_again(self):
        self.fixture.science.fail = True
        self.fixture.execute(self.fixture.plan(True))
        retry = self.fixture.plan(True, "retry")
        self.fixture.execute(retry)
        self.assertTrue(controller.add_redownload_retries(self.state, retry))
        self.fixture.execute(retry)
        self.assertEqual(self.state.export()["status_counts"], {"exhausted": 3})
        self.assertFalse(self.fixture.plan(True, "retry")["tasks"])
        downloads = len(self.fixture.casda.calls)
        again = self.fixture.plan(True, "retry_exhausted")
        self.assertEqual(set(self.planned(again).values()), {1})
        self.fixture.execute(again)
        self.assertEqual(len(self.fixture.casda.calls), downloads, "The first retry reuses the downloaded data")
        self.assertTrue(controller.add_redownload_retries(self.state, again))
        self.fixture.execute(again)
        self.assertEqual(len(self.fixture.casda.calls), downloads + 3, "The second retry downloads again")
        self.assertEqual(self.state.export()["status_counts"], {"exhausted": 3})
        self.fixture.science.fail = False
        self.fixture.execute(self.fixture.plan(True, "retry_exhausted"))
        self.assertEqual(self.state.export()["status_counts"], {"complete": 3})

    def test_round_stopped_by_the_environment_is_repeated_only_by_retry_exhausted(self):
        self.fixture.science.fail = True
        self.fixture.execute(self.fixture.plan())
        for target in (t for task in self.fixture.plan(mode="retry")["tasks"] for t in task["targets"]):
            self.state.update(target["attempt_id"], status="exhausted")
        run = self.fixture.plan(mode="retry_exhausted")
        for target in (t for task in run["tasks"] for t in task["targets"]):
            self.state.update(target["attempt_id"], status="env_failed")
        self.assertFalse(self.fixture.plan()["tasks"])
        self.assertFalse(self.fixture.plan(mode="retry")["tasks"])
        self.assertEqual(set(self.planned(self.fixture.plan(mode="retry_exhausted")).values()), {1})


if __name__ == "__main__":
    unittest.main()
