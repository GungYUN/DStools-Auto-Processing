"""Signed CASDA beam longitudes must not hide otherwise eligible observations."""
from contextlib import redirect_stdout
import copy
import csv
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
from astropy.table import Column, MaskedColumn, Table

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from askap_auto import catalogue, cli, query, settings
from validate_query_stage2 import audit_snapshot
from template_settings import template_settings


def sb54095_rows():
    """Relevant fields from the two live CASDA records that exposed the bug."""
    return Table({
        "obs_id": ["ASKAP-54095"] * 2,
        "filename": [f"scienceData.EMU_1342-60.SB54095.EMU_1342-60.{beam}_averaged_cal.leakage.ms.tar"
                     for beam in ("beam17", "beam23")],
        "obs_publisher_did": ["scan-288784-475595", "scan-288790-475601"],
        "s_ra": [-150.15109596114843, -149.35371059766487],
        "s_dec": [-60.647926389274716, -59.83942564456561],
        "em_min": [0.27542031062623434] * 2,
        "em_max": [0.3745108377212323] * 2,
        "t_exptime": [36020.99999967031] * 2,
        "t_min": [60238.03449074074] * 2,
        "t_max": [60238.45140046296] * 2,
        "quality_level": ["GOOD"] * 2,
        "access_estsize": [22298810, 22302900],
        "obs_release_date": ["2024-01-15T01:08:15.261Z"] * 2,
    })


