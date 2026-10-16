"""Human-readable command history and effective settings in the work directory."""
from contextlib import contextmanager
from pathlib import Path
import shlex
import sys
from .common import digest, now, uid, write_json


@contextmanager
def invocation(work, argv, single, config=None, settings=None, origins=None, dstools_config=None):
    entry = "askap-source" if single else "askap-batch"
    path = Path(work) / "logs" / "commands" / (uid() + ".json")
    command = [sys.executable, str(Path(__file__).resolve().parent.parent / entry), *argv]
    record = {"started_at": now(), "command": shlex.join(command), "argv": command}
    for key, file in (("config", config), ("dstools_config", dstools_config)):
        record[key] = str(Path(file).resolve()) if file else None
        record[key + "_sha256"] = digest(file) if file and Path(file).is_file() else None
    record.update(settings=settings, origins=origins, status="running")
    write_json(path, record)
    try:
        yield record
    except BaseException as exc:
        record.update(status="interrupted" if isinstance(exc, KeyboardInterrupt) else "failed", error_type=type(exc).__name__)
        raise
    else:
        record["status"] = "finished"
    finally:
        record["finished_at"] = now()
        write_json(path, record)
