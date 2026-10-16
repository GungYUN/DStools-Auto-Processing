"""Retained-MS checks for the real CASDA metadata hardlink relationship.

The actual VAST names and regular/hardlink header types are reproduced below.
Payload bytes and the minimum supporting MS files are local fixtures; the other
members of the 88-member real archive are not fabricated or claimed as tested.
No CASDA, Slurm, scientific binary, or remote filesystem is used.
"""
from __future__ import annotations

from contextlib import redirect_stdout
import io
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_processing_stage3 as checker


MS_NAME = "scienceData.VAST_0535+00.SB74372.VAST_0535+00.beam28_averaged_cal.leakage.ms"
TARGET_NAME = MS_NAME + "/ASKAP_METADATA/footprintOutput-sb74372-VAST_0535+00.txt"
LINK_NAME = MS_NAME + "/ASKAP_METADATA/footprintOutput-sb74372-src1-VAST_0535+00.txt"
FOOTPRINT = b"Local footprint payload fixture\n"


def member(name, kind=tarfile.REGTYPE, content=b"", target=""):
    info = tarfile.TarInfo(name)
    info.type, info.mode, info.linkname = kind, 0o755 if kind == tarfile.DIRTYPE else 0o644, target
    info.size = len(content) if kind == tarfile.REGTYPE else 0
    return info, content


def archive_members():
    return [member(MS_NAME, tarfile.DIRTYPE),
            member(MS_NAME + "/table.dat", content=b"Unmodified visibility-table fixture"),
            member(MS_NAME + "/table.lock", content=b"Archived temporary lock"),
            member(MS_NAME + "/ASKAP_METADATA", tarfile.DIRTYPE),
            member(TARGET_NAME, content=FOOTPRINT),
            member(LINK_NAME, tarfile.LNKTYPE, target=TARGET_NAME)]


