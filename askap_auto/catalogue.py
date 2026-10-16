"""Lossless catalogue ingestion, recorded input revisions and ICRS motion."""
from __future__ import annotations
import csv
import math
import warnings
from pathlib import Path
import astropy.units as u
import numpy as np
from astropy.coordinates import Angle, SkyCoord
from astropy.table import Table
from astropy.time import Time
from .common import digest, read_json, signature, source_name, write_csv, write_json

GENERATED = ["Processing_Index", "Source_ID", "Original_Source_Name", "Source_Name",
             "RA_deg", "Dec_deg", "PMRA_cosdec_masyr", "PMDEC_masyr", "Epoch_MJD", "Epoch_Scale"]


def numeric(value, label):
    try:
        result = float(value)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"{label} is not numeric: {value!r}") from exc
    if not math.isfinite(result):
        raise ValueError(f"{label} must be finite")
    return result


def coordinates(ra, dec, ra_unit="deg", dec_unit="deg"):
    if ra_unit not in {"deg", "hourangle"} or dec_unit != "deg":
        raise ValueError("Use RA deg/hourangle and Dec deg")
    ra_value = Angle(str(ra), unit=u.Unit(ra_unit)).degree
    dec_value = Angle(str(dec), unit=u.deg).degree
    if not math.isfinite(ra_value) or not math.isfinite(dec_value) or not 0 <= ra_value < 360 or not -90 <= dec_value <= 90:
        raise ValueError(f"Invalid ICRS position RA={ra!r}, Dec={dec!r}")
    return float(ra_value), float(dec_value)


def make_source(name, ra, dec, settings, pmra=None, pmdec=None, epoch=None, index=1):
    ra_deg, dec_deg = coordinates(ra, dec, settings["ra_unit"], settings["dec_unit"])
    if name is None:
        coordinate = SkyCoord(ra_deg * u.deg, dec_deg * u.deg)
        name = "J" + coordinate.ra.to_string(unit=u.hourangle, sep="", precision=3, pad=True) + coordinate.dec.to_string(unit=u.deg, sep="", precision=2, pad=True, alwayssign=True)
    original = str(name)
    normal = source_name(original)
    scale = (1 * u.Unit(settings["pm_unit"])).to_value(u.mas / u.yr)
    pm_ra = 0.0 if pmra is None else numeric(pmra, "pmra") * scale
    pm_dec = 0.0 if pmdec is None else numeric(pmdec, "pmdec") * scale
    if not settings["pmra_includes_cosdec"]:
        pm_ra *= math.cos(math.radians(dec_deg))
    reference = Time(epoch if epoch is not None else settings["coordinate_epoch"])
    if not reference.isscalar or not math.isfinite(float(reference.mjd)):
        raise ValueError("Coordinate epoch must be a finite scalar time")
    return {"source_id": signature({"name": original}), "processing_index": index,
            "original_name": original, "name": normal, "ra": ra_deg, "dec": dec_deg,
            "pmra_cosdec": pm_ra, "pmdec": pm_dec,
            "epoch_mjd": float(reference.mjd), "epoch_scale": reference.scale,
            "input": {"ra": str(ra), "dec": str(dec), "ra_unit": settings["ra_unit"], "dec_unit": settings["dec_unit"],
                      "pmra": pmra, "pmdec": pmdec, "pm_unit": settings["pm_unit"],
                      "pmra_includes_cosdec": settings["pmra_includes_cosdec"], "epoch": str(reference)}}


