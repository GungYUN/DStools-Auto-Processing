#!/bin/bash
#SBATCH --job-name=build-dstools-sif
#SBATCH --output=/fred/YOUR_PROJECT/YOUR_OZSTAR_USER/dstools/sif-build-YYYYMMDD/logs/build-%j.out
#SBATCH --error=/fred/YOUR_PROJECT/YOUR_OZSTAR_USER/dstools/sif-build-YYYYMMDD/logs/build-%j.err
#SBATCH --time=12:00:00
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=8
#SBATCH --mem=32G
#SBATCH --tmp=32G

# The compute node is intentionally treated as fully offline.  Every network
# artefact must already have been prepared and hashed by prep_sources.sh.
set -Eeuo pipefail
umask 027

BUILD_ROOT=/fred/YOUR_PROJECT/YOUR_OZSTAR_USER/dstools/sif-build-YYYYMMDD
source "$BUILD_ROOT/recipe/versions.env"
[ "$(uname -m)" = x86_64 ] || { echo "ERROR: this build requires x86_64." >&2; exit 1; }

trap 'rc=$?; echo "Offline SIF build failed at line ${BASH_LINENO[0]} (exit=${rc})." >&2' ERR

module load "$HOST_APPTAINER_MODULE"
if ! apptainer --version | cmp -s - "$BUILD_ROOT/manifests/apptainer-version.txt"; then
    echo "ERROR: the Apptainer module version differs from login-node preparation." >&2
    apptainer --version >&2
    cat "$BUILD_ROOT/manifests/apptainer-version.txt" >&2
    exit 1
fi

export APPTAINER_CACHEDIR="$BUILD_ROOT/cache/apptainer"
export APPTAINER_CONFIGDIR="$BUILD_ROOT/config/apptainer"
export XDG_CACHE_HOME="$BUILD_ROOT/cache/xdg"
export PIP_CACHE_DIR="$BUILD_ROOT/cache/pip"

LOCAL_TMP="${SLURM_TMPDIR:-${TMPDIR:?Slurm local TMPDIR is required}}"
export APPTAINER_TMPDIR="$LOCAL_TMP/apptainer-build"
mkdir -p "$APPTAINER_TMPDIR"

echo "=== Frozen build configuration ==="
cat "$BUILD_ROOT/recipe/versions.env"
echo

echo "=== Offline input preflight ==="
INPUT_MANIFEST="$BUILD_ROOT/manifests/build-input-files.sha256"
test -s "$INPUT_MANIFEST"
sha256sum -c --quiet "$INPUT_MANIFEST"
echo "All frozen input hashes passed."

test -s "$BUILD_ROOT/base/$BASE_SIF_NAME"
test -s "$BUILD_ROOT/offline/apt/Packages"
test -n "$(find "$BUILD_ROOT/offline/apt" -maxdepth 1 -name '*.deb' -print -quit)"
test -s "$BUILD_ROOT/offline/python/askap-python.tar.gz"
test -s "$BUILD_ROOT/offline/casa/casa-data.tar"
test -s "$BUILD_ROOT/offline/astropy/sites.json"
test -s "$BUILD_ROOT/offline/casacore/$WSRT_MEASURES_FILENAME"
test -s "$BUILD_ROOT/recipe/python-runtime-requirements.lock.txt"
test -s "$BUILD_ROOT/patches/0003-wsclean-header-only-boost-system.patch"
test -s "$BUILD_ROOT/patches/0004-wsclean-radler-offline-fetchcontent.patch"
test -s "$BUILD_ROOT/patches/0005-wsclean-consistent-abi-libraries.patch"
test -s "$BUILD_ROOT/patches/0006-dstools-subprocess-drain-and-check.patch"
test -s "$BUILD_ROOT/offline/wheels/.requirements.sha256"
for source_name in casacore wsclean python-casacore dstools-build; do
    test -s "$BUILD_ROOT/offline/sources/${source_name}.tar"
done
test -s "$BUILD_ROOT/manifests/wsclean-fetchcontent.lock"
for dependency_name in xtl xsimd xtensor xtensor-blas xtensor-fftw; do
    test -s "$BUILD_ROOT/offline/sources/fetchcontent-${dependency_name}.tar"
done

PIN_PLACEHOLDER='<PIN''_ME>'
MANUAL_PLACEHOLDER='TODO''(人工填写)'
if grep -R -n -F -e "$PIN_PLACEHOLDER" -e "$MANUAL_PLACEHOLDER" \
    --include='*.txt' --include='*.env' --include='*.def' \
    --include='*.sh' --include='*.sbatch' \
    "$BUILD_ROOT/recipe"; then
    echo "ERROR: unresolved placeholder found in frozen build inputs." >&2
    exit 2
