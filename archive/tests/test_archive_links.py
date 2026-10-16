"""Use real tar files and system tar to verify the limited MS hard-link policy."""
from __future__ import annotations

import io
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from askap_auto import archive
from askap_auto.common import read_json, write_json
from askap_auto.tar_safety import canonical_member_name, validated_members

MS = "science.SB74372.beam28.ms"
TARGET = MS + "/ASKAP_METADATA/footprintOutput-sb74372-VAST_0535+00.txt"
LINK = MS + "/ASKAP_METADATA/footprintOutput-sb74372-src1-VAST_0535+00.txt"


def member(name, *, kind=tarfile.REGTYPE, target="", data=b"archived payload"):
    result = tarfile.TarInfo(name)
    result.type = kind
    result.linkname = target
    result.mode = 0o755 if kind == tarfile.DIRTYPE else 0o644
    result.size = len(data) if result.isfile() else 0
    return result, data


class ArchiveLinkTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.tar = self.root / "input.ms.tar"

    def create_archive(self, members):
        with tarfile.open(self.tar, "w") as handle:
            for item, data in members:
                handle.addfile(item, io.BytesIO(data) if item.isfile() else None)
        return self.tar

    def validate(self, members):
        self.create_archive(members)
        with tarfile.open(self.tar) as handle:
            return validated_members(handle, MS)

    def extract(self, members, *, retain=True):
        self.create_archive(members)
        work = self.root / "downloaded"
        write_json(work / "download-ready.json", {"key": "pilot-key", "archive": str(self.tar), "ms_name": MS})
        commands = []

        def system_tar(command):
            commands.append(list(command))
            return subprocess.run(command, check=True, capture_output=True, text=True)

        raw = archive.extract(work, {"key": "pilot-key"}, retain, system_tar)
        self.assertEqual(len(commands), 1)
        self.assertEqual(commands[0][:2], ["tar", "-xf"])
        self.assertEqual(read_json(work / "raw-ready.json")["ms"], str(raw))
        return raw

    def test_direct_internal_hardlink_is_extracted_as_same_inode(self):
        raw = self.extract([
            member(MS, kind=tarfile.DIRTYPE),
            member(MS + "/ASKAP_METADATA", kind=tarfile.DIRTYPE),
            member(TARGET, data=b"real-style footprint metadata"),
            member(LINK, kind=tarfile.LNKTYPE, target=TARGET),
            member(MS + "/table.dat", data=b"visibility data"),
        ])
        linked = raw / "ASKAP_METADATA" / Path(LINK).name
        target = raw / "ASKAP_METADATA" / Path(TARGET).name
        self.assertFalse(linked.is_symlink())
        self.assertTrue(linked.samefile(target))
        self.assertEqual(linked.read_bytes(), b"real-style footprint metadata")
        self.assertTrue(self.tar.is_file(), "Single-source archive must remain intact")

    def test_ordinary_archive_and_implicit_parent_directories_still_extract(self):
        raw = self.extract([member(MS + "/FIELD/table.dat"), member(MS + "/table.dat")], retain=False)
        self.assertTrue((raw / "FIELD/table.dat").is_file())
        self.assertFalse(self.tar.exists(), "Shared archive is released after completed extraction")

    def test_canonical_names_normalise_harmless_posix_prefixes(self):
        members = self.validate([member("./" + TARGET), member("./" + LINK, kind=tarfile.LNKTYPE, target="./" + TARGET)])
        self.assertEqual(set(members), {TARGET, LINK})
        self.assertEqual(members[LINK].linkname, "./" + TARGET, "Validation must not mutate the archived link target")
        self.assertEqual(canonical_member_name("./a//b"), "a/b")

    def test_absolute_and_parent_member_paths_are_rejected(self):
        for path in ("/" + TARGET, "../" + TARGET, MS + "/x/../table.dat"):
            with self.subTest(path=path), self.assertRaisesRegex(RuntimeError, "Unsafe tar path"):
                self.validate([member(path)])

    def test_absolute_and_parent_hardlink_targets_are_rejected(self):
        for target in ("/" + TARGET, "../" + TARGET, MS + "/ASKAP_METADATA/../table.dat"):
            with self.subTest(target=target), self.assertRaisesRegex(RuntimeError, "Unsafe tar path"):
                self.validate([member(TARGET), member(LINK, kind=tarfile.LNKTYPE, target=target)])

    def test_symbolic_link_remains_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "Symbolic archive links are unsupported"):
            self.validate([member(TARGET), member(LINK, kind=tarfile.SYMTYPE, target=Path(TARGET).name)])

    def test_dangling_hardlink_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "Dangling archive hard link"):
            self.validate([member(LINK, kind=tarfile.LNKTYPE, target=TARGET)])

    def test_hardlink_chain_and_directory_target_are_rejected(self):
        for members in (
            [member(TARGET), member(LINK, kind=tarfile.LNKTYPE, target=TARGET),
             member(MS + "/alias", kind=tarfile.LNKTYPE, target=LINK)],
            [member(TARGET, kind=tarfile.DIRTYPE), member(LINK, kind=tarfile.LNKTYPE, target=TARGET)],
        ):
            with self.subTest(members=members), self.assertRaisesRegex(RuntimeError, "target is not a regular file"):
                self.validate(members)

    def test_hardlink_target_outside_same_ms_is_rejected(self):
        for target in ("outside.txt", "other.ms/table.dat", "other/" + TARGET):
            with self.subTest(target=target), self.assertRaisesRegex(RuntimeError, "outside"):
                self.validate([member(target), member(LINK, kind=tarfile.LNKTYPE, target=target)])

    def test_duplicate_normalised_paths_are_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "Duplicate normalized tar member"):
            self.validate([member(TARGET), member("./" + TARGET)])

    def test_hardlink_or_regular_member_cannot_be_a_parent_directory(self):
        parent = MS + "/alias"
        for kind in (tarfile.LNKTYPE, tarfile.REGTYPE):
            with self.subTest(kind=kind), self.assertRaisesRegex(RuntimeError, "non-directory parent"):
                self.validate([member(TARGET), member(parent, kind=kind, target=TARGET), member(parent + "/child.dat")])

    def test_special_and_unknown_member_types_are_rejected(self):
        for kind in (tarfile.FIFOTYPE, tarfile.CHRTYPE, tarfile.BLKTYPE, b"Z"):
            with self.subTest(kind=kind), self.assertRaisesRegex(RuntimeError, "Unsupported tar member type"):
                self.validate([member(MS + "/special", kind=kind)])

    def test_unsafe_link_stops_before_system_tar_and_raw_marker(self):
        self.create_archive([member(TARGET), member(LINK, kind=tarfile.LNKTYPE, target="/outside")])
        work = self.root / "downloaded"
        write_json(work / "download-ready.json", {"key": "pilot-key", "archive": str(self.tar), "ms_name": MS})
        runner = Mock()
        with self.assertRaisesRegex(RuntimeError, "Unsafe tar path"):
            archive.extract(work, {"key": "pilot-key"}, True, runner)
        runner.assert_not_called()
        self.assertFalse((work / "raw-ready.json").exists())
        self.assertFalse((work / "raw").exists())


if __name__ == "__main__":
    unittest.main()
