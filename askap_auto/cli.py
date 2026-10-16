"""Batch and single-source commands read the same config.py and dstools-config.py."""
from __future__ import annotations
import argparse
import json
import shlex
import sys
from pathlib import Path
from .catalogue import load_catalogue, make_source
from .common import lock, read_json, safe_path, signature, write_json
from .controller import plan, replace_run, run_controller
from .query import query_catalogue, query_one, select
from .settings import NESTED, SETTING_KEYS, dstools_config_path, freeze, load, query_settings, validate
from .state import State
from .run_log import invocation

# Options that do not define a plan: run --resume accepts only these.
EXECUTION_OPTIONS = ("--submit", "--prepare-download")
LOCATION_OPTIONS = ("--work-dir", "--config", "--dstools-config")
PATH_OPTIONS = (*LOCATION_OPTIONS, "--catalogue", "--container")
MODE_LABELS = {"normal": "ordinary processing", "retry": "--retry-failed",
               "retry_exhausted": "--retry-exhausted", "rerun": "--rerun-completed"}


def parsed_value(raw):
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def pairs(values):
    output = {}
    for item in values or []:
        if "=" not in item:
            raise ValueError("Expected KEY=VALUE (JSON numbers/booleans/null/lists, or a string)")
        key, value = item.split("=", 1)
        output[key] = parsed_value(value)
    return output


def add_settings(parser, single, science=False, query=False):
    parser.add_argument("--work-dir", dest="work_root", help="Persistent work directory; never reuse the old UCD workspace")
    parser.add_argument("--config", type=Path, default=Path(__file__).resolve().parent.parent / "config.py", help="Editable config.py, shared by both processing modes")
    parser.add_argument("--dstools-config", type=Path, default=None, help="Editable dstools-config.py with the DStools options; default: next to --config")
    parser.add_argument("--set", action="append", default=[], metavar="KEY=VALUE", help="Override a documented setting; JSON null disables optional limits")
    if query:
        parser.add_argument("--ra-unit", choices=("deg", "hourangle"), default=None)
        parser.add_argument("--dec-unit", choices=("deg",), default=None)
        parser.add_argument("--coordinate-epoch", default=None, help="Reference epoch, e.g. J2000")
        parser.add_argument("--pm-unit", default=None, help="Angular motion unit; default mas/yr")
        parser.add_argument("--pmra-includes-cosdec", action=argparse.BooleanOptionalAction, default=None)
        parser.add_argument("--radius-frequency-mode", choices=("central", "top"), default=None)
        parser.add_argument("--exposure-min", type=parsed_value, default=argparse.SUPPRESS, help="Strict lower exposure bound, seconds; use null to disable")
        parser.add_argument("--minimum-sb", default=None)
        parser.add_argument("--query-limit", type=int, default=None)
    if science:
        parser.add_argument("--container", default=None)
        parser.add_argument("--parallel-slots", type=int, default=None)
        parser.add_argument("--threads-per-slot", type=int, default=None)
        parser.add_argument("--memory-gb", type=float, default=None)
        parser.add_argument("--walltime-hours", type=float, default=None)
        parser.add_argument("--crop-arcmin", type=float, default=None)
        parser.add_argument("--mask-radius-arcsec", type=float, default=None)
        parser.add_argument("--model-option", action="append", default=[], metavar="OPTION=VALUE", help="DStools long option without --; e.g. robust=-0.5, imsize=4096")
        parser.add_argument("--submit", action="store_true", help="Download and submit; keep this login controller running for continuation")
        parser.add_argument("--prepare-download", action="store_true", help="Download next job only, without submitting")
        modes = parser.add_mutually_exclusive_group()
        modes.add_argument("--retry-failed", action="store_true", help="Retry processing failures: one private reprocessing, then at most one fresh-download retry")
        modes.add_argument("--retry-exhausted", action="store_true", help="Give exhausted targets the same two retries again; no other target is processed")
        modes.add_argument("--rerun-completed", action="store_true", help="Recompute selected completed results in independent attempts, preserving old success")
        parser.add_argument("--resume", action="store_true", help="Continue the unfinished plan with the options it was started with; settings are read again from the configuration files. Add --submit to process")


