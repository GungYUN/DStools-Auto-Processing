"""Stage4A: inspect existing results; exercise skip and real-MS time fallback.

Run on a compute node with the host Python. No download, sbatch or DStools
science command is issued. Original SQLite/JSON files are opened read-only;
ordinary CLI skip checks use SQLite backups and metadata copies.
The accepted SIF supplies h5py/casacore for read-only scientific inspection.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import copy
import json
import math
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time

CODE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(CODE))
from askap_auto.common import digest, now, read_json, signature, write_json
from check_processing_stage3 import read_database, REQUIRED_STEPS
from template_settings import write_settings

EXIT = {"PASS": 0, "FAIL": 1, "REVIEW": 2}
OBSERVATION_KEY = "1a0d2f532f040b634ebd43363ca54169062bf27d5e6d36e186e67f04a0eb3fe1"
SCIENCE_KEYS = ("model", "mask_radius_arcsec", "extract_minuv_m",
                "extract_noflag", "extract_baseline_average", "crop_arcmin")


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def worst(statuses):
    return "FAIL" if "FAIL" in statuses else "REVIEW" if "REVIEW" in statuses else "PASS"


def record(report, name, function):
    try:
        value = function()
        entry = {"name": name, "status": "PASS", **(value or {})}
    except Exception as exc:
        entry = {"name": name, "status": "FAIL", "error": str(exc)}
    report["checks"].append(entry)
    print(f"{entry['status']}: {name}" + (": " + entry["error"] if "error" in entry else ""), flush=True)
    return entry


def database_snapshot(work):
    path = Path(work) / "state/sb_processing.sqlite"
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.execute("BEGIN")
        require(not db.execute("SELECT id FROM runs WHERE status != 'complete'").fetchall(),
                "Original workspace still has an active run")
        tables = ("sources", "observations", "targets", "attempts", "steps", "runs", "jobs", "receipts")
        return {name: [list(row) for row in db.execute("SELECT * FROM " + name + " ORDER BY rowid")]
                for name in tables}


def protected_snapshot(work):
    work = Path(work)
    paths = [work / "stage3-case.json", work / "state/stage3-check.json"]
    for root in (work / "query", work / "state"):
        paths += [p for p in root.rglob("*") if p.is_file() and p.suffix in {".json", ".csv", ".ecsv"}]
    return {"database": signature(database_snapshot(work)),
            "metadata": {str(p): digest(p) for p in sorted(set(paths))}}


def load_case(work, case):
    work = Path(work).resolve()
    metadata = read_json(work / "stage3-case.json")
    report = read_json(work / "state/stage3-check.json")
    require(metadata and metadata["case"] == case and report and report["status"] == "PASS",
            f"Expected a completed Stage3 {case} PASS report")
    require(metadata["observation_key"] == OBSERVATION_KEY, "Unexpected Stage3 dataset")
    require(report["case_metadata"]["plan_run_id"] == metadata["plan_run_id"], "Stage3 report is stale")
    run, status, attempts, pointers, steps, jobs = read_database(work / "state/sb_processing.sqlite",
                                                               metadata["plan_run_id"])
    count = 1 if case == "single" else 3
    require(status == "complete" and len(run["tasks"]) == 1 and len(attempts) == count,
            "Current run/database does not match completed case")
    require(all(job["status"] == "complete" for job in jobs), "Case has unfinished jobs")
    require(run["frozen"] == report["frozen"], "Report frozen settings differ from database")
    targets = run["tasks"][0]["targets"]
    require({t["source"]["name"] for t in targets} == set(metadata["planned_targets"]),
            "Unexpected target identities")
    require(len(report["products"]) == count, "Missing product triples in report")
    for target in targets:
        aid = target["attempt_id"]
        require(aid in attempts and attempts[aid]["status"] == "complete", "Target is not complete")
        require(pointers[(target["source"]["source_id"], OBSERVATION_KEY)]["complete_attempt"] == aid,
                "Successful pointer changed since Stage3")
        require(all(steps[aid].get(key, {}).get("status") == "Y" for key in REQUIRED_STEPS),
                "Completed step records differ")
        product = next(p for p in report["products"] if p["attempt_id"] == aid)
        recorded = json.loads(attempts[aid]["products"])
        require(set(product["files"]) == set(recorded) == {"ds", "stokes_i", "stokes_v"},
                "Missing DS/I/V triple")
        for role, evidence in product["files"].items():
            require(evidence["path"] == recorded[role], "Product identity changed")
            path = Path(evidence["path"])
            require(path.is_file() and path.stat().st_size == evidence["size"] > 0,
                    f"Product missing/size changed: {path}")
    cleanup = report.get("cleanup_status")
    require(cleanup in {"PASS", "WARNING"}, "Cleanup status missing or not final")
    return {"work": str(work), "case": case, "metadata": metadata, "report": report,
            "run": run, "attempts": attempts, "targets": targets, "jobs": jobs}


def compare_identity(single, shared):
    from askap_auto.catalogue import position_at
    a = next(t for t in single["targets"] if t["source"]["name"] == "Anchor_A")
    b = next(t for t in shared["targets"] if t["source"]["name"] == "Anchor_A")
    for key in ("source_id", "ra", "dec", "pmra_cosdec", "pmdec", "epoch_mjd", "epoch_scale"):
        require(a["source"][key] == b["source"][key], "Anchor input differs: " + key)
    fa, fb = single["run"]["frozen"], shared["run"]["frozen"]
    require(fa["sif_sha256"] == fb["sif_sha256"], "SIF identities differ")
    for key in SCIENCE_KEYS:
        require(fa["settings"][key] == fb["settings"][key], "Science settings differ: " + key)
    for frozen in (fa, fb):
        s = frozen["settings"]
        require(not frozen["external_files"] and not s["primary_beam"] and not s["primary_beam_by_source"],
                "This acceptance pair expects no external masks/primary-beam files")
    aa, ab = single["attempts"][a["attempt_id"]], shared["attempts"][b["attempt_id"]]
    for key in ("time_source", "time_scale", "time_mjd", "ra", "dec"):
        require(aa[key] == ab[key], "Single/shared Anchor differs: " + key)
    ra, dec, motion = position_at(a["source"], aa["time_mjd"], aa["time_scale"])
    require(motion == "Y" and abs(ra - aa["ra"]) <= 1e-10 and abs(dec - aa["dec"]) <= 1e-10,
            "Stored position differs from the proper-motion calculation")
    return {"source": a["source"], "observation": single["run"]["tasks"][0]["observation"],
            "time_mjd": aa["time_mjd"], "time_scale": aa["time_scale"], "ra": aa["ra"], "dec": aa["dec"]}


def skip_completed(case, output):
    """Run the actual CLI, with complete records preserved in an isolated backup."""
    from askap_auto.state import State
    original = Path(case["work"])
    clone = output / ("skip-" + case["case"])
    (clone / "state").mkdir(parents=True)
    with closing(sqlite3.connect((original / "state/sb_processing.sqlite").as_uri() + "?mode=ro", uri=True)) as source:
        with closing(sqlite3.connect(clone / "state/sb_processing.sqlite")) as target:
            source.backup(target)
    for name in ("workspace.json", "source.json", "input_catalogue.json", "input_catalogue.csv", "crosscatalogue.json"):
        path = original / "state" / name
        if path.is_file():
            shutil.copy2(path, clone / "state" / name)
    if case["case"] == "single":
        shutil.copytree(original / "query", clone / "query")
    s = copy.deepcopy(case["run"]["frozen"]["settings"])
    s.update(work_root=str(clone), product_root=str(clone / "products"), python=sys.executable)
    single = case["case"] == "single"
    setting_file = write_settings(clone / "config.py", s)
    before = database_snapshot(clone)
    command = [sys.executable, str(CODE / ("askap-source" if single else "askap-batch")),
               "run", "--work-dir", str(clone), "--config", str(setting_file),
               "--observation-key", OBSERVATION_KEY]
    if not single:
        command += ["--begin", "1", "--end", "1"]
    # Site ASKAP_* overrides must not alter this frozen acceptance configuration.
    env = {k: v for k, v in os.environ.items() if not k.startswith("ASKAP_")}
    result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=180)
    log = output / ("skip-" + case["case"] + ".log")
    log.write_text(result.stdout + result.stderr)
    require(result.returncode == 0, f"Normal CLI skip check failed; see {log}")
    after = database_snapshot(clone)
    for name in ("attempts", "jobs", "targets", "steps"):
        require(before[name] == after[name], "Skip check changed existing " + name)
    new = [row for row in after["runs"] if row[0] not in {r[0] for r in before["runs"]}]
    require(len(new) == 1 and new[0][2] == "complete", "Expected one completed empty plan")
    run = json.loads(new[0][1])
    require(not run["tasks"] and run["summary"]["selected_datasets"] == 1 and
            run["summary"]["datasets_needing_work"] == run["summary"]["target_attempts"] == 0,
            "Normal CLI attempted to schedule completed results")
    require(State(clone).active_run() is None, "Empty skip plan was left active")
    return {"summary": run["summary"], "log": str(log), "command": command}


def accounting(case, output, runner=subprocess.run):
    ids = []
    for job in case["jobs"]:
        saved = job.get("scheduler_id")
        if not saved:
            receipt = read_json(Path(case["work"]) / "state/jobs" / (job["id"] + ".submission.json"), {})
            saved = receipt.get("job_id")
        ids.append(str(saved or ""))
    ids = sorted(set(ids))
    if not ids or any(not value.isdigit() for value in ids):
        return {"status": "REVIEW", "reason": "Numeric scheduler_id missing; accounting not covered"}
    log = output / ("sacct-" + case["case"] + ".txt")
    try:
        result = runner(["sacct", "-j", ",".join(ids), "--noheader", "--parsable2",
                         "--format=JobID,State,ExitCode,Elapsed,AllocCPUS,ReqMem,MaxRSS"],
                        capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.write_text(str(exc) + "\n")
        return {"status": "REVIEW", "reason": "sacct could not run", "log": str(log)}
    log.write_text(result.stdout + result.stderr)
    if result.returncode:
        return {"status": "REVIEW", "reason": "sacct unavailable; inspect log", "log": str(log)}
    rows = [line.strip().split("|") for line in result.stdout.splitlines() if line.strip()]
    parents = {row[0]: row for row in rows if len(row) >= 7 and row[0] in ids}
    if set(parents) != set(ids):
        return {"status": "REVIEW", "reason": "Accounting records not yet available", "log": str(log)}
    require(all(row[1] == "COMPLETED" and row[2] == "0:0" for row in parents.values()),
            f"Stage3 Slurm state/exit is not successful; see {log}")
    rss = [row[6] for row in rows if len(row) >= 7 and row[0].endswith(".batch") and row[6]]
    return {"status": "PASS" if rss else "REVIEW", "scheduler_ids": ids,
            "rows": rows, "log": str(log), "maxrss_available": bool(rss)}


def array_statistics(a, b):
    """Exact comparison plus diagnostics; no universal scientific tolerance."""
    import numpy as np
    require(a.shape == b.shape and a.size > 0, f"Array shapes differ/empty: {a.shape}, {b.shape}")
    require(a.dtype.kind in "biufc" and b.dtype.kind in "biufc", "Expected numeric science arrays")
    count = equal = finite = masks = 0
    delta2 = reference2 = maximum = 0.0
    rows = max(1, 262144 // max(1, math.prod(a.shape[1:])))
    slices = [()] if not a.shape else (slice(i, min(i + rows, a.shape[0])) for i in range(0, a.shape[0], rows))
    for selection in slices:
        x, y = np.asarray(a[selection]), np.asarray(b[selection])
        real_pairs = [(x.real, y.real)]
        if np.iscomplexobj(x) or np.iscomplexobj(y):
            real_pairs.append((x.imag, y.imag))
        same_masks = np.ones(x.shape, dtype=bool)
        same_values = np.ones(x.shape, dtype=bool)
        for left, right in real_pairs:
            same_masks &= (np.isnan(left) == np.isnan(right)) & (np.isposinf(left) == np.isposinf(right)) & (np.isneginf(left) == np.isneginf(right))
            same_values &= (left == right) | (np.isnan(left) & np.isnan(right))
        valid = np.isfinite(x) & np.isfinite(y)
        count += x.size
        masks += int(np.count_nonzero(~same_masks))
        equal += int(np.count_nonzero(same_values))
        finite += int(np.count_nonzero(valid))
        if np.any(valid):
            left = x[valid].astype(np.complex128 if np.iscomplexobj(x) or np.iscomplexobj(y) else np.float64)
            right = y[valid].astype(left.dtype)
            difference = np.abs(left - right)
            maximum = max(maximum, float(np.max(difference)))
            delta2 += float(np.sum(difference ** 2))
            reference2 += float(np.sum(np.abs(left) ** 2))
    return {"shape": list(a.shape), "single_dtype": str(a.dtype), "shared_dtype": str(b.dtype),
            "elements": count, "finite_pairs": finite, "nonfinite_pattern_differences": masks,
            "unequal_elements": count - equal, "exact_equal": equal == count and a.dtype == b.dtype,
            "max_abs_difference": maximum if finite else None,
            "rms_difference": math.sqrt(delta2 / finite) if finite else None,
            "single_rms": math.sqrt(reference2 / finite) if finite else None,
            "relative_l2": math.sqrt(delta2 / reference2) if reference2 > 0 else None}


def compare_files(plan):
    """Executed inside the accepted SIF, not with an extra host h5py install."""
    import h5py
    import numpy as np
    from astropy.io import fits
    from astropy.wcs import WCS
    result = {"checks": []}
    def hashes():
        checked = []
        for evidence in plan["all_files"]:
            require(digest(evidence["path"]) == evidence["sha256"], "Product changed since Stage3: " + evidence["path"])
            checked.append(evidence["path"])
        return {"files_verified": checked}
    record(result, "all twelve Stage3 product fingerprints", hashes)

    def ds():
        with h5py.File(plan["single"]["ds"], "r") as a, h5py.File(plan["shared"]["ds"], "r") as b:
            names = {"time", "frequency", "uvdist", "flux"}
            require(set(a) == set(b) == names, "Unexpected DS datasets")
            stats = {key: array_statistics(a[key], b[key]) for key in sorted(names)}
            for key in ("time", "frequency", "uvdist"):
                require(stats[key]["exact_equal"] and stats[key]["finite_pairs"] > 0,
                        "DS coordinate/baseline axis differs or has no finite data: " + key)
            require(all(stats[key]["finite_pairs"] == stats[key]["elements"] for key in ("time", "frequency")),
                    "DS time/frequency axis contains nonfinite data")
            attributes = {key: {"single": repr(a.attrs.get(key)), "shared": repr(b.attrs.get(key))}
                          for key in set(a.attrs) | set(b.attrs)
                          if key not in a.attrs or key not in b.attrs or
                          not np.array_equal(a.attrs[key], b.attrs[key])}
            return {"status": "PASS" if stats["flux"]["exact_equal"] and stats["flux"]["finite_pairs"] > 0 and not attributes else "REVIEW",
                    "arrays": stats, "attribute_differences": attributes}
    record(result, "Anchor DS numeric comparison", ds)

    for role in ("stokes_i", "stokes_v"):
        def image(role=role):
            with fits.open(plan["single"][role], mode="readonly", memmap=True) as a, fits.open(plan["shared"][role], mode="readonly", memmap=True) as b:
                wa, wb = WCS(a[0].header).celestial.to_header(), WCS(b[0].header).celestial.to_header()
                require(dict(wa) == dict(wb), "Cutout celestial WCS differs")
                values = array_statistics(a[0].data, b[0].data)
                keys = ("BUNIT", "BMAJ", "BMIN", "BPA", "DATE-OBS", "MJD-OBS", "RESTFRQ", "SPECSYS")
                differences = {k: [repr(a[0].header.get(k)), repr(b[0].header.get(k))]
                               for k in keys if a[0].header.get(k) != b[0].header.get(k)}
                return {"status": "PASS" if values["exact_equal"] and values["finite_pairs"] > 0 and not differences else "REVIEW",
                        "array": values, "header_differences": differences}
        record(result, "Anchor " + role + " cutout comparison", image)
    result["status"] = worst([c["status"] for c in result["checks"]])
    return result


def container_comparison(single, shared, output):
    from askap_auto.science import container_command, run_process
    products = {}
    all_files = []
    for case in (single, shared):
        for item in case["report"]["products"]:
            all_files.extend(item["files"].values())
            if item["source"] == "Anchor_A":
                products[case["case"]] = {role: value["path"] for role, value in item["files"].items()}
    plan = {"all_files": all_files, "single": products["single"], "shared": products["shared3"]}
    path = output / "comparison-plan.json"
    write_json(path, plan)
    s = inspection_settings(single, shared, output)
    command = container_command(s, [s["container_python"], str(Path(__file__).resolve()), "--compare-plan", str(path)],
                                output / "container-tmp", slots=1)
    log = output / "comparison.log"
    with log.open("w") as handle:
        result = run_process(command, stdout=handle, stderr=handle, timeout=300, check=False)
    comparison = read_json(path.with_name("comparison.json"))
    require(comparison and result.returncode == EXIT.get(comparison.get("status")),
            f"SIF product comparison failed without a valid result; see {log}")
    return {"status": comparison["status"], "details": comparison, "log": str(log)}


def inspection_settings(single, shared, output):
    s = copy.deepcopy(single["run"]["frozen"]["settings"])
    s.update(work_root=str(output), product_root=str(output / "inspection-products"),
             parallel_slots=1, threads_per_slot=1, memory_gb=4)
    s["bind"] = [f"{case['work']}:{case['work']}:ro" for case in (single, shared)]
    return s


def fallback_time(single, shared, identity, output):
    from askap_auto.catalogue import position_at
    from askap_auto.science import Executor
    s = inspection_settings(single, shared, output)
    raw = single["report"]["retained_inputs"]["raw_ms"]
    require(Path(raw).is_dir(), "Preserved original MS is missing")
    executor = Executor({"settings": s}, None, deadline=time.monotonic() + 300)
    # Short inspection job: do not inherit the production 20-minute exit reserve.
    executor.s["reserve_minutes"] = 0
    obs = identity["observation"]
    log = output / "ms-time.log"
    casda = executor.observation_time(obs, raw, output / "ms-probe", log)
    require(casda["source"] == "CASDA" and casda["mjd"] == identity["time_mjd"],
            "Valid CASDA time did not take priority")
    missing = executor.observation_time({**obs, "t_min": None}, raw, output / "ms-probe", log)
    require(missing.get("source") == "MS" and missing.get("scale") == "utc" and
            missing.get("mjd") is not None and math.isfinite(missing["mjd"]),
            f"Real-SIF MS fallback failed: {missing}")
    # Use physical observation duration, not an arbitrary equality to sampling-centre TIME.
    start, end = obs.get("t_min"), obs.get("t_max")
    in_interval = (start is not None and end is not None and math.isfinite(end) and
                   end > start and start <= missing["mjd"] <= end)
    ra, dec, status = position_at(identity["source"], missing["mjd"], missing["scale"])
    require(status == "Y" and math.isfinite(ra) and math.isfinite(dec),
            "Nonzero proper motion did not accept the MS fallback epoch")
    return {"status": "PASS" if in_interval else "REVIEW",
            "casda": casda, "ms_fallback": missing, "delta_seconds": (missing["mjd"] - start) * 86400,
            "inside_casda_interval": bool(in_interval),
            "fallback_ra": ra, "fallback_dec": dec, "log": str(log)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--single-work", type=Path)
    parser.add_argument("--shared-work", type=Path)
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--compare-plan", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.compare_plan:
        result = compare_files(read_json(args.compare_plan))
        write_json(args.compare_plan.with_name("comparison.json"), result)
        return EXIT[result["status"]]
    if not all((args.single_work, args.shared_work, args.output_root)):
        parser.error("--single-work, --shared-work and --output-root are required")
    roots = [p.expanduser().resolve() for p in (args.single_work, args.shared_work)]
    parent = args.output_root.expanduser().resolve()
    require(roots[0] != roots[1] and not any(parent.is_relative_to(p) or p.is_relative_to(parent) for p in roots),
            "Output root and both Stage3 workspaces must be independent")
    parent.mkdir(parents=True, exist_ok=True)
    output = Path(tempfile.mkdtemp(prefix="run-", dir=parent))
    report = {"stage": "4A", "started_at": now(), "work_dir": str(output), "checks": [],
              "scope": "Existing Stage3 results only; no downloads, science computation or submission"}
    print("Stage4A: existing results, isolated CLI skip, real-SIF time fallback\nResults: " + str(output), flush=True)
    before = {}
    try:
        for root in roots:
            before[str(root)] = protected_snapshot(root)
        single, shared = load_case(roots[0], "single"), load_case(roots[1], "shared3")
        for case in (single, shared):
            write_json(output / (case["case"] + "-stage3-report.json"), case["report"])
            record(report, case["case"] + " completed records and cleanup",
                   lambda case=case: {"status": "PASS" if case["report"]["cleanup_status"] == "PASS" else "REVIEW",
                                      "cleanup_status": case["report"]["cleanup_status"],
                                      "cleanup_errors": case["report"].get("cleanup_errors", []),
                                      "limitations": case["report"].get("limitations", [])})
        identity_entry = record(report, "Anchor identity, science inputs and position",
                                lambda: compare_identity(single, shared))
        def sif_unchanged():
            paths = {}
            for case in (single, shared):
                frozen = case["run"]["frozen"]
                path = frozen["settings"]["container"]
                if path not in paths:
                    paths[path] = digest(path)
                require(paths[path] == frozen["sif_sha256"], "SIF changed since Stage3: " + path)
            return {"sif_sha256": paths}
        sif = record(report, "accepted SIF unchanged since Stage3", sif_unchanged)
        for case in (single, shared):
            record(report, case["case"] + " ordinary CLI skips completed results",
                   lambda case=case: skip_completed(case, output))
        if sif["status"] == "PASS":
            record(report, "accepted-SIF product comparison", lambda: container_comparison(single, shared, output))
        if identity_entry["status"] == sif["status"] == "PASS":
            record(report, "real MS time fallback and proper motion",
                   lambda: fallback_time(single, shared, identity_entry, output))
        for case in (single, shared):
            record(report, case["case"] + " Slurm accounting", lambda case=case: accounting(case, output))
    except Exception as exc:
        report["checks"].append({"name": "Stage4A prerequisites", "status": "FAIL", "error": str(exc)})
        print("FAIL: " + str(exc), flush=True)
    finally:
        if before:
            def unchanged():
                for root, expected in before.items():
                    require(protected_snapshot(Path(root)) == expected, "Original Stage3 state/metadata changed: " + root)
                return {"original_workspaces": sorted(before)}
            record(report, "original Stage3 database and metadata unchanged", unchanged)
    report.update(status=worst([c["status"] for c in report["checks"]]), finished_at=now())
    write_json(output / "report.json", report)
    print(f"STAGE4A {report['status']}: " + str({s: sum(c["status"] == s for c in report["checks"]) for s in EXIT}), flush=True)
    print("Report: " + str(output / "report.json"), flush=True)
    return EXIT[report["status"]]


if __name__ == "__main__":
    raise SystemExit(main())
