"""Setting names and checks. Every value comes from config.py and dstools-config.py;
there are no built-in defaults."""
from __future__ import annotations
import copy
import json
import math
import os
import runpy
from pathlib import Path
from .common import digest, lock, read_json, safe_path, signature, write_json

# config.py: input/output, query, Slurm and OzSTAR environment, credentials.
CONFIG_KEYS = (
    "work_root", "product_root", "catalogue_path", "catalogue_format", "catalogue_fits_hdu",
    "catalogue_columns", "ra_unit", "dec_unit", "coordinate_frame", "coordinate_epoch", "pm_unit",
    "pmra_includes_cosdec", "begin", "end", "exposure_min", "minimum_sb", "excluded_sbs",
    "quality_levels", "max_file_size_kb", "initial_radius_deg", "radius_frequency_mode",
    "reference_fwhm_deg", "reference_frequency_ghz", "query_limit", "query_delay_seconds",
    "download_retries", "download_retry_delay_seconds", "parallel_slots", "threads_per_slot",
    "memory_gb", "tmp_gb", "hours_per_observation", "hours_per_target", "overhead_hours",
    "walltime_hours", "reserve_minutes", "staging_budget_bytes", "max_observations_per_job",
    "container", "apptainer", "bind", "python", "container_python", "modules", "partition",
    "account", "max_continuations", "poll_seconds", "casda_username", "casda_password_file",
    "casda_password_env", "ozstar_host", "ozstar_user", "ada_root", "transfer_password_file",
)
# dstools-config.py: the DStools options and the cutout size.
DSTOOLS_KEYS = ("model", "mask_radius_arcsec", "extract_minuv_m", "extract_noflag",
                "extract_baseline_average", "primary_beam", "primary_beam_by_source", "crop_arcmin")
SETTING_KEYS = CONFIG_KEYS + DSTOOLS_KEYS
FILES = {"config.py": CONFIG_KEYS, "dstools-config.py": DSTOOLS_KEYS}
# Every dstools-create-model option set in MODEL. The pipeline itself sets
# --verbose, --threads, --abs-mem and --temp-dir.
MODEL_OPTIONS = ("band", "imsize", "cell", "iterations", "mgain", "threshold", "robust", "datacolumn",
                 "channels-out", "deconvolution-channels", "subimages", "spectral-pol-terms", "multiscale",
                 "minuvw-m", "minuvw-l", "multiscale-scale-bias", "multiscale-max-scales", "fits-mask",
                 "target-mask", "galvin-clip-mask", "erode-beam-shape", "local-rms-window",
                 "mask-threshold", "phasecentre", "parallel-gridding", "parallel-reordering")
CATALOGUE_COLUMN_KEYS = ("source_name", "ra", "dec", "pmra", "pmdec", "epoch")
NESTED = {"model": MODEL_OPTIONS, "catalogue_columns": CATALOGUE_COLUMN_KEYS}
QUERY_KEYS = ("exposure_min", "minimum_sb", "excluded_sbs", "quality_levels",
              "max_file_size_kb", "initial_radius_deg", "radius_frequency_mode",
              "reference_fwhm_deg", "reference_frequency_ghz", "query_limit")
FILE_MODEL_KEYS = ("fits-mask", "target-mask", "galvin-clip-mask")
INTEGER_THRESHOLD_KEYS = ("threshold", "mask-threshold")
INTEGER_MODEL_KEYS = ("imsize", "iterations", "channels-out", "deconvolution-channels", "subimages",
                      "spectral-pol-terms", "multiscale-max-scales", "parallel-gridding", "parallel-reordering",
                      "local-rms-window", *INTEGER_THRESHOLD_KEYS)


def integer_model_value(key, value):
    """Serialize integer CLI values without rounding or mutating frozen input."""
    minimum = 0 if key in INTEGER_THRESHOLD_KEYS else 1
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value < minimum or int(value) != value):
        kind = "nonnegative" if minimum == 0 else "positive"
        raise ValueError(f"model.{key} must be a {kind} integer for the DStools CLI; got {value!r}")
    return int(value)


