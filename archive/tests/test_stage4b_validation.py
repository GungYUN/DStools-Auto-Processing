"""Real SQLite/files/receipts; simulate only the SSH/rsync transport."""
from contextlib import redirect_stdout
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest

CODE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(CODE))
import pull_products
from askap_auto.common import write_json
from askap_auto.state import State

SPEC = importlib.util.spec_from_file_location("stage4b_test_driver", Path(__file__).with_name("validate_transfer_stage4b.py"))
STAGE4B = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(STAGE4B)


class LocalRemote:
    def __init__(self):
        self.fail_receipt = False
        self.paths = []
        self.fail_import = False

    def command(self, argv):
        if self.fail_import and STAGE4B.REMOTE_IMPORT in argv:
            raise RuntimeError("fixture receipt import failed")
        if argv[0] == "/bin/bash":
            script = "module() { :; }\n" + argv[-1]
            return subprocess.run(["/bin/bash", "-c", script], capture_output=True, text=True, check=True).stdout
        return subprocess.run([sys.executable] + list(argv[1:]), capture_output=True, text=True, check=True).stdout

    def inventory(self, products, work):
        return json.loads(self.command([sys.executable, "-c", pull_products.REMOTE_INVENTORY, products, work]))

    def pull(self, products, destination, paths):
        self.paths.append(list(paths))
        for name in paths:
            out = Path(destination) / name
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(Path(products) / name, out)

    def receipt(self, local, work):
        if self.fail_receipt:
            raise RuntimeError("fixture receipt upload failed")
        out = Path(work) / "state/transfer_receipts" / local.name
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(local, out)


class Stage4BTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.remote_work = self.root / "ozstar"
        self.products = self.remote_work / "products"
        self.state = State(self.remote_work)
        entries = []
        obs = dict(key=STAGE4B.KEY, sb_id=74372, sb_name="SB74372_beam28", beam="beam28",
                   eligible=True, default_selected=True, frequency_min_ghz=.75,
                   frequency_max_ghz=1.05, radius_frequency_mode="central", separation_deg=.1)
        for index, name in enumerate(sorted(STAGE4B.NAMES), 1):
            sid = hashlib.sha256(name.encode()).hexdigest()
            source = dict(source_id=sid, name=name, original_name=name, processing_index=index, epoch_mjd=51544.5)
            entries.append(dict(source=source, observations=[obs], status="available"))
        self.state.sync_index(dict(revision="fixture", sources=entries))
        for entry in entries:
            source = entry["source"]
            aid = "attempt-" + str(source["processing_index"])
            version = self.products / ".versions" / source["source_id"] / STAGE4B.KEY / aid
            version.mkdir(parents=True)
            paths = {}
            for role, filename in (("ds", "SB74372_beam28.ds"), ("stokes_i", "wsclean_model/wsclean-MFS-I-image.fits"),
                                   ("stokes_v", "wsclean_model/wsclean-MFS-V-image.fits")):
                p = version / filename
                p.parent.mkdir(exist_ok=True)
                p.write_bytes((source["name"] + role).encode())
                paths[role] = str(p)
            self.state.create_attempt(source, obs, "fixture-run", {"version": "fixture"}, "f"*64,
                                      str(version), attempt_id=aid)
            self.state.complete(aid, paths)
            info = dict(source_id=source["source_id"], source_name=source["name"], observation_key=STAGE4B.KEY,
                        attempt_id=aid, sb_id=74372, beam="beam28")
            write_json(version / "observation.json", info)
            public = self.products / source["name"] / ("SB74372_beam28--" + STAGE4B.KEY[:12])
            public.parent.mkdir()
            public.symlink_to(version, target_is_directory=True)
        write_json(self.remote_work / "stage3-case.json", dict(case="shared3", planned_targets=sorted(STAGE4B.NAMES),
                   observation_key=STAGE4B.KEY, plan_run_id="fixture-run"))
        write_json(self.remote_work / "state/stage3-check.json",
                   dict(status="PASS", case_metadata={"plan_run_id": "fixture-run"}))
        self.output = self.root / "ada-test"
        self.output.mkdir()
        self.args = SimpleNamespace(host="offline.invalid", user="fixture", password_file=None,
                                    remote_python=sys.executable, host_python=sys.executable,
                                    remote_code=str(CODE), remote_work=str(self.remote_work),
                                    remote_products=str(self.products), host_modules=[])
        self.remote = LocalRemote()

    def run_driver(self):
        with redirect_stdout(io.StringIO()):
            return STAGE4B.run(self.args, self.output, self.remote)

    def test_first_repeat_receipts_and_actual_remote_state_import(self):
        before = self.remote.inventory(str(self.products), str(self.remote_work))
        report = self.run_driver()
        self.assertEqual(report["status"], "PASS", report)
        self.assertEqual(len(report["checks"]), 5)
        self.assertEqual([len(paths) for paths in self.remote.paths], [9, 0])
        self.assertEqual(STAGE4B.identity(before),
                         STAGE4B.identity(self.remote.inventory(str(self.products), str(self.remote_work))))
        for item in before:
            self.assertEqual(self.state.attempt(item["attempt_id"])["transfer_status"], "verified")
        with self.state.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], 3)

    def test_wrong_case_rejected_before_any_transfer(self):
        write_json(self.remote_work / "stage3-case.json", {"case": "single"})
        report = self.run_driver()
        self.assertEqual(report["status"], "FAIL")
        self.assertFalse(self.remote.paths)

    def test_host_module_shell_preserves_import_arguments(self):
        self.args.host_modules = ["gcc/13.3.0", "python/3.12.3"]
        report = self.run_driver()
        self.assertEqual(report["status"], "PASS", report)

    def test_missing_product_rejected_before_any_transfer(self):
        next(self.products.glob(".versions/*/*/*/*.ds")).unlink()
        report = self.run_driver()
        self.assertEqual(report["status"], "FAIL")
        self.assertFalse(self.remote.paths)

    def test_receipt_upload_failure_is_not_transfer_success(self):
        self.remote.fail_receipt = True
        report = self.run_driver()
        self.assertEqual(report["status"], "FAIL")
        self.assertIn("receipt upload failed", report["error"])
        self.assertEqual(len(self.remote.paths), 1)
        with self.state.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0], 0)

    def test_import_failure_does_not_claim_end_to_end_pass(self):
        self.remote.fail_import = True
        report = self.run_driver()
        self.assertEqual(report["status"], "FAIL")
        self.assertIn("import failed", report["error"])
        self.assertEqual(len(report["checks"]), 4)

    def test_driver_and_remote_programs_accept_python37_grammar(self):
        import ast
        ast.parse(Path(STAGE4B.__file__).read_text(), feature_version=(3, 7))
        ast.parse(STAGE4B.REMOTE_CASE, feature_version=(3, 7))
        ast.parse(STAGE4B.REMOTE_IMPORT, feature_version=(3, 7))


if __name__ == "__main__":
    unittest.main()
