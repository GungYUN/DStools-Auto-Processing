"""SQLite is authoritative; JSON/CSV are reproducible execution/audit views."""
from __future__ import annotations
import json
import sqlite3
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
from .common import lock, now, read_json, uid, write_csv, write_json

# Attempts in these states are not processed again; env_failed ones get a new
# attempt from the next command, failed ones only with --retry-failed and
# exhausted ones only with --retry-exhausted.
FINAL = ("complete", "failed", "exhausted", "env_failed", "superseded")
STEPS = ("archive_download", "checksum_download", "extract_archive", "observation_time", "proper_motion",
         "preprocess", "create_model", "insert_model", "subtract_model", "extract_ds",
         "crop_i", "crop_v", "product_copy", "cleanup")


class StateError(RuntimeError):
    pass


class State:
    def __init__(self, work):
        self.root = Path(work) / "state"
        self.path = self.root / "sb_processing.sqlite"
        self.root.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS sources(id TEXT PRIMARY KEY, data TEXT NOT NULL, query_status TEXT, query_error TEXT);
            CREATE TABLE IF NOT EXISTS observations(key TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS targets(source_id TEXT, observation_key TEXT, current INTEGER NOT NULL,
                index_revision TEXT, complete_attempt TEXT, latest_attempt TEXT,
                PRIMARY KEY(source_id,observation_key));
            CREATE TABLE IF NOT EXISTS attempts(id TEXT PRIMARY KEY, source_id TEXT, observation_key TEXT, run_id TEXT,
                status TEXT, kind TEXT, retry_number INTEGER, version TEXT, model_key TEXT, model_reused INTEGER,
                path TEXT, products TEXT, error_step TEXT, error TEXT, log TEXT,
                time_mjd REAL, time_scale TEXT, time_source TEXT, ra REAL, dec REAL,
                transfer_status TEXT, cleanup_error TEXT, created_at TEXT, updated_at TEXT);
            CREATE TABLE IF NOT EXISTS steps(attempt_id TEXT, step TEXT, status TEXT, seconds REAL, detail TEXT,
                PRIMARY KEY(attempt_id,step));
            CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, data TEXT NOT NULL, status TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, run_id TEXT NOT NULL, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS receipts(id TEXT PRIMARY KEY, data TEXT NOT NULL);
            ''')
            columns = {r["name"] for r in db.execute("PRAGMA table_info(attempts)")}
            for column in ("current_step", "sif_sha256"):
                if column not in columns:
                    db.execute("ALTER TABLE attempts ADD COLUMN " + column + " TEXT")

    @contextmanager
    def connect(self):
        db = None
        try:
            db = sqlite3.connect(self.path, timeout=30)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA busy_timeout=30000")
            with db:
                yield db
        except sqlite3.Error as exc:
            raise StateError(f"Cannot persist processing state: {exc}") from exc
        finally:
            if db:
                db.close()

    def query_source(self, entry):
        source = {**entry["source"], "using_previous_result": entry.get("using_previous_result", False)}
        with self.connect() as db:
            db.execute("INSERT INTO sources VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data,query_status=excluded.query_status,query_error=excluded.query_error",
                (source["source_id"], json.dumps(source), entry["status"], entry.get("error")))

    def sync_index(self, index, single=False, selected_keys=None):
        entries = [index] if single else index["sources"]
        revision = index["revision"]
        with self.connect() as db:
            db.execute("UPDATE targets SET current=0")
            for entry in entries:
                source = {**entry["source"], "using_previous_result": entry.get("using_previous_result", False)}
                db.execute("INSERT INTO sources VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data, query_status=excluded.query_status, query_error=excluded.query_error",
                    (source["source_id"], json.dumps(source), entry["status"], entry.get("error")))
                for obs in entry.get("observations", []):
                    selected = obs["key"] in selected_keys if single and selected_keys is not None else obs["default_selected"]
                    if not obs["eligible"] or not selected:
                        continue
                    db.execute("INSERT INTO observations VALUES(?,?) ON CONFLICT(key) DO UPDATE SET data=excluded.data", (obs["key"], json.dumps(obs)))
                    db.execute("INSERT INTO targets(source_id,observation_key,current,index_revision) VALUES(?,?,?,?) ON CONFLICT(source_id,observation_key) DO UPDATE SET current=excluded.current,index_revision=excluded.index_revision",
                        (source["source_id"], obs["key"], int(entry["status"] == "available"), revision))

    def target(self, source_id, key):
        with self.connect() as db:
            row = db.execute("SELECT * FROM targets WHERE source_id=? AND observation_key=?", (source_id, key)).fetchone()
        return dict(row) if row else {}

    def attempt(self, attempt_id):
        if not attempt_id:
            return {}
        with self.connect() as db:
            row = db.execute("SELECT * FROM attempts WHERE id=?", (attempt_id,)).fetchone()
        if not row:
            return {}
        result = dict(row)
        result["products"] = json.loads(result["products"]) if result["products"] else {}
        return result

    def create_attempt(self, source, obs, run_id, frozen, model_key, path, kind="normal", retry_number=0, attempt_id=None):
        attempt_id = attempt_id or uid()
        stamp = now()
        with self.connect() as db:
            db.execute("INSERT INTO attempts(id,source_id,observation_key,run_id,status,kind,retry_number,version,model_key,path,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (attempt_id, source["source_id"], obs["key"], run_id, "preparing", kind, retry_number,
                 frozen["version"], model_key, str(path), stamp, stamp))
            changed = db.execute("UPDATE targets SET latest_attempt=? WHERE source_id=? AND observation_key=?", (attempt_id, source["source_id"], obs["key"])).rowcount
            if changed != 1:
                raise StateError("Attempt has no indexed source/dataset identity")
        return attempt_id

    def update(self, attempt_id, **values):
        allowed = {"status", "products", "error_step", "error", "log", "time_mjd", "time_scale", "time_source", "ra", "dec", "transfer_status", "cleanup_error", "model_reused", "current_step"}
        if set(values) - allowed:
            raise ValueError(f"Unknown attempt fields: {set(values) - allowed}")
        if "products" in values:
            values["products"] = json.dumps(values["products"])
        values["updated_at"] = now()
        with self.connect() as db:
            count = db.execute("UPDATE attempts SET " + ",".join(k + "=?" for k in values) + " WHERE id=?", (*values.values(), attempt_id)).rowcount
            if count != 1:
                raise StateError(f"Unknown attempt: {attempt_id}")

    def step(self, attempt_id, step, status, seconds=None, detail=None):
        if step not in STEPS or status not in {"Y", "N", "NA"}:
            raise ValueError(f"Invalid step/status: {step}/{status}")
        with self.connect() as db:
            db.execute("INSERT INTO steps VALUES(?,?,?,?,?) ON CONFLICT(attempt_id,step) DO UPDATE SET status=excluded.status,seconds=excluded.seconds,detail=excluded.detail",
                (attempt_id, step, status, seconds, detail))

    def steps(self, attempt_id):
        with self.connect() as db:
            return {row["step"]: dict(row) for row in db.execute("SELECT * FROM steps WHERE attempt_id=?", (attempt_id,))}

    def complete(self, attempt_id, products):
        with self.connect() as db:
            row = db.execute("SELECT * FROM attempts WHERE id=?", (attempt_id,)).fetchone()
            db.execute("UPDATE attempts SET status='complete',products=?,error=NULL,error_step=NULL,current_step=NULL,updated_at=? WHERE id=?", (json.dumps(products), now(), attempt_id))
            db.execute("UPDATE targets SET complete_attempt=? WHERE source_id=? AND observation_key=?", (attempt_id, row["source_id"], row["observation_key"]))

    def store_run(self, run, status):
        with self.connect() as db:
            db.execute("INSERT INTO runs VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data,status=excluded.status", (run["id"], json.dumps(run), status))

    def active_run(self):
        with self.connect() as db:
            rows = list(db.execute("SELECT data FROM runs WHERE status NOT IN ('complete','superseded') ORDER BY rowid"))
        if len(rows) > 1:
            raise StateError("Multiple active runs in one workspace")
        return json.loads(rows[0]["data"]) if rows else None

    def supersede_run(self, run):
        """Keep history, but release unfinished reservations for a new invocation."""
        with self.connect() as db:
            for row in db.execute("SELECT id,source_id,observation_key FROM attempts WHERE run_id=? AND status NOT IN (" + ",".join("?" * len(FINAL)) + ")", (run["id"], *FINAL)).fetchall():
                db.execute("UPDATE attempts SET status='superseded',current_step=NULL,updated_at=? WHERE id=?", (now(), row["id"]))
                previous = db.execute("SELECT id FROM attempts WHERE source_id=? AND observation_key=? AND run_id!=? AND status!='superseded' ORDER BY rowid DESC LIMIT 1", (row["source_id"], row["observation_key"], run["id"])).fetchone()
                db.execute("UPDATE targets SET latest_attempt=? WHERE source_id=? AND observation_key=? AND latest_attempt=?", (previous["id"] if previous else None, row["source_id"], row["observation_key"], row["id"]))
            db.execute("UPDATE runs SET status='superseded' WHERE id=?", (run["id"],))

    def store_job(self, job):
        with self.connect() as db:
            db.execute("INSERT INTO jobs VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data", (job["id"], job["run_id"], json.dumps(job)))

    def jobs(self, run_id):
        with self.connect() as db:
            return [json.loads(r["data"]) for r in db.execute("SELECT data FROM jobs WHERE run_id=? ORDER BY rowid", (run_id,))]

    def import_receipts(self):
        for path in (self.root / "transfer_receipts").glob("*.json"):
            receipt = read_json(path, {})
            if receipt.get("schema") != 2 or receipt.get("status") != "complete":
                continue
            with self.connect() as db:
                if db.execute("SELECT 1 FROM receipts WHERE id=?", (receipt["id"],)).fetchone():
                    continue
                for item in receipt["observations"]:
                    row = db.execute("SELECT * FROM attempts WHERE id=?", (item["attempt_id"],)).fetchone()
                    if not row or row["source_id"] != item["source_id"] or row["observation_key"] != item["observation_key"] or row["status"] != "complete":
                        raise StateError(f"Transfer receipt identity does not match completed result: {path}")
                    if set(item["files"]) != {"ds", "stokes_i", "stokes_v"} or not all(v.get("verified") for v in item["files"].values()):
                        raise StateError("Transfer receipt does not verify the complete triple")
                    products = json.loads(row["products"])
                    from .common import digest
                    for role, evidence in item["files"].items():
                        product = Path(products[role])
                        if product.exists() and (product.stat().st_size != evidence["size"] or digest(product) != evidence["sha256"]):
                            raise StateError(f"Receipt hashes no longer match result: {product}")
                    db.execute("UPDATE attempts SET transfer_status='verified' WHERE id=?", (row["id"],))
                db.execute("INSERT INTO receipts VALUES(?,?)", (receipt["id"], json.dumps(receipt)))

    def export(self):
        with lock(self.root / "export.lock", wait=True):
            return self._export()

    def _export(self):
        self.import_receipts()
        # A single read transaction gives all CSVs one consistent DB snapshot.
        with self.connect() as db:
            db.execute("BEGIN")
            sources = {r["id"]: {**json.loads(r["data"]), "query_status": r["query_status"], "query_error": r["query_error"]} for r in db.execute("SELECT * FROM sources")}
            observations = {r["key"]: json.loads(r["data"]) for r in db.execute("SELECT * FROM observations")}
            attempts = {r["id"]: dict(r) for r in db.execute("SELECT * FROM attempts")}
            steps = {}
            durations = {}
            for row in db.execute("SELECT * FROM steps"):
                steps.setdefault(row["attempt_id"], {})[row["step"]] = row["status"]
                durations[row["attempt_id"]] = durations.get(row["attempt_id"], 0.0) + (row["seconds"] or 0.0)
            targets = list(db.execute("SELECT * FROM targets"))
        counts = Counter(t["observation_key"] for t in targets if t["current"])
        rows = []
        for t in targets:
            source, obs = sources[t["source_id"]], observations[t["observation_key"]]
            latest = attempts.get(t["latest_attempt"], {})
            success = attempts.get(t["complete_attempt"], {})
            displayed = latest or success
            products = json.loads(success.get("products") or "{}")
            rows.append({"Processing_Index": source["processing_index"], "Original_Source_Name": source["original_name"],
                "Source_Name": source["name"], "Source_ID": source["source_id"], "Observation_Key": obs["key"],
                "SB": obs["sb_id"], "Beam": obs["beam"], "Current_Index": bool(t["current"]),
                "Query_Status": source["query_status"], "Using_Previous_Query": source.get("using_previous_result", False), "Index_Revision": t["index_revision"],
                "Frequency_Min_GHz": obs["frequency_min_ghz"], "Frequency_Max_GHz": obs["frequency_max_ghz"],
                "Radius_Mode": obs["radius_frequency_mode"], "Separation_deg": obs["separation_deg"],
                "Shared_Count": counts[obs["key"]], "Status": "complete" if success else displayed.get("status", "not_started"),
                "Latest_Attempt_Status": latest.get("status", ""), "Current_Step": latest.get("current_step", ""), "Attempt_ID": displayed.get("id", ""),
                "Completed_Attempt_ID": success.get("id", ""), "Failed_Step": latest.get("error_step", ""),
                "Error": latest.get("error", ""), "Log": displayed.get("log", ""), "Retry_Number": latest.get("retry_number", 0),
                "Time_MJD": displayed.get("time_mjd"), "Time_Scale": displayed.get("time_scale"),
                "Time_Source": displayed.get("time_source"), "Epoch_MJD": source["epoch_mjd"],
                "SIF_SHA256": displayed.get("sif_sha256"), "Settings_Version": displayed.get("version"), "Model_Key": displayed.get("model_key"),
                "Model_Reused": displayed.get("model_reused"), "Updated_At": displayed.get("updated_at"),
                "Duration_Seconds": durations.get(displayed.get("id"), 0.0),
                "DS": products.get("ds", ""), "I": products.get("stokes_i", ""), "V": products.get("stokes_v", ""),
                "Transfer_Status": success.get("transfer_status", ""), "Cleanup_Error": displayed.get("cleanup_error", ""),
                **{step: steps.get(displayed.get("id"), {}).get(step, "") for step in STEPS}})
        base_columns = ["Processing_Index", "Original_Source_Name", "Source_Name", "Source_ID", "Observation_Key", "SB", "Beam", "Current_Index", "Query_Status", "Using_Previous_Query", "Index_Revision", "Frequency_Min_GHz", "Frequency_Max_GHz", "Radius_Mode", "Separation_deg", "Shared_Count", "Status", "Latest_Attempt_Status", "Current_Step", "Attempt_ID", "Completed_Attempt_ID", "Failed_Step", "Error", "Log", "Retry_Number", "Time_MJD", "Time_Scale", "Time_Source", "Epoch_MJD", "SIF_SHA256", "Settings_Version", "Model_Key", "Model_Reused", "Updated_At", "Duration_Seconds", "DS", "I", "V", "Transfer_Status", "Cleanup_Error"]
        rows.sort(key=lambda r: (r["Processing_Index"], r["SB"], r["Beam"], r["Observation_Key"]))
        write_csv(self.root / "observation_progress.csv", base_columns + list(STEPS), rows)
        write_csv(self.root / "crosscatalogue_observations.csv", base_columns[:16], rows)
        source_rows = [{"Processing_Index": s["processing_index"], "Source_Name": s["name"], "Source_ID": s["source_id"], "Query_Status": s["query_status"], "Error": s["query_error"]} for s in sources.values()]
        write_csv(self.root / "crosscatalogue_sources.csv", ["Processing_Index", "Source_Name", "Source_ID", "Query_Status", "Error"], source_rows)
        current = [r for r in rows if r["Current_Index"]]
        summary = {"exported_at": now(), "sources": len(sources), "query_counts": dict(Counter(s["query_status"] for s in sources.values())),
                   "known_target_observations": len(current), "unique_observations": len(counts),
                   "shared_groups": sum(n > 1 for n in counts.values()),
                   "shared_sources": len({r["Source_ID"] for r in current if r["Shared_Count"] > 1}),
                   "status_counts": dict(Counter(r["Status"] for r in current)),
                   "history_count": len(rows) - len(current),
                   "query_complete": all(s["query_status"] in {"available", "no_observations"} for s in sources.values())}
        write_json(self.root / "processing_summary.json", summary)
        (self.root / "observation-progress-columns.md").write_text(
            "# Progress columns\n\nOne row per stable source/dataset identity. Current_Index separates current selection from history.\n"
            "Status retains complete when a later rerun fails; Latest_Attempt_Status describes the rerun.\n"
            "Steps: Y success; N failure; blank not executed; NA not applicable. Shared model reuse is explicit.\n"
            "Status env_failed: stopped by the environment (CASDA, disk space/staging budget, Slurm job); the next command retries it.\n"
            "Status failed: processing failed; only run --retry-failed retries it.\n"
            "Status exhausted: processing failed after both retries; only run --retry-exhausted retries it.\n"
            "Angles in degrees, frequencies GHz, Time/Epoch in MJD (scales recorded), retry count is explicit science retry.\n"
            "SQLite is authoritative; CSV is regenerated, never an input to execution.\n", encoding="utf-8")
        return summary
