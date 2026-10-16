"""Configuration-free identities, atomic snapshots and filesystem locks."""
from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def now():
    return datetime.now(timezone.utc).isoformat()


def uid():
    import uuid
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:12]


def digest(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def signature(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False,
                                     separators=(",", ":")).encode()).hexdigest()


def read_json(path, default=None):
    path = Path(path)
    return json.loads(path.read_text()) if path.exists() else default


@contextmanager
def atomic_file(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="." + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            yield handle
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_json(path, value):
    with atomic_file(path) as handle:
        json.dump(value, handle, indent=2, ensure_ascii=False, allow_nan=False)
        handle.write("\n")


def write_csv(path, columns, rows):
    with atomic_file(path) as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


@contextmanager
def lock(path, wait=False):
    import fcntl
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        except BlockingIOError as exc:
            raise RuntimeError(f"Another process owns {path}") from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def source_name(value):
    value = str(value)
    if any(ord(c) < 32 and not c.isspace() or ord(c) == 127 for c in value):
        raise ValueError("Source name contains a control character")
    result = re.sub(r"\s+", "_", value.strip())
    # DStools still invokes some internal shell commands: use a documented
    # portable subset, even though our own subprocess calls use argument lists.
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.+-]{0,119}", result) or ".." in result:
        raise ValueError(f"Unsupported source name {value!r}; use 1–120 ASCII letters, digits, _, ., +, -; start with a letter/digit; no '..'")
    return result


def safe_path(path):
    path = Path(path).expanduser().resolve()
    if not re.fullmatch(r"/[A-Za-z0-9_./+-]+", str(path)) or ".." in path.parts:
        raise ValueError(f"Path is not compatible with DStools internal shell commands: {path}")
    return path


def sb_number(value):
    match = re.fullmatch(r"(?:SB)?(\d+)", str(value), re.I)
    if not match:
        raise ValueError(f"Invalid SB identifier: {value!r}")
    return int(match[1])


def dataset_tokens(filename):
    from urllib.parse import unquote
    filename = unquote(str(filename))
    if Path(filename).name != filename or "\\" in filename:
        raise ValueError(f"Unsafe archive filename: {filename}")
    # Quotes/spaces in archive names are permitted for tar/argument-list I/O,
    # but a processing MS basename is separately validated before DStools.
    match = re.search(r"(SB\d+).*?(beam\d+)(?=[._]|$)", filename, re.I)
    if match:
        return match[1].upper(), match[2].lower()
    match = re.search(r"(SB\d+)_F\d+_B(\d+)(?=[._]|$)", filename, re.I)
    if match:
        return match[1].upper(), "beam" + match[2]
    raise ValueError(f"Cannot identify SB/beam in {filename}")


def tree_size(path):
    total = 0
    for root, dirs, files in os.walk(path, followlinks=False):
        dirs[:] = [d for d in dirs if not (Path(root) / d).is_symlink()]
        for name in files:
            try:
                p = Path(root) / name
                if not p.is_symlink():
                    total += p.stat().st_size
            except FileNotFoundError:
                pass
    return total
