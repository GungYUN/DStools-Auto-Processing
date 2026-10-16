"""Login controller, bounded Slurm continuation and recorded processing plans."""
from __future__ import annotations
import copy
import json
import math
import re
import shlex
import shutil
import subprocess
import time
from pathlib import Path
from . import archive
from .common import digest, now, read_json, uid, write_csv, write_json
from .science import dataset_dir, triple_exists
from .settings import model_identity
from .state import FINAL, StateError

# Slurm states after which the next job may still succeed; any other job that
# ends without a completion record stops the controller.
TRANSIENT_JOB_STATES = {"NODE_FAIL", "BOOT_FAIL"}
# --retry-failed ("retry") and --retry-exhausted give each target its own private
# model: first reusing the download, then with a fresh download.
RETRY_MODES = {"retry", "retry_exhausted"}


def desired_targets(state, source, obs, mode):
    target = state.target(source["source_id"], obs["key"])
    complete = state.attempt(target.get("complete_attempt"))
    latest = state.attempt(target.get("latest_attempt"))
    if mode == "rerun":
        return bool(complete), 0
    if complete:
        if not triple_exists(complete["products"]) and complete.get("transfer_status") != "verified":
            raise RuntimeError(f"Completed products unexpectedly missing for {source['name']} {obs['sb_name']}; use explicit --rerun-completed after inspecting storage")
        return False, 0
    status, number = latest.get("status"), latest.get("retry_number", 0)
    if status == "env_failed":
        # Stopped by the environment, so it never ran; repeating it uses up no
        # retry. An explicit retry is repeated by the same retry option, an
        # ordinary attempt by any command except --retry-exhausted.
        if number:
            return mode == latest.get("kind"), number
        return mode != "retry_exhausted", 0
    if mode == "retry_exhausted":
        return status == "exhausted", 1
    if mode == "retry":
        return status == "failed" and number < 2, number + 1
    return not latest or status not in {"failed", "exhausted"}, 0


