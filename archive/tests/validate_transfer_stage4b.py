"""Ada Stage4B pilot: real pull, zero-repeat transfer, receipts and state import.

Python >=3.7, standard library only. Uses the production pull_products module.
Only receipts and transfer/progress state are written on Ozstar. No scientific
processing, deletion or controlled corruption is performed.
"""
from __future__ import annotations
import argparse
from contextlib import redirect_stdout
import json
from pathlib import Path
import shlex
import sqlite3
import subprocess
import sys
import tempfile
from types import SimpleNamespace

CODE = Path(__file__).resolve().parents[1]
if CODE.name == "archive":
    CODE = CODE.parent
sys.path.insert(0, str(CODE))
import pull_products as pull

NAMES = {"Anchor_A", "Partner+02", "Partner-03"}
KEY = "1a0d2f532f040b634ebd43363ca54169062bf27d5e6d36e186e67f04a0eb3fe1"
REMOTE_CASE = r'''
import json,sys
from pathlib import Path
w=Path(sys.argv[1])
m=json.loads((w/'stage3-case.json').read_text())
r=json.loads((w/'state/stage3-check.json').read_text())
assert m['case']=='shared3' and r['status']=='PASS'
assert m['plan_run_id']==r['case_metadata']['plan_run_id']
print(json.dumps({'names':m['planned_targets'],'key':m['observation_key']}))
'''
REMOTE_IMPORT = r'''
import json,sys
sys.path.insert(0,sys.argv[1])
from askap_auto.state import State
s=State(sys.argv[2])
expected=json.loads(sys.argv[3]); receipt_ids=json.loads(sys.argv[4])
with s.connect() as db:
    before={t:db.execute('SELECT COUNT(*) FROM '+t).fetchone()[0] for t in ('attempts','runs','jobs')}
summary=s.export()
rows=[]
for item in expected:
    a=s.attempt(item['attempt_id'])
    assert a['status']=='complete' and a['transfer_status']=='verified'
    assert a['source_id']==item['source_id'] and a['observation_key']==item['observation_key']
    assert s.target(a['source_id'],a['observation_key'])['complete_attempt']==a['id']
    rows.append({'source':item['source_name'],'attempt_id':a['id'],'transfer_status':a['transfer_status']})
with s.connect() as db:
    for rid in receipt_ids:
        assert db.execute('SELECT 1 FROM receipts WHERE id=?',(rid,)).fetchone(), 'New receipt not imported'
    after={t:db.execute('SELECT COUNT(*) FROM '+t).fetchone()[0] for t in before}
assert before==after, 'Transfer import created scientific work'
print(json.dumps({'rows':rows,'receipt_ids_imported':receipt_ids,'science_counts':after}))
'''


def require(value, message):
    if not value:
        raise AssertionError(message)


def identity(records):
    return sorted((r["source_id"], r["observation_key"], r["attempt_id"],
                   tuple(sorted((role, f["relative_path"], f["size"], f["sha256"])
                                for role, f in r["files"].items()))) for r in records)


def validate_snapshot(records):
    require(len(records) == 3 and {r["source_name"] for r in records} == NAMES,
            "Expected exactly the three Stage3B sources")
    require(all(r["observation_key"] == KEY for r in records), "Wrong Stage3B dataset")
    for item in records:
        pull.validate_record(item)
        require(all(f["size"] > 0 for f in item["files"].values()), "Empty scientific product")
    return records


def local_check(destination, baseline, results):
    with sqlite3.connect(str(destination / "state/transfer_ledger.sqlite")) as db:
        require(db.execute("SELECT COUNT(*) FROM observations WHERE status='complete'").fetchone()[0] == 3,
                "Local ledger does not show three complete triples")
        require(db.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 9 and
                db.execute("SELECT COUNT(*) FROM files WHERE status='Y'").fetchone()[0] == 9,
                "Local ledger does not verify all nine files")
        require(db.execute("SELECT COUNT(*) FROM history WHERE status='complete'").fetchone()[0] == 2,
                "Expected two successful pulls")
    receipt_ids = []
    for result in results:
        receipt = json.loads(Path(result["receipt"]).read_text())
        require(receipt["schema"] == 2 and receipt["status"] == "complete", "Invalid local receipt")
        require(identity(receipt["observations"]) == identity(baseline), "Receipt product identities differ")
        require(all(f.get("verified") for r in receipt["observations"] for f in r["files"].values()),
                "Receipt contains unverified files")
        receipt_ids.append(receipt["id"])
    require(len(set(receipt_ids)) == 2, "Repeated pull did not issue its own receipt")
    for item in baseline:
        for f in item["files"].values():
            path = destination / f["relative_path"]
            require(path.is_file() and path.stat().st_size == f["size"] and pull.digest(path) == f["sha256"],
                    "Ada product failed final SHA/size verification: " + str(path))
        public = destination / item["public_path"]
        version = (destination / item["files"]["ds"]["relative_path"]).parent
        require(public.is_symlink() and public.resolve() == version.resolve(), "Ada published link is incorrect")
    return receipt_ids