def build_parser(single):
    parser = argparse.ArgumentParser(prog="askap-source" if single else "askap-batch", description="ASKAP visibility → DStools DS/I/V on Ozstar")
    sub = parser.add_subparsers(dest="command", required=True)
    query = sub.add_parser("query", help="Query only: no MS downloads and no Slurm submission")
    add_settings(query, single, query=True)
    if single:
        query.add_argument("--ra", required=True)
        query.add_argument("--dec", required=True, help="Degrees or sexagesimal; e.g. --dec=-28:36:57.24")
        query.add_argument("--name")
        query.add_argument("--pmra", type=float)
        query.add_argument("--pmdec", type=float)
    else:
        query.add_argument("--catalogue", dest="catalogue_path")
        query.add_argument("--update-query", action="store_true", help="Refresh all sources and discover new observations")
        query.add_argument("--retry-query-failures", action="store_true")
    run = sub.add_parser("run", help="Plan or resume; without --submit/--prepare-download only writes a plan")
    add_settings(run, single, science=True)
    if single:
        run.add_argument("--all-beams-for-sb", nargs="+", default=[], metavar="SB", help="Expand these SBs to all eligible beams; other SBs still use the nearest")
    else:
        run.add_argument("--catalogue", dest="catalogue_path")
        run.add_argument("--begin", type=int)
        run.add_argument("--end", type=int)
        run.add_argument("--source", action="append", default=[], help="Normalised source name; repeated values select several")
    run.add_argument("--observation-key", action="append", default=[], help="Restrict to these exact dataset keys")
    for name in ("progress", "failures"):
        item = sub.add_parser(name)
        item.add_argument("--work-dir", type=Path, required=True)
    resolve = sub.add_parser("resolve-submission", help="Reconcile an uncertain submission after checking Slurm; never blindly resubmit")
    resolve.add_argument("--work-dir", type=Path, required=True)
    resolve.add_argument("--job", required=True, help="Pipeline job ID printed in the error")
    choice = resolve.add_mutually_exclusive_group(required=True)
    choice.add_argument("--scheduler-id", help="The existing Slurm job ID")
    choice.add_argument("--confirmed-not-submitted", action="store_true", help="Explicitly assert that no Slurm job was created")
    password = sub.add_parser("set-casda-password", help="Store the CASDA password in a protected file (hidden input)")
    password.add_argument("--config", type=Path, default=Path(__file__).resolve().parent.parent / "config.py", help="config.py providing CASDA_USERNAME and CASDA_PASSWORD_FILE")
    password.add_argument("--no-check", action="store_true", help="Do not test the CASDA login after saving")
    return parser


def set_casda_password(args, prompt=None):
    """Prompt twice without echo and save the password; never print or log it."""
    import getpass
    import os
    from . import casda_client
    prompt = prompt or getpass.getpass
    settings, _ = load(batch_config=args.config, use_environment=True, validate_values=False, complete=False)
    missing = [key.upper() for key in ("casda_username", "casda_password_file", "casda_password_env") if key not in settings]
    if missing:
        raise ValueError(f"Add {', '.join(missing)} to {args.config}")
    if not settings["casda_password_file"]:
        raise ValueError("Set CASDA_PASSWORD_FILE in config.py to the file that should hold the password")
    username = settings["casda_username"]
    try:
        password = prompt("CASDA password" + (f" for {username}" if username else "") + ": ")
        if password and prompt("Repeat the password: ") != password:
            raise ValueError("The two entries differ; nothing was saved")
    except (KeyboardInterrupt, EOFError):
        raise ValueError("Cancelled; nothing was saved") from None
    path = casda_client.write_password_file(settings["casda_password_file"], password)
    print(f"Saved the CASDA password to {path} (readable only by you).")
    if os.environ.get(settings["casda_password_env"]):
        print(f"Note: {settings['casda_password_env']} is set in this shell and takes precedence over the file; "
              "unset it to use the saved password. The login was not tested.")
    elif not username:
        print("Set CASDA_USERNAME in config.py before downloading; the login was not tested.")
    elif not args.no_check:
        try:
            casda_client.build_casda_client(settings)
            print("CASDA login OK.")
        except casda_client.CasdaCredentialError as exc:
            print(f"The password was saved, but the login test failed: {exc}")