class Stage3ArchiveLinkTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.archive = self.root / "accepted.ms.tar"
        self.write_archive(archive_members())
        destination = self.root / "raw"
        destination.mkdir()
        with tarfile.open(self.archive) as handle:
            options = {"filter": "data"} if hasattr(tarfile, "data_filter") else {}
            handle.extractall(destination, **options)
        self.raw = destination / MS_NAME
        self.target = destination / TARGET_NAME
        self.link = destination / LINK_NAME
        self.assertTrue(self.target.samefile(self.link))

    def write_archive(self, members):
        with tarfile.open(self.archive, "w") as handle:
            for info, data in members:
                handle.addfile(info, io.BytesIO(data) if info.isfile() else None)

    def compare(self):
        return checker.compare_raw_archive(self.archive, self.raw, MS_NAME)

    def test_real_vast_names_and_direct_internal_hardlink_pass(self):
        with tarfile.open(self.archive) as handle:
            self.assertEqual(handle.getmember(TARGET_NAME).type, b"0")
            self.assertEqual(handle.getmember(LINK_NAME).type, tarfile.LNKTYPE)
            self.assertEqual(handle.getmember(LINK_NAME).size, 0)
        report = self.compare()
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(report["files_compared"], 3)
        self.assertEqual(len(report["hardlinks_verified"]), 1)
        evidence = report["hardlinks_verified"][0]
        self.assertEqual((evidence["member"], evidence["target"]), (LINK_NAME, TARGET_NAME))
        self.assertEqual(evidence["header_size"], 0)
        self.assertEqual(evidence["size"], len(FOOTPRINT))
        self.assertTrue(evidence["samefile"])
        self.assertEqual(evidence["sha256"], checker.fingerprint(self.target)["sha256"])

    def test_hardlink_replaced_with_identical_independent_bytes_fails(self):
        self.link.unlink()
        self.link.write_bytes(FOOTPRINT)
        self.assertEqual(self.link.read_bytes(), self.target.read_bytes())
        self.assertFalse(self.link.samefile(self.target))
        with self.assertRaisesRegex(AssertionError, "no longer shares its target inode"):
            self.compare()

    def test_target_tampering_fails_despite_preserved_hardlink(self):
        self.target.write_bytes(b"Tampered footprint payload fixture\n")
        self.assertTrue(self.link.samefile(self.target))
        with self.assertRaisesRegex(AssertionError, "Raw MS was changed after extraction"):
            self.compare()

    def test_ordinary_payload_replaced_with_symlink_fails(self):
        original = self.raw / "table.dat"
        replacement = self.root / "replacement-table.dat"
        replacement.write_bytes(original.read_bytes())
        original.unlink()
        original.symlink_to(replacement)
        with self.assertRaisesRegex(AssertionError, "symlink"):
            self.compare()

    def test_hardlink_replaced_with_symlink_to_target_fails(self):
        self.link.unlink()
        self.link.symlink_to(self.target.name)
        with self.assertRaisesRegex(AssertionError, "symlink"):
            self.compare()

    def test_directory_replaced_with_internal_symlink_fails(self):
        metadata = self.raw / "ASKAP_METADATA"
        replacement = self.raw / "moved-metadata"
        metadata.rename(replacement)
        metadata.symlink_to(replacement.name, target_is_directory=True)
        with self.assertRaisesRegex(AssertionError, "symlink"):
            self.compare()

    def test_transient_table_lock_is_still_explicitly_excluded(self):
        (self.raw / "table.lock").write_bytes(b"CASACORE changed this transient lock")
        report = self.compare()
        self.assertEqual(report["excluded_lock_files"], ["table.lock"])
        self.assertNotIn("table.lock", report["files"])
        self.assertEqual(len(report["hardlinks_verified"]), 1)

    def test_canonical_dot_and_separator_names_keep_hardlink_identity(self):
        members = archive_members()
        for info, _ in members:
            info.name = "./" + info.name.replace("/ASKAP_METADATA/", "//ASKAP_METADATA//")
            if info.islnk():
                info.linkname = "./" + TARGET_NAME
        self.write_archive(members)
        report = self.compare()
        self.assertEqual(report["hardlinks_verified"][0]["target"], TARGET_NAME)

    def test_unsafe_headers_are_rejected_before_payload_comparison(self):
        examples = {
            "absolute": archive_members() + [member("/" + MS_NAME + "/unexpected")],
            "traversal": archive_members() + [member(MS_NAME + "/../escape")],
            "symlink": archive_members() + [member(MS_NAME + "/sym", tarfile.SYMTYPE, target="table.dat")],
            "duplicate": archive_members() + [member("./" + TARGET_NAME, content=FOOTPRINT)],
            "dangling hardlink": archive_members()[:-1] + [member(LINK_NAME, tarfile.LNKTYPE, target=MS_NAME + "/missing")],
            "outside MS hardlink": archive_members()[:-1] + [member("outside.txt", content=FOOTPRINT),
                                       member(LINK_NAME, tarfile.LNKTYPE, target="outside.txt")],
            "hardlink chain": archive_members() + [member(MS_NAME + "/second-link", tarfile.LNKTYPE, target=LINK_NAME)],
            "parent is file": archive_members() + [member(MS_NAME + "/parent", content=b"x"),
                                                     member(MS_NAME + "/parent/child", content=b"x")],
            "fifo": archive_members() + [member(MS_NAME + "/fifo", tarfile.FIFOTYPE)],
        }
        for label, members in examples.items():
            with self.subTest(label=label):
                self.write_archive(members)
                with self.assertRaises(RuntimeError):
                    self.compare()

    def test_failure_terminal_output_contains_recorded_attempt_error(self):
        error = "Archive links are unsupported: " + LINK_NAME

        def failed(_work, report):
            report["attempts"] = [{"id": "failed-attempt", "status": "failed",
                                   "error_step": "extract_archive", "log": "/retained/extract.log", "error": error}]
            raise AssertionError("Attempt failed")

        output = io.StringIO()
        with patch.object(checker, "check_case", side_effect=failed), redirect_stdout(output):
            result = checker.main(["--work-dir", str(self.root / "failed-case")])
        self.assertEqual(result, 1)
        self.assertIn(error, output.getvalue())
        self.assertIn("step=extract_archive", output.getvalue())


if __name__ == "__main__":
    unittest.main()