fi

if find "$BUILD_ROOT/offline/wheels" -maxdepth 1 -type f \
    -iname 'python_casacore*' -print -quit | grep -q .; then
    echo "ERROR: python-casacore wheel is forbidden; it must be built from source." >&2
    exit 3
fi

TREEHASH="$(cut -d' ' -f1 "$BUILD_ROOT/manifests/dstools-patched-source-tree.sha256" | cut -c1-12)"
# Include the full frozen input set in the name: a recipe-only fix must
# produce a new candidate without overwriting a prior successful image.
INPUT_HASH="$(sha256sum "$INPUT_MANIFEST" | cut -c1-12)"
WSCLEAN_VERSION="${WSCLEAN_TAG#v}"
CASACORE_VERSION="${CASACORE_TAG#v}"
PYTHON_LABEL="py${PYTHON_VERSION//./}"
SIF_NAME="dstools-dev-${TREEHASH}_wsclean-${WSCLEAN_VERSION}_casacore-${CASACORE_VERSION}_${PYTHON_LABEL}_inputs-${INPUT_HASH}.candidate.sif"
SIF_OUT="$BUILD_ROOT/artifacts/$SIF_NAME"
# A retry within the same interactive Slurm allocation must use a new
# temporary candidate path without touching an earlier failed attempt.
SIF_PART="$BUILD_ROOT/artifacts/.${SIF_NAME}.part-${SLURM_JOB_ID:-interactive}-$$"

echo "Candidate SIF: $SIF_OUT"
test ! -e "$SIF_OUT"
test ! -e "$SIF_PART"

cd "$BUILD_ROOT/recipe"
apptainer build --fakeroot "$SIF_PART" askap-dstools.def
test -s "$SIF_PART"

# Apptainer's %test runs under the build's fakeroot context and can read data
# that the real OzSTAR uid cannot. Check the final image as this ordinary user
# before publishing it as a candidate. Keep CASA and python-casacore imports
# in separate processes, as DStools does.
echo "=== Ordinary-user, clean-environment runtime smoke tests ==="
apptainer exec --cleanenv --no-home "$SIF_PART" /opt/askap/bin/python -c '
import casacore.tables
from casacore.measures import measures
from dstools.utils import LOCATIONS
print("DStools locations:", sorted(LOCATIONS))
m = measures()
m.measure(m.epoch("utc", "today"), "tai")
print("Astropy and casacore data readable by the ordinary user")
'
apptainer exec --cleanenv --no-home "$SIF_PART" /opt/askap/bin/python -c '
from casatasks import tclean, phaseshift, uvsub, mstransform
print("CASA tasks and data readable by the ordinary user")
'
apptainer exec --cleanenv --no-home "$SIF_PART" /opt/askap/bin/python -c '
import logging
import os
import subprocess
import sys
from dstools.logger import parse_stdout_stderr
logger = logging.getLogger("dstools-subprocess-ordinary-user-test")
child = subprocess.Popen(
    [sys.executable, "-c", "import os,time; os.close(2); print(\"first\", flush=True); time.sleep(0.1); print(\"last\", flush=True)"],
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
)
assert parse_stdout_stderr(child, logger) == 0
child = subprocess.Popen(
    [sys.executable, "-c", "raise SystemExit(7)"],
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
)
try:
    parse_stdout_stderr(child, logger)
except subprocess.CalledProcessError as error:
    assert error.returncode == 7
else:
    raise AssertionError("DStools swallowed a non-zero child exit")
print("DStools subprocess handling works for the ordinary runtime user")
'
mv -n "$SIF_PART" "$SIF_OUT"
if [ -e "$SIF_PART" ]; then
    echo "ERROR: candidate path appeared during the build; refusing to overwrite it: $SIF_OUT" >&2
    exit 4
fi

sha256sum "$SIF_OUT" > "$BUILD_ROOT/manifests/candidate-sif.sha256"
apptainer inspect --json "$SIF_OUT" \
    > "$BUILD_ROOT/manifests/candidate-inspect.json"
apptainer inspect --deffile "$SIF_OUT" \
    > "$BUILD_ROOT/manifests/candidate-definition.txt"

echo
echo "=== Offline build completed ==="
echo "Candidate SIF: $SIF_OUT"
echo "Do not replace the production SIF until Gate 1 through Gate 5 pass."