def position_at(source, mjd=None, scale="utc"):
    if source["pmra_cosdec"] == 0 and source["pmdec"] == 0:
        return source["ra"], source["dec"], "NA"
    if mjd is None or not math.isfinite(mjd):
        raise ValueError("无法取得观测时间，不能进行自行修正 / No observation time for nonzero proper motion")
    position = SkyCoord(ra=source["ra"] * u.deg, dec=source["dec"] * u.deg,
        pm_ra_cosdec=source["pmra_cosdec"] * u.mas/u.yr, pm_dec=source["pmdec"] * u.mas/u.yr,
        obstime=Time(source["epoch_mjd"], format="mjd", scale=source["epoch_scale"]), frame="icrs")
    # Angular proper-motion propagation; no fabricated distance/radial velocity.
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message=".*distance overridden.*")
        result = position.apply_space_motion(new_obstime=Time(mjd, format="mjd", scale=scale))
    return float(result.ra.deg), float(result.dec.deg), "Y"


def load_catalogue(settings):
    path = Path(settings["catalogue_path"] or "")
    if not path.is_file():
        raise ValueError("Configure an existing catalogue_path")
    mapping = settings["catalogue_columns"]
    input_signature = signature({"file_sha256": digest(path), "format": settings["catalogue_format"],
        "hdu": settings["catalogue_fits_hdu"], "mapping": mapping,
        "coordinates": {k: settings[k] for k in ("ra_unit", "dec_unit", "coordinate_frame", "coordinate_epoch", "pm_unit", "pmra_includes_cosdec")}})
    root = Path(settings["work_root"]) / "state"
    old = read_json(root / "input_catalogue.json")
    if old and old["signature"] == input_signature:
        return old["sources"]
    if settings["catalogue_format"] == "csv":
        with path.open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            columns = reader.fieldnames or []
            records = list(reader)
        if len(set(columns)) != len(columns):
            raise ValueError("Duplicate CSV column names")
        if any(None in row or any(v is None for v in row.values()) for row in records):
            raise ValueError("CSV has rows with inconsistent column counts")
    else:
        table = Table.read(path, format="fits", hdu=settings["catalogue_fits_hdu"])
        columns = list(table.colnames)
        records = [{col: "" if np.ma.is_masked(row[col]) else
                    row[col].decode().strip() if isinstance(row[col], bytes) else str(row[col]) for col in columns} for row in table]
    collision = set(columns) & set(GENERATED)
    if collision:
        raise ValueError(f"Input contains reserved generated columns: {sorted(collision)}")
    for key in ("source_name", "ra", "dec"):
        if not mapping.get(key):
            raise ValueError(f"Required column mapping missing: {key}")
    missing = {col for col in mapping.values() if col is not None and col not in columns}
    if missing:
        raise ValueError(f"Catalogue missing configured column(s): {sorted(missing)}")
    sources, output, names = [], [], {}
    for index, row in enumerate(records, 1):
        try:
            source = make_source(row[mapping["source_name"]], row[mapping["ra"]], row[mapping["dec"]], settings,
                pmra=row[mapping["pmra"]] if mapping.get("pmra") else None,
                pmdec=row[mapping["pmdec"]] if mapping.get("pmdec") else None,
                epoch=row[mapping["epoch"]] if mapping.get("epoch") else None, index=index)
            if source["name"] in names:
                raise ValueError(f"Duplicate/normalised name collision with row {names[source['name']]}")
            names[source["name"]] = index
        except Exception as exc:
            raise ValueError(f"Catalogue row {index}: {exc}") from exc
        sources.append(source)
        output.append({**row, **dict(zip(GENERATED, (index, source["source_id"], source["original_name"], source["name"], source["ra"], source["dec"], source["pmra_cosdec"], source["pmdec"], source["epoch_mjd"], source["epoch_scale"])) )})
    if old:
        import shutil
        history = Path(settings["work_root"]) / "logs" / "catalogues" / old["signature"]
        history.mkdir(parents=True, exist_ok=True)
        for name in ("input_catalogue.json", "input_catalogue.csv"):
            previous = root / name
            if previous.is_file() and not (history / name).exists():
                shutil.copy2(previous, history / name)
    write_csv(root / "input_catalogue.csv", columns + GENERATED, output)
    write_json(root / "input_catalogue.json", {"schema": 1, "signature": input_signature, "sources": sources})
    return sources
