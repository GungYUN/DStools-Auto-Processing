"""Archive-link recovery preserves evidence and never falls back to CASDA."""
from contextlib import redirect_stderr, redirect_stdout
import io
from pathlib import Path
import shutil
import subprocess
import tarfile
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from askap_auto import controller, science
from askap_auto.common import digest, read_json, write_json
from askap_auto.worker import run_job
import test_stage3_validation as fixtures


class ArchiveRetryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.Stage3ValidationTests(methodName="test_prepared_case_checker_returns_not_ready")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.work, self.state, self.old = self.fixture.assert_prepared("single")
        self.work = self.work.resolve()
        self.task = self.old["tasks"][0]
        self.original_id = self.task["targets"][0]["attempt_id"]
        job = self.state.jobs(self.old["id"])[0]
        self.casda = fixtures.FixtureCasda(self.fixture.root / "retry-transport")
        controller.prepare_job(self.state, job, client_factory=lambda _: self.casda)
        root = Path(self.task["raw_root"])
        self.ready = read_json(root / "download-ready.json")
        name = self.ready["ms_name"]
        target = name + "/ASKAP_METADATA/footprintOutput-sb74372-VAST_0535+00.txt"
        link = name + "/ASKAP_METADATA/footprintOutput-sb74372-src1-VAST_0535+00.txt"
        payload = b"Original footprint metadata\n"
        with tarfile.open(self.ready["archive"], "a") as archive:
            member = tarfile.TarInfo(target)
            member.size = len(payload)
            archive.addfile(member, io.BytesIO(payload))
            member = tarfile.TarInfo(link)
            member.type = tarfile.LNKTYPE
            member.linkname = target
            archive.addfile(member)
        Path(self.ready["checksum"]).write_text(digest(self.ready["archive"]) + "  " + Path(self.ready["archive"]).name + "\n")
        self.ready["files"] = {self.ready[k]: Path(self.ready[k]).stat().st_size for k in ("archive", "checksum")}
        write_json(root / "download-ready.json", self.ready)
        error = "Archive links are unsupported: " + link
        self.state.step(self.original_id, "extract_archive", "N", detail=error)
        self.state.update(self.original_id, status="failed", error_step="extract_archive", error=error,
                          log=str(root / "extract.log"))
        job["status"] = "complete"
        self.state.store_job(job)
        self.state.store_run(self.old, "complete")
        rc, report = self.fixture.checker(self.work)
        self.assertEqual(rc, 1)
        self.original_metadata = (self.work / "stage3-case.json").read_bytes()
        self.original_report = (self.state.root / "stage3-check.json").read_bytes()
        self.original_archive_hash = digest(self.ready["archive"])

    def retry(self):
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            rc = fixtures.STAGE3.main(["retry", "--work-dir", str(self.work)])
        return rc, output.getvalue() + errors.getvalue()

    def count_attempts(self):
        with self.state.connect() as db:
            return db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0]

    def test_retry_freezes_one_attempt_preserves_evidence_and_is_idempotent(self):
        rc, output = self.retry()
        self.assertEqual(rc, 0, output)
        self.assertIn("STAGE3 RETRY PREPARED", output)
        retry = self.state.active_run()
        self.assertEqual(retry["mode"], "retry")
        self.assertFalse(retry["allow_redownload_retry"])
        self.assertEqual(retry["frozen"], self.old["frozen"])
        task = retry["tasks"][0]
        self.assertTrue(task["reuse_download_only"])
        self.assertFalse(task["redownload"])
        self.assertEqual(task["raw_root"], self.task["raw_root"])
        self.assertEqual(task["targets"][0]["retry_number"], 1)
        self.assertEqual(self.state.attempt(self.original_id)["status"], "failed")
        metadata = read_json(self.work / "stage3-case.json")
        history = Path(metadata["archive_link_recovery"]["history_dir"])
        self.assertEqual((history / "stage3-case.json").read_bytes(), self.original_metadata)
        self.assertEqual((history / "stage3-check.json").read_bytes(), self.original_report)
        self.assertEqual(read_json(self.state.root / "stage3-check.json")["status"], "NOT_READY")
        self.assertEqual(digest(self.ready["archive"]), self.original_archive_hash)
        self.assertEqual(len(self.casda.download_calls), 1)
        self.assertEqual(self.state.jobs(retry["id"])[0]["status"], "planned")
        self.assertEqual(self.count_attempts(), 2)
        rc, output = self.retry()
        self.assertEqual(rc, 0, output)
        self.assertIn("ALREADY PREPARED", output)
        self.assertEqual(self.count_attempts(), 2)

    def test_retained_hardlink_recovery_completes_checker_without_download(self):
        rc, output = self.retry()
        self.assertEqual(rc, 0, output)
        retry = self.state.active_run()
        job = self.state.jobs(retry["id"])[0]
        with patch("askap_auto.archive.download", side_effect=AssertionError("Recovery must not download")) as download:
            controller.prepare_job(self.state, job)
        download.assert_not_called()
        _, manifest = controller.write_sbatch(self.state, job)
        executor = fixtures.FixtureScience()
        rc = run_job(job, manifest,
                     executor_factory=lambda frozen, state, deadline: science.Executor(frozen, state, deadline, executor),
                     preflight_fn=lambda _: {})
        self.assertEqual(rc, 0)
        job["status"] = "complete"
        self.state.store_job(job)
        controller.run_controller(self.state, retry, submit=True)
        rc, report = self.fixture.checker(self.work)
        self.assertEqual(rc, 0, report.get("message"))
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(self.count_attempts(), 2)
        self.assertEqual(self.state.attempt(self.original_id)["status"], "failed")
        self.assertEqual(len(self.casda.download_calls), 1)
        self.assertEqual(digest(self.ready["archive"]), self.original_archive_hash)

    def test_missing_cache_after_freeze_fails_without_download_or_retry2(self):
        self.assertEqual(self.retry()[0], 0)
        retry = self.state.active_run()
        Path(self.ready["archive"]).unlink()
        job = self.state.jobs(retry["id"])[0]
        with patch("askap_auto.archive.download", side_effect=AssertionError("Forbidden network")) as download:
            controller.prepare_job(self.state, job)
        download.assert_not_called()
        attempt = self.state.attempt(retry["tasks"][0]["targets"][0]["attempt_id"])
        self.assertEqual(attempt["status"], "failed")
        self.assertIn("recovery cannot download again", attempt["error"])
        with redirect_stdout(io.StringIO()):
            controller.run_controller(self.state, retry, submit=True)
        self.assertFalse(self.state.active_run())
        self.assertEqual(self.count_attempts(), 2)

    def test_same_size_archive_change_after_freeze_is_rejected(self):
        self.assertEqual(self.retry()[0], 0)
        retry = self.state.active_run()
        archive = Path(self.ready["archive"])
        with archive.open("r+b") as handle:
            handle.write(b"X")
        job = self.state.jobs(retry["id"])[0]
        with patch("askap_auto.archive.download", side_effect=AssertionError("Forbidden network")) as download:
            controller.prepare_job(self.state, job)
        download.assert_not_called()
        attempt = self.state.attempt(retry["tasks"][0]["targets"][0]["attempt_id"])
        self.assertIn("SHA256 changed", attempt["error"])

    def test_marker_cannot_redirect_frozen_download_paths(self):
        self.assertEqual(self.retry()[0], 0)
        retry = self.state.active_run()
        ready = dict(self.ready)
        alternative = Path(self.task["raw_root"]) / "download/alternative"
        alternative.mkdir()
        for role in ("archive", "checksum"):
            destination = alternative / Path(ready[role]).name
            shutil.copy2(ready[role], destination)
            ready[role] = str(destination)
        ready["files"] = {ready[k]: Path(ready[k]).stat().st_size for k in ("archive", "checksum")}
        write_json(Path(self.task["raw_root"]) / "download-ready.json", ready)
        job = self.state.jobs(retry["id"])[0]
        with patch("askap_auto.archive.download", side_effect=AssertionError("Forbidden network")) as download:
            controller.prepare_job(self.state, job)
        download.assert_not_called()
        attempt = self.state.attempt(retry["tasks"][0]["targets"][0]["attempt_id"])
        self.assertIn("paths differ from the frozen recovery", attempt["error"])

    def test_worker_checks_archive_again_after_download_preparation(self):
        self.assertEqual(self.retry()[0], 0)
        retry = self.state.active_run()
        job = self.state.jobs(retry["id"])[0]
        controller.prepare_job(self.state, job)
        self.assertEqual(job["status"], "prepared")
        _, manifest = controller.write_sbatch(self.state, job)
        with Path(self.ready["archive"]).open("r+b") as handle:
            handle.write(b"X")
        executor = fixtures.FixtureScience()
        rc = run_job(job, manifest,
                     executor_factory=lambda frozen, state, deadline: science.Executor(frozen, state, deadline, executor),
                     preflight_fn=lambda _: {})
        self.assertEqual(rc, 0)
        attempt = self.state.attempt(retry["tasks"][0]["targets"][0]["attempt_id"])
        self.assertEqual(attempt["status"], "failed")
        self.assertIn("SHA256 changed", attempt["error"])
        self.assertEqual(executor.commands, [], "Changed archive must be rejected before tar/science")

    def test_science_failure_in_corrective_attempt_does_not_redownload(self):
        self.assertEqual(self.retry()[0], 0)
        retry = self.state.active_run()
        aid = retry["tasks"][0]["targets"][0]["attempt_id"]
        self.state.update(aid, status="failed", error_step="extract_ds", error="New science failure")
        with redirect_stdout(io.StringIO()):
            controller.run_controller(self.state, retry, submit=True)
        self.assertFalse(controller.add_redownload_retries(self.state, retry))
        self.assertEqual(self.count_attempts(), 2)
        self.assertEqual(len(self.casda.download_calls), 1)

    def test_crash_after_plan_reservation_adopts_existing_retry(self):
        original_write = fixtures.STAGE3.write_json
        def fail_metadata(path, value):
            if Path(path) == self.work / "stage3-case.json" and "archive_link_recovery" in value:
                raise OSError("Simulated crash before metadata switch")
            return original_write(path, value)
        with patch.object(fixtures.STAGE3, "write_json", fail_metadata):
            rc, output = self.retry()
        self.assertEqual(rc, 1, output)
        reserved = self.state.active_run()["id"]
        self.assertEqual(read_json(self.work / "stage3-case.json")["plan_run_id"], self.old["id"])
        rc, output = self.retry()
        self.assertEqual(rc, 0, output)
        self.assertEqual(read_json(self.work / "stage3-case.json")["plan_run_id"], reserved)
        self.assertEqual(self.count_attempts(), 2)

    def test_crash_after_metadata_switch_finishes_intent_and_report(self):
        original_write = fixtures.STAGE3.write_json
        def fail_intent(path, value):
            if Path(path).parent == self.state.root.resolve() / "stage3-retry" and value.get("status") == "prepared":
                raise OSError("Simulated crash after metadata switch")
            return original_write(path, value)
        with patch.object(fixtures.STAGE3, "write_json", fail_intent):
            rc, output = self.retry()
        self.assertEqual(rc, 1, output)
        reserved = self.state.active_run()["id"]
        self.assertEqual(read_json(self.work / "stage3-case.json")["plan_run_id"], reserved)
        self.assertEqual(read_json(self.state.root / "stage3-check.json")["status"], "FAIL")
        rc, output = self.retry()
        self.assertEqual(rc, 0, output)
        self.assertIn("ALREADY PREPARED", output)
        self.assertEqual(read_json(self.state.root / "stage3-check.json")["status"], "NOT_READY")
        record = read_json(self.work / "stage3-case.json")["processing_recovery"]
        self.assertEqual(read_json(record["intent_file"])["status"], "prepared")
        self.assertEqual(self.count_attempts(), 2)

    def fail_first_retry_at_integer_parser(self):
        self.assertEqual(self.retry()[0], 0)
        first = self.state.active_run()
        job = self.state.jobs(first["id"])[0]
        controller.prepare_job(self.state, job)
        _, manifest = controller.write_sbatch(self.state, job)
        class IntegerParserFailure(fixtures.FixtureScience):
            def __call__(self, command, cwd=None, **kwargs):
                if "dstools-create-model" in command:
                    kwargs["stderr"].write("Error: Invalid value for '-t' / '--threshold': '3.0' is not a valid integer.\n")
                    raise subprocess.CalledProcessError(2, command)
                return super().__call__(command, cwd=cwd, **kwargs)
        executor = IntegerParserFailure()
        run_job(job, manifest,
                executor_factory=lambda frozen, state, deadline: science.Executor(frozen, state, deadline, executor),
                preflight_fn=lambda _: {})
        job["status"] = "complete"
        self.state.store_job(job)
        controller.run_controller(self.state, first, submit=True)
        aid = first["tasks"][0]["targets"][0]["attempt_id"]
        self.assertEqual(self.state.attempt(aid)["error_step"], "create_model")
        self.assertEqual(self.state.attempt(aid)["retry_number"], 1)
        self.fixture.checker(self.work)
        return first, aid

    def test_integer_parser_correction_reuses_raw_and_finishes_retry2(self):
        first, first_id = self.fail_first_retry_at_integer_parser()
        first_log = Path(self.state.attempt(first_id)["log"]).read_bytes()
        # Match the already deployed workspace: only the older recovery key
        # exists, with no newly introduced intent_file/reason fields.
        legacy_metadata = read_json(self.work / "stage3-case.json")
        legacy_metadata.pop("processing_recovery")
        for key in ("intent_file", "reason"):
            legacy_metadata["archive_link_recovery"].pop(key)
        write_json(self.work / "stage3-case.json", legacy_metadata)
        raw_marker = Path(first["tasks"][0]["raw_root"]) / "raw-ready.json"
        raw_before = raw_marker.read_bytes()
        rc, output = self.retry()
        self.assertEqual(rc, 0, output)
        self.assertIn("integer-threshold", output)
        corrected = self.state.active_run()
        self.assertEqual(corrected["recovery_of"], first["id"])
        task = corrected["tasks"][0]
        self.assertEqual(task["targets"][0]["retry_number"], 2)
        self.assertFalse(task["redownload"])
        self.assertEqual(task["raw_root"], first["tasks"][0]["raw_root"])
        self.assertEqual(corrected["frozen"], first["frozen"])
        metadata = read_json(self.work / "stage3-case.json")
        history = Path(metadata["processing_recovery"]["history_dir"])
        self.assertEqual((history / "commands.log").read_bytes(), first_log)
        job = self.state.jobs(corrected["id"])[0]
        with patch("askap_auto.archive.download", side_effect=AssertionError("No further downloads")) as download:
            controller.prepare_job(self.state, job)
        download.assert_not_called()
        _, manifest = controller.write_sbatch(self.state, job)
        executor = fixtures.FixtureScience()
        run_job(job, manifest,
                executor_factory=lambda frozen, state, deadline: science.Executor(frozen, state, deadline, executor),
                preflight_fn=lambda _: {})
        job["status"] = "complete"
        self.state.store_job(job)
        controller.run_controller(self.state, corrected, submit=True)
        rc, report = self.fixture.checker(self.work)
        self.assertEqual(rc, 0, report.get("message"))
        self.assertEqual(raw_marker.read_bytes(), raw_before)
        self.assertEqual(self.count_attempts(), 3)
        self.assertEqual(len(self.casda.download_calls), 1)
        self.assertEqual(self.state.attempt(first_id)["status"], "failed")
        self.assertEqual(self.retry()[0], 0)
        self.assertEqual(self.count_attempts(), 3)

    def test_unrelated_model_failure_is_not_accepted_as_integer_correction(self):
        first, aid = self.fail_first_retry_at_integer_parser()
        Path(self.state.attempt(aid)["log"]).write_text("WSClean failed: unrelated data problem\n")
        rc, output = self.retry()
        self.assertEqual(rc, 0, output)  # Existing recovery is displayed, never resubmitted.
        self.assertIn("ALREADY PREPARED", output)
        self.assertEqual(self.count_attempts(), 2)
        self.assertFalse(self.state.active_run())

    def test_changed_sif_prevents_new_attempt(self):
        self.fixture.container.write_bytes(b"Changed SIF")
        rc, output = self.retry()
        self.assertEqual(rc, 1)
        self.assertIn("SIF changed", output)
        self.assertEqual(self.count_attempts(), 1)
        self.assertFalse(self.state.active_run())

    def test_missing_archive_before_retry_prevents_new_attempt(self):
        Path(self.ready["archive"]).unlink()
        rc, output = self.retry()
        self.assertEqual(rc, 1)
        self.assertIn("recovery cannot download again", output)
        self.assertEqual(self.count_attempts(), 1)
        self.assertFalse(self.state.active_run())


if __name__ == "__main__":
    unittest.main()