def plan(state, index, frozen, mode="normal", single=False, selected=None, begin=1, end=None, names=(), observation_keys=(),
         reuse_download_only=False, recovery_of=None, argv=None):
    """Build all identity/selection records before performing downloads."""
    s = frozen["settings"]
    if single:
        state.sync_index(index, single=True, selected_keys={o["key"] for o in selected if o["selected"]})
    run_id = uid()
    entries = [index] if single else index["sources"]
    by_key, seeds = {}, set()
    names = set(names)
    known_names = {e["source"]["name"] for e in entries}
    if names - known_names:
        raise ValueError(f"Unknown source names: {sorted(names - known_names)}")
    selected_map = {o["key"]: o for o in selected} if single else {}
    selected_keys = {o["key"] for o in selected if o["selected"]} if single else None
    for entry in entries:
        if entry["status"] != "available":
            continue
        source = entry["source"]
        in_range = source["processing_index"] >= begin and (end is None or source["processing_index"] <= end)
        if names:
            in_range = in_range and source["name"] in names
        for obs in entry["observations"]:
            if not obs["eligible"] or (single and obs["key"] not in selected_keys) or (not single and not obs["default_selected"]):
                continue
            obs = selected_map.get(obs["key"], obs)
            by_key.setdefault(obs["key"], {"observation": obs, "sources": [], "cache": entry["cache"], "cache_sha256": entry["cache_sha256"]})["sources"].append(source)
            if in_range:
                seeds.add(obs["key"])
    if observation_keys:
        requested = set(observation_keys)
        if requested - seeds:
            raise ValueError("Requested observation key is not selected by a source in this range")
        seeds &= requested
    tasks = []
    per_task = math.floor((s["walltime_hours"] - max(s["overhead_hours"], s["reserve_minutes"] / 60) - s["hours_per_observation"]) / s["hours_per_target"])
    if per_task < 1:
        raise ValueError("Configured walltime/estimates cannot fit one observation and one target; increase walltime or adjust estimates")
    for key in sorted(seeds):
        group = by_key[key]
        obs = group["observation"]
        model_key = model_identity(frozen, obs)
        normal_targets = []
        for source in group["sources"]:
            # Shared expansion is allowed for ordinary processing/retry, never
            # silently expand successful reruns beyond the explicit source scope.
            if mode == "rerun" and not (source["processing_index"] >= begin and (end is None or source["processing_index"] <= end) and (not names or source["name"] in names)):
                continue
            should, retry_number = desired_targets(state, source, obs, mode)
            if should:
                normal_targets.append({"source": source, "attempt_id": uid(), "retry_number": retry_number})
        if not normal_targets:
            continue
        # Retries build private models and may have different download policy.
        size = 1 if mode in RETRY_MODES or single else per_task
        for offset in range(0, len(normal_targets), size):
            targets = normal_targets[offset:offset + size]
            raw_root = Path(s["work_root"]) / ("observations" if single else "staging/_datasets") / dataset_dir(obs)
            public_raw_root = raw_root
            force = any(t["retry_number"] >= 2 for t in targets) and not reuse_download_only
            if force:
                raw_root = raw_root / "redownloads" / targets[0]["attempt_id"]
            task = {"id": uid(), "observation": obs, "targets": targets, "all_sources": group["sources"],
                    "cache": group["cache"], "cache_sha256": group["cache_sha256"], "raw_root": str(raw_root), "public_raw_root": str(public_raw_root), "model_key": model_key,
                    "model_root": str(Path(s["work_root"]) / "staging/_shared" / model_key),
                    "shared": not single and mode not in RETRY_MODES, "retain": single, "redownload": force}
            if reuse_download_only:
                ready = retained_download_ready(task)
                task["reuse_download_only"] = True
                task["retained_download_hashes"] = {ready[key]: digest(ready[key]) for key in ("archive", "checksum")}
            for target in targets:
                parent = raw_root if single else Path(s["work_root"]) / "staging" / target["source"]["name"] / dataset_dir(obs)
                target["path"] = str(parent / "attempts" / target["attempt_id"])
            tasks.append(task)
    run = {"schema": 1, "id": run_id, "created_at": now(), "mode": mode, "single": single,
           "index_revision": index["revision"], "frozen": frozen, "tasks": tasks,
           "selection": {"begin": begin, "end": end, "names": sorted(names), "observation_keys": list(observation_keys)},
           "selected_observations": selected if single else None}
    if argv is not None:
        run["argv"] = list(argv)  # Plan options for run --resume.
    if reuse_download_only:
        run["allow_redownload_retry"] = False
    if recovery_of is not None:
        run["recovery_of"] = recovery_of
    unique = {task["observation"]["key"]: task["observation"] for task in tasks}
    run["summary"] = {"selected_datasets": len(seeds), "datasets_needing_work": len(unique),
                      "target_attempts": sum(len(task["targets"]) for task in tasks),
                      "selected_archive_bytes_for_work": sum(obs["estimated_bytes"] for obs in unique.values())}
    root = state.root / "runs" / run_id
    write_json(root / "effective-settings.json", frozen)
    write_json(root / "execution-plan.json", run)
    rows = [{"Source_Name": t["source"]["name"], "Source_ID": t["source"]["source_id"],
             "SB": task["observation"]["sb_id"], "Beam": task["observation"]["beam"], "Observation_Key": task["observation"]["key"],
             "Attempt_ID": t["attempt_id"], "Selected": True, "Selection_Reason": task["observation"].get("selection_reason", "batch_shared_selection"),
             "Estimated_Bytes": task["observation"]["estimated_bytes"]} for task in tasks for t in task["targets"]]
    write_csv(root / "execution-plan.csv", ["Source_Name", "Source_ID", "SB", "Beam", "Observation_Key", "Attempt_ID", "Selected", "Selection_Reason", "Estimated_Bytes"], rows)
    # Run and attempts are reserved in ONE transaction. A crash cannot strand
    # half of a plan or increment only some retry counters.
    with state.connect() as db:
        db.execute("INSERT INTO runs VALUES(?,?,?)", (run_id, json.dumps(run), "active" if tasks else "complete"))
        for task in tasks:
            for target in task["targets"]:
                db.execute("INSERT INTO attempts(id,source_id,observation_key,run_id,status,kind,retry_number,version,model_key,path,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (target["attempt_id"], target["source"]["source_id"], task["observation"]["key"], run_id, "preparing", mode,
                     target["retry_number"], frozen["version"], task["model_key"], target["path"], now(), now()))
                db.execute("UPDATE attempts SET sif_sha256=? WHERE id=?", (frozen["sif_sha256"], target["attempt_id"]))
                count = db.execute("UPDATE targets SET latest_attempt=? WHERE source_id=? AND observation_key=?",
                    (target["attempt_id"], target["source"]["source_id"], task["observation"]["key"])).rowcount
                if count != 1:
                    raise StateError("Plan target missing from query index")
    return run