def settings_from_args(args, single, validate_values=True):
    overrides = pairs(args.set)
    for key in SETTING_KEYS:
        value = getattr(args, key, None)
        if value is not None:
            overrides[key] = value
    if getattr(args, "model_option", None):
        overrides["model"] = {**overrides.get("model", {}), **pairs(args.model_option)}
    if hasattr(args, "exposure_min"):
        overrides["exposure_min"] = args.exposure_min
    settings, origins = load(batch_config=args.config, dstools_config=args.dstools_config or dstools_config_path(args.config),
        overrides=overrides, use_environment=True, validate_values=False)
    if single:
        # Catalogue ranges and the batch publication directory do not select
        # a single source; its products stay in its own observation directory.
        settings.update(begin=1, end=None, product_root=None)
        for key in ("begin", "end", "product_root"):
            origins[key] = "single-source mode"
    if validate_values:
        validate(settings)
    if not settings["work_root"]:
        raise ValueError("--work-dir / WORK_ROOT is required")
    return settings, origins


def summary_query(snapshot):
    observations = snapshot["observations"]
    eligible = [o for o in observations if o["eligible"]]
    selected = [o for o in observations if o["default_selected"]]
    print(json.dumps({"query_version": snapshot["revision"], "conditions": snapshot["conditions"],
        "candidates": snapshot["candidate_count"], "eligible": len(eligible), "SBs": len({o["sb_id"] for o in eligible}),
        "default_selected": len(selected), "estimated_download_bytes": sum(o["estimated_bytes"] for o in selected),
        "rule": "Nearest eligible beam per SB; inspect observations.csv before run"}, indent=2))


def plan_arguments(argv):
    """The run options that define a plan, with absolute paths, recorded for run --resume."""
    tokens, result = list(argv[1:]), []
    while tokens:
        token = tokens.pop(0)
        name, has_value, value = token.partition("=")
        if name in EXECUTION_OPTIONS or name == "--resume":
            continue
        if name in PATH_OPTIONS and (has_value or tokens):
            value = value if has_value else tokens.pop(0)
            token = name + "=" + str(Path(value).expanduser().resolve())
        result.append(token)
    return result


def resume_arguments(args, single, argv):
    """Rebuild run --resume from the options recorded by the unfinished plan."""
    extra = sorted({token.split("=", 1)[0] for token in argv[1:] if token.startswith("-")}
                   - {"--resume", *EXECUTION_OPTIONS, *LOCATION_OPTIONS})
    if extra:
        raise ValueError("--resume repeats the options of the unfinished plan; remove " + ", ".join(extra))
    settings, _ = settings_from_args(args, single, validate_values=False)
    work = safe_path(settings["work_root"])
    active = State(work).active_run() if (work / "state/sb_processing.sqlite").is_file() else None
    if not active:
        raise ValueError(f"No unfinished plan to resume in {work}; start one with run")
    if "argv" not in active:
        raise ValueError("The unfinished plan does not record its options; repeat its run command instead of --resume")
    print(f"Resuming plan {active['id']}: run {shlex.join(active['argv'])}", flush=True)
    resumed = ["run", *active["argv"], *[flag for flag in EXECUTION_OPTIONS if getattr(args, flag[2:].replace("-", "_"))]]
    return build_parser(single).parse_args(resumed), resumed, work