def run(args, work, remote):
    report = {"stage": "4B", "work_dir": str(work), "checks": [], "status": "RUNNING",
              "driver_sha256": pull.digest(__file__), "pull_sha256": pull.digest(pull.__file__)}
    def passed(name, **detail):
        report["checks"].append({"name": name, "status": "PASS", **detail})
        print("PASS: " + name, flush=True)
        pull.atomic_json(work / "report.json", report)
    try:
        evidence = json.loads(remote.command([args.remote_python, "-c", REMOTE_CASE, args.remote_work]))
        require(set(evidence["names"]) == NAMES and evidence["key"] == KEY, "Not the accepted Stage3B case")
        baseline = validate_snapshot(remote.inventory(args.remote_products, args.remote_work))
        pull.atomic_json(work / "source-snapshot.json", baseline)
        passed("Stage3B inventory: three complete triples")
        destination = work / "products"
        transfer = SimpleNamespace(host=args.host, user=args.user, remote_python=args.remote_python,
                                   password_file=args.password_file, remote_work=args.remote_work,
                                   remote_products=args.remote_products, destination=str(destination), source=[])
        results = []
        for label, expected in (("first", 9), ("repeat", 0)):
            log = work / (label + "-pull.log")
            with log.open("w") as handle, redirect_stdout(handle):
                result = pull.pull(transfer, remote)
            pull.atomic_json(work / (label + "-result.json"), result)
            require(result["status"] == "complete" and result["triples"] == 3 and result["files_transferred"] == expected,
                    label + " pull returned unexpected counts; see " + str(log))
            receipt = json.loads(Path(result["receipt"]).read_text())
            require(identity(receipt["observations"]) == identity(baseline), "Source version changed between pulls")
            results.append(result)
            passed(label + " pull: " + str(expected) + " files transferred", result=result)
        receipts = local_check(destination, baseline, results)
        passed("Ada ledger, nine SHA/size checks, receipts and public links")
        expected = [{k: r[k] for k in ("source_name", "source_id", "observation_key", "attempt_id")} for r in baseline]
        command = [args.host_python, "-c", REMOTE_IMPORT, args.remote_code,
                   args.remote_work, json.dumps(expected), json.dumps(receipts)]
        if args.host_modules:
            script = ("set -e\nmodule load " + " ".join(shlex.quote(v) for v in args.host_modules) +
                      "\nexec " + " ".join(shlex.quote(v) for v in command))
            command = ["/bin/bash", "-lc", script]
        imported = json.loads(remote.command(command).strip().splitlines()[-1])
        require(len(imported["rows"]) == 3 and {r["source"] for r in imported["rows"]} == NAMES and
                all(r["transfer_status"] == "verified" for r in imported["rows"]), "Remote import incomplete")
        require(identity(validate_snapshot(remote.inventory(args.remote_products, args.remote_work))) == identity(baseline),
                "Ozstar scientific products changed during the test")
        passed("Ozstar imported both new receipts; three transfers verified", imported=imported)
        report["status"] = "PASS"
    except Exception as exc:
        report["status"] = "FAIL"
        message = str(exc)
        if isinstance(exc, subprocess.CalledProcessError):
            error_log = work / "transport-error.log"
            error_log.write_text((exc.stdout or "") + (exc.stderr or ""))
            message = "External command exited %s; see %s" % (exc.returncode, error_log)
        report["error"] = message
        print("FAIL: " + message, flush=True)
    finally:
        pull.atomic_json(work / "report.json", report)
    print("STAGE4B " + report["status"], flush=True)
    print("Report: " + str(work / "report.json"), flush=True)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="ozstar.swin.edu.au")
    parser.add_argument("--user", required=True)
    parser.add_argument("--remote-work", required=True)
    parser.add_argument("--remote-products")
    parser.add_argument("--remote-code", required=True)
    parser.add_argument("--remote-python", default="/usr/bin/python3")
    parser.add_argument("--host-python", required=True, help="Ozstar host Python for importing transfer receipts")
    parser.add_argument("--host-modules", nargs="*", default=["gcc/13.3.0", "python/3.12.3"])
    parser.add_argument("--password-file")
    parser.add_argument("--output-root", type=Path, required=True)
    args = parser.parse_args(argv)
    args.remote_products = args.remote_products or args.remote_work.rstrip("/") + "/products"
    for value in (args.remote_work, args.remote_products, args.remote_code, args.host_python, args.remote_python):
        pull.safe_path(value)
    root = args.output_root.expanduser().resolve()
    pull.safe_path(root)
    root.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="run-", dir=str(root)))
    print("Stage4B: real Ada pull + repeat + receipt import\nResults: " + str(work), flush=True)
    try:
        remote = pull.Remote(args)
    except Exception as exc:
        pull.atomic_json(work / "report.json", {"stage": "4B", "status": "FAIL", "error": str(exc)})
        print("STAGE4B FAIL: " + str(exc) + "\nReport: " + str(work / "report.json"), flush=True)
        return 1
    return 0 if run(args, work, remote)["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
