"""Offline scientific execution with frozen options and explicit retention."""
from __future__ import annotations
import json
import math
import os
import re
import shlex
import signal
import shutil
import subprocess
import time
from pathlib import Path
from . import archive
from .catalogue import position_at
from .common import digest, lock, now, read_json, tree_size, uid, write_json
from .crop import crop_fits
from .state import StateError
from .settings import INTEGER_MODEL_KEYS, integer_model_value

PACKAGE = Path(__file__).resolve().parent
# dstools-subtract-model --split-ms writes the residuals to DATA of a new MS.
EXTRACT_DATACOLUMN = "data"


class Partial(RuntimeError):
    pass


def run_process(command, *, cwd=None, stdout=None, stderr=None, text=True, timeout=None, check=True):
    """Terminate the whole container/tool process group on a walltime timeout."""
    with subprocess.Popen(command, cwd=cwd, stdout=stdout, stderr=stderr, text=text,
                          start_new_session=True) as process:
        try:
            output, errors = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.communicate()
            raise
        if check and process.returncode:
            raise subprocess.CalledProcessError(process.returncode, command, output, errors)
        return subprocess.CompletedProcess(command, process.returncode, output, errors)


def dataset_dir(obs):
    # Filename-independent identity suffix prevents dataset revisions colliding.
    return obs["sb_name"] + "--" + obs["key"][:12]


def model_options(settings):
    """Pass every MODEL value explicitly; None (and False for a switch) is not passed."""
    result = []
    for key, value in settings["model"].items():
        if value is None:
            continue
        if key in INTEGER_MODEL_KEYS:
            # Keep numeric intent and the frozen dictionary intact; Click INT
            # accepts "3" but refuses "3.0" before scientific execution.
            value = integer_model_value(key, value)
        if value is False:
            continue
        result.append("--" + key)
        if not isinstance(value, bool):
            result.extend(str(x) for x in value) if isinstance(value, (list, tuple)) else result.append(str(value))
    return result


def slot_memory(settings, slots=None):
    """Match UCDs: divide allocated MiB by actual job slots, reserve 25%."""
    slots = settings["parallel_slots"] if slots is None else slots
    budget_mib = math.ceil(settings["memory_gb"] * 1024) // slots
    return budget_mib, max(1, int(budget_mib / 1024 * 0.75))


def create_model_command(settings, ms, temp, slots=None):
    # Output directory and prefix are the same DStools defaults used by UCDs.
    return ["dstools-create-model", "--verbose", *model_options(settings), "--threads", str(settings["threads_per_slot"]),
            "--abs-mem", str(slot_memory(settings, slots)[1]),
            "--temp-dir", str(temp), str(ms)]


def insert_model_command(settings, ra, dec, model, ms):
    arguments = ["dstools-insert-model", "--mask-pos", f"{ra:.12f}", f"{dec:.12f}"]
    if settings["mask_radius_arcsec"] is not None:
        arguments += ["--mask-radius", str(settings["mask_radius_arcsec"])]
    return [*arguments, str(model), str(ms)]


def extract_ds_command(settings, source_name, ra, dec, ms, ds):
    # The residual column, baseline averaging and flag handling are always explicit.
    args = ["dstools-extract-ds", "--datacolumn", EXTRACT_DATACOLUMN,
            "--minuvdist", f"{settings['extract_minuv_m']:g}",
            "--phasecentre", f"{ra:.12f}", f"{dec:.12f}",
            "--baseline-average" if settings["extract_baseline_average"] else "--no-baseline-average"]
    if settings["extract_noflag"]:
        args.append("--noflag")
    pb = settings["primary_beam_by_source"].get(source_name, settings["primary_beam"])
    if pb:
        args += ["--primary-beam", pb]
    return [*args, str(ms), str(ds)]


def subtract_model_command(ms):
    return ["dstools-subtract-model", "--split-ms", str(ms)]