def show(value):
    return "(not set)" if value is ... else repr(value)


def plan_differences(active, frozen, settings, selection, mode, query_matches, cached, same_beams):
    """Readable differences between the unfinished plan and this command."""
    lines = []
    old, new = active["frozen"]["settings"], (frozen or {"settings": settings})["settings"]
    for key in sorted(set(old) | set(new)):
        if key in {"begin", "end"}:
            continue  # Reported with the selection below.
        before, after = old.get(key, ...), new.get(key, ...)
        if key in NESTED and isinstance(before, dict) and isinstance(after, dict):
            lines += [f"{key.upper()}[{option!r}]: {show(before.get(option, ...))} -> {show(after.get(option, ...))}"
                      for option in sorted(set(before) | set(after)) if before.get(option, ...) != after.get(option, ...)]
        elif before != after:
            lines.append(f"{key.upper()}: {show(before)} -> {show(after)}")
    if frozen:
        if frozen["sif_sha256"] != active["frozen"]["sif_sha256"]:
            lines.append("CONTAINER: the image file contents changed")
        files = active["frozen"]["external_files"], frozen["external_files"]
        lines += [f"{path}: file contents changed" for path in sorted(set(files[0]) | set(files[1]))
                  if files[0].get(path) != files[1].get(path)]
        if frozen.get("input_signature") != active["frozen"].get("input_signature"):
            lines.append("the catalogue or source input changed")
    before = active["selection"]
    if (before["begin"], before["end"]) != (selection["begin"], selection["end"]):
        rows = lambda s: f"{s['begin']}-{'end' if s['end'] is None else s['end']}"
        lines.append(f"rows (BEGIN-END): {rows(before)} -> {rows(selection)}")
    for key, label in (("names", "--source"), ("observation_keys", "--observation-key")):
        if before[key] != selection[key]:
            lines.append(f"{label}: {before[key]} -> {selection[key]}")
    if active["mode"] != mode:
        lines.append(f"mode: {MODE_LABELS[active['mode']]} -> {MODE_LABELS[mode]}")
    if not same_beams:
        lines.append("selected beams (--all-beams-for-sb) changed")
    if not query_matches:
        lines.append("query conditions or sources changed: observations are queried again")
    elif active["index_revision"] != cached.get("revision"):
        lines.append("the observation index changed after the plan was made")
    return lines


def confirm_replacement(active, changes, command):
    """Ask before discarding an unfinished plan; anything but yes keeps it."""
    print(f"An unfinished plan exists: {active['id']} (created {active['created_at']}).")
    if command == "query":
        print("query replaces it, because new query results invalidate the plan.")
    if changes:
        print("This command differs from it in:")
        for line in changes:
            print("  " + line)
    print("Replacing it keeps all completed results and keeps the old plan in the history; targets it did "
          "not finish are processed only if this command selects them.")
    try:
        answer = input("Replace the unfinished plan? [y/N] ")
    except EOFError:
        answer = ""
    if answer.strip().lower() not in {"y", "yes"}:
        raise ValueError("Cancelled; the unfinished plan is unchanged. To continue it, repeat its command "
                         "or use run --resume.")


def resolve_submission(args):
    state = State(args.work_dir)
    with lock(state.root / "controller.lock"):
        run = state.active_run()
        if not run:
            raise ValueError("No active run")
        matches = [job for job in state.jobs(run["id"]) if job["id"] == args.job]
        if len(matches) != 1 or matches[0]["status"] != "submitting":
            raise ValueError("Only an uncertain 'submitting' job can be resolved")
        job = matches[0]
        path = Path(job["manifest_path"])
        if path.with_suffix(".outcome.json").exists() and args.confirmed_not_submitted:
            raise ValueError("Worker outcome exists; cannot assert this job was never submitted")
        if args.scheduler_id:
            if not args.scheduler_id.isdigit():
                raise ValueError("scheduler-id must be numeric")
            write_json(path.with_suffix(".submission.json"), {"job_id": args.scheduler_id, "manually_reconciled": True})
            job.update(status="submitted", scheduler_id=args.scheduler_id)
        else:
            job["status"] = "prepared"
        state.store_job(job)
        print("Submission state reconciled; use run --submit to resume.")


