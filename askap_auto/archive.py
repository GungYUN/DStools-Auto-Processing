"""Login-only download and compute-only command-line extraction."""
from __future__ import annotations
import errno
import shutil
import tarfile
import time
from pathlib import Path
from urllib.parse import unquote, urlparse
from astropy.table import Table
from .casda_client import CasdaCredentialError, build_casda_client, _redact_download_error
from .common import now, read_json, tree_size, uid, write_json
from .tar_safety import validated_members


class SpaceError(RuntimeError):
    """Disk space or the staging budget ran out: an environment failure that stops processing."""


class DataError(RuntimeError):
    """The downloaded archive itself is unusable: a processing failure."""


def no_space(exc):
    """True for a full disk or exhausted quota, including errors collected by copytree."""
    if isinstance(exc, OSError) and exc.errno in {errno.ENOSPC, errno.EDQUOT}:
        return True
    return isinstance(exc, shutil.Error) and any(
        "No space left on device" in str(item) or "Disk quota exceeded" in str(item) for item in exc.args[0])


def check_space(root, settings, needed):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(root).free < needed:
        raise SpaceError(f"Insufficient filesystem space under {root}: need {needed} bytes")
    staging = Path(settings["work_root"]) / "staging"
    used = tree_size(staging)
    if used + needed > settings["staging_budget_bytes"]:
        raise SpaceError(f"Staging budget exceeded: {used} bytes used under {staging} + {needed} needed "
                         f"> STAGING_BUDGET_BYTES {settings['staging_budget_bytes']}")


def discard_unused(directory, keep=None):
    """Delete every child of directory except the one containing keep.

    Interrupted or failed downloads and unpacks are never resumed, so a fresh
    attempt deletes them first; only the completed copy named by its marker stays.
    """
    directory = Path(directory)
    if not directory.is_dir():
        return
    keep = Path(keep).resolve() if keep else None
    for child in directory.iterdir():
        if keep is not None and keep.is_relative_to(child.resolve()):
            continue
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()


def download(observation, cache, root, settings, force=False, client_factory=build_casda_client):
    root = Path(root)
    marker = root / "download-ready.json"
    ready = read_json(marker, {})
    if not force and ready.get("key") == observation["key"]:
        raw = read_json(root / "raw-ready.json", {})
        intact = bool(ready.get("files")) and all(Path(p).is_file() and Path(p).stat().st_size == n
                                                  for p, n in ready["files"].items())
        if intact or (raw.get("key") == observation["key"] and Path(raw.get("ms", "")).is_dir()):
            discard_unused(root / "download", ready.get("archive"))
            return ready
    # Nothing reusable: delete interrupted/failed downloads (archive AND checksum)
    # and the stale marker before downloading again.
    marker.unlink(missing_ok=True)
    discard_unused(root / "download")
    check_space(root, settings, observation["estimated_bytes"])
    rows = Table.read(cache, format="ascii.ecsv")
    row = rows[[observation["row_index"]]]
    value = row["filename"][0]
    if unquote(value.decode() if isinstance(value, bytes) else str(value)) != observation["filename"]:
        raise RuntimeError("Frozen CASDA row does not match selected dataset")
    client = client_factory(settings)
    for attempt in range(settings["download_retries"]):
        directory = root / "download" / uid()
        directory.mkdir(parents=True)
        urls = []
        try:
            urls = list(client.stage_data(row))
            names = [unquote(Path(urlparse(url).path).name) for url in urls]
            expected = observation["filename"]
            if expected not in names or expected + ".checksum" not in names:
                raise RuntimeError("CASDA did not return the selected archive/checksum pair")
            if any(Path(name).name != name or name in {".", ".."} or "\\" in name for name in names):
                raise RuntimeError("Unsafe staged download filename")
            # Preserve the verified contract: ONE call with ALL staged URLs.
            client.download_files(urls, savedir=str(directory))
            for path in list(directory.iterdir()):
                if path.is_file() and unquote(path.name) != path.name:
                    destination = path.with_name(unquote(path.name))
                    if destination.exists():
                        raise RuntimeError("Ambiguous decoded download filename")
                    path.rename(destination)
            archive = directory / expected
            checksum = directory / (expected + ".checksum")
            if not archive.is_file() or not checksum.is_file():
                raise RuntimeError("Download returned without archive/checksum")
            try:
                with tarfile.open(archive) as handle:
                    ms_names = {part for member in handle for part in Path(member.name).parts if part.endswith(".ms")}
            except tarfile.TarError as exc:
                raise DataError(f"Downloaded archive cannot be read: {exc}") from None
            if len(ms_names) != 1:
                raise DataError(f"Expected one MS per dataset, found {len(ms_names)}")
            ready = {"key": observation["key"], "archive": str(archive), "checksum": str(checksum),
                     "ms_name": next(iter(ms_names)), "files": {str(p): p.stat().st_size for p in (archive, checksum)}}
            write_json(marker, ready)
            return ready
        except Exception as exc:
            error = _redact_download_error(exc, urls)
            # The partial files are never used again; the error stays in download.log.
            with (root / "download.log").open("a", encoding="utf-8") as handle:
                handle.write(f"{now()} download attempt {attempt + 1} of {settings['download_retries']} failed: {error}\n")
            shutil.rmtree(directory, ignore_errors=True)
            if no_space(exc):
                raise SpaceError(error) from None
            if attempt + 1 == settings["download_retries"]:
                raise (DataError if isinstance(exc, DataError) else RuntimeError)(error) from None
            time.sleep(settings["download_retry_delay_seconds"])


def extract(root, observation, retain, run_command, settings=None):
    root = Path(root)
    old = read_json(root / "raw-ready.json", {})
    if old.get("key") == observation["key"] and Path(old["ms"]).is_dir():
        discard_unused(root / "raw", old["ms"])
        return Path(old["ms"])
    ready = read_json(root / "download-ready.json", {})
    if ready.get("key") != observation["key"]:
        raise RuntimeError("No completed download for this dataset")
    archive = Path(ready["archive"])
    with tarfile.open(archive) as handle:
        validated_members(handle, ready["ms_name"])
    # An interrupted unpack is never resumed: delete it before unpacking again.
    (root / "raw-ready.json").unlink(missing_ok=True)
    discard_unused(root / "raw")
    if settings is not None:
        check_space(root, settings, archive.stat().st_size)
    directory = root / "raw" / uid()
    directory.mkdir(parents=True)
    run_command(["tar", "-xf", str(archive), "-C", str(directory)])
    matches = [p for p in directory.rglob(ready["ms_name"]) if p.is_dir()]
    if len(matches) != 1:
        raise RuntimeError("Extracted archive does not contain the expected unique MS")
    write_json(root / "raw-ready.json", {"key": observation["key"], "ms": str(matches[0])})
    if not retain:
        archive.unlink()
    return matches[0]
