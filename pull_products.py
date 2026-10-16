#!/usr/bin/env python3
"""Ada-side verified pull. Standard library only; Python >=3.7.

Transfers immutable versioned DS/I/V triples, verifies SHA256, uploads a
version-specific receipt, then publishes local current-result links.
Never deletes Ozstar or Ada scientific data. Requires ssh/rsync (sshpass optional).
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
import re
import shlex
import sqlite3
import subprocess
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROLES = ("ds", "stokes_i", "stokes_v")
REMOTE_INVENTORY = r'''
import hashlib,json,sqlite3,sys
from pathlib import Path
root=Path(sys.argv[1]).resolve(); work=Path(sys.argv[2]).resolve()
db=sqlite3.connect('file:'+str(work/'state/sb_processing.sqlite')+'?mode=ro', uri=True)
db.row_factory=sqlite3.Row
result=[]
for source in sorted(root.iterdir()):
    if not source.is_dir() or source.name.startswith('.'):
        continue
    for public in sorted(source.iterdir()):
        sidecar=public/'observation.json'
        if not sidecar.is_file():
            continue
        info=json.loads(sidecar.read_text())
        row=db.execute("SELECT * FROM attempts WHERE id=? AND status='complete'",(info['attempt_id'],)).fetchone()
        if not row or row['source_id'] != info['source_id'] or row['observation_key'] != info['observation_key']:
            continue
        products=json.loads(row['products']); files={}
        if set(products) != {'ds','stokes_i','stokes_v'}:
            continue
        for role,path in products.items():
            p=Path(path).resolve()
            try: relative=p.relative_to(root).as_posix()
            except ValueError: raise RuntimeError('Product outside configured product root')
            if not p.is_file(): break
            h=hashlib.sha256()
            with p.open('rb') as f:
                for chunk in iter(lambda:f.read(1024*1024),b''): h.update(chunk)
            files[role]={'relative_path':relative,'sha256':h.hexdigest(),'size':p.stat().st_size}
        if len(files)==3:
            result.append(dict(info,files=files,public_path=public.relative_to(root).as_posix()))
print(json.dumps(result))
'''


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024*1024), b""):
            h.update(chunk)
    return h.hexdigest()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=str(path.parent), prefix="."+path.name)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(value, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def safe_path(raw):
    if not re.fullmatch(r"/[A-Za-z0-9_./+-]+", str(raw)) or ".." in Path(raw).parts:
        raise ValueError("Use an absolute path with portable filename characters: " + str(raw))
    return str(raw)


def validate_record(item):
    if set(item["files"]) != set(ROLES):
        raise ValueError("Snapshot lacks complete DS/I/V triple")
    for key in ("source_id", "observation_key"):
        if not re.fullmatch(r"[0-9a-f]{64}", item[key]):
            raise ValueError("Invalid source/dataset identity")
    if not re.fullmatch(r"[A-Za-z0-9_.+-]+", item["attempt_id"]):
        raise ValueError("Invalid attempt identity")
    for value in [item["public_path"]] + [f["relative_path"] for f in item["files"].values()]:
        if value.startswith("/") or ".." in Path(value).parts or not re.fullmatch(r"[A-Za-z0-9_./+-]+", value):
            raise ValueError("Unsafe snapshot path")
    for f in item["files"].values():
        if not re.fullmatch(r"[0-9a-f]{64}", f["sha256"]) or not isinstance(f["size"], int) or f["size"] < 0:
            raise ValueError("Invalid file checksum/size")


class Remote:
    def __init__(self, args):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", args.user) or not re.fullmatch(r"[A-Za-z0-9_.-]+", args.host):
            raise ValueError("Invalid SSH user/host")
        self.address = args.user + "@" + args.host
        self.python = args.remote_python
        self.prefix = []
        if args.password_file:
            path = Path(args.password_file).expanduser()
            if path.is_symlink() or path.stat().st_mode & 0o077:
                raise ValueError("SSH password file must be a regular mode-600 file")
            self.prefix = ["sshpass", "-f", str(path)]
        self.ssh = ["ssh", "-o", "ConnectTimeout=30"]
        if not self.prefix:
            self.ssh += ["-o", "BatchMode=yes"]

    def command(self, argv):
        return subprocess.run(self.prefix + self.ssh + [self.address, " ".join(shlex.quote(str(x)) for x in argv)], check=True, text=True, capture_output=True).stdout

    def inventory(self, products, work):
        result = json.loads(self.command([self.python, "-c", REMOTE_INVENTORY, products, work]))
        for item in result:
            validate_record(item)
        return result

    def pull(self, products, destination, paths):
        if not paths:
            return
        with tempfile.NamedTemporaryFile(mode="w", delete=False) as handle:
            handle.write("\n".join(paths)+"\n")
            list_path = handle.name
        try:
            subprocess.run(self.prefix + ["rsync", "-a", "--ignore-times", "--files-from="+list_path,
                "-e", " ".join(shlex.quote(x) for x in self.ssh), self.address+":"+products.rstrip("/")+"/", str(destination)+"/"], check=True)
        finally:
            os.unlink(list_path)

    def receipt(self, local, work):
        root = work.rstrip("/") + "/state/transfer_receipts"
        final = root + "/" + local.name
        part = final + ".part"
        self.command(["mkdir", "-p", root])
        subprocess.run(self.prefix + ["rsync", "-a", "-e", " ".join(shlex.quote(x) for x in self.ssh), str(local), self.address+":"+part], check=True)
        script = "import hashlib,os,sys; p=sys.argv[1]; h=hashlib.sha256(open(p,'rb').read()).hexdigest(); assert h==sys.argv[3], 'Receipt hash mismatch'; os.replace(p,sys.argv[2])"
        self.command([self.python, "-c", script, part, final, digest(local)])
        return final


def pull(args, remote=None):
    import fcntl
    products = safe_path(args.remote_products)
    work = safe_path(args.remote_work)
    local = Path(args.destination).expanduser().resolve()
    safe_path(local)
    local.mkdir(parents=True, exist_ok=True)
    state = local / "state"
    state.mkdir(exist_ok=True)
    remote = remote or Remote(args)
    with (state / "pull.lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another pull owns this Ada destination")
        records = remote.inventory(products, work)
        if args.source:
            known = {r["source_name"] for r in records}
            if set(args.source) - known:
                raise ValueError("Requested source has no complete transferable triple")
            records = [r for r in records if r["source_name"] in args.source]
        run_id = uuid.uuid4().hex
        manifest = state / ("manifest-"+run_id+".json")
        atomic_json(manifest, records)
        ledger = sqlite3.connect(str(state / "transfer_ledger.sqlite"))
        ledger.executescript('''CREATE TABLE IF NOT EXISTS observations(source_id TEXT,observation_key TEXT,attempt_id TEXT,status TEXT,receipt TEXT,checked_at TEXT,PRIMARY KEY(source_id,observation_key,attempt_id));
        CREATE TABLE IF NOT EXISTS files(source_id TEXT,observation_key TEXT,attempt_id TEXT,role TEXT,path TEXT,sha256 TEXT,status TEXT,PRIMARY KEY(source_id,observation_key,attempt_id,role));
        CREATE TABLE IF NOT EXISTS history(id TEXT PRIMARY KEY,status TEXT,manifest TEXT,error TEXT);''')
        try:
            needed = []
            for item in records:
                validate_record(item)
                for role, f in item["files"].items():
                    path = local / f["relative_path"]
                    ok = path.is_file() and path.stat().st_size == f["size"] and digest(path) == f["sha256"]
                    if not ok:
                        needed.append(f["relative_path"])
                    ledger.execute("INSERT OR REPLACE INTO files VALUES(?,?,?,?,?,?,?)", (item["source_id"],item["observation_key"],item["attempt_id"],role,str(path),f["sha256"],"Y" if ok else "pending"))
                ledger.execute("INSERT OR REPLACE INTO observations VALUES(?,?,?,?,?,?)", (item["source_id"],item["observation_key"],item["attempt_id"],"verifying",None,datetime.now(timezone.utc).isoformat()))
            ledger.commit()
            print("Snapshot: %d complete triples; %d files require transfer" % (len(records),len(needed)))
            remote.pull(products, local, sorted(set(needed)))
            for item in records:
                for role, f in item["files"].items():
                    path = local / f["relative_path"]
                    if not path.is_file() or path.stat().st_size != f["size"] or digest(path) != f["sha256"]:
                        ledger.execute("UPDATE files SET status='N' WHERE source_id=? AND observation_key=? AND attempt_id=? AND role=?", (item["source_id"],item["observation_key"],item["attempt_id"],role))
                        ledger.commit()
                        raise RuntimeError("Transferred file failed SHA256/size verification: " + str(path))
                    ledger.execute("UPDATE files SET status='Y' WHERE source_id=? AND observation_key=? AND attempt_id=? AND role=?", (item["source_id"],item["observation_key"],item["attempt_id"],role))
                    f["verified"] = True
            latest = {(r["source_id"],r["observation_key"]):r for r in remote.inventory(products,work)}
            for item in records:
                other = latest.get((item["source_id"],item["observation_key"]))
                if not other or other["attempt_id"] != item["attempt_id"] or any(other["files"][role]["sha256"] != item["files"][role]["sha256"] for role in ROLES):
                    raise RuntimeError("Ozstar result changed during transfer; no completion receipt issued")
            if not records:
                return {"triples": 0, "files_transferred": 0, "status": "empty"}
            receipt = {"schema": 2, "id": run_id, "status": "complete", "observations": records,
                       "verified_at": datetime.now(timezone.utc).isoformat(), "source_root": products, "destination": str(local)}
            receipt_path = state / "receipts" / ("transfer-"+run_id+".json")
            atomic_json(receipt_path, receipt)
            remote.receipt(receipt_path, work)
            for item in records:
                public = local / item["public_path"]
                version = (local / item["files"]["ds"]["relative_path"]).parent
                public.parent.mkdir(parents=True, exist_ok=True)
                temporary = public.with_name("."+public.name+"."+run_id)
                temporary.symlink_to(os.path.relpath(version, public.parent), target_is_directory=True)
                os.replace(temporary, public)
                ledger.execute("UPDATE observations SET status='complete',receipt=? WHERE source_id=? AND observation_key=? AND attempt_id=?", (str(receipt_path),item["source_id"],item["observation_key"],item["attempt_id"]))
            ledger.execute("INSERT INTO history VALUES(?,?,?,?)", (run_id,"complete",str(manifest),None))
            ledger.commit()
            return {"triples": len(records), "files_transferred": len(needed), "receipt": str(receipt_path), "status": "complete"}
        except Exception as exc:
            ledger.execute("INSERT OR REPLACE INTO history VALUES(?,?,?,?)", (run_id,"failed",str(manifest),str(exc)))
            ledger.commit()
            raise
        finally:
            ledger.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, help="Optional batch config.py copied to Ada; CLI overrides it")
    parser.add_argument("--host")
    parser.add_argument("--user")
    parser.add_argument("--remote-products")
    parser.add_argument("--remote-work")
    parser.add_argument("--destination", help="Your local destination directory on Ada, e.g. /PATH/TO/ada/products")
    parser.add_argument("--password-file", help="Optional mode-600 SSH password file; default SSH keys/agent")
    parser.add_argument("--remote-python", default="/usr/bin/python3")
    parser.add_argument("--source", action="append", default=[])
    args = parser.parse_args(argv)
    import runpy
    values = runpy.run_path(str(args.config)) if args.config else {}
    mapping = {"host": "OZSTAR_HOST", "user": "OZSTAR_USER", "remote_work": "WORK_ROOT",
               "remote_products": "PRODUCT_ROOT", "destination": "ADA_ROOT", "password_file": "TRANSFER_PASSWORD_FILE"}
    for key, setting in mapping.items():
        if getattr(args, key) is None:
            raw = os.environ.get("ASKAP_" + setting)
            if raw is not None:
                try: raw = json.loads(raw)
                except ValueError: pass
            setattr(args, key, raw if raw is not None else values.get(setting))
    args.host = args.host or "ozstar.swin.edu.au"
    if args.remote_products is None and args.remote_work:
        args.remote_products = str(Path(args.remote_work) / "products")
    for key in ("user", "remote_work", "remote_products", "destination"):
        if not getattr(args, key):
            parser.error("Configure --" + key.replace("_", "-") + " or its config.py setting")
    print(json.dumps(pull(args), indent=2))

if __name__ == "__main__":
    main()