def container_command(settings, arguments, scratch, slots=None):
    threads = settings["threads_per_slot"]
    memory_mib, absolute_memory = slot_memory(settings, slots)
    scratch = Path(scratch)
    scratch.mkdir(parents=True, exist_ok=True)
    command = [settings["apptainer"], "exec", "--cleanenv", "--no-home"]
    binds = {str(Path(settings["work_root"]).resolve()), str(Path(settings["product_root"]).resolve()), str(PACKAGE.parent)}
    for path in (*[settings["model"][k] for k in ("fits-mask", "target-mask", "galvin-clip-mask")], settings["primary_beam"], *settings["primary_beam_by_source"].values()):
        if path:
            binds.add(str(Path(path).resolve().parent))
    auto_binds = []
    for bind in sorted(binds):
        if not any(Path(bind).is_relative_to(Path(parent)) for parent in auto_binds):
            auto_binds.append(bind)
    for bind in auto_binds + list(settings["bind"]):
        command += ["--bind", bind]
    env = {"MPLCONFIGDIR": str(scratch / "matplotlib"), "OMP_NUM_THREADS": str(threads),
           "OPENBLAS_NUM_THREADS": "1", "MKL_NUM_THREADS": str(threads), "NUMEXPR_NUM_THREADS": str(threads),
           "SLURM_CPUS_PER_TASK": str(threads), "SLURM_MEM_PER_NODE": str(memory_mib),
           "ASKAP_WSCLEAN_SLOT_THREADS": str(threads), "ASKAP_WSCLEAN_ABS_MEM": str(absolute_memory)}
    for key, value in env.items():
        command += ["--env", key + "=" + value]
    command += [settings["container"], "/bin/bash", "-c",
                'export PATH=' + shlex.quote(str(PACKAGE / "wsclean_slot")) + ':"$PATH"; exec "$@"', "bash", *map(str, arguments)]
    return command


def validate_frozen(frozen):
    s = frozen["settings"]
    if digest(s["container"]) != frozen["sif_sha256"]:
        raise RuntimeError("SIF changed after this run was prepared")
    for path, expected in frozen["external_files"].items():
        if digest(path) != expected:
            raise RuntimeError(f"Scientific input changed after preparation: {path}")


def preflight(frozen, runner=subprocess.run):
    """Check the actual installed image, never silently accept dev-only flags."""
    s = frozen["settings"]
    flags = {
        "dstools-askap-preprocess": [],
        "dstools-create-model": [x for x in model_options(s) if x.startswith("--")] + ["--verbose", "--threads", "--abs-mem", "--temp-dir"],
        "dstools-insert-model": ["--mask-pos"] + (["--mask-radius"] if s["mask_radius_arcsec"] is not None else []),
        "dstools-subtract-model": ["--split-ms"],
        "dstools-extract-ds": ["--datacolumn", "--minuvdist", "--phasecentre",
                               "--baseline-average" if s["extract_baseline_average"] else "--no-baseline-average"]
            + (["--noflag"] if s["extract_noflag"] else [])
            + (["--primary-beam"] if s["primary_beam"] or s["primary_beam_by_source"] else []),
    }
    help_texts = {}
    for tool, options in flags.items():
        result = runner(container_command(s, [tool, "--help"], Path(s["work_root"]) / "staging/_preflight",
                                          slots=frozen.get("runtime_slots")), text=True, capture_output=True, check=True)
        output = result.stdout + result.stderr
        missing = [flag for flag in options if not re.search(re.escape(flag) + r"(?=[\s,=/\]])", output)]
        if missing:
            raise RuntimeError(f"Deployed SIF does not support {tool} options: {missing}")
        help_texts[tool] = output
    return help_texts


def triple_exists(products):
    return set(products) == {"ds", "stokes_i", "stokes_v"} and all(Path(p).is_file() for p in products.values())


