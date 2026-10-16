"""Interrupted or failed copies are deleted before a fresh attempt; complete data is reused."""
from pathlib import Path
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from askap_auto import archive, controller
from askap_auto.common import read_json
import test_auto as fixtures


class FailingCasda(fixtures.Casda):
    def download_files(self, urls, savedir):
        (Path(savedir) / self.filename).write_bytes(b"half an archive")
        raise ConnectionError("connection reset by " + urls[0])


class LeftoverCleanupTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.Tests("runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root, self.state, self.s = self.fixture.root, self.fixture.state, self.fixture.s

    def test_failed_download_is_deleted_and_logged_without_signed_urls(self):
        snapshot = self.fixture.snapshot()
        root = self.root / "download"
        with self.assertRaisesRegex(RuntimeError, "connection reset"):
            archive.download(snapshot["observations"][0], snapshot["cache"], root, self.s,
                             client_factory=lambda _: FailingCasda(self.root))
        self.assertEqual(list((root / "download").iterdir()), [])
        self.assertFalse((root / "download-ready.json").exists())
        log = (root / "download.log").read_text()
        self.assertEqual(log.count("failed"), self.s["download_retries"])
        self.assertNotIn("secret", log)

    def test_interrupted_unpack_is_deleted_before_unpacking_again(self):
        snapshot = self.fixture.snapshot()
        obs, root = snapshot["observations"][0], self.root / "dataset"
        archive.download(obs, snapshot["cache"], root, self.s, client_factory=lambda _: self.fixture.casda)
        partial = root / "raw" / "interrupted"
        partial.mkdir(parents=True)
        (partial / "table.dat").write_bytes(b"half")
        ms = archive.extract(root, obs, True, lambda command: subprocess.run(command, check=True), self.s)
        self.assertFalse(partial.exists())
        self.assertEqual(len(list((root / "raw").iterdir())), 1)
        self.assertEqual(read_json(root / "raw-ready.json")["ms"], str(ms))
        stray = root / "raw" / "stray"
        stray.mkdir()
        self.assertEqual(archive.extract(root, obs, True, None, self.s), ms)  # Reused without unpacking.
        self.assertFalse(stray.exists())

    def test_interrupted_work_is_deleted_in_single_source_mode(self):
        run = self.fixture.plan(True)
        paths = [Path(t["path"]) for task in run["tasks"] for t in task["targets"]]
        for path in paths:
            (path / "work").mkdir(parents=True)
            (path / "interrupted-work-old").mkdir()
        self.fixture.execute(run)
        for path in paths:
            self.assertEqual(sorted(p.name for p in path.iterdir() if "work" in p.name), ["work"])
        self.assertEqual(self.state.export()["status_counts"], {"complete": 3})

    def test_unfinished_model_build_is_deleted_before_building_again(self):
        run = self.fixture.plan()
        roots = [Path(task["model_root"]) for task in run["tasks"]]
        for root in roots:
            (root / "build-interrupted").mkdir(parents=True)
        self.fixture.science.fail = True  # Keeps the new build: cleanup only follows success.
        self.fixture.execute(run)
        for root in roots:
            builds = [p.name for p in root.glob("build-*")]
            self.assertEqual(len(builds), 1)
            self.assertNotIn("build-interrupted", builds)
            self.assertEqual(Path(read_json(root / "ready.json")["ms"]).parent.name, builds[0])

    def test_working_copies_of_replaced_or_environment_failed_attempts_are_deleted(self):
        run = self.fixture.plan()
        task = run["tasks"][0]
        target = task["targets"][0]
        stale = {}
        with self.state.connect() as db:
            for status in ("superseded", "env_failed", "failed"):
                path = Path(target["path"]).parent / status
                for name in ("work", "interrupted-work-x"):
                    (path / name).mkdir(parents=True)
                db.execute("INSERT INTO attempts(id,source_id,observation_key,run_id,status,retry_number,path) VALUES(?,?,?,?,?,?,?)",
                           (status, target["source"]["source_id"], task["observation"]["key"], "old", status, 0, str(path)))
                stale[status] = path
        job = controller.pack_job(self.state, run)
        controller.prepare_job(self.state, job, client_factory=lambda _: self.fixture.casda)
        for status, path in stale.items():
            with self.subTest(status=status):
                kept = status == "failed"  # Processing failures keep their data for inspection.
                self.assertEqual((path / "work").exists(), kept)
                self.assertEqual((path / "interrupted-work-x").exists(), kept)

if __name__ == "__main__":
    unittest.main()
