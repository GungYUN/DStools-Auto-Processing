"""Inspect a completed stage-3 case without submitting or executing anything.

This checks frozen identity, SQLite steps, retained files and fingerprints.
It does not inspect DS/FITS scientific contents or replace manual comparison.
Only state/stage3-check.json is written; SQLite and scientific files are read-only.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import re
import shlex
import sqlite3
import sys
import tarfile
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from askap_auto.tar_safety import canonical_member_name, validated_members


REQUIRED_STEPS = (
    "archive_download", "checksum_download", "extract_archive", "observation_time",
    "proper_motion", "preprocess", "create_model", "insert_model", "subtract_model",
    "extract_ds", "crop_i", "crop_v", "product_copy",
)
PRODUCT_NAMES = {"ds": "{sb}.ds", "stokes_i": "wsclean-MFS-I-image.fits",
                 "stokes_v": "wsclean-MFS-V-image.fits"}
PENDING = {"preparing", "queued", "running", "partial"}


class NotReady(RuntimeError):
    pass


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def load_json(path):
    with Path(path).open(encoding="utf-8") as handle:
        return json.load(handle)


def stream_sha256(handle):
    result = hashlib.sha256()
    for block in iter(lambda: handle.read(1024 * 1024), b""):
        result.update(block)
    return result.hexdigest()


def fingerprint(path):
    path = Path(path)
    before = path.stat()
    with path.open("rb") as handle:
        value = stream_sha256(handle)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
        raise AssertionError(f"File changed during fingerprinting: {path}")
    return {"path": str(path), "size": after.st_size, "sha256": value}


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def inside(path, root):
    result = Path(path)
    require(result.is_absolute() and result.resolve().is_relative_to(Path(root).resolve()),
            f"Path is outside its frozen workspace: {result}")
    return result


def command_lines(log):
    commands = []
    if not Path(log).is_file():
        return commands
    for line in Path(log).read_text(errors="replace").splitlines():
        match = re.match(r"^\d{4}-\d{2}-\d{2}T\S+ (.+)$", line)
        if match:
            try:
                commands.append(shlex.split(match.group(1)))
            except ValueError:
                raise AssertionError(f"Malformed recorded command in {log}") from None
    return commands


def command_counts(log):
    return dict(Counter(token for command in command_lines(log) for token in command
                        if token.startswith("dstools-")))


def read_database(path, run_id):
    # A single read transaction avoids a mixture of worker/controller snapshots.
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=30)) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        saved = db.execute("SELECT data,status FROM runs WHERE id=?", (run_id,)).fetchone()
        require(saved is not None, "Recorded plan_run_id is absent from SQLite")
        attempts = {r["id"]: dict(r) for r in db.execute("SELECT * FROM attempts WHERE run_id=?", (run_id,))}
        targets = {(r["source_id"], r["observation_key"]): dict(r) for r in db.execute("SELECT * FROM targets")}
        steps = {key: {} for key in attempts}
        for row in db.execute("SELECT * FROM steps"):
            if row["attempt_id"] in steps:
                steps[row["attempt_id"]][row["step"]] = dict(row)
        jobs = [json.loads(r["data"]) for r in db.execute("SELECT data FROM jobs WHERE run_id=?", (run_id,))]
        return json.loads(saved["data"]), saved["status"], attempts, targets, steps, jobs


def compare_raw_archive(archive, raw, ms_name):
    """Compare payload and internal hardlinks; ignore transient table.lock only."""
    raw = Path(raw)
    require(raw.is_dir() and not raw.is_symlink(), "Raw MS root is missing or is a symlink")
    payload, ignored, ms_prefixes, hardlinks = {}, [], set(), []
    with tarfile.open(archive) as handle:
        members = validated_members(handle, ms_name)
        for canonical, member in members.items():
            parts = PurePosixPath(canonical).parts
            if ms_name not in parts:
                continue
            index = parts.index(ms_name)
            prefix = parts[:index + 1]
            ms_prefixes.add(prefix)
            relative = PurePosixPath(*parts[index + 1:])
            if member.isdir():
                continue
            if relative.name == "table.lock":
                ignored.append(str(relative))
                continue
            name = str(relative)
            destination = raw / Path(*relative.parts)
            require(not destination.is_symlink(), f"Raw MS payload became a symlink: {destination}")
            destination = inside(destination, raw)
            require(destination.is_file(), f"Raw MS payload is missing: {destination}")
            source_member = member
            target_name = None
            if member.islnk():
                target_name = canonical_member_name(member.linkname)
                source_member = members[target_name]
                target_relative = PurePosixPath(target_name).relative_to(PurePosixPath(*prefix))
                target_path = raw / Path(*target_relative.parts)
                require(not target_path.is_symlink(), f"Raw MS hardlink target became a symlink: {target_path}")
                target_path = inside(target_path, raw)
                require(target_path.is_file() and destination.samefile(target_path),
                        f"Raw MS hardlink no longer shares its target inode: {destination} -> {target_path}")
            with handle.extractfile(source_member) as source:
                expected = stream_sha256(source)
            actual = fingerprint(destination)
            require(actual["size"] == source_member.size and actual["sha256"] == expected,
                    f"Raw MS was changed after extraction: {destination}")
            payload[name] = {"size": actual["size"], "sha256": expected,
                             "member_type": "hardlink" if member.islnk() else "regular"}
            if target_name is not None:
                payload[name]["target"] = target_name
                hardlinks.append({"member": canonical, "relative_path": name,
                                  "target": target_name, "target_relative_path": str(target_relative),
                                  "header_size": member.size, "size": actual["size"],
                                  "sha256": expected, "samefile": True, "status": "PASS"})
    require(len(ms_prefixes) == 1 and payload, "Archive has no unique nonempty raw MS payload")
    current = set()
    for path in raw.rglob("*"):
        # Also reject directory symlinks: a regular file could otherwise be
        # reached through a replacement directory alias inside the MS.
        if path.name != "table.lock":
            require(not path.is_symlink(), f"Raw MS contains an unexpected symlink: {path}")
        if path.is_file():
            relative = path.relative_to(raw).as_posix()
            if path.name == "table.lock":
                ignored.append(relative)
            else:
                current.add(relative)
    require(current == set(payload), "Raw MS has unexpected extra or missing payload files")
    return {"status": "PASS", "files_compared": len(payload), "files": payload,
            "hardlinks_verified": hardlinks,
            "excluded_lock_files": sorted(set(ignored)),
            "exclusion": "Only files named table.lock are excluded; casacore may create/change these locks."}


def provenance(path, source, obs, attempt, task, frozen):
    value = load_json(path)
    expected = {"source_id": source["source_id"], "source_name": source["name"],
                "observation_key": obs["key"], "sb_id": obs["sb_id"], "beam": obs["beam"],
                "attempt_id": attempt["id"], "sif_sha256": frozen["sif_sha256"],
                "settings_version": frozen["version"], "model_key": task["model_key"],
                "filename": obs["filename"]}
    require(all(value.get(key) == item for key, item in expected.items()),
            f"Product provenance differs from its frozen attempt: {path}")
    return {"path": str(path), **expected}


def retained_single(work, task, target, attempt, report):
    obs, source = task["observation"], target["source"]
    raw_root = inside(task["raw_root"], work)
    ready = load_json(raw_root / "download-ready.json")
    raw_ready = load_json(raw_root / "raw-ready.json")
    require(ready.get("key") == obs["key"] == raw_ready.get("key"), "Retained raw/download identity differs")
    archive, checksum, raw = (
        inside(ready["archive"], work), inside(ready["checksum"], work), inside(raw_ready["ms"], work))
    require(archive.is_file() and checksum.is_file() and raw.is_dir(), "Single-source archive/checksum/raw MS missing")
    report["retained_inputs"] = {"archive": fingerprint(archive), "checksum": fingerprint(checksum), "raw_ms": str(raw)}
    report["raw_ms_payload"] = compare_raw_archive(archive, raw, ready["ms_name"])
    root = inside(attempt["path"], work)
    private = root / "work"
    model, insertion = private / "wsclean_model", private / "insertion_model"
    for path in (private / "input.ms", private / "input.subtracted.ms", model, insertion):
        require(path.is_dir() and not path.is_symlink(), f"Retained independent work directory missing: {path}")
    require((private / "input.ms").resolve() != raw.resolve(), "Processed MS aliases the raw input")
    require(model.resolve() != insertion.resolve(), "Insertion model aliases the unmasked model")
    required = [model / f"wsclean-MFS-{pol}-image.fits" for pol in ("I", "V")]
    required.append(model / "wsclean-MFS-I-model.fits")
    channels = [p for p in model.glob("*-model.fits") if "MFS" not in p.name]
    require(channels and all(p.is_file() for p in required), "Full retained I/V images or channel/MFS models missing")
    require(all((insertion / path.name).is_file() for path in required), "Retained insertion model has incomplete I/V images/models")
    model_hashes, compared, absent = {}, [], []
    for path in sorted(model.glob("*-model.fits")):
        inserted = insertion / path.name
        require(inserted.is_file(), f"Insertion model copy missing: {inserted}")
        original = fingerprint(path)
        model_hashes[path.name] = original
        premask = insertion / path.name.replace(".fits", ".premask.fits")
        if premask.is_file():
            require(fingerprint(premask)["sha256"] == original["sha256"],
                    f"Preserved model differs from its pre-insertion copy: {path}")
            compared.append(path.name)
        else:
            absent.append(path.name)
    commands = command_lines(attempt["log"])
    inserts = [args for args in commands if "dstools-insert-model" in args]
    require(inserts and all(str(insertion) in args and str(model) not in args for args in inserts),
            "Recorded insertion did not use the separate insertion_model copy")
    report["retained_models"] = {"directory": str(model), "insertion_directory": str(insertion),
                                 "files": model_hashes, "premask_matches": compared,
                                 "premask_not_available": absent}
    if absent:
        report["limitations"].append("SIF did not retain every premask copy; unmasked-model SHA comparison covers only listed premask_matches.")
    for role, pol in (("stokes_i", "I"), ("stokes_v", "V")):
        cutout = Path(attempt["products"][role])
        require(cutout.resolve() == (root / "cutouts" / f"wsclean-MFS-{pol}-image.fits").resolve(),
                "Single-source cutout was not saved to its independent output directory")
        original = model / cutout.name
        require(cutout.resolve() != original.resolve() and not cutout.samefile(original),
                "Cutout overwrote/aliases the full retained image")
    require(Path(attempt["products"]["ds"]).resolve() == (root / "products" / (obs["sb_name"] + ".ds")).resolve(),
            "Single-source DS path differs from its retained attempt")
    report["provenance"].append(provenance(root / "observation.json", source, obs, attempt, task, report["frozen"]))


def shared_outputs(work, task, target, attempt, report):
    source, obs, frozen = target["source"], task["observation"], report["frozen"]
    product_root = Path(frozen["settings"]["product_root"])
    version = product_root / ".versions" / source["source_id"] / obs["key"] / attempt["id"]
    expected = {"ds": version / (obs["sb_name"] + ".ds"),
                "stokes_i": version / "wsclean_model/wsclean-MFS-I-image.fits",
                "stokes_v": version / "wsclean_model/wsclean-MFS-V-image.fits"}
    require(all(Path(attempt["products"][role]).resolve() == path.resolve() for role, path in expected.items()),
            "Shared products do not belong to their immutable attempt version")
    public = product_root / source["name"] / (obs["sb_name"] + "--" + obs["key"][:12])
    require(public.is_symlink() and public.resolve() == version.resolve(), "Managed public product link is incorrect")
    report["managed_links"].append({"path": str(public), "version": str(version), "target": str(public.readlink())})
    report["provenance"].append(provenance(version / "observation.json", source, obs, attempt, task, frozen))
    private = Path(attempt["path"]) / "work"
    if private.exists():
        require(attempt.get("cleanup_error"), "Shared private MS/model work remains without a recorded cleanup error")
        report["cleanup_residuals"].append(str(private))


def shared_model(work, task, report):
    root = inside(task["model_root"], work)
    history = sorted((root / "history").glob("*.json"))
    require(len(history) == 1, "Expected exactly one committed shared model build")
    value = load_json(history[0])
    require(value.get("model_key") == task["model_key"], "Shared model history identity differs")
    for key in ("preprocess_seconds", "create_model_seconds"):
        require(isinstance(value.get(key), (int, float)) and math.isfinite(value[key]) and value[key] > 0,
                "Shared model history has no positive finite " + key)
    counts = command_counts(root / "model.log")
    require(counts.get("dstools-askap-preprocess") == 1 and counts.get("dstools-create-model") == 1,
            "Expected one shared preprocess and one create-model command")
    report["shared_model"] = {"history": str(history[0]), "build": value, "command_counts": counts}
    leftover = list(root.glob("build-*"))
    if (root / "ready.json").exists():
        leftover.append(root / "ready.json")
    for raw_root in {Path(task["raw_root"]), Path(task.get("public_raw_root", task["raw_root"]))}:
        leftover += [p for p in (raw_root / "raw", raw_root / "raw-ready.json") if p.exists()]
        leftover += list(raw_root.rglob("*.tar")) + list(raw_root.rglob("*.tar.gz"))
    if leftover:
        require(report["cleanup_errors"], "Shared raw/master/archive data remains without a recorded cleanup error")
        report["cleanup_residuals"] += sorted({str(p) for p in leftover})


def check_case(work, report):
    metadata_path = work / "stage3-case.json"
    if not metadata_path.is_file() or not (work / "state/sb_processing.sqlite").is_file():
        raise NotReady("Stage-3 case metadata or processing database has not been prepared")
    metadata = load_json(metadata_path)
    report["case_metadata"] = metadata
    required = {"case", "plan_run_id", "observation_key", "planned_targets", "sif_sha256"}
    require(required <= set(metadata), "Stage-3 metadata is missing required contract keys")
    require(metadata["case"] in {"single", "shared3"}, "Unknown stage-3 case")
    names = metadata["planned_targets"]
    expected_count = 1 if metadata["case"] == "single" else 3
    require(isinstance(names, list) and len(names) == len(set(names)) == expected_count, "Unexpected target count/names")
    run, status, attempts, pointers, steps, jobs = read_database(work / "state/sb_processing.sqlite", metadata["plan_run_id"])
    report["run_status"], report["jobs"] = status, jobs
    for value in attempts.values():
        value["products"] = json.loads(value["products"] or "{}")
    report["attempts"] = [{**value, "steps": steps[key]} for key, value in attempts.items()]
    frozen = run["frozen"]
    report["frozen"] = frozen
    require(run["id"] == metadata["plan_run_id"] and run["single"] == (metadata["case"] == "single"), "Run identity/mode differs")
    require(frozen["settings"]["work_root"] == str(work), "Frozen workspace differs")
    require(frozen["sif_sha256"] == metadata["sif_sha256"], "Frozen SIF differs from accepted stage-2 SIF")
    require(len(run["tasks"]) == 1, "Stage-3 case must contain exactly one dataset task")
    task = run["tasks"][0]
    require(task["observation"]["key"] == metadata["observation_key"], "Wrong observation selected")
    targets = task["targets"]
    require({t["source"]["name"] for t in targets} == set(names) and len(targets) == expected_count, "Planned targets differ")
    require(set(attempts) == {t["attempt_id"] for t in targets}, "Run has unexpected/missing attempts")
    require(task["retain"] == run["single"] and task["shared"] != run["single"], "Retention/shared mode differs")
    waiting = []
    for target in targets:
        attempt = attempts[target["attempt_id"]]
        require(attempt["source_id"] == target["source"]["source_id"] and attempt["observation_key"] == metadata["observation_key"],
                "Attempt identity differs from the frozen target")
        require(Path(attempt["path"]).resolve() == Path(target["path"]).resolve(), "Attempt path differs from its frozen plan")
        if attempt["status"] in PENDING:
            waiting.append(target["source"]["name"] + ":" + attempt["status"])
        else:
            require(attempt["status"] == "complete", "Attempt failed: " + target["source"]["name"] + ":" + attempt["status"])
    if waiting or status != "complete" or not jobs or any(job["status"] != "complete" for job in jobs):
        raise NotReady("Controller/worker has not finished: " + ", ".join(waiting or ["run/jobs still active"]))
    report["sif"] = fingerprint(frozen["settings"]["container"])
    require(report["sif"]["sha256"] == metadata["sif_sha256"], "Actual SIF changed after preparation")
    for target in targets:
        source, aid = target["source"], target["attempt_id"]
        attempt, recorded = attempts[aid], steps[aid]
        require(pointers[(source["source_id"], metadata["observation_key"])]["complete_attempt"] == aid, "Successful pointer differs")
        require(attempt["sif_sha256"] == metadata["sif_sha256"] and attempt["model_key"] == task["model_key"] and attempt["version"] == frozen["version"],
                "Completed attempt scientific identity differs")
        require(not attempt.get("error") and not attempt.get("error_step"), "Complete attempt still has a science error")
        require(any(source.get(key, 0) != 0 for key in ("pmra_cosdec", "pmdec")), "Stage-3 source must have nonzero test proper motion")
        for step in REQUIRED_STEPS:
            require(recorded.get(step, {}).get("status") == "Y", f"{source['name']}: step {step} is not Y")
        require(attempt.get("time_mjd") is not None and math.isfinite(attempt["time_mjd"]) and attempt.get("time_scale") == "utc" and attempt.get("time_source") in {"CASDA", "MS"},
                "Proper motion did not use a resolved observation UTC epoch")
        casda_time = task["observation"].get("t_min")
        if casda_time is not None and math.isfinite(casda_time):
            require(attempt["time_source"] == "CASDA" and math.isclose(attempt["time_mjd"], casda_time, rel_tol=0, abs_tol=1e-9),
                    "Processing time differs from frozen CASDA t_min")
        require(attempt.get("ra") is not None and attempt.get("dec") is not None and
                0 <= attempt["ra"] < 360 and -90 <= attempt["dec"] <= 90,
                "Actual processing coordinates are missing/invalid")
        if attempt.get("cleanup_error"):
            report["cleanup_errors"].append({"source": source["name"], "attempt_id": aid, "error": attempt["cleanup_error"]})
        cleanup = recorded.get("cleanup", {}).get("status")
        require(cleanup == ("NA" if run["single"] else "Y") or
                (not run["single"] and cleanup == "N" and attempt.get("cleanup_error")), "Unexpected cleanup status")
        require(set(attempt["products"]) == set(PRODUCT_NAMES), "Required DS/I/V product triple differs")
        hashes = {}
        product_root = work if run["single"] else Path(frozen["settings"]["product_root"])
        for role, name in attempt["products"].items():
            product = inside(name, product_root)
            require(product.is_file(), f"Required completed product missing: {product}")
            hashes[role] = fingerprint(product)
        report["products"].append({"source": source["name"], "attempt_id": aid, "files": hashes})
        report["command_counts"][source["name"]] = command_counts(attempt["log"])
        if run["single"]:
            require(attempt["model_reused"] == 0, "Single-source case unexpectedly reused a shared model")
            retained_single(work, task, target, attempt, report)
        else:
            require(attempt["model_reused"] == 1, "Shared target did not use the common model")
            shared_outputs(work, task, target, attempt, report)
    if not run["single"]:
        shared_model(work, task, report)
    report["scientific_status"] = "PASS"
    report["cleanup_status"] = "WARNING" if report["cleanup_errors"] else "PASS"


def save_report(path, report):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=".stage3-check-", delete=False) as handle:
        temporary = Path(handle.name)
        json.dump(report, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write("\n")
    temporary.replace(path)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    work = args.work_dir.expanduser().resolve()
    report = {"schema": 1, "stage": 3, "started_at": timestamp(), "work_dir": str(work),
              "scope": "Frozen identities, process steps, file retention and SHA256 only; DS/FITS quality remains manual.",
              "scientific_status": "NOT_READY", "cleanup_status": "NOT_READY", "attempts": [],
              "products": [], "provenance": [], "managed_links": [], "command_counts": {},
              "cleanup_errors": [], "cleanup_residuals": [], "limitations": []}
    try:
        check_case(work, report)
        report["status"] = "PASS"
    except NotReady as exc:
        report.update(status="NOT_READY", message=str(exc))
    except Exception as exc:
        report.update(status="FAIL", error_type=type(exc).__name__, message=str(exc))
        report["scientific_status"] = "FAIL"
    report["finished_at"] = timestamp()
    output = work / "state/stage3-check.json"
    save_report(output, report)
    print("STAGE3 " + report["status"] + ": " + report.get("message", "required steps, retained files and fingerprints passed"), flush=True)
    if report["cleanup_errors"]:
        print("Cleanup WARNING: recorded independently; scientifically completed attempts remain complete.", flush=True)
    for entry in report["attempts"]:
        if entry.get("error") or entry.get("status") != "complete":
            print(f"Attempt {entry['id']}: {entry.get('status')}; step={entry.get('error_step')}; log={entry.get('log')}; error={entry.get('error')}", flush=True)
    print("Report: " + str(output), flush=True)
    return {"PASS": 0, "NOT_READY": 2, "FAIL": 1}[report["status"]]


if __name__ == "__main__":
    raise SystemExit(main())
