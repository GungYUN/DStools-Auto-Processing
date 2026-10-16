"""Offline site acceptance for catalogue ingestion; no service or batch config.

Run from the deployed checkout with --output-root in an isolated validation area.
Fixtures are synthetic parser examples, not scientific targets/proper motions.
"""
from __future__ import annotations

import argparse
import copy
import csv
import json
import math
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from astropy.table import Table
from askap_auto import catalogue, settings
from template_settings import template_settings
from askap_auto.common import digest, now, read_json, safe_path, write_csv, write_json

FIXTURES = Path(__file__).resolve().parents[1] / "examples/validation"
MAPPING = {"source_name": "Object", "ra": "RightAscension", "dec": "Declination",
           "pmra": None, "pmdec": None, "epoch": None}
MOTION_MAPPING = {**MAPPING, "pmra": "MuAlphaStar", "pmdec": "MuDelta", "epoch": "Epoch"}
REFERENCE = [
    ("001", "001", 180.0, -28.0),
    ("Source A", "Source_A", 180.125, -28.25),
    ("Target+03", "Target+03", 0.0, -60.0),
    ("Target-04", "Target-04", 359.9995833333333, 15.5),
]


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def close(actual, expected):
    require(math.isclose(actual, expected, rel_tol=0, abs_tol=1e-9),
            f"Expected {expected}, found {actual}")


def check_positions(sources):
    require(len(sources) == len(REFERENCE), "Expected four source rows")
    for index, (source, reference) in enumerate(zip(sources, REFERENCE), 1):
        original, normalised, ra, dec = reference
        require(source["processing_index"] == index, "Original row index changed")
        require(source["original_name"] == original, "Original name/leading zeros changed")
        require(source["name"] == normalised, "Unexpected normalised name")
        close(source["ra"], ra)
        close(source["dec"], dec)


