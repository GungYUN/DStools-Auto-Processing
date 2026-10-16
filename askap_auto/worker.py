"""Offline worker. No imports of batch config and no CASDA network operations."""
from __future__ import annotations
import argparse
import os
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from . import archive
from .common import lock, read_json, write_json
from .controller import pending, retained_download_ready
from .science import Executor, Partial, preflight, validate_frozen
from .state import State, StateError


def cleanup_group(state, task):
    if task["retain"]:
        return
    obs = task["observation"]
    for source in task["all_sources"]:
        target = state.target(source["source_id"], obs["key"])
        if not target.get("complete_attempt") or state.attempt(target.get("latest_attempt")).get("status") != "complete":
            return
    try:
        for root in {Path(task["raw_root"]), Path(task.get("public_raw_root", task["raw_root"]))}:
            if (root / "raw").exists():
                shutil.rmtree(root / "raw")
            (root / "raw-ready.json").unlink(missing_ok=True)
        model = Path(task["model_root"])
        for build in model.glob("build-*"):
            shutil.rmtree(build)
        (model / "ready.json").unlink(missing_ok=True)
    except OSError as exc:
        for target in task["targets"]:
            state.update(target["attempt_id"], cleanup_error=str(exc))


def execute_task(state, executor, task):
    if not pending(state, task):
        return
    try:
        for target in pending(state, task):
            state.update(target["attempt_id"], status="running", current_step="extract_archive", log=str(Path(task["raw_root"]) / "extract.log"))
        if task.get("reuse_download_only"):
            retained_download_ready(task)
        raw = executor.raw(task)
        for target in pending(state, task):
            state.step(target["attempt_id"], "extract_archive", "Y")
        master = executor.master(task, raw) if task["shared"] else None
    except Partial:
        raise
    except StateError:
        raise
    except Exception as exc:
        # Space exhaustion is an environment failure and stops this job;
        # anything else here is a processing failure of the data.
        space = isinstance(exc, archive.SpaceError)
        for target in pending(state, task):
            step = state.attempt(target["attempt_id"]).get("current_step") or "extract_archive"
            log = Path(task["raw_root"]) / "extract.log" if step == "extract_archive" else Path(task["model_root"]) / "model.log"
            state.step(target["attempt_id"], step, "N", detail=str(exc))
            status = "env_failed" if space else "exhausted" if target["retry_number"] >= 2 else "failed"
            state.update(target["attempt_id"], status=status, error_step=step, error=str(exc), log=str(log))
        if space:
            raise
        return
    for target in pending(state, task):
        executor.target(task, target, raw, master)
    cleanup_group(state, task)


def run_job(job, manifest_path, executor_factory=Executor, preflight_fn=preflight):
    frozen = job["frozen"]
    s = frozen["settings"]
    state = State(s["work_root"])
    path = Path(manifest_path)
    allocated = int(os.environ.get("SLURM_CPUS_PER_TASK", job["slots"] * s["threads_per_slot"]))
    if allocated < job["slots"] * s["threads_per_slot"]:
        raise RuntimeError("Allocated Slurm CPUs are fewer than the frozen plan")
    with lock(state.root / "compute.lock"):
        runtime_frozen = {**frozen, "runtime_slots": job["slots"]}
        preflight_path = state.root / "runs" / job["run_id"] / "sif-help.json"
        try:
            validate_frozen(frozen)
            if not preflight_path.is_file():
                write_json(preflight_path, {"sif": frozen["sif_sha256"], "help": preflight_fn(runtime_frozen)})
        except (RuntimeError, OSError, subprocess.CalledProcessError) as exc:
            # The image would fail the same way for every job: stop and report why.
            write_json(path.with_suffix(".outcome.json"), {"status": "stopped", "error": "Image check failed: " + str(exc)})
            return 1
        deadline = time.monotonic() + job["walltime_hours"] * 3600
        stopped = Event()
        partial = Event()
        reasons = []
        def lane(number):
            executor = executor_factory(runtime_frozen, state, deadline)
            try:
                for task in job["tasks"]:
                    if task["lane"] != number or stopped.is_set():
                        continue
                    execute_task(state, executor, task)
            except Partial:
                partial.set()
                stopped.set()
            except archive.SpaceError as exc:
                reasons.append(str(exc))
                stopped.set()
            except Exception:
                stopped.set()
                raise
        try:
            with ThreadPoolExecutor(max_workers=job["slots"]) as pool:
                futures = [pool.submit(lane, number) for number in range(job["slots"])]
                for future in futures:
                    future.result()
        except StateError as exc:
            write_json(path.with_suffix(".outcome.json"), {"status": "fatal_state_error", "error": str(exc)})
            raise
        if reasons:
            write_json(path.with_suffix(".outcome.json"), {"status": "stopped", "error": reasons[0]})
            return 1
        outcome = "partial" if partial.is_set() or any(pending(state, t) for t in job["tasks"]) else "finished"
        write_json(path.with_suffix(".outcome.json"), {"status": outcome})
        return 75 if outcome == "partial" else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args(argv)
    return run_job(read_json(args.manifest), args.manifest)

if __name__ == "__main__":
    raise SystemExit(main())
