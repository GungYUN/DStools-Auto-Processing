"""Release filtering must survive masked TAP values and ECSV round trips.

The fake CASDA boundary intentionally uses the two filtering expressions from
astroquery 0.4.11. This keeps the regression independent of a local installation
while exercising the production public_indices function without replacing it.
"""
from __future__ import annotations

import copy
from pathlib import Path
import sys
import tempfile
from types import ModuleType
import unittest
from unittest.mock import patch

import numpy as np
from astropy.table import Column, MaskedColumn, Table

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from askap_auto.query import public_indices

NOW = "2026-10-14T00:00:00.000000"
PAST = "2020-01-01T00:00:00.000000"
FUTURE = "2099-01-01T00:00:00.000000"


class OfficialFilteringCasda:
    @staticmethod
    def filter_out_unreleased(table):
        now = NOW
        return table[(table["obs_release_date"] != "") & (table["obs_release_date"] < now)]


class PublicReleaseTests(unittest.TestCase):
    def setUp(self):
        astroquery = ModuleType("astroquery")
        astroquery.__path__ = []
        casda = ModuleType("astroquery.casda")
        casda.Casda = OfficialFilteringCasda
        astroquery.casda = casda
        fake_modules = patch.dict(sys.modules, {"astroquery": astroquery, "astroquery.casda": casda})
        fake_modules.start()
        self.addCleanup(fake_modules.stop)

    def assert_unchanged(self, table, before):
        self.assertEqual(table.colnames, before.colnames)
        self.assertEqual(table.meta, before.meta)
        for name in before.colnames:
            self.assertEqual(table[name].dtype, before[name].dtype)
            np.testing.assert_array_equal(np.ma.getdata(table[name]), np.ma.getdata(before[name]))
            np.testing.assert_array_equal(np.ma.getmaskarray(table[name]), np.ma.getmaskarray(before[name]))

    def checked_indices(self, table, expected):
        before = copy.deepcopy(table)
        try:
            result = public_indices(table)
        finally:
            self.assert_unchanged(table, before)
        self.assertEqual(result, expected)
        return result

    def table_with(self, values):
        table = Table({"filename": [f"row-{index}" for index in range(len(values))]})
        table["obs_release_date"] = values
        table.meta["origin"] = {"source": "release regression"}
        return table

    def test_unicode_empty_future_and_masked_past_are_not_public(self):
        release = MaskedColumn(
            [PAST, "", FUTURE, PAST, FUTURE, NOW],
            mask=[False, False, False, True, True, False],
            dtype="U26",
        )
        self.checked_indices(self.table_with(release), {0})

    def test_bytes_empty_future_and_masked_past_are_not_public(self):
        release = MaskedColumn(
            [PAST.encode(), b"", FUTURE.encode(), PAST.encode(), FUTURE.encode(), NOW.encode()],
            mask=[False, False, False, True, True, False],
            dtype="S26",
        )
        self.checked_indices(self.table_with(release), {0})

    def test_none_and_empty_object_values_are_not_public(self):
        release = Column([PAST, None, "", FUTURE, NOW], dtype=object)
        self.checked_indices(self.table_with(release), {0})

    def test_mixed_object_bytes_unicode_none_and_masks(self):
        release = MaskedColumn(
            [PAST, PAST.encode(), None, "", b"", FUTURE, FUTURE.encode(), PAST],
            dtype=object,
            mask=[False, False, False, False, False, False, False, True],
        )
        self.checked_indices(self.table_with(release), {0, 1})

    def test_ecsv_roundtrip_of_empty_release_preserves_public_indices(self):
        table = self.table_with(Column([PAST, "", FUTURE, NOW], dtype="U26"))
        original = self.checked_indices(table, {0})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "metadata.ecsv"
            table.write(path, format="ascii.ecsv")
            restored = Table.read(path, format="ascii.ecsv")
        self.assertTrue(np.ma.getmaskarray(restored["obs_release_date"])[1], "Fixture must expose the ECSV empty-string mask")
        self.checked_indices(restored, original)

    def test_zero_rows_with_release_column_returns_empty_set(self):
        table = Table(names=("filename", "obs_release_date"), dtype=("U32", "U26"))
        self.checked_indices(table, set())

    def test_missing_release_column_is_explicit_value_error(self):
        for table in (Table({"filename": ["row-0"]}), Table(names=("filename",), dtype=("U32",))):
            with self.subTest(rows=len(table)):
                before = copy.deepcopy(table)
                try:
                    with self.assertRaisesRegex(ValueError, "obs_release_date"):
                        public_indices(table)
                finally:
                    self.assert_unchanged(table, before)


if __name__ == "__main__":
    unittest.main()