def pending(state, task):
    return [t for t in task["targets"] if state.attempt(t["attempt_id"])["status"] not in FINAL]


def replace_run(state, run, runner=subprocess.run, confirm=None):
    """Replace an old plan only after its submitted workers have stopped.

    confirm, when given, is called once the jobs are known to have stopped and
    raises to keep the old plan unchanged.
    """
    stopped = []
    for job in state.jobs(run["id"]):
        if job["status"] not in {"submitting", "submitted"}:
            continue
        outcome = reconcile_job(state, job, runner)
        if outcome == "waiting" and job.get("last_terminal_state"):
            outcome = reconcile_job(state, job, runner)
        if outcome == "waiting":
            raise RuntimeError(f"Slurm job {job.get('scheduler_id', job['id'])} is still queued/running or its state cannot be confirmed. Wait for it to finish, or cancel it with scancel and run this command again. Nothing has been changed.")
        if outcome == "fatal_state_error":
            raise StateError("Worker reported a state database error; repair state before starting new work")
        stopped.append(job)
    if confirm is not None:
        confirm()
    for job in stopped:
        job["status"] = "complete"
        state.store_job(job)
    state.supersede_run(run)


def discard_stale_attempts(state, task):
    """Delete working copies that replaced or environment-stopped attempts left behind.

    Only called on the login node while no job of this workspace runs. Data of
    processing failures stays for inspection.
    """
    keep = Path(task["raw_root"]).resolve()
    stale = set()
    with state.connect() as db:
        for target in task["targets"]:
            rows = db.execute("SELECT id,path FROM attempts WHERE source_id=? AND observation_key=? AND id!=? AND status IN ('env_failed','superseded')",
                              (target["source"]["source_id"], task["observation"]["key"], target["attempt_id"])).fetchall()
            stale |= {(row["id"], row["path"]) for row in rows}
    paths = []
    for attempt_id, path in stale:
        root = Path(path)
        paths += [root / "work", *root.glob("interrupted-work-*")]
        paths.append(Path(task["public_raw_root"]) / "redownloads" / attempt_id)
    for path in paths:
        if path.is_dir() and not keep.is_relative_to(path.resolve()):
            try:
                shutil.rmtree(path)
            except OSError as exc:
                print(f"Warning: could not delete stale working data {path}: {exc}", flush=True)


def pack_job(state, run):
    s = run["frozen"]["settings"]
    loads = [0.0] * s["parallel_slots"]
    tasks, seen = [], set()
    available = s["walltime_hours"] - max(s["overhead_hours"], s["reserve_minutes"] / 60)
    for original in run["tasks"]:
        left = pending(state, original)
        if not left:
            continue
        # Never process two chunks of the same dataset concurrently.
        if original["observation"]["key"] in seen:
            continue
        ready = read_json(Path(original["model_root"]) / "ready.json", {})
        reuse = original["shared"] and ready.get("model_key") == original["model_key"] and Path(ready.get("ms", "/nonexistent")).is_dir()
        hours = (0 if reuse else s["hours_per_observation"]) + len(left) * s["hours_per_target"]
        lane = min(range(len(loads)), key=loads.__getitem__)
        if loads[lane] + hours > available:
            continue
        tasks.append({**original, "targets": left, "lane": lane})
        seen.add(original["observation"]["key"])
        loads[lane] += hours
        if len(tasks) >= s["max_observations_per_job"]:
            break
    if not tasks:
        return None
    walltime = max(loads) + max(s["overhead_hours"], s["reserve_minutes"] / 60)
    return {"id": uid(), "run_id": run["id"], "frozen": run["frozen"], "tasks": tasks,
            "slots": min(s["parallel_slots"], len(tasks)), "walltime_hours": min(s["walltime_hours"], walltime),
            "status": "planned", "continuation": 0}


