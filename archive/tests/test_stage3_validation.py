"""Offline regression for stage-3 preparation and completion evidence.

These tests generate accepted stage-2 evidence through the real CLI protocol,
then use real SQLite, archives, copies, WCS FITS and worker steps. Only CASDA
transport and the scientific binaries are fixtures; no network/Slurm/SIF runs.
"""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import shutil
import sys
import tarfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np
from astropy.io import fits
from astropy.wcs import WCS

CODE_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(CODE_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from askap_auto import controller, science
from askap_auto.common import digest, read_json, write_json
from askap_auto.state import State
from askap_auto.worker import run_job
import test_stage2_validation as stage2_tests


def load_script(name):
    path = Path(__file__).with_name(name + ".py")
    spec = importlib.util.spec_from_file_location(name + "_regression", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


STAGE3 = load_script("validate_processing_stage3")
CHECKER = load_script("check_processing_stage3")


class FixtureCasda:
    """Write one real archive/checksum from the exact requested metadata row."""
    def __init__(self, root):
        self.root = root
        self.requests = []
        self.download_calls = []

    def stage_data(self, rows):
        if len(rows) != 1:
            raise AssertionError("One selected dataset row required")
        self.filename = str(rows["filename"][0])
        self.requests.append(self.filename)
        return ["https://offline.invalid/" + self.filename,
                "https://offline.invalid/" + self.filename + ".checksum"]

    def download_files(self, urls, savedir):
        self.download_calls.append(list(urls))
        raw = self.root / "fixture" / self.filename[:-4]
        raw.mkdir(parents=True)
        (raw / "table.dat").write_bytes(b"Unmodified archived visibility fixture")
        (raw / "FIELD").mkdir()
        (raw / "FIELD/table.dat").write_bytes(b"Original FIELD fixture")
        archive = Path(savedir) / self.filename
        with tarfile.open(archive, "w") as handle:
            handle.add(raw, arcname=raw.name)
        Path(str(archive) + ".checksum").write_text(digest(archive) + "  " + self.filename + "\n")


class FixtureScience:
    """Scientific process boundary with images covering all three real targets."""
    def __init__(self):
        self.commands = []

    def __call__(self, command, cwd=None, **kwargs):
        self.commands.append(list(command))
        if command[0] == "tar":
            archive = Path(command[command.index("-xf") + 1])
            destination = Path(command[command.index("-C") + 1])
            with tarfile.open(archive) as handle:
                options = {"filter": "data"} if hasattr(tarfile, "data_filter") else {}
                handle.extractall(destination, **options)
            return SimpleNamespace(stdout="", stderr="")
        tools = [arg for arg in command if arg.startswith("dstools-")]
        if len(tools) != 1:
            raise AssertionError("Unexpected scientific fixture command: " + repr(command))
        tool = tools[0]
        if tool == "dstools-askap-preprocess":
            ms = Path(command[-1])
            (ms / "FIELD").rename(ms / "FIELD_OLD")
            (ms / "FIELD").mkdir()
            (ms / "FIELD/table.dat").write_bytes(b"Preprocessed FIELD")
            (ms / "table.dat").write_bytes(b"Preprocessed private visibility")
        elif tool == "dstools-create-model":
            model = Path(cwd) / "wsclean_model"
            model.mkdir()
            wcs = WCS(naxis=2)
            wcs.wcs.crpix = [151, 151]
            wcs.wcs.cdelt = [-0.005, 0.005]
            wcs.wcs.crval = [84.54, -2.50]
            wcs.wcs.ctype = ["RA---TAN", "DEC--TAN"]
            for pol in ("I", "V"):
                fits.PrimaryHDU(np.ones((300, 300)), wcs.to_header()).writeto(model / f"wsclean-MFS-{pol}-image.fits")
            (model / "wsclean-MFS-I-model.fits").write_bytes(b"Original unmasked MFS model")
            (model / "wsclean-0000-I-model.fits").write_bytes(b"Original unmasked channel model")
        elif tool == "dstools-insert-model":
            model, ms = map(Path, command[-2:])
            if not (ms / "FIELD_OLD").is_dir():
                raise AssertionError("Insertion did not receive preprocessed private MS")
            for path in list(model.glob("*-model.fits")):
                shutil.copy2(path, path.with_name(path.name.replace(".fits", ".premask.fits")))
                path.write_bytes(b"Independently masked insertion model")
        elif tool == "dstools-subtract-model":
            shutil.copytree(Path(command[-1]), Path(command[-1]).with_name("input.subtracted.ms"))
        elif tool == "dstools-extract-ds":
            Path(command[-1]).write_bytes(b"Offline DS fixture: science content requires site acceptance")
        else:
            raise AssertionError("Unexpected DStools fixture: " + tool)
        return SimpleNamespace(stdout="", stderr="")


class Stage3ValidationTests(unittest.TestCase):
    def setUp(self):
        fixture = stage2_tests.Stage2ValidationTests(methodName="test_complete_cli_query_and_plans_with_shared_and_multibeam_data")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        result, report, output, error = fixture.run_acceptance()
        self.assertEqual(result, 0, error + output)
        self.root = fixture.root
        self.container = fixture.container
        self.stage2 = Path(report["work_dir"])

    def prepare_case(self, case="single", name=None):
        work = self.root / (name or "stage3-" + case)
        output, errors = io.StringIO(), io.StringIO()
        argv = ["prepare", "--stage2-run", str(self.stage2), "--work-dir", str(work),
                "--case", case, "--container", str(self.container), "--python", sys.executable,
                "--casda-username", "offline@example.invalid"]
        release = lambda rows: [index for index, public in enumerate(rows["fixture_public"]) if public]
        with (
            patch("askap_auto.query.public_indices", release),
            patch("askap_auto.archive.download", side_effect=AssertionError("Prepare cannot download")) as download,
            patch("subprocess.run", side_effect=AssertionError("Prepare cannot spawn processes")) as run,
            redirect_stdout(output), redirect_stderr(errors),
        ):
            result = STAGE3.main(argv)
        download.assert_not_called()
        run.assert_not_called()
        return result, work, output.getvalue(), errors.getvalue()

    def assert_prepared(self, case):
        result, work, output, error = self.prepare_case(case)
        self.assertEqual(result, 0, error + output)
        state = State(work)
        run = state.active_run()
        self.assertEqual(len(run["tasks"]), 1)
        self.assertEqual(len(run["tasks"][0]["targets"]), 1 if case == "single" else 3)
        self.assertEqual((run["tasks"][0]["observation"]["sb_id"], run["tasks"][0]["observation"]["beam"]), ("SB74372", "beam28"))
        effective = run["frozen"]["settings"]
        self.assertEqual((effective["parallel_slots"], effective["threads_per_slot"], effective["memory_gb"], effective["tmp_gb"]), (1, 8, 40, 10))
        for target in run["tasks"][0]["targets"]:
            self.assertNotEqual((target["source"]["pmra_cosdec"], target["source"]["pmdec"]), (0, 0))
            self.assertEqual(state.attempt(target["attempt_id"])["status"], "preparing")
        jobs = state.jobs(run["id"])
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["status"], "planned")
        self.assertFalse(jobs[0].get("scheduler_id"))
        script = (state.root / "jobs" / (jobs[0]["id"] + ".sbatch")).read_text()
        for line in ("#SBATCH --cpus-per-task=8", "#SBATCH --mem=40960M", "#SBATCH --tmp=10240M"):
            self.assertIn(line + "\n", script)
        self.assertFalse(any(work.rglob("*.tar")))
        self.assertFalse(any(work.rglob("*.ms")))
        metadata = read_json(work / "stage3-case.json")
        for path, expected in metadata["accepted_stage2_files"].items():
            self.assertEqual(digest(path), expected)
        with state.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM steps").fetchone()[0], 0)
        return work, state, run

    def checker(self, work):
        with redirect_stdout(io.StringIO()):
            result = CHECKER.main(["--work-dir", str(work)])
        return result, read_json(work / "state/stage3-check.json")

    def process_fixture(self, work, state, run):
        job = state.jobs(run["id"])[0]
        casda = FixtureCasda(self.root / work.name)
        controller.prepare_job(state, job, client_factory=lambda _: casda)
        _, manifest = controller.write_sbatch(state, job)
        executor = FixtureScience()
        result = run_job(job, manifest,
                         executor_factory=lambda frozen, worker_state, deadline: science.Executor(frozen, worker_state, deadline, executor),
                         preflight_fn=lambda _: {})
        self.assertEqual(result, 0)
        for target in run["tasks"][0]["targets"]:
            attempt = state.attempt(target["attempt_id"])
            self.assertEqual(attempt["status"], "complete", attempt)
        job["status"] = "complete"
        state.store_job(job)
        state.store_run(run, "complete")
        self.assertEqual(len(casda.requests), 1)
        self.assertEqual(len(casda.download_calls), 1)
        return executor

    def test_prepare_single_has_exact_dataset_motion_resources_and_no_execution(self):
        work, state, run = self.assert_prepared("single")
        self.assertTrue(run["single"])
        self.assertTrue(run["tasks"][0]["retain"])
        self.assertFalse(run["tasks"][0]["shared"])

    def test_prepare_shared_keeps_seed_range_and_expands_three_partners(self):
        work, state, run = self.assert_prepared("shared3")
        self.assertEqual((run["selection"]["begin"], run["selection"]["end"]), (1, 1))
        self.assertEqual({target["source"]["processing_index"] for target in run["tasks"][0]["targets"]}, {1, 2, 3})
        self.assertTrue(run["tasks"][0]["shared"])
        self.assertFalse(run["tasks"][0]["retain"])

    def test_changed_sif_is_rejected_before_creating_workspace(self):
        self.container.write_bytes(b"Changed after accepted stage2 fingerprint")
        result, work, output, error = self.prepare_case()
        self.assertEqual(result, 1)
        self.assertIn("differs from the accepted Stage2 image", error)
        self.assertFalse(work.exists())

    def test_nonpass_stage2_is_rejected_before_creating_workspace(self):
        report_path = self.stage2 / "report.json"
        report = read_json(report_path)
        report["status"] = "INCOMPLETE"
        write_json(report_path, report)
        result, work, output, error = self.prepare_case()
        self.assertEqual(result, 1)
        self.assertIn("PASS report from Stage2 is required", error)
        self.assertFalse(work.exists())

    def test_prepared_case_checker_returns_not_ready(self):
        work, state, run = self.assert_prepared("single")
        result, report = self.checker(work)
        self.assertEqual(result, 2)
        self.assertEqual(report["status"], "NOT_READY")
        self.assertEqual(report["scientific_status"], "NOT_READY")

    def test_single_completion_checker_passes_and_raw_tampering_fails(self):
        work, state, run = self.assert_prepared("single")
        self.process_fixture(work, state, run)
        result, report = self.checker(work)
        self.assertEqual(result, 0, report.get("message"))
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(report["raw_ms_payload"]["files_compared"], 2)
        self.assertEqual(len(report["retained_models"]["premask_matches"]), 2)
        raw = read_json(Path(run["tasks"][0]["raw_root"]) / "raw-ready.json")
        (Path(raw["ms"]) / "table.dat").write_bytes(b"Illegally changed retained raw visibility")
        result, report = self.checker(work)
        self.assertEqual(result, 1)
        self.assertEqual(report["status"], "FAIL")
        self.assertIn("Raw MS was changed after extraction", report["message"])

    def test_shared_completion_checker_proves_one_model_and_three_products(self):
        work, state, run = self.assert_prepared("shared3")
        executor = self.process_fixture(work, state, run)
        result, report = self.checker(work)
        self.assertEqual(result, 0, report.get("message"))
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(len(report["products"]), 3)
        self.assertEqual(len(report["managed_links"]), 3)
        counts = report["shared_model"]["command_counts"]
        self.assertEqual(counts["dstools-askap-preprocess"], 1)
        self.assertEqual(counts["dstools-create-model"], 1)
        self.assertEqual(sum("dstools-insert-model" in command for command in executor.commands), 3)
        inserts = [command for command in executor.commands if "dstools-insert-model" in command]
        self.assertTrue(all(Path(command[-2]).name == "wsclean_model" for command in inserts),
                        "A batch model is already private and must not be copied a second time")


if __name__ == "__main__":
    unittest.main()
