"""CASDA candidates, qualification and selection are separate operations."""
from __future__ import annotations
import math
import time
from pathlib import Path
from urllib.parse import unquote
import numpy as np
import astropy.units as u
from astropy.coordinates import SkyCoord
from astropy.table import Column, Table
from .common import dataset_tokens, digest, now, read_json, sb_number, signature, uid, write_csv, write_json
from .settings import query_settings

C = 299792458.0
COLUMNS = {"Record_ID": "key", "SB_ID": "sb_id", "Beam": "beam", "Filename": "filename",
    "Dataset_ID": "publisher_did", "Beam_RA_deg": "ra", "Beam_Dec_deg": "dec",
    "Separation_deg": "separation_deg", "t_min": "t_min", "t_max": "t_max",
    "Exposure_seconds": "exposure", "Frequency_Min_GHz": "frequency_min_ghz",
    "Frequency_Max_GHz": "frequency_max_ghz", "Frequency_Central_GHz": "frequency_central_ghz",
    "Radius_Frequency_Mode": "radius_frequency_mode", "Radius_deg": "radius_deg",
    "Within_Radius": "within_radius", "Quality": "quality", "Public": "public",
    "Estimated_Bytes": "estimated_bytes", "Eligible": "eligible", "Eligibility_Reason": "reason",
    "Nearest_In_SB": "default_selected", "Default_Selected": "default_selected"}


def text(value):
    return value.decode() if isinstance(value, bytes) else str(value)


def finite(value):
    try:
        result = float(value)
        return result if math.isfinite(result) and not np.ma.is_masked(value) else None
    except (ValueError, TypeError):
        return None


def tap_query(source, settings):
    from astroquery.utils.tap.core import TapPlus
    radius = settings["initial_radius_deg"]
    exposure = "" if settings["exposure_min"] is None else f" AND t_exptime > {float(settings['exposure_min'])}"
    adql = (f"SELECT TOP {int(settings['query_limit'])} * FROM ivoa.obscore WHERE dataproduct_type = 'visibility'"
            + exposure + f" AND 1=CONTAINS(POINT('ICRS',{source['ra']},{source['dec']}),CIRCLE('ICRS',s_ra,s_dec,{radius}))")
    rows = TapPlus(url="https://casda.csiro.au/casda_vo_tools/tap").launch_job_async(adql).get_results()
    if len(rows) >= settings["query_limit"]:
        raise RuntimeError("CASDA TOP limit reached; query may be truncated. Increase query_limit and query again.")
    return rows


def public_indices(rows):
    """Use CASDA release rules on an unmasked, consistently decoded date column.

    Empty ECSV fields can become masked strings. Astroquery 0.4.11's boolean
    comparison retains masked underlying True values when indexing a Table;
    explicitly filling missing dates with empty strings keeps them non-public.
    Normalize only a copy, so the original query/cache metadata is preserved.
    """
    if "obs_release_date" not in rows.colnames:
        raise ValueError("CASDA missing obs_release_date; cannot determine public data")
    if not len(rows):
        return set()
    from astroquery.casda import Casda
    marked = Table(rows, copy=True)
    release_dates = ["" if np.ma.is_masked(value) or value is None else text(value).strip()
                     for value in rows["obs_release_date"]]
    marked.replace_column("obs_release_date", Column(release_dates, name="obs_release_date"))
    marked["_askap_row_index"] = np.arange(len(marked))
    public = Casda.filter_out_unreleased(marked)
    return set(int(i) for i in public["_askap_row_index"])