def retained_download_ready(task):
    """Require the saved archive pair; recovery must never fall back to CASDA.

    A missing or changed retained archive is a processing failure (DataError).
    """
    root = Path(task["raw_root"]).resolve()
    ready = read_json(root / "download-ready.json", {})
    obs = task["observation"]
    if ready.get("key") != obs["key"]:
        raise archive.DataError("Retained download identity differs; recovery cannot download again")
    hashes = task.get("retained_download_hashes")
    if (task.get("reuse_download_only") and hashes is None) or (hashes is not None
            and set(hashes) != {ready.get("archive"), ready.get("checksum")}):
        raise archive.DataError("Retained download paths differ from the frozen recovery; recovery cannot download again")
    files = ready.get("files", {})
    for role, name in (("archive", obs["filename"]), ("checksum", obs["filename"] + ".checksum")):
        value = ready.get(role)
        path = Path(value) if value else None
        if (path is None or not path.is_absolute() or path.name != name or path.is_symlink()
                or not path.resolve().is_relative_to(root) or not path.is_file()
                or path.stat().st_size <= 0 or files.get(value) != path.stat().st_size):
            raise archive.DataError(f"Retained {role} missing or changed; recovery cannot download again")
        if hashes is not None and digest(path) != hashes[value]:
            raise archive.DataError(f"Retained {role} SHA256 changed; recovery cannot download again")
    return ready


def prepare_job(state, job, client_factory=None):
    s = job["frozen"]["settings"]
    for task in job["tasks"]:
        targets = pending(state, task)
        if not targets:
            continue
        try:
            if digest(task["cache"]) != task["cache_sha256"]:
                raise RuntimeError("Frozen CASDA metadata changed; refusing to stage a different dataset")
            discard_stale_attempts(state, task)
            if task.get("previous_attempt") and not task["retain"]:
                old = state.attempt(task["previous_attempt"])
                old_root = Path(old["path"])
                for obsolete in [old_root / "work", *old_root.glob("interrupted-work-*")]:
                    if obsolete.exists():
                        shutil.rmtree(obsolete)
            kwargs = {} if client_factory is None else {"client_factory": client_factory}
            started = time.monotonic()
            if task.get("reuse_download_only"):
                retained_download_ready(task)
                detail = "reused retained archive/checksum without CASDA for dataset " + task["observation"]["key"]
            else:
                archive.download(task["observation"], task["cache"], task["raw_root"], s, **kwargs)
                detail = "one archive/checksum download call for dataset " + task["observation"]["key"]
            for target in targets:
                for step in ("archive_download", "checksum_download"):
                    state.step(target["attempt_id"], step, "Y", seconds=time.monotonic()-started if step == "archive_download" else 0, detail=detail)
        except (StateError, ImportError, archive.CasdaCredentialError):
            raise
        except Exception as exc:
            # A damaged archive is a processing failure; CASDA, network and
            # space problems are environment failures retried by the next command.
            processing = isinstance(exc, archive.DataError)
            for target in targets:
                aid = target["attempt_id"]
                state.step(aid, "archive_download", "N", detail=str(exc))
                status = ("exhausted" if target["retry_number"] >= 2 else "failed") if processing else "env_failed"
                state.update(aid, status=status, error_step="archive_download", error=str(exc))
            if isinstance(exc, archive.SpaceError):
                state.store_job(job)
                raise archive.SpaceError(f"{exc}. Processing stopped while downloading {task['observation']['sb_name']}; "
                                         "free space or raise STAGING_BUDGET_BYTES, then run the command again. "
                                         "The affected targets are retried automatically.") from None
    job["status"] = "prepared"
    state.store_job(job)


