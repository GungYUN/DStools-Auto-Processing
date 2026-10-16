"""Exercise the stage-2 acceptance script with offline CASDA metadata fixtures.

The normal acceptance script uses live CASDA. Only these regression tests replace
its network boundary; catalogue ingestion, CLI commands, snapshots, plans, SQLite
state and generated Slurm scripts run unchanged.
"""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import copy
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from astropy.table import Table

CODE_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(CODE_ROOT))
from askap_auto import query
from askap_auto.state import State

SCRIPT_PATH = Path(__file__).with_name("validate_query_stage2.py")
SPEC = importlib.util.spec_from_file_location("stage2_acceptance_script", SCRIPT_PATH)
STAGE2 = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(STAGE2)


def casda_rows(source, multiple_beams=True):
    """One exact dataset shared by the first three catalogue positions."""
    if source["ra"] < 30:
        records = [(67772, "beam18", 24.20, 9.55, True)]
    else:
        records = [
            (74372, "beam28", 84.54, -2.50, True),
            (80000, "beam10", 84.60, -2.60, True),
            # Closest to the anchor but unreleased: it must not win selection.
            (74372, "beam26", 84.5194, -2.2313, False),
        ]
        if multiple_beams:
            records.append((74372, "beam27", 85.10, -2.30, True))
    return Table(
        {
            "obs_id": [f"ASKAP-{sb}" for sb, *_ in records],
            "filename": [f"science.SB{sb}.{beam}.ms.tar" for sb, beam, *_ in records],
            "obs_publisher_did": [f"ivo://offline/SB{sb}/{beam}" for sb, beam, *_ in records],
            "s_ra": [ra for _, _, ra, _, _ in records],
            "s_dec": [dec for _, _, _, dec, _ in records],
            "em_min": [0.21] * len(records),
            "em_max": [0.30] * len(records),
            "t_exptime": [600.0] * len(records),
            "t_min": [59000.0] * len(records),
            "t_max": [59000.1] * len(records),
            "quality_level": ["GOOD"] * len(records),
            "access_estsize": [100.0] * len(records),
            "fixture_public": [public for *_, public in records],
            "obs_release_date": ["2020-01-01T00:00:00.000000" if public else ""
                                 for *_, public in records],
        }
    )