def candidates(rows, source, settings, released=None):
    if not len(rows):
        return []
    required = {"filename", "obs_id", "access_estsize", "s_ra", "s_dec", "quality_level", "em_min", "em_max", "t_exptime"}
    if required - set(rows.colnames):
        raise ValueError(f"CASDA missing columns: {sorted(required - set(rows.colnames))}")
    released = public_indices(rows) if released is None else set(released)
    target = SkyCoord(source["ra"] * u.deg, source["dec"] * u.deg)
    output, seen = [], set()
    for index, row in enumerate(rows):
        reasons = []
        get = lambda col: row[col] if col in rows.colnames else None
        filename = unquote(text(row["filename"]))
        sb, beam = "", ""
        try:
            sb, beam = dataset_tokens(filename)
            if int(text(row["obs_id"]).split("-")[-1]) != sb_number(sb):
                raise ValueError("obs_id/filename mismatch")
        except (ValueError, TypeError) as exc:
            reasons.append("invalid_identity: " + str(exc))
        bounds = []
        for col in ("em_min", "em_max"):
            val = finite(row[col])
            unit = getattr(rows[col], "unit", None) or u.m
            bounds.append(None if val is None else float((val * u.Unit(unit)).to_value(u.m)))
        em_min, em_max = bounds
        identity = {"sb_id": sb, "beam": beam, "filename": filename,
                    "publisher_did": text(get("obs_publisher_did") or ""), "em_min_m": em_min, "em_max_m": em_max}
        key = signature(identity)
        if key in seen:
            continue
        seen.add(key)
        obs = {**identity, "key": key, "row_index": index, "sb_name": f"{sb}_{beam}",
               "ra": finite(row["s_ra"]), "dec": finite(row["s_dec"]),
               "t_min": finite(get("t_min")), "t_max": finite(get("t_max")),
               "exposure": finite(get("t_exptime")), "quality": text(row["quality_level"]).strip(),
               "public": index in released, "radius_frequency_mode": settings["radius_frequency_mode"],
               "frequency_min_ghz": None, "frequency_max_ghz": None, "frequency_central_ghz": None,
               "radius_deg": None, "within_radius": None, "separation_deg": None, "default_selected": False}
        size = finite(row["access_estsize"])
        obs["estimated_bytes"] = None if size is None or size < 0 else math.ceil(size * 1024)
        if size is None or size < 0:
            reasons.append("invalid_size")
        elif settings["max_file_size_kb"] is not None and size > settings["max_file_size_kb"]:
            reasons.append("size_limit")
        if obs["quality"] not in settings["quality_levels"]:
            reasons.append("quality")
        if not obs["public"]:
            reasons.append("unreleased")
        if obs["exposure"] is None or obs["exposure"] < 0:
            reasons.append("invalid_exposure")
        elif settings["exposure_min"] is not None and obs["exposure"] <= settings["exposure_min"]:
            reasons.append("exposure")
        if sb:
            if settings["minimum_sb"] is not None and sb_number(sb) < settings["minimum_sb"]:
                reasons.append("minimum_sb")
            if sb_number(sb) in settings["excluded_sbs"]:
                reasons.append("excluded_sb")
        if obs["ra"] is None or obs["dec"] is None:
            reasons.append("invalid_coordinates")
        else:
            try:
                obs["separation_deg"] = float(target.separation(SkyCoord(obs["ra"] * u.deg, obs["dec"] * u.deg)).deg)
            except ValueError:
                reasons.append("invalid_coordinates")
        if em_min is None or em_max is None or em_min <= 0 or em_max < em_min:
            reasons.append("invalid_frequency")
        else:
            low, high = C / em_max / 1e9, C / em_min / 1e9
            central = (low + high) / 2
            freq = central if settings["radius_frequency_mode"] == "central" else high
            radius = settings["reference_fwhm_deg"] / 2 * settings["reference_frequency_ghz"] / freq
            obs.update(frequency_min_ghz=low, frequency_max_ghz=high, frequency_central_ghz=central, radius_deg=radius)
            if obs["separation_deg"] is not None:
                obs["within_radius"] = obs["separation_deg"] <= radius
                if not obs["within_radius"]:
                    reasons.append("outside_half_power_radius")
        obs.update(eligible=not reasons, reason=";".join(reasons) or "eligible")
        output.append(obs)
    for sb in {o["sb_id"] for o in output if o["eligible"]}:
        nearest = min((o for o in output if o["eligible"] and o["sb_id"] == sb), key=lambda o: (o["separation_deg"], o["key"]))
        nearest["default_selected"] = True
    return output


def select(observations, all_beams_for_sb=()):
    expanded = {"SB" + str(sb_number(value)) for value in all_beams_for_sb}
    known = {o["sb_id"] for o in observations}
    if expanded - known:
        raise ValueError(f"Requested SB absent from query snapshot: {sorted(expanded - known)}")
    for sb in expanded:
        eligible = [o for o in observations if o["sb_id"] == sb and o["eligible"]]
        if not eligible:
            raise ValueError(f"{sb} has no eligible beams: " + "; ".join(o["reason"] for o in observations if o["sb_id"] == sb))
        beams = [o["beam"] for o in eligible]
        if len(set(beams)) != len(beams):
            raise ValueError(f"{sb} has multiple different datasets for the same beam; use a new query with unambiguous selection conditions")
    result = []
    for obs in observations:
        chosen = obs["eligible"] and (obs["default_selected"] or obs["sb_id"] in expanded)
        result.append({**obs, "selected": chosen, "selection_reason":
                       ("all_beams_for_requested_sb" if obs["sb_id"] in expanded else "nearest_eligible_beam") if chosen else "not_selected: " + obs["reason"]})
    return result


