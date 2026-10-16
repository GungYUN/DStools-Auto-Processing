"""Offline tests for acceptance decisions; no real SIF/MS/Slurm is certified."""
from contextlib import redirect_stdout
import copy
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

try:
    import h5py
except ImportError:
    h5py = None
import numpy as np
from astropy.io import fits

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from askap_auto.common import digest, read_json, write_json
from askap_auto.science import Executor
import test_stage3_validation as fixtures

STAGE4 = fixtures.load_script("validate_results_stage4a")
BASE_SCIENCE = fixtures.FixtureScience


class ScientificFixture(BASE_SCIENCE):
    def __call__(self, command, cwd=None, **kwargs):
        result = super().__call__(command, cwd=cwd, **kwargs)
        if "dstools-extract-ds" in command:
            with h5py.File(command[-1], "w") as handle:
                handle.attrs["phasecentre"] = "fixture-anchor"
                for name, data in {
                    "time": np.array([59000., 59000.0001]),
                    "frequency": np.array([800e6, 801e6]),
                    "uvdist": np.array([1000.]),
                    "flux": np.array([[[[1+2j, 3+4j], [5+6j, complex(np.nan, np.nan)]]]]),
                }.items():
                    handle.create_dataset(name, data=data)
        return result


@unittest.skipIf(h5py is None, "Optional local HDF5 fixtures need h5py; site inspection uses SIF h5py")
class Stage4AValidationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.Stage3ValidationTests(methodName="test_prepare_single_has_exact_dataset_motion_resources_and_no_execution")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.cases = []
        for kind in ("single", "shared3"):
            result, work, _, _ = self.fixture.prepare_case(kind)
            self.assertEqual(result, 0)
            state = fixtures.State(work)
            run = state.active_run()
            with patch.object(fixtures, "FixtureScience", ScientificFixture):
                self.fixture.process_fixture(work, state, run)
            self.assertEqual(self.fixture.checker(work)[0], 0)
            self.cases.append(work)
        observation_key = read_json(self.cases[0] / "stage3-case.json")["observation_key"]
        key_patch = patch.object(STAGE4, "OBSERVATION_KEY", observation_key)
        key_patch.start()
        self.addCleanup(key_patch.stop)
        self.single = STAGE4.load_case(self.cases[0], "single")
        self.shared = STAGE4.load_case(self.cases[1], "shared3")
        self.output = self.root / "stage4-output"
        self.output.mkdir()

    def plan(self):
        products = {}
        all_files = []
        for case in (self.single, self.shared):
            for item in case["report"]["products"]:
                all_files += list(item["files"].values())
                if item["source"] == "Anchor_A":
                    products["single" if case["case"] == "single" else "shared"] = {
                        role: evidence["path"] for role, evidence in item["files"].items()}
        return {**products, "all_files": all_files}

    def compare(self, plan=None):
        with redirect_stdout(io.StringIO()):
            return STAGE4.compare_files(plan or self.plan())

    def test_actual_cli_skip_uses_backups_preserves_both_originals(self):
        before = [STAGE4.protected_snapshot(work) for work in self.cases]
        for case in (self.single, self.shared):
            result = STAGE4.skip_completed(case, self.output)
            self.assertEqual(result["summary"]["datasets_needing_work"], 0)
            self.assertEqual(result["summary"]["target_attempts"], 0)
            self.assertNotIn("--submit", result["command"])
            self.assertNotIn("--prepare-download", result["command"])
        self.assertEqual(before, [STAGE4.protected_snapshot(work) for work in self.cases])

    def test_equal_numeric_products_pass_even_when_hdf5_layout_hash_differs(self):
        plan = self.plan()
        path = plan["shared"]["ds"]
        with h5py.File(path, "r+") as handle:
            values = handle["flux"][...]
            del handle["flux"]
            handle.create_dataset("flux", data=values, compression="gzip")
        for evidence in plan["all_files"]:
            if evidence["path"] == path:
                evidence["sha256"] = digest(path)
        result = self.compare(plan)
        self.assertEqual(result["status"], "PASS", result)
        self.assertEqual(len(result["checks"][0]["files_verified"]), 12)

    def test_flux_difference_needs_review_without_arbitrary_tolerance(self):
        plan = self.plan()
        with h5py.File(plan["shared"]["ds"], "r+") as handle:
            handle["flux"][0, 0, 0, 0] += 0.00001
        for evidence in plan["all_files"]:
            if evidence["path"] == plan["shared"]["ds"]:
                evidence["sha256"] = digest(evidence["path"])
        result = self.compare(plan)
        self.assertEqual(result["status"], "REVIEW")
        self.assertGreater(result["checks"][1]["arrays"]["flux"]["rms_difference"], 0)

    def test_changed_stage3_product_is_failure(self):
        plan = self.plan()
        with h5py.File(plan["shared"]["ds"], "r+") as handle:
            handle["frequency"][0] += 1
        result = self.compare(plan)
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["checks"][0]["status"], "FAIL")
        self.assertEqual(result["checks"][1]["status"], "FAIL")

    def test_cutout_wcs_mismatch_is_failure(self):
        plan = self.plan()
        path = plan["shared"]["stokes_i"]
        with fits.open(path, mode="update") as handle:
            handle[0].header["CRVAL1"] += 0.1
        for evidence in plan["all_files"]:
            if evidence["path"] == path:
                evidence["sha256"] = digest(path)
        result = self.compare(plan)
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["checks"][2]["status"], "FAIL")

    def test_sif_reader_keeps_failure_details_and_binds_inputs_readonly(self):
        plan = self.plan()
        with h5py.File(plan["shared"]["ds"], "r+") as handle:
            handle["flux"][0, 0, 0, 0] += 1
        def fake_container(command, **kwargs):
            for work in self.cases:
                work = work.resolve()
                self.assertIn(f"{work}:{work}:ro", command)
            path = Path(command[-1])
            result = self.compare(read_json(path))
            write_json(path.with_name("comparison.json"), result)
            return SimpleNamespace(returncode=STAGE4.EXIT[result["status"]])
        with patch("askap_auto.science.run_process", fake_container):
            result = STAGE4.container_comparison(self.single, self.shared, self.output)
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["details"]["checks"][0]["status"], "FAIL")

    def test_complex_nan_does_not_hide_other_component_or_mask_differences(self):
        a = np.array([complex(np.nan, 1.), 2+3j])
        for b in (np.array([complex(np.nan, 2.), 2+3j]),
                  np.array([complex(1., np.nan), 2+3j])):
            result = STAGE4.array_statistics(a, b)
            self.assertFalse(result["exact_equal"])
            self.assertEqual(result["unequal_elements"], 1)
        self.assertTrue(STAGE4.array_statistics(a, a.copy())["exact_equal"])

    def test_identity_or_stale_success_pointer_is_rejected(self):
        modified = copy.deepcopy(self.shared)
        anchor = next(t for t in modified["targets"] if t["source"]["name"] == "Anchor_A")
        anchor["source"]["pmra_cosdec"] += 1
        with self.assertRaisesRegex(AssertionError, "Anchor input differs"):
            STAGE4.compare_identity(self.single, modified)
        state = fixtures.State(self.cases[1])
        with state.connect() as db:
            db.execute("UPDATE targets SET complete_attempt=NULL WHERE observation_key=?",
                       (STAGE4.OBSERVATION_KEY,))
        with self.assertRaisesRegex(AssertionError, "Successful pointer"):
            STAGE4.load_case(self.cases[1], "shared3")

    def test_ms_fallback_dispatch_uses_real_helper_and_accepts_utc_epoch(self):
        identity = STAGE4.compare_identity(self.single, self.shared)
        calls = []
        def fake_command(executor, arguments, cwd, log, **kwargs):
            calls.append(arguments)
            return json.dumps({"mjd": identity["time_mjd"] + 1/86400, "scale": "utc",
                               "source": "MS", "ms_time_unit": "s", "ms_time_scale": "utc"})
        with patch.object(Executor, "command", fake_command):
            result = STAGE4.fallback_time(self.single, self.shared, identity, self.output)
        self.assertEqual(len(calls), 1)  # valid CASDA time did not open MS
        self.assertTrue(calls[0][1].endswith("/askap_auto/ms_time.py"))
        self.assertEqual(calls[0][2], self.single["report"]["retained_inputs"]["raw_ms"])
        self.assertEqual(result["status"], "PASS")
        self.assertAlmostEqual(result["delta_seconds"], 1., places=5)

    def test_missing_real_ms_time_fails_and_outside_interval_needs_review(self):
        identity = STAGE4.compare_identity(self.single, self.shared)
        with patch.object(Executor, "command", side_effect=RuntimeError("MS unavailable")):
            with self.assertRaisesRegex(AssertionError, "Real-SIF MS fallback failed"):
                STAGE4.fallback_time(self.single, self.shared, identity, self.output)
        returned = json.dumps({"mjd": identity["time_mjd"] - 1, "scale": "utc", "source": "MS"})
        with patch.object(Executor, "command", return_value=returned):
            result = STAGE4.fallback_time(self.single, self.shared, identity, self.output)
        self.assertEqual(result["status"], "REVIEW")

    def test_accounting_missing_or_failed_parent_is_not_pass(self):
        missing = STAGE4.accounting(self.single, self.output)
        self.assertEqual(missing["status"], "REVIEW")
        case = {**self.single, "jobs": [{"scheduler_id": "12345"}]}
        def accounting(stdout):
            return lambda *a, **k: SimpleNamespace(returncode=0, stdout=stdout, stderr="")
        result = STAGE4.accounting(case, self.output, accounting("12345|COMPLETED|0:0|00:10:00|8|40G||\n"))
        self.assertEqual(result["status"], "REVIEW")  # no MaxRSS step information
        result = STAGE4.accounting(case, self.output, accounting(
            "12345|COMPLETED|0:0|00:10:00|8|40G||\n12345.batch|COMPLETED|0:0|00:10:00|8|40G|2000K|\n"))
        self.assertEqual(result["status"], "PASS")
        receipt = self.output / "state/jobs/fixture.submission.json"
        write_json(receipt, {"job_id": "12345"})
        result = STAGE4.accounting({"work": str(self.output), "case": "single", "jobs": [{"id": "fixture"}]},
                                  self.output, accounting(
            "12345|COMPLETED|0:0|00:10:00|8|40G||\n12345.batch|COMPLETED|0:0|00:10:00|8|40G|2000K|\n"))
        self.assertEqual(result["scheduler_ids"], ["12345"])
        with self.assertRaisesRegex(AssertionError, "not successful"):
            STAGE4.accounting(case, self.output, accounting("12345|FAILED|1:0|00:10:00|8|40G||\n"))

    def test_full_driver_and_original_immutability_with_only_site_boundaries_mocked(self):
        before = [STAGE4.protected_snapshot(work) for work in self.cases]
        def comparison(single, shared, output):
            result = self.compare()
            return {"status": result["status"], "details": result}
        def time_command(*args, **kwargs):
            return json.dumps({"mjd": 59000., "scale": "utc", "source": "MS",
                               "ms_time_unit": "s", "ms_time_scale": "utc"})
        with (patch.object(STAGE4, "container_comparison", comparison),
              patch.object(STAGE4, "accounting", return_value={"status": "PASS", "fixture": True}),
              patch.object(Executor, "command", time_command), redirect_stdout(io.StringIO())):
            result = STAGE4.main(["--single-work", str(self.cases[0]), "--shared-work", str(self.cases[1]),
                                  "--output-root", str(self.output)])
        report = read_json(next(self.output.glob("run-*/report.json")))
        self.assertEqual(result, 0, report)
        self.assertEqual(len(report["checks"]), 11)
        self.assertEqual(before, [STAGE4.protected_snapshot(work) for work in self.cases])

    def test_driver_detects_original_metadata_change(self):
        def changed(*args):
            write_json(self.cases[0] / "state/unexpected.json", {"changed": True})
            return {"status": "PASS", "fixture": True}
        with (patch.object(STAGE4, "container_comparison", changed),
              patch.object(STAGE4, "accounting", return_value={"status": "PASS", "fixture": True}),
              patch.object(STAGE4, "fallback_time", return_value={"status": "PASS", "fixture": True}),
              redirect_stdout(io.StringIO())):
            result = STAGE4.main(["--single-work", str(self.cases[0]), "--shared-work", str(self.cases[1]),
                                  "--output-root", str(self.output)])
        report = read_json(next(self.output.glob("run-*/report.json")))
        self.assertEqual(result, 1)
        self.assertEqual(report["checks"][-1]["status"], "FAIL")
        self.assertIn("Original Stage3 state/metadata changed", report["checks"][-1]["error"])


if __name__ == "__main__":
    unittest.main()