def dstools_config_path(config):
    """dstools-config.py is looked for next to config.py unless given explicitly."""
    return Path(config).with_name("dstools-config.py")


def read_settings_file(path, name):
    path = Path(path).expanduser()
    if not path.is_file():
        raise ValueError(f"Settings file not found: {path} (expected {name})")
    values = runpy.run_path(str(path))
    return {k.lower(): v for k, v in values.items() if k.isupper() and not k.startswith("_")}


def merge(settings, overrides, origins, origin, allowed=SETTING_KEYS):
    unknown = set(overrides) - set(SETTING_KEYS)
    if unknown:
        raise ValueError(f"Unknown setting(s) in {origin}: {', '.join(sorted(k.upper() for k in unknown))}")
    for name, keys in FILES.items():
        misplaced = sorted(k.upper() for k in set(overrides) - set(allowed) if k in keys)
        if misplaced:
            raise ValueError(f"{', '.join(misplaced)} belong(s) in {name}, not in {origin}")
    for key, value in overrides.items():
        if key in NESTED:
            if not isinstance(value, dict):
                raise ValueError(f"{key.upper()} must be a dictionary of options")
            unknown_options = set(value) - set(NESTED[key])
            if unknown_options:
                raise ValueError(f"Unknown {key.upper()} options in {origin}: {sorted(unknown_options)}")
            settings.setdefault(key, {}).update(copy.deepcopy(value))
            for option in value:
                origins[key + "." + option] = origin
        else:
            settings[key] = value
        origins[key] = origin


def missing_settings(settings, files=FILES):
    """Names absent from every source, grouped by the file that should define them."""
    result = {}
    for name, keys in files.items():
        absent = [key.upper() for key in keys if key not in settings]
        for key in keys:
            if key in NESTED and key in settings:
                absent += [f"{key.upper()}[{option!r}]" for option in NESTED[key] if option not in settings[key]]
        if absent:
            result[name] = absent
    return result


def load(batch_config=None, dstools_config=None, overrides=None, use_environment=False,
         validate_values=True, complete=True):
    """Read config.py, then dstools-config.py, then ASKAP_<SETTING>, then CLI values."""
    settings, origins = {}, {}
    if batch_config is not None:
        merge(settings, read_settings_file(batch_config, "config.py"), origins,
              str(Path(batch_config).expanduser().resolve()), CONFIG_KEYS)
    if dstools_config is not None:
        merge(settings, read_settings_file(dstools_config, "dstools-config.py"), origins,
              str(Path(dstools_config).expanduser().resolve()), DSTOOLS_KEYS)
    if use_environment:
        for key in SETTING_KEYS:
            name = "ASKAP_" + key.upper()
            if name in os.environ:
                raw = os.environ[name]
                try:
                    value = json.loads(raw)
                except json.JSONDecodeError:
                    value = raw
                merge(settings, {key: value}, origins, "environment:" + name)
        # Explicit documented compatibility alias, without hidden paths.
        if "ASKAP_WORK" in os.environ and "ASKAP_WORK_ROOT" not in os.environ:
            merge(settings, {"work_root": os.environ["ASKAP_WORK"]}, origins, "environment:ASKAP_WORK")
    merge(settings, overrides or {}, origins, "CLI")
    if complete:
        missing = missing_settings(settings)
        if missing:
            raise ValueError("Settings missing (there are no built-in defaults): "
                             + "; ".join(f"{name}: {', '.join(names)}" for name, names in missing.items())
                             + ". Copy them, with their comments, from the repository version of the file"
                             " (for example: git show HEAD:config.py).")
    if validate_values:
        validate(settings)
    return settings, origins