def main(argv=None, single=False):
    argv = list(sys.argv[1:] if argv is None else argv)
    # argparse otherwise interprets negative sexagesimal Dec as an option.
    for index in range(len(argv)-1):
        if argv[index] == "--dec" and argv[index+1].startswith("-") and not argv[index+1].startswith("--"):
            argv[index:index+2] = ["--dec=" + argv[index+1]]
            break
    args = build_parser(single).parse_args(argv)
    resumed_work = None
    if args.command == "run" and args.resume:
        args, argv, resumed_work = resume_arguments(args, single, argv)
    if args.command == "set-casda-password":
        return set_casda_password(args)
    if args.command == "resolve-submission":
        return resolve_submission(args)
    if args.command in {"progress", "failures"}:
        if not (args.work_dir / "state/sb_processing.sqlite").is_file():
            raise ValueError("No processing database in this work directory")
        state = State(args.work_dir)
        summary = state.export()
        if args.command == "failures":
            with state.connect() as db:
                rows = [dict(r) for r in db.execute("SELECT source_id,observation_key,status,error_step,error,log FROM attempts WHERE status IN ('failed','exhausted','env_failed') ORDER BY updated_at")]
            print(json.dumps(rows, indent=2, ensure_ascii=False))
            return rows
        return summary  # Printed once by the entry point.
    settings, origins = settings_from_args(args, single)
    work = safe_path(settings["work_root"])
    if resumed_work is not None and work != resumed_work:
        raise ValueError(f"The unfinished plan belongs to {resumed_work}, but its options select {work}")
    state = State(work)
    dstools = args.dstools_config or dstools_config_path(args.config)
    with lock(state.root / "controller.lock"):
        with invocation(work, argv, single, args.config, settings, origins, dstools) as record:
            return execute(args, single, settings, origins, state, record,
                           plan_arguments(argv) if args.command == "run" else None)