def write_sbatch(state, job):
    s = job["frozen"]["settings"]
    root = state.root / "jobs"
    root.mkdir(exist_ok=True)
    path = root / (job["id"] + ".json")
    write_json(path, job)
    seconds = math.ceil(job["walltime_hours"] * 3600)
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    log_root = Path(s["work_root"]) / "logs"
    log_root.mkdir(exist_ok=True)
    args = [s["python"], "-m", "askap_auto.worker", "--manifest", str(path)]
    lines = ["#!/bin/bash", "#SBATCH --job-name=ASKAP-auto", f"#SBATCH --output={log_root}/slurm-%j.out",
             f"#SBATCH --error={log_root}/slurm-%j.err", "#SBATCH --ntasks=1",
             f"#SBATCH --cpus-per-task={job['slots'] * s['threads_per_slot']}",
             f"#SBATCH --mem={math.ceil(s['memory_gb'] * 1024)}M", f"#SBATCH --tmp={math.ceil(s['tmp_gb'] * 1024)}M",
             f"#SBATCH --time={hours:02}:{minutes:02}:{seconds:02}"]
    for key in ("partition", "account"):
        if s[key]:
            if not __import__('re').fullmatch(r"[A-Za-z0-9_.-]+", s[key]):
                raise ValueError(f"Invalid Slurm {key}")
            lines.append(f"#SBATCH --{key}={s[key]}")
    lines += ["set -Eeuo pipefail"]
    for module in s["modules"]:
        lines.append("module load " + shlex.quote(module))
    lines += ["export PYTHONPATH=" + shlex.quote(str(Path(__file__).resolve().parent.parent)),
              "exec " + shlex.join(args)]
    script = path.with_suffix(".sbatch")
    script.write_text("\n".join(lines) + "\n")
    return script, path


def submit_job(state, job, runner=subprocess.run):
    script, path = write_sbatch(state, job)
    job["status"] = "submitting"
    job["manifest_path"] = str(path)
    state.store_job(job)  # Persist intent BEFORE invoking sbatch.
    try:
        result = runner(["sbatch", "--parsable", str(script)], text=True, capture_output=True, check=True)
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or str(exc)).strip()
        if re.search(r"invalid (?:account|partition|qos|time limit)|invalid.*account/partition", detail, re.IGNORECASE):
            job["status"] = "prepared"
            state.store_job(job)
            raise RuntimeError("Slurm rejected the job: " + detail) from None
        raise RuntimeError("Slurm submission could not be confirmed: " + detail
                           + "; check Slurm and use resolve-submission before resuming") from None
    scheduler_id = result.stdout.strip().split(";")[0]
    if not scheduler_id.isdigit():
        raise RuntimeError("sbatch response is uncertain; inspect Slurm before resolving this job")
    write_json(path.with_suffix(".submission.json"), {"job_id": scheduler_id, "submitted_at": now()})
    job.update(status="submitted", scheduler_id=scheduler_id)
    state.store_job(job)
    for task in job["tasks"]:
        for target in pending(state, task):
            # A fast worker may already be running: never overwrite that status.
            with state.connect() as db:
                db.execute("UPDATE attempts SET status='queued',updated_at=? WHERE id=? AND status='preparing'", (now(), target["attempt_id"]))


def reconcile_job(state, job, runner=subprocess.run):
    path = Path(job["manifest_path"])
    receipt = read_json(path.with_suffix(".submission.json"))
    if not receipt:
        raise RuntimeError(f"Submission outcome unknown for {job['id']}; use resolve-submission after checking Slurm. No duplicate job will be submitted.")
    outcome = read_json(path.with_suffix(".outcome.json"))
    if outcome:
        return outcome["status"]
    result = runner(["sacct", "-n", "-X", "-j", receipt["job_id"], "--format=JobIDRaw,State", "--parsable2"], text=True, capture_output=True, check=False)
    terminal = {"COMPLETED", "FAILED", "CANCELLED", "TIMEOUT", "OUT_OF_MEMORY", "NODE_FAIL", "PREEMPTED", "BOOT_FAIL", "DEADLINE", "REVOKED"}
    states = []
    for line in result.stdout.splitlines():
        parts = line.split("|")
        if len(parts) > 1 and parts[0] == receipt["job_id"] and parts[1].strip():
            states.append(parts[1].split()[0].rstrip("+"))
    if result.returncode == 0 and states and states[0] in terminal:
        if job.get("last_terminal_state") == states[0]:
            return "partial" if states[0] in {"TIMEOUT", "PREEMPTED"} else "failed"
        job["last_terminal_state"] = states[0]
    else:
        job.pop("last_terminal_state", None)
    state.store_job(job)
    return "waiting"


