"""Prepare one accepted dataset, download, run, and check Stage 3 independently.

prepare: cache/plan only. download: authenticated CASDA staging, no Slurm.
retry: freeze a diagnosed interface recovery using retained downloads, no Slurm.
run --submit: explicit real Slurm execution. check: completion/file/state evidence.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import copy
import json
import os
from pathlib import Path
import sys
import tarfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from astropy.table import Table
from askap_auto import catalogue, cli, controller, query, settings
from template_settings import template_settings, write_settings
from askap_auto.common import digest, lock, now, read_json, safe_path, signature, uid, write_csv, write_json
from askap_auto.science import validate_frozen
from askap_auto.state import State

SOURCE_FILE = Path(__file__).resolve().parents[1] / "examples/validation/stage3_sources.json"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def rebuild_query(original, source, effective, directory, stage2_root):
    """Keep the original TAP timestamp/rows; qualify cached rows for explicit PM metadata."""
    cache = Path(original["cache"])
    require(cache.resolve().is_relative_to(stage2_root), "Stage2 cache escapes the accepted run")
    require(digest(cache) == original["cache_sha256"], "Accepted Stage2 ECSV changed")
    rows = Table.read(cache, format="ascii.ecsv")
    observations = query.candidates(rows, source, effective)
    revision = uid()
    new_cache = directory / "versions" / revision / "candidates.ecsv"
    new_cache.parent.mkdir(parents=True, exist_ok=True)
    new_cache.write_bytes(cache.read_bytes())
    conditions = settings.query_settings(effective)
    snapshot = {**copy.deepcopy(original), "revision": revision, "source": source,
                "conditions": conditions, "condition_signature": signature({"conditions": conditions, "source": source}),
                "status": "available" if any(o["eligible"] for o in observations) else "no_observations",
                "observations": observations, "cache": str(new_cache), "cache_sha256": digest(new_cache),
                "query_origin": {"type": "accepted-stage2-cache", "run": str(stage2_root),
                                 "original_revision": original["revision"], "requalified_at": now()}}
    query.export_query(directory, snapshot)
    write_json(new_cache.parent / "query-snapshot.json", snapshot)
    write_json(directory / "query-snapshot.json", snapshot)
    return snapshot


def prepare(args):
    stage2 = safe_path(args.stage2_run)
    report = read_json(stage2 / "report.json")
    require(report and report.get("status") == "PASS" and report.get("stage") == 2,
            "A completed PASS report from Stage2 is required")
    locations = read_json(stage2 / "plan-locations.json")
    require(locations and "batch_range" in locations, "Accepted Stage2 plan locations missing")
    old_plan_dir = Path(locations["batch_range"]).resolve()
    require(old_plan_dir.is_relative_to(stage2), "Accepted Stage2 plan escapes its run")
    old_plan = read_json(old_plan_dir / "execution-plan.json")
    require(old_plan and len(old_plan["tasks"]) == 1, "Expected one accepted shared dataset plan")
    observation = old_plan["tasks"][0]["observation"]
    require(observation["sb_id"] == "SB74372" and observation["beam"] == "beam28",
            "Stage3 pilot expects the accepted SB74372/beam28 reference dataset")
    container = safe_path(args.container)
    require(container.is_file() and digest(container) == old_plan["frozen"]["sif_sha256"],
            "SIF missing or differs from the accepted Stage2 image")
    require(Path(args.python).is_absolute() and Path(args.python).is_file() and os.access(args.python, os.X_OK),
            "Host Python must be an absolute executable path")
    work = safe_path(args.work_dir)
    require(not work.is_relative_to(stage2) and not stage2.is_relative_to(work),
            "Stage3 must have a workspace independent of Stage2")
    require(not work.exists() or not any(work.iterdir()),
            "Work directory already contains data; resume with download/run/check or choose a new path")
    spec = read_json(SOURCE_FILE)
    specifications = spec["sources"][:1] if args.case == "single" else spec["sources"]
    accepted_index = read_json(stage2 / "batch_range/state/crosscatalogue.json")
    accepted_entries = {entry["source"]["original_name"]: entry for entry in accepted_index["sources"]}
    for source in specifications:
        accepted = accepted_entries.get(source["name"])
        require(accepted is not None, "Reference source absent from Stage2: " + source["name"])
        require(accepted["source"]["ra"] == source["ra"] and accepted["source"]["dec"] == source["dec"],
                "Reference coordinates differ from accepted Stage2 input")
    source_hashes = {str(stage2 / "report.json"): digest(stage2 / "report.json"),
                     str(old_plan_dir / "execution-plan.json"): digest(old_plan_dir / "execution-plan.json")}
    work.mkdir(parents=True, exist_ok=True)
    effective = template_settings()
    effective.update(work_root=str(work), product_root=str(work / "products"), container=str(container),
                     python=args.python, parallel_slots=1, threads_per_slot=8, memory_gb=40, tmp_gb=10,
                     casda_username=args.casda_username, casda_password_file=args.password_file,
                     coordinate_epoch=spec["coordinate_epoch"], pm_unit=spec["pm_unit"],
                     pmra_includes_cosdec=spec["pmra_includes_cosdec"])
    effective.update({key: value for key, value in old_plan["frozen"]["settings"].items()
                      if key in settings.QUERY_KEYS})
    settings.validate(effective)
    snapshots = []
    for index, source in enumerate(specifications, 1):
        mapped = catalogue.make_source(source["name"], source["ra"], source["dec"], effective,
                                       pmra=str(source["pmra"]), pmdec=str(source["pmdec"]), index=index)
        old_source = accepted_entries[source["name"]]["source"]
        old_snapshot_path = stage2 / "batch_range/state/queries" / old_source["source_id"] / "query-snapshot.json"
        original = read_json(old_snapshot_path)
        require(original and original.get("complete"), "Accepted full query missing for " + source["name"])
        source_hashes[str(old_snapshot_path)] = digest(old_snapshot_path)
        source_hashes[original["cache"]] = original["cache_sha256"]
        directory = work / "query" if args.case == "single" else work / "state/queries" / mapped["source_id"]
        snapshot = rebuild_query(original, mapped, effective, directory, stage2)
        chosen = [o for o in snapshot["observations"] if o["key"] == observation["key"]]
        require(len(chosen) == 1 and chosen[0]["eligible"] and chosen[0]["default_selected"],
                "Accepted pilot dataset is not the default eligible selection for " + source["name"])
        snapshots.append(snapshot)
    if args.case == "single":
        state = State(work)
        write_json(state.root / "source.json", snapshots[0]["source"])
        write_json(state.root / "workspace.json", {"schema": 1, "mode": "single"})
        settings_file = write_settings(work / "config.py", effective)
        argv = ["run", "--work-dir", str(work), "--config", str(settings_file),
                "--observation-key", observation["key"]]
    else:
        input_file = work / "targets_with_motion.csv"
        rows = [{"Name": src["name"], "RA": src["ra"], "DEC": src["dec"], "PMRA": src["pmra"],
                 "PMDEC": src["pmdec"], "Epoch": spec["coordinate_epoch"], "Reference_ID": src["reference_id"]}
                for src in specifications]
        write_csv(input_file, list(rows[0]), rows)
        effective.update(catalogue_path=str(input_file), catalogue_columns={"source_name": "Name", "ra": "RA", "dec": "DEC",
                         "pmra": "PMRA", "pmdec": "PMDEC", "epoch": "Epoch"})
        sources = catalogue.load_catalogue(effective)
        require(sources == [s["source"] for s in snapshots], "Batch input differs from prepared motion metadata")
        state = State(work)
        write_json(state.root / "workspace.json", {"schema": 1, "mode": "batch"})
        entries = [{**s, "observations": [obs for obs in s["observations"] if obs["default_selected"]],
                    "done": True, "using_previous_result": False, "error": None} for s in snapshots]
        index = {"schema": 1, "revision": uid(), "complete": True, "finished_at": now(), "sources": entries,
                 "signature": signature({"conditions": settings.query_settings(effective), "sources": sources})}
        write_json(state.root / "crosscatalogue.json", index)
        # Independent Stage3 site settings (with dstools-config.py), password contents never stored.
        settings_file = write_settings(work / "batch_config.py", effective)
        argv = ["run", "--work-dir", str(work), "--config", str(settings_file), "--begin", "1", "--end", "1",
                "--observation-key", observation["key"]]
    cli.main(argv, single=args.case == "single")
    run = state.active_run()
    require(run and {task["observation"]["key"] for task in run["tasks"]} == {observation["key"]},
            "Plan did not restrict execution to the accepted pilot dataset")
    targets = {t["source"]["name"] for task in run["tasks"] for t in task["targets"]}
    expected = {s["source"]["name"] for s in snapshots}
    require(targets == expected, "Plan has unexpected target expansion")
    metadata = {"schema": 1, "case": args.case, "created_at": now(), "stage2_run": str(stage2),
                "plan_run_id": run["id"], "observation_key": observation["key"], "dataset": observation,
                "planned_targets": sorted(expected), "expected_source_specs": spec,
                "sif_sha256": run["frozen"]["sif_sha256"], "settings_file": str(settings_file),
                "resources": {key: effective[key] for key in ("parallel_slots", "threads_per_slot", "memory_gb", "tmp_gb", "walltime_hours")},
                "run_argv": argv, "accepted_stage2_files": source_hashes}
    write_json(work / "stage3-case.json", metadata)
    require(all(digest(path) == checksum for path, checksum in source_hashes.items()), "Preparation modified accepted Stage2 evidence")
    print("STAGE3 PREPARED: " + args.case, flush=True)
    print(json.dumps({"work_dir": str(work), "SB": observation["sb_id"], "beam": observation["beam"],
                      "observation_key": observation["key"], "planned_targets": sorted(expected),
                      "estimated_archive_bytes": observation["estimated_bytes"], "resources": metadata["resources"],
                      "motion": [{k: s[k] for k in ("name", "pmra", "pmdec")} for s in specifications]}, indent=2), flush=True)
    print("Frozen plan: " + str(state.root / "runs" / run["id"] / "execution-plan.csv"), flush=True)
    return 0


def execute(work, download_only=False):
    metadata = read_json(work / "stage3-case.json")
    require(metadata is not None, "Run prepare in this independent Stage3 workspace first")
    state = State(work)
    with state.connect() as db:
        row = db.execute("SELECT data,status FROM runs WHERE id=?", (metadata["plan_run_id"],)).fetchone()
        require(row is not None, "Prepared Stage3 run missing from database")
        run = json.loads(row["data"])
        previous_status = row["status"]
    if previous_status == "complete":
        print("Prepared run already ended; checking its actual completion", flush=True)
        from check_processing_stage3 import main as check_main
        return check_main(["--work-dir", str(work)])
    active = state.active_run()
    require(active and active["id"] == metadata["plan_run_id"], "Prepared run is no longer active; use check to inspect this workspace")
    argv = metadata["run_argv"] + (["--prepare-download"] if download_only else ["--submit"])
    cli.main(argv, single=metadata["case"] == "single")
    if download_only:
        for task in run["tasks"]:
            ready = read_json(Path(task["raw_root"]) / "download-ready.json")
            require(ready and ready["key"] == metadata["observation_key"], "Archive download did not complete for this dataset")
            require(Path(ready["archive"]).is_file() and Path(ready["checksum"]).is_file(), "Archive/checksum missing")
        print("STAGE3 DOWNLOADED: archive/checksum present; no job submitted", flush=True)
        return 0
    from check_processing_stage3 import main as check_main
    return check_main(["--work-dir", str(work)])


def corrective_retry_reason(attempt, model):
    """Recognize only the two diagnosed interface failures, within retry limits."""
    if attempt["status"] != "failed" or attempt["retry_number"] >= 2:
        return None
    if (attempt["retry_number"] == 0 and attempt.get("error_step") == "extract_archive"
            and str(attempt.get("error", "")).startswith("Archive links are unsupported:")):
        return "archive-link"
    if attempt.get("error_step") == "create_model" and "exit status 2" in str(attempt.get("error", "")):
        log = Path(attempt["log"]) if attempt.get("log") else None
        if log and log.is_file():
            import re
            if re.search(r"Error: Invalid value for .*--(?:threshold|mask-threshold).*not a valid integer\.",
                         log.read_text(errors="replace")):
                # Refuse to round a fractional frozen setting in a corrective retry.
                for key in settings.INTEGER_THRESHOLD_KEYS:
                    value = model[key]
                    if value is not None:
                        settings.integer_model_value(key, value)
                return "integer-threshold"
    return None


def prepare_processing_retry(work):
    """One bounded corrective retry; preserve evidence and reuse retained files."""
    from askap_auto.tar_safety import validated_members
    from check_processing_stage3 import main as check_main

    state = State(work)
    with lock(state.root / "controller.lock"):
        metadata = read_json(work / "stage3-case.json")
        require(metadata and metadata.get("case") == "single", "Corrective recovery is only for the single-source case")
        old_id = metadata["plan_run_id"]
        with state.connect() as db:
            row = db.execute("SELECT data,status FROM runs WHERE id=?", (old_id,)).fetchone()
            require(row is not None, "Recorded Stage3 run is missing")
            old, old_status = json.loads(row["data"]), row["status"]
            related = [value for row in db.execute("SELECT data FROM runs")
                       if (value := json.loads(row["data"])).get("recovery_of") == old_id]
        require(old["single"] and len(old["tasks"]) == 1 and len(old["tasks"][0]["targets"]) == 1,
                "Recovery requires one dataset/target")
        task, target = old["tasks"][0], old["tasks"][0]["targets"][0]
        original = state.attempt(target["attempt_id"])
        require(task["observation"]["key"] == metadata["observation_key"]
                and old["frozen"]["sif_sha256"] == metadata["sif_sha256"], "Frozen case identity differs")
        validate_frozen(old["frozen"])
        require(digest(task["cache"]) == task["cache_sha256"], "Frozen CASDA metadata changed")
        reason = corrective_retry_reason(original, old["frozen"]["settings"]["model"])
        recovery = metadata.get("processing_recovery", metadata.get("archive_link_recovery"))
        active = state.active_run()
        if reason is None:
            require(recovery and old_id == recovery["retry_run_id"]
                    and old.get("recovery_of") == recovery["original_run_id"]
                    and old.get("allow_redownload_retry") is False and task.get("reuse_download_only"),
                    "No diagnosed interface failure is eligible for another corrective retry")
            require(not active or active["id"] == old_id, "Another run is active in this workspace")
            intent_path = Path(recovery.get("intent_file", state.root / "stage3-archive-retry.json"))
            require(intent_path.resolve().is_relative_to(state.root.resolve()), "Recovery intent escapes this workspace")
            intent = read_json(intent_path)
            require(intent and intent["original_run_id"] == recovery["original_run_id"]
                    and intent["history_dir"] == recovery["history_dir"], "Recovery intent identity differs")
            history = Path(intent["history_dir"])
            require(history.resolve().is_relative_to(state.root.resolve())
                    and digest(history / "stage3-case.json") == intent["metadata_sha256"], "Saved metadata changed")
            controller.retained_download_ready(task)
            intent.update(status="prepared", retry_run_id=old_id)
            write_json(intent_path, intent)
            check_main(["--work-dir", str(work)])
            print("STAGE3 RETRY ALREADY PREPARED: " + old_id, flush=True)
            return 0
        require(old_status == "complete", "The previous controller must have ended before corrective recovery")
        require(len(related) <= 1, "Multiple recovery runs exist; inspect state")
        require(not active or (related and active["id"] == related[0]["id"]), "Another run is active in this workspace")
        ready = controller.retained_download_ready(task)
        with tarfile.open(ready["archive"]) as archive:
            members = validated_members(archive, ready["ms_name"])
            if reason == "archive-link":
                require(any(member.islnk() for member in members.values()), "Expected the diagnosed archive hardlink")
        intent_path = state.root / "stage3-retry" / (old_id + ".json")
        intent = read_json(intent_path)
        if intent:
            require(intent["original_run_id"] == old_id and intent["reason"] == reason, "Recovery intent belongs to another failure")
            history = Path(intent["history_dir"])
            require(history.resolve().is_relative_to(state.root.resolve()), "Recovery history escapes this workspace")
            require(digest(history / "stage3-case.json") == intent["metadata_sha256"]
                    == digest(work / "stage3-case.json"), "Original metadata changed during recovery")
        else:
            require(not related, "Recovery run exists without a saved intent")
            history = state.root / "stage3-history" / uid()
            history.mkdir(parents=True)
            (history / "stage3-case.json").write_bytes((work / "stage3-case.json").read_bytes())
            previous_report = state.root / "stage3-check.json"
            if previous_report.is_file():
                (history / "stage3-check.json").write_bytes(previous_report.read_bytes())
            previous_log = Path(original["log"]) if original.get("log") else None
            write_json(history / "failure-evidence.json", {
                "saved_at": now(), "original_run_id": old_id, "original_attempt": original,
                "original_steps": state.steps(original["id"]),
                "failure_log": {"path": str(previous_log), "present": bool(previous_log and previous_log.is_file()),
                                "sha256": digest(previous_log) if previous_log and previous_log.is_file() else None},
                "retained_downloads": {ready[key]: {"size": Path(ready[key]).stat().st_size, "sha256": digest(ready[key])}
                                       for key in ("archive", "checksum")}})
            if previous_log and previous_log.is_file():
                (history / previous_log.name).write_bytes(previous_log.read_bytes())
            intent = {"schema": 1, "original_run_id": old_id, "reason": reason, "history_dir": str(history),
                      "metadata_sha256": digest(history / "stage3-case.json"), "status": "planning"}
            write_json(intent_path, intent)
        if related:
            retry_run = related[0]
        else:
            require(state.target(target["source"]["source_id"], task["observation"]["key"]).get("latest_attempt") == original["id"],
                    "This failure is no longer the latest attempt")
            snapshot = read_json(work / "query/query-snapshot.json")
            require(snapshot and snapshot["revision"] == old["index_revision"]
                    and snapshot["source"] == target["source"] and snapshot["cache"] == task["cache"]
                    and snapshot["cache_sha256"] == task["cache_sha256"], "Original query/source identity changed")
            retry_run = controller.plan(state, snapshot, old["frozen"], mode="retry", single=True,
                                        selected=old["selected_observations"], **old["selection"],
                                        reuse_download_only=True, recovery_of=old_id)
        require(len(retry_run["tasks"]) == 1 and len(retry_run["tasks"][0]["targets"]) == 1, "Recovery plan must have one dataset/target")
        retry_task = retry_run["tasks"][0]
        require(retry_run["frozen"] == old["frozen"] and retry_run.get("allow_redownload_retry") is False
                and retry_task.get("reuse_download_only") and not retry_task["redownload"]
                and retry_task["raw_root"] == task["raw_root"]
                and retry_task["observation"]["key"] == metadata["observation_key"]
                and retry_task["targets"][0]["retry_number"] == original["retry_number"] + 1
                and retry_task["targets"][0]["source"] == target["source"], "Recovery plan differs from the permitted correction")
        if state.active_run():
            controller.run_controller(state, retry_run)  # Plan/sbatch only.
        metadata["plan_run_id"] = retry_run["id"]
        record = {"original_run_id": old_id, "original_attempt_id": original["id"], "reason": reason,
                  "retry_run_id": retry_run["id"], "history_dir": str(history), "intent_file": str(intent_path),
                  "download_policy": "retained-only", "prepared_at": now()}
        metadata["processing_recovery"] = record
        if reason == "archive-link":
            metadata["archive_link_recovery"] = record  # Existing case metadata compatibility.
        write_json(work / "stage3-case.json", metadata)
        intent.update(status="prepared", retry_run_id=retry_run["id"])
        write_json(intent_path, intent)
        check_main(["--work-dir", str(work)])
        print("STAGE3 RETRY PREPARED: " + retry_run["id"] + " (" + reason + ")", flush=True)
        print("Retained archive/checksum only; no download or job submission. History: " + str(history), flush=True)
        print("Frozen plan: " + str(state.root / "runs" / retry_run["id"] / "execution-plan.csv"), flush=True)
        return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare_parser = commands.add_parser("prepare", help="Copy accepted metadata and freeze one dataset; no download/submission")
    prepare_parser.add_argument("--stage2-run", type=Path, required=True)
    prepare_parser.add_argument("--work-dir", type=Path, required=True)
    prepare_parser.add_argument("--case", choices=("single", "shared3"), default="single")
    prepare_parser.add_argument("--container", type=Path, required=True)
    prepare_parser.add_argument("--python", default=sys.executable)
    prepare_parser.add_argument("--casda-username", required=True, help="Account name/email only, never a password")
    prepare_parser.add_argument("--password-file", default="~/.config/askap/casda-password")
    for name in ("download", "run", "check", "retry"):
        item = commands.add_parser(name)
        item.add_argument("--work-dir", type=Path, required=True)
        if name == "run":
            item.add_argument("--submit", action="store_true", required=True, help="Explicit real Slurm submission/continuation")
    args = parser.parse_args(argv)
    env = {key: value for key, value in os.environ.items() if not key.startswith("ASKAP_")}
    try:
        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, env, clear=True))
            if args.command == "prepare":
                def forbidden(*_args, **_kwargs):
                    raise RuntimeError("Stage3 prepare cannot query TAP, download, submit or run subprocesses")
                for name in ("askap_auto.query.tap_query", "askap_auto.cli.query_one", "askap_auto.cli.query_catalogue",
                             "askap_auto.controller.prepare_job", "askap_auto.controller.submit_job", "subprocess.Popen"):
                    if name == "askap_auto.cli.query_catalogue":
                        # Batch run may read its already prepared index; forbid its network boundary instead.
                        continue
                    stack.enter_context(patch(name, forbidden))
                # query_catalogue's default fetch was bound at definition time, so pass a forbidden fetch explicitly.
                original = query.query_catalogue
                def cached_only(*qargs, **qkwargs):
                    return original(*qargs, **qkwargs, fetch=forbidden)
                stack.enter_context(patch("askap_auto.cli.query_catalogue", cached_only))
                return prepare(args)
            work = safe_path(args.work_dir)
            if args.command == "retry":
                def no_execution(*_args, **_kwargs):
                    raise RuntimeError("Stage3 retry preparation cannot download, submit, or execute subprocesses")
                for name in ("askap_auto.archive.download", "askap_auto.controller.submit_job", "subprocess.Popen"):
                    stack.enter_context(patch(name, no_execution))
                return prepare_processing_retry(work)
            if args.command == "check":
                from check_processing_stage3 import main as check_main
                return check_main(["--work-dir", str(work)])
            if args.command == "download":
                def no_submit(*_args, **_kwargs):
                    raise RuntimeError("Stage3 download cannot submit Slurm or execute subprocesses")
                stack.enter_context(patch("askap_auto.controller.submit_job", no_submit))
                stack.enter_context(patch("subprocess.Popen", no_submit))
            return execute(work, download_only=args.command == "download")
    except (ValueError, RuntimeError, OSError, tarfile.TarError) as error:
        print(f"STAGE3 FAIL: {error}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