class Executor:
    def __init__(self, frozen, state, deadline=None, command_runner=None):
        self.frozen, self.s, self.state, self.deadline = frozen, frozen["settings"], state, deadline
        self.runtime_slots = frozen.get("runtime_slots", self.s["parallel_slots"])
        self.command_runner = command_runner or run_process

    def command(self, arguments, cwd, log, container=True, capture=False):
        remaining = None if self.deadline is None else self.deadline - time.monotonic() - self.s["reserve_minutes"] * 60
        if remaining is not None and remaining <= 0:
            raise Partial("Reached walltime exit reserve")
        cwd, log = Path(cwd), Path(log)
        cwd.mkdir(parents=True, exist_ok=True)
        log.parent.mkdir(parents=True, exist_ok=True)
        command = container_command(self.s, arguments, cwd / "tmp", slots=self.runtime_slots) if container else list(map(str, arguments))
        with log.open("a") as handle:
            handle.write(f"\n{now()} {shlex.join(command)}\n")
            handle.flush()
            try:
                result = self.command_runner(command, cwd=cwd, stdout=subprocess.PIPE if capture else handle,
                    stderr=handle, text=True, timeout=remaining, check=True)
            except subprocess.TimeoutExpired as exc:
                raise Partial("Command reached walltime reserve") from exc
        return result.stdout if capture else None

    def copy(self, source, destination):
        with lock(self.state.root / "copy.lock", wait=True):
            archive.check_space(self.s["work_root"], self.s, tree_size(source))
            try:
                shutil.copytree(source, destination)
            except OSError as exc:
                if archive.no_space(exc):
                    raise archive.SpaceError(f"No space left while copying {source}: {exc}") from None
                raise

    def create_model(self, ms, directory, log):
        temp = directory / "tmp"
        temp.mkdir(parents=True, exist_ok=True)
        args = create_model_command(self.s, ms, temp, slots=self.runtime_slots)
        self.command(args, directory, log)
        model = directory / "wsclean_model"
        expected = [model / f"wsclean-MFS-{p}-image.fits" for p in ("I", "V")]
        if not all(p.is_file() for p in expected) or not (model / "wsclean-MFS-I-model.fits").is_file() or not any("MFS" not in p.name for p in model.glob("*-model.fits")):
            raise RuntimeError("create-model did not produce I/V images and channel models")
        return model

    def raw(self, task):
        root = Path(task["raw_root"])
        log = root / "extract.log"
        return archive.extract(root, task["observation"], task["retain"],
            lambda command: self.command(command, root, log, container=False), self.s)

    def master(self, task, raw):
        root = Path(task["model_root"])
        root.mkdir(parents=True, exist_ok=True)
        with lock(root / "model.lock", wait=True):
            ready = read_json(root / "ready.json", {})
            if ready.get("model_key") == task["model_key"] and Path(ready["ms"]).is_dir() and all(Path(ready["model"]).joinpath(f"wsclean-MFS-{p}-image.fits").is_file() for p in ("I", "V")):
                return ready
            # An unfinished build is never resumed: delete it before building again.
            (root / "ready.json").unlink(missing_ok=True)
            for stale in root.glob("build-*"):
                shutil.rmtree(stale)
            work = root / ("build-" + uid())
            work.mkdir()
            ms = work / "input.ms"
            self.copy(raw, ms)
            for target in task["targets"]:
                self.state.update(target["attempt_id"], status="running", current_step="preprocess")
            start = time.monotonic()
            self.command(["dstools-askap-preprocess", str(ms)], work, root / "model.log")
            preprocess_seconds = time.monotonic() - start
            for target in task["targets"]:
                self.state.step(target["attempt_id"], "preprocess", "Y", seconds=0, detail="shared:" + task["model_key"])
            start = time.monotonic()
            for target in task["targets"]:
                self.state.update(target["attempt_id"], current_step="create_model")
            model = self.create_model(ms, work, root / "model.log")
            ready = {"model_key": task["model_key"], "ms": str(ms), "model": str(model),
                     "runtime_slots": self.runtime_slots, "slot_memory_mib": slot_memory(self.s, self.runtime_slots)[0],
                     "preprocess_seconds": preprocess_seconds, "create_model_seconds": time.monotonic() - start}
            write_json(root / "history" / (work.name + ".json"), ready)
            write_json(root / "ready.json", ready)
            return ready

    def observation_time(self, obs, raw, work, log):
        if obs.get("t_min") is not None and math.isfinite(obs["t_min"]):
            return {"mjd": obs["t_min"], "scale": "utc", "source": "CASDA"}
        try:
            result = self.command([self.s["container_python"], str(PACKAGE / "ms_time.py"), str(raw)], work, log, capture=True)
            return json.loads(result.strip().splitlines()[-1])
        except Partial:
            raise
        except Exception as exc:
            return {"mjd": None, "scale": None, "source": "unavailable", "error": str(exc)}

    def target(self, task, target, raw, master=None):
        source, obs = target["source"], task["observation"]
        attempt_id = target["attempt_id"]
        attempt = self.state.attempt(attempt_id)
        if attempt["status"] in {"complete", "failed", "exhausted", "env_failed"}:
            return
        root = Path(attempt["path"])
        root.mkdir(parents=True, exist_ok=True)
        log = root / "logs/commands.log"
        self.state.update(attempt_id, status="running", log=str(log), current_step="observation_time")
        active = "observation_time"
        checkpoints = self.state.steps(attempt_id)
        work = root / "work"
        try:
            epoch = self.observation_time(obs, raw, root, log)
            self.state.update(attempt_id, time_mjd=epoch["mjd"], time_scale=epoch["scale"], time_source=epoch["source"])
            self.state.step(attempt_id, "observation_time", "Y" if epoch["mjd"] is not None else "NA", detail=epoch.get("error"))
            active = "proper_motion"
            self.state.update(attempt_id, current_step=active)
            ra, dec, motion_status = position_at(source, epoch["mjd"], epoch["scale"] or "utc")
            self.state.update(attempt_id, ra=ra, dec=dec)
            self.state.step(attempt_id, active, motion_status)
            # Restart interrupted mutating stages from clean private copies.
            # Only committed DS or completed model checkpoints may be reused.
            ds = root / "products" / (obs["sb_name"] + ".ds")
            if checkpoints.get("extract_ds", {}).get("status") != "Y" or not ds.is_file():
                ds.unlink(missing_ok=True)
                # Interrupted work is never resumed: set it aside, then delete it.
                if work.exists():
                    work.rename(root / ("interrupted-work-" + uid()))
                for stale in root.glob("interrupted-work-*"):
                    shutil.rmtree(stale)
                work.mkdir()
                ms = work / "input.ms"
                self.copy(Path(master["ms"]) if master else raw, ms)
                if master:
                    self.copy(Path(master["model"]), work / "wsclean_model")
                    self.state.update(attempt_id, model_reused=1)
                    for step in ("preprocess", "create_model"):
                        self.state.step(attempt_id, step, "Y", seconds=0, detail="shared:" + task["model_key"])
                else:
                    active = "preprocess"
                    self.run_step(attempt_id, active, ["dstools-askap-preprocess", str(ms)], work, log)
                    active = "create_model"
                    self.state.update(attempt_id, current_step=active)
                    start = time.monotonic()
                    self.create_model(ms, work, log)
                    self.state.step(attempt_id, active, "Y", time.monotonic()-start)
                    self.state.update(attempt_id, model_reused=0)
                # Each batch model is already private; only single-source
                # preservation needs a second unmasked model copy.
                insertion = work / "wsclean_model"
                if task["retain"]:
                    insertion = work / "insertion_model"
                    self.copy(work / "wsclean_model", insertion)
                active = "insert_model"
                self.run_step(attempt_id, active, insert_model_command(self.s, ra, dec, insertion, ms), work, log)
                active = "subtract_model"
                self.run_step(attempt_id, active, subtract_model_command(ms), work, log)
                subtracted = work / "input.subtracted.ms"
                if not subtracted.is_dir():
                    raise RuntimeError("subtract-model did not create input.subtracted.ms")
                active = "extract_ds"
                ds.parent.mkdir(exist_ok=True)
                args = extract_ds_command(self.s, source["name"], ra, dec, subtracted, ds)
                self.run_step(attempt_id, active, args, work, log)
                if not ds.is_file():
                    raise RuntimeError("extract-ds did not create output DS")
            products = {"ds": str(ds)}
            for pol in ("I", "V"):
                active = "crop_" + pol.lower()
                image = work / "wsclean_model" / f"wsclean-MFS-{pol}-image.fits"
                output = root / "cutouts" / f"wsclean-MFS-{pol}-image.fits"
                self.state.update(attempt_id, current_step=active)
                start = time.monotonic()
                crop_fits(image, ra, dec, self.s["crop_arcmin"], output=output)
                self.state.step(attempt_id, active, "Y", time.monotonic()-start)
                products["stokes_" + pol.lower()] = str(output)
            active = "product_copy"
            self.state.update(attempt_id, current_step=active)
            if not triple_exists(products):
                raise RuntimeError("Required DS/I/V product triple missing")
            if not task["retain"]:
                products = self.publish(source, obs, attempt_id, products, task)
            else:
                write_json(root / "observation.json", self.provenance(source, obs, attempt_id, task))
            self.state.step(attempt_id, "product_copy", "Y")
            # Completion is durable BEFORE cleanup; cleanup errors never retry science.
            self.state.complete(attempt_id, products)
            if task["retain"]:
                self.state.step(attempt_id, "cleanup", "NA", detail="single-source preservation")
            else:
                try:
                    shutil.rmtree(work)
                    self.state.step(attempt_id, "cleanup", "Y")
                except OSError as exc:
                    self.state.step(attempt_id, "cleanup", "N", detail=str(exc))
                    self.state.update(attempt_id, cleanup_error=str(exc))
        except Partial:
            self.state.update(attempt_id, status="partial")
            raise
        except StateError:
            raise
        except archive.SpaceError as exc:
            # Environment failure: retried by the next command; this job stops.
            self.state.step(attempt_id, active, "N", detail=str(exc))
            self.state.update(attempt_id, status="env_failed", error_step=active, error=str(exc))
            raise
        except Exception as exc:
            self.state.step(attempt_id, active, "N", detail=str(exc))
            self.state.update(attempt_id, status="exhausted" if attempt["retry_number"] >= 2 else "failed", error_step=active, error=str(exc))

    def run_step(self, attempt, step, args, work, log):
        self.state.update(attempt, current_step=step)
        start = time.monotonic()
        self.command(args, work, log)
        self.state.step(attempt, step, "Y", time.monotonic()-start)

    def provenance(self, source, obs, attempt, task):
        return {"schema": 2, "source_id": source["source_id"], "source_name": source["name"], "observation_key": obs["key"],
                "sb_id": obs["sb_id"], "beam": obs["beam"], "sb_name": obs["sb_name"], "attempt_id": attempt,
                "settings_version": self.frozen["version"], "sif_sha256": self.frozen["sif_sha256"],
                "model_key": task["model_key"], "filename": obs["filename"], "published_at": now()}

    def publish(self, source, obs, attempt, products, task):
        root = Path(self.s["product_root"])
        version = root / ".versions" / source["source_id"] / obs["key"] / attempt
        temporary = version.with_name("." + attempt + ".tmp-" + uid())
        temporary.mkdir(parents=True)
        destinations = {"ds": obs["sb_name"] + ".ds", "stokes_i": "wsclean_model/wsclean-MFS-I-image.fits", "stokes_v": "wsclean_model/wsclean-MFS-V-image.fits"}
        for role, name in destinations.items():
            destination = temporary / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(products[role], destination)
        write_json(temporary / "observation.json", self.provenance(source, obs, attempt, task))
        if version.exists():
            # Crash after immutable publication: identity must agree before reuse.
            if read_json(version / "observation.json", {}).get("attempt_id") != attempt:
                raise RuntimeError("Product version conflict")
            shutil.rmtree(temporary)
        else:
            os.replace(temporary, version)
        public = root / source["name"] / dataset_dir(obs)
        public.parent.mkdir(parents=True, exist_ok=True)
        link = public.with_name("." + public.name + "." + uid())
        link.symlink_to(os.path.relpath(version, public.parent), target_is_directory=True)
        os.replace(link, public)
        return {role: str(version / name) for role, name in destinations.items()}
