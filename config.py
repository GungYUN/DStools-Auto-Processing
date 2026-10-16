"""Program settings: input/output, query, Slurm/OzSTAR environment, credentials.
DStools options are in dstools-config.py next to this file.
CLI > ASKAP_<SETTING> environment > these files. Both askap-batch and
askap-source read both files on every command. There are no built-in
defaults: keep every setting (None where a value is optional).
"""
import sys

# Edit this file directly. Stop the controller and any outstanding Slurm job
# before changing settings for unfinished work, then run the command again.
# Single-source coordinates/name are supplied to askap-source query on the CLI;
# CATALOGUE_* and BEGIN/END select the batch catalogue only.
# ----------------------------------------------------------------------
# 01 Input/output: absolute paths; None requires CLI input.
# PRODUCT_ROOT=None means WORK_ROOT/products. Never use a production workspace
# from the old UCD pipeline. Previous input revisions are kept under logs/.
WORK_ROOT = None
PRODUCT_ROOT = None
CATALOGUE_PATH = None
# 02 CSV/FITS and exact column mapping. Optional None means absent => zero PM.
# Configured but empty/invalid PM cells are errors, never silently zero.
CATALOGUE_FORMAT = "csv"
CATALOGUE_FITS_HDU = 1
CATALOGUE_COLUMNS = {"source_name": "Name", "ra": "RA", "dec": "DEC",
                     "pmra": None, "pmdec": None, "epoch": None}
# 03 ICRS only; hourangle RA accepts sexagesimal, DEC always degrees.
# Epoch is the time of the catalogue position, not an observation date.
RA_UNIT = "deg"
DEC_UNIT = "deg"
COORDINATE_FRAME = "icrs"
COORDINATE_EPOCH = "J2000"
PM_UNIT = "mas/yr"
PMRA_INCLUDES_COSDEC = True
# 04 Inclusive original row numbers (1-based); None end = full table.
BEGIN = 1
END = None
# 05 Public ASKAP visibility. Exposure strictly > seconds; None disables.
# SB minimum includes endpoint; empty exclusion list removes no SB.
EXPOSURE_MIN = 500.0
MINIMUM_SB = None
EXCLUDED_SBS = []
QUALITY_LEVELS = ["GOOD", "UNCERTAIN"]
MAX_FILE_SIZE_KB = 50 * 1024 * 1024  # CASDA KB, None disables upper bound.
INITIAL_RADIUS_DEG = 1.0
RADIUS_FREQUENCY_MODE = "central"  # central arithmetic frequency mean / top
REFERENCE_FWHM_DEG = 1.2
REFERENCE_FREQUENCY_GHZ = 1.4
QUERY_LIMIT = 5000  # Reaching TOP fails closed; increase and query again.
QUERY_DELAY_SECONDS = 0.2
DOWNLOAD_RETRIES = 3  # Transient staging/download attempts, independent of science retry.
DOWNLOAD_RETRY_DELAY_SECONDS = 30.0
# 06 Resources: slots × threads <=32, memory is TOTAL GiB, tmp is Slurm GB.
# Persistent MS/models do not use Slurm tmp. Estimate is max lane load + overhead.
PARALLEL_SLOTS = 4
THREADS_PER_SLOT = 8
MEMORY_GB = 160
TMP_GB = 10
HOURS_PER_OBSERVATION = 2.0
HOURS_PER_TARGET = 0.5
OVERHEAD_HOURS = 2.0
WALLTIME_HOURS = 48.0  # Upper bound, not a fixed request. <=48 hours.
RESERVE_MINUTES = 20.0
STAGING_BUDGET_BYTES = 2 * 1024**4
MAX_OBSERVATIONS_PER_JOB = 40
# 07 Site-specific values. Use your own SIF and a host Python available on
# login and compute nodes. By default use the interpreter running this command.
CONTAINER = None
APPTAINER = "apptainer"
BIND = []  # Optional additional Apptainer bind strings; runtime paths auto-bound.
PYTHON = sys.executable
CONTAINER_PYTHON = "/opt/askap/bin/python"
MODULES = ["gcc/13.3.0", "python/3.12.3", "apptainer"]
PARTITION = None
ACCOUNT = None
# 08 Recovery/login-only credentials. Password contents never enter snapshots.
MAX_CONTINUATIONS = 20
POLL_SECONDS = 60.0
CASDA_USERNAME = None
CASDA_PASSWORD_FILE = "~/.config/askap/casda-password"
CASDA_PASSWORD_ENV = "CASDA_PASSWORD"
OZSTAR_HOST = "ozstar.swin.edu.au"
OZSTAR_USER = None
ADA_ROOT = None  # Optional local destination on Ada, e.g. /PATH/TO/ada/products.

TRANSFER_PASSWORD_FILE = None  # Ada SSH file, mode 600; None uses SSH keys/agent.
