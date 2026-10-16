"""Real CASDA queries and frozen plans only; no downloads or job submission.

Every invocation creates independent workspaces. The existing SIF is read/hashed,
never executed. Missing live shared/multi-beam coverage yields INCOMPLETE, not PASS.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from contextlib import ExitStack
import copy
import csv
import json
import math
import os
from pathlib import Path
import shlex
import shutil
import sys
import tempfile
from unittest.mock import patch
from urllib.parse import unquote

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import astropy.units as u
import numpy as np
from astropy.table import Table
from askap_auto import catalogue, cli, query, settings
from template_settings import template_settings, write_settings
from askap_auto.common import digest, now, read_json, safe_path, write_json
from askap_auto.state import State

DEFAULT_CATALOGUE = Path(__file__).resolve().parents[1] / "examples/validation/stage2_targets.csv"


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def finite(value):
    if np.ma.is_masked(value):
        return None
    try:
        number = float(value)
    except (ValueError, TypeError):
        return None
    return number if math.isfinite(number) else None


def same_number(actual, expected, label):
    require(actual is None if expected is None else actual is not None and
            math.isclose(actual, expected, rel_tol=1e-10, abs_tol=1e-9), label)


def audit_snapshot(snapshot, effective):
    require(snapshot["complete"], "Incomplete query snapshot")
    require(snapshot["status"] in {"available", "no_observations"}, "Query did not succeed")
    cache = Path(snapshot["cache"])
    require(digest(cache) == snapshot["cache_sha256"], "CASDA ECSV fingerprint changed")
    rows = Table.read(cache, format="ascii.ecsv")
    require(len(rows) == snapshot["candidate_count"], "Raw candidate count changed")
    public = query.public_indices(rows) if len(rows) else set()
    observations = snapshot["observations"]
    groups = defaultdict(list)
    for obs in observations:
        require(0 <= obs["row_index"] < len(rows), "Candidate row mapping outside saved ECSV")
        raw = rows[obs["row_index"]]
        filename = raw["filename"]
        filename = filename.decode() if isinstance(filename, bytes) else str(filename)
        require(obs["filename"] == unquote(filename), "Dataset filename differs from mapped original CASDA row")
        if obs["public"] != (obs["row_index"] in public):
            require(False, "Public flag differs from saved CASDA release metadata: " +
                    f"file={obs['filename']}, row={obs['row_index']}, stored={obs['public']}, " +
                    f"recomputed={obs['row_index'] in public}, " +
                    f"release={raw['obs_release_date']!r}, masked={bool(np.ma.is_masked(raw['obs_release_date']))}, " +
                    f"dtype={rows['obs_release_date'].dtype}, queried_at={snapshot['queried_at']}")
        ra, dec = finite(raw["s_ra"]), finite(raw["s_dec"])
        same_number(obs["ra"], ra, "Beam RA differs from saved CASDA row")
        same_number(obs["dec"], dec, "Beam Dec differs from saved CASDA row")
        same_number(obs["exposure"], finite(raw["t_exptime"]), "Exposure differs from saved CASDA row")
        size_kb = finite(raw["access_estsize"])
        size = None if size_kb is None or size_kb < 0 else math.ceil(size_kb * 1024)
        require(obs["estimated_bytes"] == size, "CASDA KB-to-byte conversion differs")
        raw_quality = raw["quality_level"]
        quality = (raw_quality.decode() if isinstance(raw_quality, bytes) else str(raw_quality)).strip()
        require(obs["quality"] == quality, "Quality differs from saved CASDA row")
        if ra is not None and dec is not None and -90 <= dec <= 90:
            target = snapshot["source"]
            delta_ra = math.radians(ra - target["ra"])
            delta_dec = math.radians(dec - target["dec"])
            value = (math.sin(delta_dec / 2) ** 2 + math.cos(math.radians(dec)) *
                     math.cos(math.radians(target["dec"])) * math.sin(delta_ra / 2) ** 2)
            value = min(1, max(0, value))
            separation = math.degrees(2 * math.atan2(math.sqrt(value), math.sqrt(1-value)))
            same_number(obs["separation_deg"], separation, "Angular separation differs from raw sky coordinates")
        wavelengths = []
        for column in ("em_min", "em_max"):
            number = finite(raw[column])
            unit = rows[column].unit or u.m
            wavelengths.append(None if number is None else number * u.Unit(unit).to(u.m))
        minimum, maximum = wavelengths
        same_number(obs["em_min_m"], minimum, "Lower wavelength bound differs from original unit conversion")
        same_number(obs["em_max_m"], maximum, "Upper wavelength bound differs from original unit conversion")
        if minimum is not None and maximum is not None and minimum > 0 and maximum >= minimum:
            same_number(obs["frequency_min_ghz"], 299792458 / maximum / 1e9, "Minimum GHz frequency differs from wavelength")
            same_number(obs["frequency_max_ghz"], 299792458 / minimum / 1e9, "Maximum GHz frequency differs from wavelength")
        groups[obs["sb_id"]].append(obs)
        expected = bool(obs["sb_id"] and obs["beam"] and obs["public"]
                        and obs["quality"] in effective["quality_levels"])
        expected &= obs["exposure"] is not None and obs["exposure"] >= 0
        if effective["exposure_min"] is not None:
            expected &= obs["exposure"] is not None and obs["exposure"] > effective["exposure_min"]
        expected &= obs["estimated_bytes"] is not None
        if effective["max_file_size_kb"] is not None and obs["estimated_bytes"] is not None:
            expected &= obs["estimated_bytes"] <= effective["max_file_size_kb"] * 1024
        expected &= obs["ra"] is not None and obs["dec"] is not None
        if obs["ra"] is not None and obs["dec"] is not None:
            expected &= -90 <= obs["dec"] <= 90
        if obs["sb_id"]:
            number = int(obs["sb_id"][2:])
            expected &= number not in effective["excluded_sbs"]
            if effective["minimum_sb"] is not None:
                expected &= number >= effective["minimum_sb"]
        low, high = obs["frequency_min_ghz"], obs["frequency_max_ghz"]
        expected &= low is not None and high is not None
        if low is not None and high is not None:
            mean = (low + high) / 2
            require(math.isclose(obs["frequency_central_ghz"], mean, rel_tol=1e-12), "Central frequency is not arithmetic mean")
            reference = mean if effective["radius_frequency_mode"] == "central" else high
            radius = effective["reference_fwhm_deg"] / 2 * effective["reference_frequency_ghz"] / reference
            require(math.isclose(obs["radius_deg"], radius, rel_tol=1e-12), "Frequency-scaled half-power radius differs")
            if obs["separation_deg"] is not None:
                require(obs["within_radius"] == (obs["separation_deg"] <= radius), "Radius flag differs from separation")
        expected &= obs["within_radius"] is True
        # The identity parser also rejects obs_id/filename mismatches.
        expected &= "invalid_identity:" not in obs["reason"]
        require(obs["eligible"] == bool(expected), f"Eligibility inconsistent for {obs['filename']}: {obs['reason']}")
    for sb, candidates in groups.items():
        eligible = [o for o in candidates if o["eligible"]]
        chosen = [o for o in candidates if o["default_selected"]]
        require(len(chosen) == (1 if eligible else 0), f"Expected one nearest eligible record for {sb}")
        if eligible:
            nearest = min(eligible, key=lambda o: (o["separation_deg"], o["key"]))
            require(chosen[0]["key"] == nearest["key"], f"Wrong nearest eligible beam in {sb}")
    return groups


def audit_plan(work, python_path, container, code_root):
    state = State(work)
    run = state.active_run()
    require(run and run["tasks"], "No nonempty active plan was generated")
    frozen = run["frozen"]
    require(frozen["settings"]["work_root"] == str(work), "Frozen work directory differs")
    require(frozen["settings"]["python"] == python_path, "Wrong host Python in frozen plan")
    require(frozen["settings"]["container"] == str(container), "Wrong SIF in frozen plan")
    require(Path(frozen["settings"]["product_root"]).resolve() == (work / "products").resolve(), "Unexpected product workspace")
    require(frozen["settings"]["parallel_slots"] == 1 and frozen["settings"]["threads_per_slot"] == 8,
            "Expected one slot and eight threads in frozen settings")
    require(frozen["settings"]["memory_gb"] == 32 and frozen["settings"]["tmp_gb"] == 32,
            "Expected the stage-2 32-GiB memory/tmp planning profile")
    jobs = state.jobs(run["id"])
    require(jobs, "No planned job generated")
    for job in jobs:
        require(job["status"] == "planned" and not job.get("scheduler_id"), "Unexpected submission status")
        script = state.root / "jobs" / (job["id"] + ".sbatch")
        contents = script.read_text()
        require("exec " + shlex.quote(python_path) + " -m askap_auto.worker" in contents, "Wrong Python in sbatch")
        require("export PYTHONPATH=" + shlex.quote(str(code_root)) in contents, "Wrong deployed code path in sbatch")
        require("#SBATCH --cpus-per-task=8\n" in contents, "Expected one slot with eight threads")
        expected_memory = math.ceil(frozen["settings"]["memory_gb"] * 1024)
        require(f"#SBATCH --mem={expected_memory}M\n" in contents, "Memory request differs from frozen configuration")
        expected_tmp = math.ceil(frozen["settings"]["tmp_gb"] * 1024)
        require(f"#SBATCH --tmp={expected_tmp}M\n" in contents, "Temporary-disk request differs from frozen configuration")
        require(0 < job["walltime_hours"] <= frozen["settings"]["walltime_hours"], "Invalid estimated job walltime")
        seconds = math.ceil(job["walltime_hours"] * 3600)
        hours, remainder = divmod(seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        require(f"#SBATCH --time={hours:02}:{minutes:02}:{seconds:02}\n" in contents,
                "Slurm time request differs from the estimated job walltime")
    for task in run["tasks"]:
        require(digest(task["cache"]) == task["cache_sha256"], "Plan refers to changed CASDA metadata")
        for path in (task["cache"], task["raw_root"], task["model_root"], *(t["path"] for t in task["targets"])):
            require(Path(path).resolve().is_relative_to(work.resolve()), "Task path escaped its independent workspace")
    return state, run


def require_no_execution(workspaces):
    for work in workspaces:
        state = State(work)
        with state.connect() as db:
            require(db.execute("SELECT COUNT(*) FROM steps").fetchone()[0] == 0, "Processing/download step unexpectedly ran")
            attempts = [row[0] for row in db.execute("SELECT status FROM attempts")]
            require(all(value in {"preparing", "superseded"} for value in attempts), "Planned attempt unexpectedly executed")
            for row in db.execute("SELECT data FROM jobs"):
                job = json.loads(row[0])
                require(job["status"] == "planned" and not job.get("scheduler_id"), "Job unexpectedly submitted")
        forbidden = [path for path in work.rglob("*") if
                     path.name.endswith((".submission.json", ".outcome.json", ".ms"))
                     or path.name.endswith(("raw-ready.json", "download-ready.json"))
                     or ".tar" in path.name]
        require(not forbidden, f"Unexpected downloaded/submitted material: {forbidden[:3]}")


def run_stage(root, args, record, uncovered):
    container = safe_path(args.container)
    require(container.is_file(), f"SIF does not exist: {container}")
    require(Path(args.python).is_absolute() and Path(args.python).is_file() and os.access(args.python, os.X_OK),
            "Host Python path must be an existing absolute executable")
    inputs = root / "inputs"
    inputs.mkdir()
    catalogue_path = inputs / "stage2_targets.csv"
    shutil.copy2(args.catalogue, catalogue_path)
    batch = root / "batch_range"
    single = root / "single_nearest"
    workspaces = [batch, single]
    effective = template_settings()
    effective.update(work_root=str(batch), product_root=str(batch / "products"),
                     catalogue_path=str(catalogue_path), container=str(container), python=args.python,
                     parallel_slots=1, threads_per_slot=8, memory_gb=32, tmp_gb=32)
    settings.validate(effective)
    # Isolated stage-2 settings (with dstools-config.py beside them); no production config imported.
    config = write_settings(inputs / "batch_config.py", effective)
    site = write_settings(inputs / "single_config.py", effective)
    code_root = Path(__file__).resolve().parents[2]
    sources = catalogue.load_catalogue(effective)
    require(len(sources) >= 2, "At least two real positions needed for a shared-plan check")
    record("Real-position input loaded without UCS/spectral-type column mapping")

    print("Querying CASDA for the small batch catalogue...", flush=True)
    index = cli.main(["query", "--config", str(config)])
    errors = [{"name": e["source"]["name"], "error": e.get("error")} for e in index["sources"]
              if e["status"] not in {"available", "no_observations"}]
    require(not errors, "Real CASDA query failure (not zero observations): " + json.dumps(errors))
    require(index["complete"], "Batch query checkpoint incomplete")
    record("All real batch queries succeeded", {e["source"]["name"]: e["status"] for e in index["sources"]})
    for entry in index["sources"]:
        snapshot = read_json(batch / "state/queries" / entry["source"]["source_id"] / "query-snapshot.json")
        require(snapshot is not None, "Full candidate snapshot missing")
        audit_snapshot(snapshot, effective)
        require({o["key"] for o in entry["observations"]} ==
                {o["key"] for o in snapshot["observations"] if o["default_selected"]}, "Compact batch selection differs from full candidate snapshot")
        record("Candidate filters, radius and nearest eligible beam: " + entry["source"]["name"],
               {"raw_candidates": snapshot["candidate_count"],
                "eligible": sum(o["eligible"] for o in snapshot["observations"]),
                "selected": len(entry["observations"])})
    index_path = batch / "state/crosscatalogue.json"
    checksum = digest(index_path)
    cli.main(["query", "--config", str(config)])
    require(digest(index_path) == checksum, "Ordinary query rewrote the saved query index")
    record("Ordinary batch query reuses the frozen index")

    anchor = sources[0]
    anchor_entry = next(e for e in index["sources"] if e["source"]["source_id"] == anchor["source_id"])
    require(anchor_entry["observations"], "Anchor has no selected public observation; choose another real test target")
    partners = defaultdict(set)
    for entry in index["sources"]:
        for obs in entry["observations"]:
            partners[obs["key"]].add(entry["source"]["name"])
    shared_keys = [o["key"] for o in anchor_entry["observations"] if len(partners[o["key"]]) > 1]
    historical = [o["key"] for o in anchor_entry["observations"] if o["sb_id"] == "SB74372" and o["beam"] == "beam28"]
    chosen_key = next((key for key in historical if key in shared_keys),
                      min(shared_keys or [o["key"] for o in anchor_entry["observations"]]))
    if not shared_keys:
        uncovered("Shared partners outside range", "Current query has no exact dataset shared with the anchor; choose another known shared sample")
    print("Writing batch plan for original row 1 only; checking shared expansion...", flush=True)
    cli.main(["run", "--config", str(config), "--begin", "1", "--end", "1", "--observation-key", chosen_key])
    batch_state, batch_run = audit_plan(batch, args.python, container, code_root)
    require(batch_run["selection"]["begin"] == 1 and batch_run["selection"]["end"] == 1, "Original row range changed")
    require({task["observation"]["key"] for task in batch_run["tasks"]} == {chosen_key}, "Dataset restriction ignored")
    actual = {t["source"]["name"] for task in batch_run["tasks"] for t in task["targets"]}
    require(actual == partners[chosen_key], "Batch shared expansion differs from full-catalogue associations")
    record("Range endpoints and exact dataset restriction", {"observation_key": chosen_key, "targets": sorted(actual)})
    if shared_keys:
        record("Range-external partners included in a shared model group", sorted(actual))

    print("Querying CASDA through the independent single-source entry...", flush=True)
    single_query = cli.main(["query", "--config", str(site), "--work-dir", str(single),
                             "--name", anchor["original_name"], "--ra", str(anchor["ra"]),
                             "--dec=" + str(anchor["dec"])], single=True)
    groups = audit_snapshot(single_query, effective)
    require(single_query["source"]["source_id"] == anchor["source_id"], "Single-source identity differs from batch input")
    require({o["key"] for o in single_query["observations"] if o["default_selected"]} ==
            {o["key"] for o in anchor_entry["observations"]},
            "Fresh single/batch default selections differ; inspect both live snapshots for service changes or parsing errors")
    record("Real single-source query agrees with batch default selection")
    print("Writing single-source default plan...", flush=True)
    cli.main(["run", "--config", str(site), "--work-dir", str(single)], single=True)
    single_state, single_run = audit_plan(single, args.python, container, code_root)
    default_keys = {o["key"] for o in single_query["observations"] if o["default_selected"]}
    require({t["observation"]["key"] for t in single_run["tasks"]} == default_keys, "Single-source plan differs from default query selection")
    require(all(task["retain"] and not task["shared"] for task in single_run["tasks"]), "Single-source retention/shared settings differ")
    record("Single-source plan selects one nearest eligible beam per SB")

    candidates = []
    for sb, observations in groups.items():
        eligible = [o for o in observations if o["eligible"]]
        if len(eligible) >= 2 and len({o["beam"] for o in eligible}) == len(eligible):
            candidates.append(sb)
    if candidates:
        expanded_sb = "SB74372" if "SB74372" in candidates else sorted(candidates)[0]
        expanded = root / "single_expanded"
        workspaces.append(expanded)
        shutil.copytree(single / "query", expanded / "query")
        expanded_snapshot = copy.deepcopy(single_query)
        relative_cache = Path(single_query["cache"]).relative_to(single / "query")
        expanded_snapshot["cache"] = str(expanded / "query" / relative_cache)
        write_json(expanded / "query/query-snapshot.json", expanded_snapshot)
        write_json(Path(expanded_snapshot["cache"]).parent / "query-snapshot.json", expanded_snapshot)
        write_json(expanded / "state/source.json", anchor)
        write_json(expanded / "state/workspace.json", {"schema": 1, "mode": "single"})
        print("Writing independent all-beams plan for " + expanded_sb + "...", flush=True)
        cli.main(["run", "--config", str(site), "--work-dir", str(expanded),
                  "--all-beams-for-sb", expanded_sb], single=True)
        _, expanded_run = audit_plan(expanded, args.python, container, code_root)
        expected = {o["key"] for o in single_query["observations"] if o["eligible"] and
                    (o["sb_id"] == expanded_sb or o["default_selected"])}
        require({task["observation"]["key"] for task in expanded_run["tasks"]} == expected, "Expanded plan selected wrong beams")
        record("Requested SB expands to every eligible beam", {"SB": expanded_sb, "selected_datasets": len(expected)})
        if any(sb != expanded_sb and any(o["eligible"] for o in obs) for sb, obs in groups.items()):
            record("Other SBs retain the nearest-eligible-beam rule")
        else:
            uncovered("Other SB remains nearest", "This live sample has no second eligible SB")
    else:
        uncovered("All-beams expansion", "No unambiguous SB with two or more eligible beams in current single-source query")

    plan_path = batch_state.root / "runs" / batch_run["id"] / "execution-plan.json"
    before = digest(plan_path)
    # A different range replaces the unfinished preview only after the user confirms.
    with patch("builtins.input", return_value="y") as confirm:
        cli.main(["run", "--config", str(config), "--work-dir", str(batch), "--begin", "4", "--end", "4"])
    with batch_state.connect() as db:
        changed_run = json.loads(db.execute("SELECT data FROM runs ORDER BY rowid DESC LIMIT 1").fetchone()["data"])
    require(confirm.called and digest(plan_path) == before and changed_run["id"] != batch_run["id"]
            and changed_run["selection"]["begin"] == 4,
            "New invocation did not apply its current range or changed historical records")
    record("Changed range replaces the previous preview after confirmation and preserves its history")
    with patch("builtins.input", return_value="y"):
        cli.main(["run", "--config", str(config), "--begin", "1", "--end", "1", "--observation-key", chosen_key])
    require_no_execution(workspaces)
    hashes = {State(work).active_run()["frozen"]["sif_sha256"] for work in workspaces}
    require(len(hashes) == 1, "SIF changed between plan preparations")
    record("Generated sbatch uses new Python/code path and consistent resources; SIF fingerprint stable", next(iter(hashes)))
    record("No archives/MS downloads, processing steps, scheduler IDs or submission receipts")
    write_json(root / "plan-locations.json", {work.name: str(State(work).root / "runs" / State(work).active_run()["id"]) for work in workspaces})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--container", type=Path, required=True, help="Existing accepted SIF, read for hashing only")
    parser.add_argument("--catalogue", type=Path, default=DEFAULT_CATALOGUE)
    parser.add_argument("--python", default=sys.executable, help="Host Python to record in generated sbatch")
    args = parser.parse_args(argv)
    args.catalogue = args.catalogue.resolve()
    output = safe_path(args.output_root)
    output.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="run-", dir=output))
    report = {"stage": 2, "started_at": now(), "python": sys.executable,
              "work_dir": str(root), "checks": [], "not_covered": []}
    report_path = root / "report.json"

    def save():
        write_json(report_path, report)

    def record(label, detail=None):
        report["checks"].append({"check": label, "status": "PASS", "detail": detail})
        save()
        print("PASS: " + label, flush=True)
        if detail is not None:
            print(json.dumps(detail, ensure_ascii=False), flush=True)

    def uncovered(label, reason):
        report["not_covered"].append({"check": label, "reason": reason})
        save()
        print(f"NOT_COVERED: {label}: {reason}", flush=True)

    def forbidden(*_args, **_kwargs):
        raise RuntimeError("Stage 2 forbids archive preparation, subprocesses, Slurm submission and scientific execution")

    print("Stage 2: real CASDA query + plan only (no download/submit/DStools)", flush=True)
    print("Results: " + str(root), flush=True)
    try:
        # Guards block execution; they do not replace or simulate CASDA queries.
        env = {key: value for key, value in os.environ.items() if not key.startswith("ASKAP_")}
        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, env, clear=True))
            for name in ("askap_auto.controller.prepare_job", "askap_auto.controller.submit_job",
                         "subprocess.Popen", "askap_auto.science.preflight"):
                stack.enter_context(patch(name, forbidden))
            run_stage(root, args, record, uncovered)
        report["status"] = "INCOMPLETE" if report["not_covered"] else "PASS"
    except Exception as error:
        report.update(status="FAIL", error_type=type(error).__name__, error=str(error))
        print(f"FAIL: {type(error).__name__}: {error}", file=sys.stderr, flush=True)
    report["finished_at"] = now()
    save()
    print(f"STAGE2 {report['status']}: {len(report['checks'])} checks passed; {len(report['not_covered'])} not covered", flush=True)
    print("Report: " + str(report_path), flush=True)
    return {"PASS": 0, "INCOMPLETE": 2, "FAIL": 1}[report["status"]]


if __name__ == "__main__":
    raise SystemExit(main())