class SignedRATests(unittest.TestCase):
    def setUp(self):
        self.settings = template_settings()
        self.settings["ra_unit"] = "hourangle"
        self.source = catalogue.make_source("SignedRA", "14:03:49.40535", "-60:22:22.9266", self.settings)

    def candidates(self, rows, released=None, effective=None, source=None):
        return query.candidates(rows, source or self.source, effective or self.settings,
                                released=range(len(rows)) if released is None else released)

    def test_sb54095_matches_positive_ra_and_selects_beam23(self):
        rows = sb54095_rows()
        before = rows.copy()
        positive = rows.copy()
        positive["s_ra"] = np.asarray(positive["s_ra"]) + 360.0
        result = self.candidates(rows)
        for signed, unsigned in zip(result, self.candidates(positive)):
            self.assertEqual({k: v for k, v in signed.items() if k != "ra"},
                             {k: v for k, v in unsigned.items() if k != "ra"})
        np.testing.assert_array_equal([obs["ra"] for obs in result], rows["s_ra"])
        self.assertTrue(all(obs["eligible"] for obs in result))
        self.assertEqual([obs["beam"] for obs in result if obs["default_selected"]], ["beam23"])
        self.assertAlmostEqual(result[0]["separation_deg"], 0.6103132216384917, places=10)
        self.assertAlmostEqual(result[1]["separation_deg"], 0.5554650733494239, places=10)
        for name in rows.colnames:
            np.testing.assert_array_equal(rows[name], before[name])

    def test_ra_wrap_boundary_keeps_nearby_positions_close(self):
        effective = template_settings()
        source = catalogue.make_source("ZeroCrossing", 359.99, -20.0, effective)
        rows = sb54095_rows()[:1]
        rows["s_dec"] = [-20.0]
        rows["s_ra"] = [-0.02]
        observation = self.candidates(rows, effective=effective, source=source)[0]
        self.assertEqual(observation["ra"], -0.02)
        self.assertTrue(observation["eligible"])
        self.assertLess(observation["separation_deg"], 0.011)
        for value in (-360.0, 0.0, 360.0, -1e-15):
            with self.subTest(ra=value):
                rows["s_ra"] = [value]
                observation = self.candidates(rows, effective=effective, source=source)[0]
                self.assertEqual(observation["ra"], value)
                self.assertLess(observation["separation_deg"], 0.011)
                self.assertTrue(observation["eligible"])

    def test_missing_nonfinite_and_masked_ra_remain_invalid(self):
        for value in (None, np.nan, np.inf, -np.inf, "not-a-coordinate"):
            with self.subTest(ra=value):
                rows = sb54095_rows()[:1]
                rows["s_ra"] = Column([value], dtype=object)
                observation = self.candidates(rows)[0]
                self.assertIsNone(observation["ra"])
                self.assertEqual(observation["reason"], "invalid_coordinates")
                self.assertFalse(observation["eligible"])
                self.assertIsNone(observation["separation_deg"])
        rows = sb54095_rows()[:1]
        rows["s_ra"] = MaskedColumn([-150.15109596114843], mask=[True])
        with np.testing.suppress_warnings() as warnings:
            warnings.filter(UserWarning, "Warning: converting a masked element to nan.")
            observation = self.candidates(rows)[0]
        self.assertIsNone(observation["ra"])
        self.assertFalse(observation["eligible"])

    def test_invalid_declination_remains_invalid(self):
        for value in (None, np.nan, np.inf, -np.inf, -90.01, 90.01):
            with self.subTest(dec=value):
                rows = sb54095_rows()[:1]
                rows["s_dec"] = Column([value], dtype=object)
                observation = self.candidates(rows)[0]
                self.assertEqual(observation["reason"], "invalid_coordinates")
                self.assertFalse(observation["eligible"])

    def test_signed_ra_preserves_other_filters(self):
        for reason in ("quality", "unreleased", "size_limit", "exposure", "excluded_sb", "outside_half_power_radius"):
            with self.subTest(reason=reason):
                rows = sb54095_rows()
                effective = copy.deepcopy(self.settings)
                released = {0, 1}
                if reason == "quality":
                    rows["quality_level"] = ["BAD"] * 2
                elif reason == "unreleased":
                    released = set()
                elif reason == "size_limit":
                    rows["access_estsize"] = [effective["max_file_size_kb"] + 1] * 2
                elif reason == "exposure":
                    rows["t_exptime"] = [500.0] * 2
                elif reason == "excluded_sb":
                    effective["excluded_sbs"] = [54095]
                else:
                    rows["s_dec"] = [60.0] * 2
                for observation in self.candidates(rows, released, effective):
                    self.assertEqual(observation["reason"], reason)
                    self.assertFalse(observation["eligible"])
                    self.assertFalse(observation["default_selected"])

    def test_single_cli_preserves_signed_ra_and_raw_cache(self):
        rows = sb54095_rows()
        original_query_one = query.query_one
        def fetch_query(source, effective, directory):
            return original_query_one(source, effective, directory,
                                      fetch=lambda *_: rows.copy(), release_check=lambda _: {0, 1})
        with tempfile.TemporaryDirectory() as directory:
            with patch("askap_auto.cli.query_one", side_effect=fetch_query), redirect_stdout(io.StringIO()):
                snapshot = cli.main(["query", "--ra", "14:03:49.40535", "--dec=-60:22:22.9266",
                                     "--ra-unit", "hourangle", "--work-dir", directory], single=True)
            cached = Table.read(snapshot["cache"], format="ascii.ecsv")
            np.testing.assert_array_equal(cached["s_ra"], rows["s_ra"])
            with (Path(directory) / "query/observations.csv").open() as handle:
                exported = list(csv.DictReader(handle))
            self.assertEqual(len(exported), 2)
            np.testing.assert_array_equal([float(row["Beam_RA_deg"]) for row in exported], rows["s_ra"])
            self.assertTrue(all(row["Eligible"] == "True" for row in exported))
            self.assertEqual([row["Beam"] for row in exported if row["Default_Selected"] == "True"], ["beam23"])
            with patch("askap_auto.query.public_indices", return_value={0, 1}):
                audit_snapshot(snapshot, self.settings)

    def test_batch_index_keeps_selected_sb_with_negative_ra(self):
        with tempfile.TemporaryDirectory() as directory:
            effective = copy.deepcopy(self.settings)
            effective.update(work_root=directory, query_delay_seconds=0)
            index = query.query_catalogue([self.source], effective,
                                          fetch=lambda *_: sb54095_rows(), release_check=lambda _: {0, 1})
            entry = index["sources"][0]
            self.assertEqual(entry["status"], "available")
            self.assertEqual(len(entry["observations"]), 1)
            observation = entry["observations"][0]
            self.assertEqual(observation["sb_id"], "SB54095")
            self.assertEqual(observation["beam"], "beam23")
            self.assertAlmostEqual(observation["ra"], -149.35371059766487)


if __name__ == "__main__":
    unittest.main()