class Stage2ValidationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.container = self.root / "accepted.sif"
        self.container.write_bytes(b"Offline fixture for SIF fingerprint only")
        self.output = self.root / "results"
        self.calls = []

    def run_acceptance(self, *, multiple_beams=True, fail_anchor=False):
        def fetch(source, settings):
            self.calls.append(source["name"])
            if fail_anchor and source["processing_index"] == 1:
                raise RuntimeError("Synthetic TAP connection failed")
            return casda_rows(source, multiple_beams=multiple_beams)

        def release_check(rows):
            return [index for index, public in enumerate(rows["fixture_public"]) if public]

        def batch_query(sources, settings, *args, **kwargs):
            settings = {**settings, "query_delay_seconds": 0}
            return query.query_catalogue(
                sources, settings, *args, fetch=fetch, release_check=release_check, **kwargs
            )

        def single_query(source, settings, directory):
            return query.query_one(
                source, settings, directory, fetch=fetch, release_check=release_check
            )

        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            patch("askap_auto.cli.query_catalogue", batch_query),
            patch("askap_auto.cli.query_one", single_query),
            patch("askap_auto.query.public_indices", release_check),
            patch("askap_auto.archive.download", side_effect=AssertionError("Download forbidden")) as download,
            patch("subprocess.run", side_effect=AssertionError("Subprocess forbidden")) as subprocess_run,
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            result = STAGE2.main(
                [
                    "--output-root", str(self.output),
                    "--container", str(self.container),
                    "--python", sys.executable,
                ]
            )
        download.assert_not_called()
        subprocess_run.assert_not_called()
        reports = list(self.output.glob("run-*/report.json"))
        self.assertEqual(len(reports), 1)
        report = json.loads(reports[0].read_text())
        return result, report, stdout.getvalue(), stderr.getvalue()

    def test_complete_cli_query_and_plans_with_shared_and_multibeam_data(self):
        result, report, stdout, stderr = self.run_acceptance()
        self.assertEqual(result, 0, stderr)
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(report["not_covered"], [])
        self.assertEqual(len(self.calls), 5, "Four batch queries and one fresh single query; cached query must not fetch")
        self.assertIn("STAGE2 PASS", stdout)
        root = Path(report["work_dir"])
        batch = State(root / "batch_range")
        batch_run = batch.active_run()
        self.assertEqual(len(batch_run["tasks"]), 1)
        task = batch_run["tasks"][0]
        self.assertEqual((task["observation"]["sb_id"], task["observation"]["beam"]), ("SB74372", "beam28"))
        self.assertEqual({target["source"]["processing_index"] for target in task["targets"]}, {1, 2, 3})
        self.assertTrue(task["shared"])
        self.assertEqual(batch_run["selection"]["end"], 1)
        nearest = State(root / "single_nearest").active_run()
        expanded = State(root / "single_expanded").active_run()
        nearest_beams = {(task["observation"]["sb_id"], task["observation"]["beam"]) for task in nearest["tasks"]}
        expanded_beams = {(task["observation"]["sb_id"], task["observation"]["beam"]) for task in expanded["tasks"]}
        self.assertEqual(nearest_beams, {("SB74372", "beam28"), ("SB80000", "beam10")})
        self.assertEqual(expanded_beams, nearest_beams | {("SB74372", "beam27")})
        self.assertNotIn(("SB74372", "beam26"), expanded_beams)
        STAGE2.require_no_execution([root / name for name in ("batch_range", "single_nearest", "single_expanded")])

    def test_missing_multiple_eligible_beams_is_incomplete(self):
        result, report, stdout, stderr = self.run_acceptance(multiple_beams=False)
        self.assertEqual(result, 2, stderr)
        self.assertEqual(report["status"], "INCOMPLETE")
        self.assertEqual([item["check"] for item in report["not_covered"]], ["All-beams expansion"])
        self.assertIn("STAGE2 INCOMPLETE", stdout)
        self.assertNotIn("STAGE2 PASS", stdout)
        self.assertFalse((Path(report["work_dir"]) / "single_expanded").exists())

    def test_tap_failure_is_failure_instead_of_zero_observations(self):
        result, report, stdout, stderr = self.run_acceptance(fail_anchor=True)
        self.assertEqual(result, 1)
        self.assertEqual(report["status"], "FAIL")
        self.assertIn("Real CASDA query failure (not zero observations)", report["error"])
        self.assertIn("Synthetic TAP connection failed", report["error"])
        self.assertIn("STAGE2 FAIL", stdout)
        self.assertIn("FAIL:", stderr)
        state = State(Path(report["work_dir"]) / "batch_range")
        self.assertIsNone(state.active_run())
        with state.connect() as db:
            query_states = {row["query_status"] for row in db.execute("SELECT query_status FROM sources")}
        self.assertIn("query_failed", query_states)
        self.assertNotIn("no_observations", query_states)

    def test_wrong_but_consistent_separation_is_detected_from_raw_coordinates(self):
        effective = STAGE2.template_settings()
        source = STAGE2.catalogue.make_source("Anchor A", 84.5194, -2.2313, effective)
        rows = casda_rows(source)
        public = [index for index, value in enumerate(rows["fixture_public"]) if value]
        snapshot = query.query_one(source, effective, self.root / "query",
                                   fetch=lambda *_: rows, release_check=lambda _: public)
        with patch("askap_auto.query.public_indices", lambda _: public):
            STAGE2.audit_snapshot(snapshot, effective)
            damaged = copy.deepcopy(snapshot)
            observation = next(obs for obs in damaged["observations"] if obs["default_selected"])
            observation["separation_deg"] += 0.05
            observation["within_radius"] = observation["separation_deg"] <= observation["radius_deg"]
            with self.assertRaisesRegex(AssertionError, "Angular separation differs"):
                STAGE2.audit_snapshot(damaged, effective)


if __name__ == "__main__":
    unittest.main()