def run_stage(root, record):
    inputs = root / "inputs"
    shutil.copytree(FIXTURES, inputs)
    profile_settings = {}
    profiles = {}

    def ingest(name, path, **overrides):
        effective = template_settings()
        effective.update(catalogue_path=str(path), work_root=str(root / name), **overrides)
        settings.validate(effective)
        sources = catalogue.load_catalogue(effective)
        check_positions(sources)
        profile_settings[name], profiles[name] = effective, sources
        write_json(root / name / "catalogue-settings.json", {key: effective[key] for key in (
            "catalogue_path", "catalogue_format", "catalogue_fits_hdu", "catalogue_columns",
            "ra_unit", "dec_unit", "coordinate_epoch", "pm_unit", "pmra_includes_cosdec")})
        record(name + ": names, original indices and reference coordinates")
        return sources

    minimal = ingest("minimal", inputs / "stage1_minimal.csv")
    ingest("mapped_degrees", inputs / "stage1_mapped_degrees.csv", catalogue_columns=MAPPING)
    ingest("sexagesimal", inputs / "stage1_sexagesimal.csv", ra_unit="hourangle",
           catalogue_columns={**MAPPING, "ra": "RA_HMS", "dec": "DEC_DMS"})

    with (inputs / "stage1_mapped_degrees.csv").open(newline="", encoding="utf-8") as handle:
        degree_rows = list(csv.DictReader(handle))
    fits_path = inputs / "stage1_mapped_degrees.fits"
    Table({"Object": [row["Object"] for row in degree_rows],
           "RightAscension": [float(row["RightAscension"]) for row in degree_rows],
           "Declination": [float(row["Declination"]) for row in degree_rows],
           "Label": [row["Label"] for row in degree_rows]}).write(fits_path)
    ingest("fits", fits_path, catalogue_format="fits", catalogue_columns=MAPPING)

    # These first four inputs describe the same zero-motion/J2000 sources.
    for name in ("mapped_degrees", "sexagesimal", "fits"):
        for left, right in zip(minimal, profiles[name]):
            for key in ("source_id", "original_name", "name", "processing_index",
                        "pmra_cosdec", "pmdec", "epoch_mjd", "epoch_scale"):
                require(left[key] == right[key], f"{name}: {key} differs from minimal CSV")
    for source in minimal:
        close(source["pmra_cosdec"], 0)
        close(source["pmdec"], 0)
        close(source["epoch_mjd"], 51544.5)
        require(source["epoch_scale"] == "tt", "J2000 should carry the TT reference scale")
    record("CSV/FITS identity equivalence; absent PM=0 and default epoch=J2000")
    snapshot_path = root / "mapped_degrees/state/input_catalogue.csv"
    with snapshot_path.open(newline="", encoding="utf-8") as handle:
        snapshots = list(csv.DictReader(handle))
    for original, snapshot in zip(degree_rows, snapshots):
        for key, value in original.items():
            require(snapshot[key] == value, f"Original column lost: {key}")
    record("Input snapshot preserves all original columns and strings")

    motion = ingest("proper_motion", inputs / "stage1_proper_motion.csv", catalogue_columns=MOTION_MAPPING)
    expected_motion = [(0, 0, 51544.5), (100, -20, 57388.5), (-250, 80, 51544.5), (0, 0, 55197.0)]
    for source, expected, zero_motion_source in zip(motion, expected_motion, minimal):
        require(source["source_id"] == zero_motion_source["source_id"], "Motion changed source identity")
        for key, value in zip(("pmra_cosdec", "pmdec", "epoch_mjd"), expected):
            close(source[key], value)
    record("Explicit PM in mas/yr, cos(dec) convention and per-row reference epochs")

    with (inputs / "stage1_proper_motion.csv").open(newline="", encoding="utf-8") as handle:
        motion_rows = list(csv.DictReader(handle))
    arcsec_rows = copy.deepcopy(motion_rows)
    for row in arcsec_rows:
        for key in ("MuAlphaStar", "MuDelta"):
            row[key] = float(row[key]) / 1000
    arcsec_path = inputs / "stage1_proper_motion_arcsec.csv"
    write_csv(arcsec_path, list(motion_rows[0]), arcsec_rows)
    arcsec = ingest("proper_motion_arcsec", arcsec_path, catalogue_columns=MOTION_MAPPING, pm_unit="arcsec/yr")
    for original, converted in zip(motion, arcsec):
        close(original["pmra_cosdec"], converted["pmra_cosdec"])
        close(original["pmdec"], converted["pmdec"])
    record("Equivalent angular motion in arcsec/yr converts to mas/yr")
    raw_ra_motion = ingest("proper_motion_without_cosdec", inputs / "stage1_proper_motion.csv",
                          catalogue_columns=MOTION_MAPPING, pmra_includes_cosdec=False)
    for source, expected in zip(raw_ra_motion, expected_motion):
        close(source["pmra_cosdec"], expected[0] * math.cos(math.radians(source["dec"])))
    record("Explicit unprojected RA motion is multiplied by cos(dec)")

    for name, effective in profile_settings.items():
        snapshot = Path(effective["work_root"]) / "state/input_catalogue.json"
        checksum = digest(snapshot)
        require(catalogue.load_catalogue(effective) == profiles[name], "Resumed input differs")
        require(digest(snapshot) == checksum, "Resume rewrote the frozen source mapping")
    record("All valid profiles resume from unchanged frozen input")

    def reject(name, path, fragments, **overrides):
        effective = template_settings()
        effective.update(catalogue_path=str(path), work_root=str(root / name), **overrides)
        try:
            catalogue.load_catalogue(effective)
        except ValueError as error:
            message = str(error)
            require(all(fragment in message for fragment in fragments), f"Unexpected rejection: {message}")
            require(not (Path(effective["work_root"]) / "state/input_catalogue.json").exists(),
                    "Invalid input published a source mapping")
            record(name + ": correctly rejected", message)
        else:
            raise AssertionError(name + " unexpectedly accepted")

    bad = inputs / "bad_missing_dec.csv"
    write_csv(bad, ["Name", "RA"], [{"Name": "001", "RA": 180}])
    reject("missing_column", bad, ["missing configured", "DEC"])
    bad = inputs / "bad_collision.csv"
    write_csv(bad, ["Name", "RA", "DEC"], [
        {"Name": "Source A", "RA": 180, "DEC": -28},
        {"Name": "Source_A", "RA": 180.125, "DEC": -28.25}])
    reject("normalised_name_collision", bad, ["row 2", "collision"])
    bad = inputs / "bad_coordinate.csv"
    write_csv(bad, ["Name", "RA", "DEC"], [
        {"Name": "001", "RA": 180, "DEC": -28},
        {"Name": "Source A", "RA": 400, "DEC": -28.25}])
    reject("invalid_coordinate", bad, ["row 2", "Invalid ICRS"])
    bad = inputs / "bad_pm.csv"
    blank_motion = copy.deepcopy(motion_rows)
    blank_motion[1]["MuAlphaStar"] = ""
    write_csv(bad, list(motion_rows[0]), blank_motion)
    reject("blank_mapped_motion", bad, ["row 2", "pmra"], catalogue_columns=MOTION_MAPPING)

    original_settings = profile_settings["minimal"]
    original_file = Path(original_settings["catalogue_path"])
    original_text = original_file.read_text()
    snapshot = Path(original_settings["work_root"]) / "state/input_catalogue.json"
    old = json.loads(snapshot.read_text())
    original_file.write_text(original_text.replace("Source A", "Renamed Source"))
    revised = catalogue.load_catalogue(original_settings)
    require(any(source["original_name"] == "Renamed Source" for source in revised), "Changed input was not read")
    history = Path(original_settings["work_root"]) / "logs/catalogues" / old["signature"] / "input_catalogue.json"
    require(json.loads(history.read_text()) == old, "Previous source mapping was not retained")
    record("Changed catalogue applies in the same workspace and preserves its previous mapping")
    original_file.write_text(original_text)
    catalogue.load_catalogue(original_settings)
    changed_mapping = copy.deepcopy(original_settings)
    changed_mapping["ra_unit"] = "hourangle"
    converted = inputs / "converted_hours.csv"
    with original_file.open(newline="") as handle:
        records = list(csv.DictReader(handle))
    ra_column = changed_mapping["catalogue_columns"]["ra"]
    for row in records:
        row[ra_column] = str(float(row[ra_column]) / 15)
    write_csv(converted, list(records[0]), records)
    changed_mapping["catalogue_path"] = str(converted)
    converted_sources = catalogue.load_catalogue(changed_mapping)
    require(all(abs(a["ra"] - b["ra"]) < 1e-10 for a, b in zip(converted_sources, old["sources"])),
            "Updated coordinate units do not preserve the intended positions")
    record("Changed coordinate units apply in the same workspace with input history retained")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, required=True,
                        help="Isolated validation root; every invocation creates a new run directory")
    args = parser.parse_args(argv)
    output = safe_path(args.output_root)
    output.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="run-", dir=output))
    report = {"stage": 1, "started_at": now(), "synthetic_inputs": True,
              "python": sys.executable, "work_dir": str(root), "checks": []}

    def record(label, detail=""):
        report["checks"].append({"check": label, "status": "PASS", "detail": detail})
        print("PASS: " + label, flush=True)

    print("Stage 1: offline catalogue validation (no CASDA/Slurm/DStools)", flush=True)
    print("Results: " + str(root), flush=True)
    try:
        run_stage(root, record)
        report["status"] = "PASS"
    except Exception as error:
        report.update(status="FAIL", error_type=type(error).__name__, error=str(error))
        print(f"FAIL: {type(error).__name__}: {error}", file=sys.stderr, flush=True)
    report["finished_at"] = now()
    write_json(root / "report.json", report)
    print(f"STAGE1 {report['status']}: {len(report['checks'])} checks passed", flush=True)
    print("Report: " + str(root / "report.json"), flush=True)
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