def execute(args, single, settings, origins, state, record, plan_argv=None):
    work = Path(settings["work_root"])
    expected_mode = "single" if single else "batch"
    workspace_path = state.root / "workspace.json"
    workspace = read_json(workspace_path, {})
    if workspace and workspace.get("mode") != expected_mode:
        raise ValueError("This workspace belongs to the other processing mode; choose a separate directory")
    if not workspace:
        write_json(workspace_path, {"schema": 1, "mode": expected_mode})
    active = state.active_run()
    if active and active["single"] != single:
        raise ValueError("Workspace mode differs from this command")
    sources = None if single else load_catalogue(settings)
    query_root = work / "query"
    cached = read_json(query_root / "query-snapshot.json" if single else state.root / "crosscatalogue.json", {})
    query_matches = (cached.get("conditions") == query_settings(settings) if single else
                     cached.get("signature") == signature({"conditions": query_settings(settings), "sources": sources}))
    frozen = freeze(settings, origins) if args.command == "run" else None
    mode = ("retry" if getattr(args, "retry_failed", False) else "retry_exhausted" if getattr(args, "retry_exhausted", False)
            else "rerun" if getattr(args, "rerun_completed", False) else "normal")
    selection = {"begin": settings["begin"], "end": settings["end"],
                 "names": sorted(set(getattr(args, "source", ()))), "observation_keys": list(getattr(args, "observation_key", ()))}
    same_beams = (not single or not active or
                  {o["key"] for o in select(cached.get("observations", []), getattr(args, "all_beams_for_sb", ())) if o["selected"]}
                  == {o["key"] for o in active.get("selected_observations", []) if o["selected"]})
    if active:
        if (args.command == "run" and active["frozen"]["version"] == frozen["version"]
                and active["selection"] == selection and active["mode"] == mode and query_matches
                and active["index_revision"] == cached.get("revision") and same_beams):
            record["run_id"] = active["id"]
            print("Continuing run " + active["id"] + "; current configuration and selection are unchanged.")
            return run_controller(state, active, args.submit, args.prepare_download)
        if args.command == "query":  # query selects nothing: compare settings and query inputs only.
            changes = plan_differences(active, None, settings, active["selection"], active["mode"], query_matches, cached, True)
        else:
            changes = plan_differences(active, frozen, settings, selection, mode, query_matches, cached, same_beams)
            changes = changes or ["the recorded settings differ"]
        replace_run(state, active, confirm=lambda: confirm_replacement(active, changes, args.command))
        print("Previous plan " + active["id"] + " is kept as history; this command and the current configuration apply.")
    if single:
        if args.command == "query":
            source = make_source(args.name, args.ra, args.dec, settings, args.pmra, args.pmdec)
            snapshot = query_one(source, settings, query_root)
            write_json(state.root / "source.json", source)
            state.sync_index(snapshot, single=True)
            state.export()
            summary_query(snapshot)
            print("Observation table: " + str(query_root / "observations.csv"))
            return snapshot
        snapshot = cached
        if not snapshot.get("complete"):
            raise ValueError("Run query successfully before run")
        if not query_matches:
            print("Query settings changed; refreshing observations with the current configuration.")
            snapshot = query_one(snapshot["source"], settings, query_root)
        selected = select(snapshot["observations"], args.all_beams_for_sb)
        state.sync_index(snapshot, single=True)
    else:
        if settings["primary_beam"] and len(sources) > 1:
            raise ValueError("For multiple sources configure primary_beam_by_source, not one global target beam image")
        if settings["model"]["target-mask"] and len(sources) > 1:
            raise ValueError("A target-specific create-model mask cannot be implicitly shared across a multi-source catalogue; use a source-specific run/workspace")
        unknown = set(settings["primary_beam_by_source"]) - {s["name"] for s in sources}
        if unknown:
            raise ValueError(f"Unknown source names in primary_beam_by_source: {sorted(unknown)}")
        if getattr(args, "update_query", False) and getattr(args, "retry_query_failures", False):
            raise ValueError("Use full query update OR failed-query retry")
        refresh = bool(cached and not query_matches) or getattr(args, "update_query", False)
        if cached and not query_matches:
            print("Query settings or source inputs changed; refreshing observations with the current configuration.")
        try:
            snapshot = query_catalogue(sources, settings, refresh, getattr(args, "retry_query_failures", False), progress_callback=state.query_source)
        finally:
            state.export()
        state.sync_index(snapshot)
        state.export()
        if args.command == "query":
            print(json.dumps(state.export(), indent=2))
            return snapshot
        selected = None
    run = plan(state, snapshot, frozen, mode=mode, single=single, selected=selected,
               begin=settings["begin"], end=settings["end"], names=getattr(args, "source", ()), observation_keys=args.observation_key,
               argv=plan_argv)
    record["run_id"] = run["id"]
    print(json.dumps(run["summary"], indent=2))
    print("Execution plan: " + str(state.root / "runs" / run["id"] / "execution-plan.csv"))
    print("Recorded settings: " + str(state.root / "runs" / run["id"] / "effective-settings.json"))
    return run_controller(state, run, args.submit, args.prepare_download)


def batch_main():
    try:
        result = main(single=False)
        if isinstance(result, dict) and "status_counts" in result:
            print(json.dumps(result, indent=2, ensure_ascii=False))
    except (ValueError, RuntimeError, OSError) as exc:
        raise SystemExit(str(exc)) from None


def source_main():
    try:
        result = main(single=True)
        if isinstance(result, dict) and "status_counts" in result:
            print(json.dumps(result, indent=2, ensure_ascii=False))
    except (ValueError, RuntimeError, OSError) as exc:
        raise SystemExit(str(exc)) from None