def fail_pending(state, job, reason):
    """Mark the targets a job left unfinished as environment failures."""
    for task in job["tasks"]:
        for target in pending(state, task):
            state.update(target["attempt_id"], status="env_failed", error_step="worker", error=reason)


def add_environment_retries(state, run):
    """Once per command, give each environment failure of this plan a new attempt.

    The retry number is unchanged: environment failures never use up a retry.
    """
    added, kinds = [], {}
    for task in run["tasks"]:
        targets = []
        for target in task["targets"]:
            attempt = state.attempt(target["attempt_id"])
            latest = state.target(target["source"]["source_id"], task["observation"]["key"]).get("latest_attempt")
            if attempt.get("status") == "env_failed" and latest == target["attempt_id"]:
                retry = copy.deepcopy(target)
                retry["attempt_id"] = uid()
                retry["path"] = str(Path(target["path"]).parent / retry["attempt_id"])
                kinds[retry["attempt_id"]] = attempt["kind"]
                targets.append(retry)
        if targets:
            child = copy.deepcopy(task)
            child.update(id=uid(), targets=targets)
            child.pop("redownload_created", None)
            added.append(child)
    if not added:
        return 0
    run["tasks"].extend(added)
    with state.connect() as db:
        for task in added:
            for target in task["targets"]:
                source, obs = target["source"], task["observation"]
                db.execute("INSERT INTO attempts(id,source_id,observation_key,run_id,status,kind,retry_number,version,model_key,path,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (target["attempt_id"], source["source_id"], obs["key"], run["id"], "preparing", kinds[target["attempt_id"]], target["retry_number"],
                     run["frozen"]["version"], task["model_key"], target["path"], now(), now()))
                db.execute("UPDATE attempts SET sif_sha256=? WHERE id=?", (run["frozen"]["sif_sha256"], target["attempt_id"]))
                db.execute("UPDATE targets SET latest_attempt=? WHERE source_id=? AND observation_key=?", (target["attempt_id"], source["source_id"], obs["key"]))
        db.execute("UPDATE runs SET data=? WHERE id=?", (json.dumps(run), run["id"]))
    return sum(len(task["targets"]) for task in added)