def validate(s):
    if s["coordinate_frame"].lower() != "icrs":
        raise ValueError("Only ICRS coordinates are supported")
    if s["ra_unit"] not in {"deg", "hourangle"} or s["dec_unit"] != "deg":
        raise ValueError("RA unit must be deg/hourangle; Dec unit must be deg")
    if s["catalogue_format"] not in {"csv", "fits"}:
        raise ValueError("catalogue_format must be csv or fits")
    if s["radius_frequency_mode"] not in {"central", "top"}:
        raise ValueError("radius_frequency_mode must be central or top")
    for key in ("parallel_slots", "threads_per_slot", "query_limit", "download_retries", "max_observations_per_job", "begin"):
        if isinstance(s[key], bool) or not isinstance(s[key], int) or s[key] < 1:
            raise ValueError(f"{key} must be a positive integer")
    if s["parallel_slots"] * s["threads_per_slot"] > 32:
        raise ValueError("Ozstar CPU limit: slots × threads must be <= 32")
    for key in ("memory_gb", "tmp_gb", "hours_per_observation", "hours_per_target", "walltime_hours", "staging_budget_bytes", "crop_arcmin", "initial_radius_deg", "reference_fwhm_deg", "reference_frequency_ghz", "poll_seconds"):
        if not isinstance(s[key], (int, float)) or not math.isfinite(s[key]) or s[key] <= 0:
            raise ValueError(f"{key} must be positive and finite")
    if s["memory_gb"] / s["parallel_slots"] < 2:
        raise ValueError("Allocate at least 2 GiB per slot for WSClean and Python headroom")
    if s["walltime_hours"] > 48:
        raise ValueError("walltime_hours must not exceed 48")
    for key in ("overhead_hours", "reserve_minutes", "query_delay_seconds", "download_retry_delay_seconds", "extract_minuv_m"):
        if not isinstance(s[key], (int, float)) or not math.isfinite(s[key]) or s[key] < 0:
            raise ValueError(f"{key} must be finite and nonnegative")
    if s["walltime_hours"] <= max(s["overhead_hours"], s["reserve_minutes"] / 60):
        raise ValueError("Walltime must exceed overhead and exit reserve")
    if s["end"] is not None and (not isinstance(s["end"], int) or s["end"] < s["begin"]):
        raise ValueError("end must be an integer >= begin, or None")
    for key in ("exposure_min", "max_file_size_kb", "mask_radius_arcsec"):
        if s[key] is not None and (not math.isfinite(s[key]) or s[key] < 0):
            raise ValueError(f"{key} must be finite and nonnegative, or None")
    if not s["quality_levels"] or not all(isinstance(x, str) for x in s["quality_levels"]):
        raise ValueError("quality_levels must be a nonempty string list")
    if s["primary_beam"] and s["primary_beam_by_source"]:
        raise ValueError("Choose primary_beam or primary_beam_by_source")
    for key in ("pmra_includes_cosdec", "extract_noflag", "extract_baseline_average"):
        if not isinstance(s[key], bool):
            raise ValueError(f"{key} must be boolean")
    if not isinstance(s["bind"], list) or not isinstance(s["modules"], list):
        raise ValueError("bind and modules must be lists")
    if s["max_continuations"] < 0:
        raise ValueError("max_continuations cannot be negative")
    model = s["model"]
    if model["band"] not in {"AK_low", "AK_mid", "AK_high"}:
        raise ValueError("Only ASKAP AK_* bands are supported")
    if model["datacolumn"] not in {"data", "corrected"}:
        raise ValueError("model datacolumn must be data/corrected")
    if model["minuvw-m"] is not None and model["minuvw-l"] is not None:
        raise ValueError("Use only one of minuvw-m and minuvw-l")
    for key in INTEGER_MODEL_KEYS:
        if model[key] is not None:
            integer_model_value(key, model[key])
    for key in ("parallel-gridding", "parallel-reordering"):
        if model[key] is not None and model[key] > s["threads_per_slot"]:
            raise ValueError(f"model.{key} exceeds threads_per_slot")
    if model["phasecentre"] is not None:
        from .catalogue import coordinates
        if not isinstance(model["phasecentre"], (list, tuple)) or len(model["phasecentre"]) != 2:
            raise ValueError("model.phasecentre must be [RA_deg, Dec_deg] or None")
        model["phasecentre"] = list(coordinates(*model["phasecentre"]))
    for key in ("multiscale", "erode-beam-shape"):
        if not isinstance(model[key], bool):
            raise ValueError(f"model.{key} must be boolean")
    if not 0 < model["mgain"] <= 1 or not -2 <= model["robust"] <= 2:
        raise ValueError("mgain must be (0,1]; robust must be [-2,2]")
    for key in ("cell", "multiscale-scale-bias", "minuvw-m", "minuvw-l"):
        if model[key] is not None and (not math.isfinite(model[key]) or model[key] < 0):
            raise ValueError(f"Invalid model.{key}")
    if s["work_root"]:
        s["work_root"] = str(safe_path(s["work_root"]))
        s["product_root"] = str(safe_path(s["product_root"] or Path(s["work_root"]) / "products"))
        work, products = Path(s["work_root"]), Path(s["product_root"])
        if work == products or products in work.parents:
            raise ValueError("PRODUCT_ROOT must not equal or contain WORK_ROOT")
        for reserved in ("state", "staging", "logs", "observations", "query"):
            internal = work / reserved
            if products == internal or internal in products.parents:
                raise ValueError("PRODUCT_ROOT overlaps a reserved runtime directory")
    for key in ("container", "catalogue_path"):
        if s[key]:
            s[key] = str(Path(s[key]).expanduser().resolve())
    from .common import sb_number
    s["excluded_sbs"] = sorted({sb_number(x) for x in s["excluded_sbs"]})
    if s["minimum_sb"] is not None:
        s["minimum_sb"] = sb_number(s["minimum_sb"])