def export_query(directory, snapshot):
    directory = Path(directory)
    data = []
    for obs in snapshot.get("observations", []):
        data.append({**{col: obs.get(key) for col, key in COLUMNS.items()},
            "Query_Time": snapshot["queried_at"], "Query_Version": snapshot["revision"], "Condition_Signature": snapshot["condition_signature"]})
    write_csv(directory / "observations.csv", [*COLUMNS, "Query_Time", "Query_Version", "Condition_Signature"], data)
    (directory / "observations-columns.md").write_text(
        "# CASDA query columns\n\nAngles/radii: degrees; frequencies: GHz; exposure: seconds; t_min/t_max: MJD UTC; size: bytes.\n"
        "Within_Radius blank = invalid position/frequency, not outside. Eligible requires every enabled condition.\n"
        "Nearest_In_SB/Default_Selected mark the nearest eligible record, with data identity as the stable tie-break.\n"
        "Record_ID is the immutable dataset hash; Dataset_ID is CASDA publisher DID.\n"
        "Server-filtered records are absent. This CSV is a view; run reads query-snapshot.json and the immutable ECSV.\n"
        "Conditions: " + str(snapshot["conditions"]) + "\n", encoding="utf-8")


def query_one(source, settings, directory, fetch=tap_query, release_check=None):
    directory = Path(directory)
    revision = uid()
    rows = fetch(source, settings)
    if len(rows) >= settings["query_limit"]:
        raise RuntimeError("Query reached TOP limit; refusing incomplete snapshot")
    observations = candidates(rows, source, settings, released=release_check(rows) if release_check else None)
    cache = directory / "versions" / revision / "candidates.ecsv"
    cache.parent.mkdir(parents=True, exist_ok=True)
    rows.write(cache, format="ascii.ecsv", overwrite=False)
    conditions = query_settings(settings)
    snapshot = {"schema": 1, "revision": revision, "queried_at": now(), "source": source,
                "status": "available" if any(o["eligible"] for o in observations) else "no_observations",
                "conditions": conditions, "condition_signature": signature({"conditions": conditions, "source": source}),
                "candidate_count": len(rows), "observations": observations, "cache": str(cache), "cache_sha256": digest(cache), "complete": True}
    write_json(cache.parent / "query-snapshot.json", snapshot)
    export_query(directory, snapshot)
    write_json(directory / "query-snapshot.json", snapshot)
    return snapshot


def query_catalogue(sources, settings, update=False, retry_failed=False, fetch=tap_query, release_check=None, progress_callback=None):
    root = Path(settings["work_root"]) / "state"
    index_path = root / "crosscatalogue.json"
    expected = signature({"conditions": query_settings(settings), "sources": sources})
    old = read_json(index_path, {})
    if old and old["signature"] != expected and not update:
        raise ValueError("Query settings changed; explicitly use --update-query. Old results are not relabelled.")
    if old and old["signature"] == expected and not update and not retry_failed:
        return old
    checkpoint = read_json(root / "crosscatalogue-update.json", {})
    if not checkpoint or checkpoint["signature"] != expected or checkpoint.get("complete"):
        previous = {s["source"]["source_id"]: s for s in old.get("sources", [])}
        entries = []
        for source in sources:
            prior = previous.get(source["source_id"])
            entries.append({**(prior or {}), "source": source, "status": "unknown", "done": False, "observations": (prior or {}).get("observations", []), "using_previous_result": bool(prior)})
            if retry_failed and prior and prior["status"] in {"available", "no_observations"}:
                entries[-1] = {**prior, "done": True}
        checkpoint = {"schema": 1, "revision": uid(), "signature": expected, "sources": entries, "complete": False}
    journal_root = root / "query-journals" / checkpoint["revision"]
    for entry in checkpoint["sources"]:
        saved = read_json(journal_root / (entry["source"]["source_id"] + ".json"))
        if saved:
            entry.update(saved)
        if progress_callback:
            progress_callback(entry)
    if retry_failed and not checkpoint.get("retry_failed_pass"):
        checkpoint["revision"] = uid()
        checkpoint["retry_failed_pass"] = True
        journal_root = root / "query-journals" / checkpoint["revision"]
        for entry in checkpoint["sources"]:
            if entry["status"] in {"query_failed", "unknown"}:
                entry.update(done=False, status="unknown", error=None)
    write_json(root / "crosscatalogue-update.json", checkpoint)
    for entry in checkpoint["sources"]:
        if entry.get("done"):
            continue
        try:
            result = query_one(entry["source"], settings, root / "queries" / entry["source"]["source_id"], fetch, release_check)
            # Full candidates remain in the per-source query snapshot/ECSV.
            # The full-catalogue index stores only selected associations.
            result["observations"] = [obs for obs in result["observations"] if obs["default_selected"]]
            entry.update(result, using_previous_result=False, error=None)
        except Exception as exc:
            entry.update(status="query_failed", error=f"{type(exc).__name__}: {exc}")
        entry["done"] = True
        write_json(journal_root / (entry["source"]["source_id"] + ".json"), entry)
        if progress_callback:
            progress_callback(entry)
        if settings["query_delay_seconds"]:
            time.sleep(settings["query_delay_seconds"])
    checkpoint["complete"] = True
    checkpoint["finished_at"] = now()
    write_json(root / "crosscatalogue-update.json", checkpoint)
    write_json(index_path, checkpoint)
    return checkpoint