def add_redownload_retries(state, run):
    """One explicit retry request allows one private rebuild then one redownload."""
    if run["mode"] not in RETRY_MODES or not run.get("allow_redownload_retry", True):
        return False
    added = []
    for task in run["tasks"]:
        if len(task["targets"]) != 1:
            continue
        target = task["targets"][0]
        attempt = state.attempt(target["attempt_id"])
        if attempt["status"] != "failed" or attempt["retry_number"] != 1 or task.get("redownload_created"):
            continue
        child = copy.deepcopy(task)
        child["id"] = uid()
        child["redownload"] = True
        child["previous_attempt"] = target["attempt_id"]
        new_target = child["targets"][0]
        new_target["attempt_id"] = uid()
        new_target["retry_number"] = 2
        child["raw_root"] = str(Path(task["raw_root"]) / "redownloads" / new_target["attempt_id"])
        new_target["path"] = str(Path(target["path"]).parent / new_target["attempt_id"])
        added.append(child)
        task["redownload_created"] = True
    if not added:
        return False
    run["tasks"].extend(added)
    with state.connect() as db:
        for task in added:
            target = task["targets"][0]
            source, obs = target["source"], task["observation"]
            db.execute("INSERT INTO attempts(id,source_id,observation_key,run_id,status,kind,retry_number,version,model_key,path,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (target["attempt_id"], source["source_id"], obs["key"], run["id"], "preparing", run["mode"], 2, run["frozen"]["version"], task["model_key"], target["path"], now(), now()))
            db.execute("UPDATE attempts SET sif_sha256=? WHERE id=?", (run["frozen"]["sif_sha256"], target["attempt_id"]))
            db.execute("UPDATE targets SET latest_attempt=? WHERE source_id=? AND observation_key=?", (target["attempt_id"], source["source_id"], obs["key"]))
        db.execute("UPDATE runs SET data=? WHERE id=?", (json.dumps(run), run["id"]))
    return True


def run_controller(state, run, submit=False, prepare_only=False, runner=subprocess.run, client_factory=None):
    if not run["tasks"]:
        return state.export()
    retried = add_environment_retries(state, run)
    if retried:
        print(f"Retrying {retried} target(s) stopped by the environment (download, space or Slurm job).", flush=True)
    s = run["frozen"]["settings"]
    while True:
        jobs = state.jobs(run["id"])
        active = [job for job in jobs if job["status"] != "complete"]
        job = active[-1] if active else pack_job(state, run)
        if job is None:
            if add_redownload_retries(state, run):
                continue
            if any(pending(state, task) for task in run["tasks"]):
                raise RuntimeError("Remaining tasks cannot fit the frozen resource plan")
            state.store_run(run, "complete")
            return state.export()
        if not active:
            state.store_job(job)
        print(f"Job {job['id']}: {len(job['tasks'])} datasets, {job['slots']} slots, {job['walltime_hours']:.2f} h, {job['slots'] * s['threads_per_slot']} CPUs", flush=True)
        if not submit and not prepare_only:
            if job["status"] in {"planned", "prepared"}:
                write_sbatch(state, job)
            return state.export()
        if job["status"] == "planned":
            prepare_job(state, job, client_factory)
        if job["status"] in {"planned", "prepared"} and not any(pending(state, task) for task in job["tasks"]):
            job["status"] = "complete"
            state.store_job(job)
            continue
        if not submit:
            if job["status"] in {"planned", "prepared"}:
                write_sbatch(state, job)
            return state.export()
        if job["status"] == "prepared":
            submit_job(state, job, runner)
        outcome = reconcile_job(state, job, runner)
        if outcome == "fatal_state_error":
            raise StateError("Worker reported a fatal state database error; repair state before resuming")
        if outcome == "waiting":
            state.export()
            time.sleep(s["poll_seconds"])
            continue
        if outcome == "partial":
            continuation = job["continuation"] + 1
            if continuation <= s["max_continuations"]:
                # Immutable earlier job/receipt/outcome remain available for audit.
                job["status"] = "complete"
                state.store_job(job)
                continuation_job = pack_job(state, run)
                if continuation_job:
                    continuation_job["continuation"] = continuation
                    state.store_job(continuation_job)
                continue
            fail_pending(state, job, "Bounded walltime continuation exhausted")
        elif outcome == "stopped":
            reason = read_json(Path(job["manifest_path"]).with_suffix(".outcome.json"), {}).get("error", "unknown reason")
            fail_pending(state, job, reason)
            job["status"] = "complete"
            state.store_job(job)
            state.export()
            raise RuntimeError(f"Slurm job {job.get('scheduler_id')} stopped: {reason}. Fix the cause and run the command again; "
                               "the unfinished targets are retried automatically.")
        elif outcome != "finished":
            slurm = job.get("last_terminal_state", "unknown")
            fail_pending(state, job, f"Slurm job ended ({slurm}) without a completion record")
            if slurm not in TRANSIENT_JOB_STATES:
                job["status"] = "complete"
                state.store_job(job)
                state.export()
                raise RuntimeError(f"Slurm job {job.get('scheduler_id')} ended ({slurm}) without a completion record; see "
                                   f"{Path(s['work_root']) / 'logs'}/slurm-{job.get('scheduler_id')}.err. Fix the cause and "
                                   "run the command again; the unfinished targets are retried automatically.")
        job["status"] = "complete"
        state.store_job(job)
        state.export()