def query_settings(s):
    return {key: copy.deepcopy(s[key]) for key in QUERY_KEYS}


def freeze(s, origins):
    """Hash scientific inputs, record effective configuration; never read a password."""
    if not s["work_root"]:
        raise ValueError("A work directory is required")
    if not s["container"] or not Path(s["container"]).is_file():
        raise ValueError("Configure an existing DStools SIF with --container / CONTAINER")
    safe_path(s["container"])
    products = Path(s["product_root"])
    products.mkdir(parents=True, exist_ok=True)
    with lock(products / ".owner.lock", wait=True):
        owner = read_json(products / ".askap-workspace.json", {})
        if owner and owner.get("work_root") != s["work_root"]:
            raise ValueError("PRODUCT_ROOT belongs to another workspace; use an independent product directory")
        write_json(products / ".askap-workspace.json", {"schema": 1, "work_root": s["work_root"]})
    frozen = copy.deepcopy(s)
    files = {}
    paths = [s["model"][key] for key in FILE_MODEL_KEYS]
    paths += [s["primary_beam"], *s["primary_beam_by_source"].values()]
    for raw in paths:
        if raw:
            path = safe_path(raw)
            if not path.is_file():
                raise ValueError(f"Configured scientific input file is missing: {path}")
            files[str(path)] = digest(path)
    for key in FILE_MODEL_KEYS:
        if frozen["model"][key]:
            frozen["model"][key] = str(safe_path(frozen["model"][key]))
    for key in ("primary_beam",):
        if frozen[key]:
            frozen[key] = str(safe_path(frozen[key]))
    frozen["primary_beam_by_source"] = {name: str(safe_path(path)) for name, path in frozen["primary_beam_by_source"].items()}
    payload = {"schema": 1, "settings": frozen, "origins": origins,
               "sif_sha256": digest(s["container"]), "external_files": files}
    state_root = Path(s["work_root"]) / "state"
    catalogue_input = read_json(state_root / "input_catalogue.json", {})
    source_input = read_json(state_root / "source.json", {})
    if catalogue_input:
        payload["input_signature"] = catalogue_input["signature"]
    elif source_input:
        payload["input_signature"] = signature(source_input)
    payload["version"] = signature({k: v for k, v in payload.items() if k != "origins"})
    return payload


def model_identity(frozen, observation):
    s = frozen["settings"]
    return signature({"observation": observation["key"], "sif": frozen["sif_sha256"],
                      "model": s["model"], "threads": s["threads_per_slot"],
                      "memory_gb": s["memory_gb"], "slots": s["parallel_slots"],
                      "masks": {str(s["model"][k]): frozen["external_files"].get(str(s["model"][k])) for k in FILE_MODEL_KEYS}})
