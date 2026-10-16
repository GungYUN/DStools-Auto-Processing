#!/bin/bash
# =============================================================================
# OzSTAR login-node preparation for a fully offline compute-node SIF build.
#
# Run this script on a login node, where outbound network access is available.
# It downloads and freezes every network-derived input under BUILD_ROOT.  The
# later Slurm job performs no network access at all.
# =============================================================================
set -Eeuo pipefail
umask 027

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/versions.env"

# This recipe-level correctness patch was added after the original frozen
# dev snapshot exposed a subprocess pipe race during the OzSTAR Gate 5 run.
# Keep the upstream snapshot hash unchanged and include the patch in the
# derived build key alongside the two pre-existing ASKAP patches.
DSTOOLS_PATCH_FILES+=("0006-dstools-subprocess-drain-and-check.patch")

die() {
    echo "ERROR: $*" >&2
    exit 1
}

trap 'rc=$?; echo "Preparation failed at line ${BASH_LINENO[0]} (exit=${rc})." >&2' ERR

if [ "$SCRIPT_DIR" != "$BUILD_ROOT/recipe" ]; then
    die "Place this complete recipe directory at $BUILD_ROOT/recipe before running it."
fi
[ "$(uname -m)" = x86_64 ] || die "This frozen ASKAP stack requires a Linux x86_64 host."

BASE_DIR="$BUILD_ROOT/base"
CACHE_DIR="$BUILD_ROOT/cache"
MANIFEST_DIR="$BUILD_ROOT/manifests"
OFFLINE_DIR="$BUILD_ROOT/offline"
PATCH_DIR="$BUILD_ROOT/patches"
SRC_DIR="$BUILD_ROOT/src"
WORK_DIR="$BUILD_ROOT/work"

mkdir -p \
    "$BASE_DIR" \
    "$CACHE_DIR/apptainer" "$CACHE_DIR/mamba" "$CACHE_DIR/pip" "$CACHE_DIR/poetry" "$CACHE_DIR/xdg" \
    "$BUILD_ROOT/config/apptainer" "$BUILD_ROOT/config/xdg" "$BUILD_ROOT/config/poetry" \
    "$BUILD_ROOT/data/xdg" "$WORK_DIR/tmp" "$WORK_DIR/apptainer-tmp" \
    "$BUILD_ROOT/logs" "$BUILD_ROOT/validation" "$BUILD_ROOT/artifacts" \
    "$MANIFEST_DIR/baseline/ada" "$MANIFEST_DIR/baseline/old-ozstar-sif" \
    "$OFFLINE_DIR" "$PATCH_DIR" "$SRC_DIR" "$WORK_DIR"

export APPTAINER_CACHEDIR="$CACHE_DIR/apptainer"
export APPTAINER_CONFIGDIR="$BUILD_ROOT/config/apptainer"
export APPTAINER_TMPDIR="$WORK_DIR/apptainer-tmp"
export XDG_CACHE_HOME="$CACHE_DIR/xdg"
export XDG_CONFIG_HOME="$BUILD_ROOT/config/xdg"
export XDG_DATA_HOME="$BUILD_ROOT/data/xdg"
export PIP_CACHE_DIR="$CACHE_DIR/pip"
export PIP_DISABLE_PIP_VERSION_CHECK=1
export POETRY_CACHE_DIR="$CACHE_DIR/poetry"
export POETRY_CONFIG_DIR="$BUILD_ROOT/config/poetry"
export TMPDIR="$WORK_DIR/tmp"

module load "$HOST_GCC_MODULE"
module load "$HOST_PYTHON_MODULE"
module load "$HOST_APPTAINER_MODULE"

command -v apptainer >/dev/null || die "apptainer is not available after module load."
command -v git >/dev/null || die "git is required on the login node."
command -v curl >/dev/null || die "curl is required on the login node."
APPTAINER_VERSION="$(apptainer --version)"
if [ -e "$MANIFEST_DIR/apptainer-version.txt" ]; then
    [ "$(<"$MANIFEST_DIR/apptainer-version.txt")" = "$APPTAINER_VERSION" ] \
        || die "The Apptainer module version changed; use a new BUILD_ROOT."
else
    printf '%s\n' "$APPTAINER_VERSION" > "$MANIFEST_DIR/apptainer-version.txt"
fi

HOST_PYTHON="$(command -v python3 || command -v python || true)"
test -n "$HOST_PYTHON" || die "Python is not available after loading $HOST_PYTHON_MODULE."
"$HOST_PYTHON" -c 'import sys; assert sys.version_info[:2] == (3, 12), sys.version' \
    || die "The login-node preparation Python must be Python 3.12."

record_or_verify_sha() {
    local input_file=$1
    local manifest_file=$2
    if [ -e "$manifest_file" ]; then
        sha256sum -c "$manifest_file"
    else
        sha256sum "$input_file" > "$manifest_file"
    fi
}

write_or_compare() {
    local new_file=$1
    local frozen_file=$2
    if [ -e "$frozen_file" ]; then
        if ! cmp -s "$new_file" "$frozen_file"; then
            die "$frozen_file differs from the newly resolved content. Use a new BUILD_ROOT for a new dependency solve."
        fi
        rm -f "$new_file"
    else
        mv "$new_file" "$frozen_file"
    fi
}

write_tree_manifest() {
    local tree_root=$1
    local output_file=$2
    (
        cd "$tree_root"
        find . -type f \
            ! -path '*/__pycache__/*' \
            ! -name '*.pyc' \
            ! -name '.DS_Store' \
            -print0 \
            | LC_ALL=C sort -z \
            | xargs -0 -r sha256sum
    ) > "$output_file"
}

validate_pure_python_wheel() {
    local wheel_file=$1
    "$HOST_PYTHON" - "$wheel_file" <<'PY'
import pathlib
import sys
import zipfile

wheel = pathlib.Path(sys.argv[1])
with zipfile.ZipFile(wheel) as archive:
    names = archive.namelist()
    native = [
        name for name in names
        if name.lower().endswith((".so", ".dylib", ".dll", ".pyd"))
    ]
    metadata_names = [name for name in names if name.endswith(".dist-info/WHEEL")]
    if len(metadata_names) != 1:
        raise SystemExit(f"{wheel}: expected one .dist-info/WHEEL file")
    metadata = archive.read(metadata_names[0]).decode("utf-8")

if native:
    raise SystemExit(f"{wheel}: contains native libraries: {native}")
if "Root-Is-Purelib: true" not in metadata:
    raise SystemExit(f"{wheel}: is not marked as a pure-Python wheel")
if "Tag: py3-none-any" not in metadata:
    raise SystemExit(f"{wheel}: is not tagged py3-none-any")
PY
}

patch_rm_lite_wheel() {
    local input_wheel=$1
    local output_dir=$2
    local base_version=$3
    "$HOST_PYTHON" - "$input_wheel" "$output_dir" "$base_version" <<'PY'
import base64
import copy
import csv
import hashlib
import io
import pathlib
import re
import sys
import zipfile

input_wheel = pathlib.Path(sys.argv[1])
output_dir = pathlib.Path(sys.argv[2])
base_version = sys.argv[3]
patched_version = f"{base_version}+askap1"
old_dist_info = f"rm_lite-{base_version}.dist-info"
new_dist_info = f"rm_lite-{patched_version}.dist-info"
output_wheel = output_dir / f"rm_lite-{patched_version}-py3-none-any.whl"

with zipfile.ZipFile(input_wheel) as source:
    source_items = [(copy.copy(info), source.read(info.filename)) for info in source.infolist()]

output_items = []
metadata_patches = 0
for info, payload in source_items:
    new_name = info.filename.replace(old_dist_info, new_dist_info, 1)
    if new_name == f"{new_dist_info}/RECORD":
        continue
    if new_name == f"{new_dist_info}/METADATA":
        text = payload.decode("utf-8")
        old_version = f"Version: {base_version}\n"
        old_dependency = "Requires-Dist: tdqm\n"
        if text.count(old_version) != 1 or text.count(old_dependency) != 1:
            raise SystemExit("rm-lite metadata did not contain the expected version/dependency exactly once")
        text = text.replace(old_version, f"Version: {patched_version}\n", 1)
        text = text.replace(old_dependency, "Requires-Dist: tqdm\n", 1)
        payload = text.encode("utf-8")
        metadata_patches += 1
    info.filename = new_name
    output_items.append((info, payload))

if metadata_patches != 1:
    raise SystemExit("rm-lite wheel did not contain exactly one expected METADATA file")

record_buffer = io.StringIO()
record_writer = csv.writer(record_buffer, lineterminator="\n")
for info, payload in output_items:
    digest = base64.urlsafe_b64encode(hashlib.sha256(payload).digest()).rstrip(b"=").decode("ascii")
    record_writer.writerow((info.filename, f"sha256={digest}", len(payload)))
record_name = f"{new_dist_info}/RECORD"
record_writer.writerow((record_name, "", ""))

with zipfile.ZipFile(output_wheel, "w") as target:
    for info, payload in output_items:
        target.writestr(info, payload)
    target.writestr(record_name, record_buffer.getvalue().encode("utf-8"), zipfile.ZIP_DEFLATED)

with zipfile.ZipFile(output_wheel) as check:
    metadata = check.read(f"{new_dist_info}/METADATA").decode("utf-8")
if f"Version: {patched_version}\n" not in metadata:
    raise SystemExit("patched rm-lite version is missing")
if "Requires-Dist: tqdm\n" not in metadata or re.search(r"^Requires-Dist: tdqm$", metadata, re.MULTILINE):
    raise SystemExit("patched rm-lite dependency metadata is incorrect")

print(output_wheel)
PY
}

ensure_checkout() {
    local name=$1
    local url=$2
    local tag=$3
    local expected_commit=$4
    local recursive=${5:-no}
    local checkout="$SRC_DIR/$name"

    if [ ! -d "$checkout/.git" ]; then
        if [ -e "$checkout" ]; then
            die "$checkout exists but is not a Git checkout. Move it aside and rerun."
        fi
        if [ "$recursive" = yes ]; then
            git clone --branch "$tag" --depth 1 --recursive "$url" "$checkout"
        else
            git clone --branch "$tag" --depth 1 "$url" "$checkout"
        fi
    fi

    if [ "$recursive" = yes ]; then
        git -C "$checkout" submodule update --init --recursive
        if git -C "$checkout" submodule status --recursive | grep -Eq '^[-+U]'; then
            git -C "$checkout" submodule status --recursive >&2
            die "$name has missing or mismatched submodules."
        fi
    fi

    local actual_commit
    actual_commit="$(git -C "$checkout" rev-parse HEAD)"
    [ "$actual_commit" = "$expected_commit" ] \
        || die "$name HEAD=$actual_commit, expected $expected_commit for $tag."
    git -C "$checkout" diff --quiet
    git -C "$checkout" diff --cached --quiet
    [ -z "$(git -C "$checkout" status --porcelain --untracked-files=all)" ] \
        || die "$name checkout is not clean."
    printf '%s\n' "$actual_commit" > "$MANIFEST_DIR/$name.commit"
}

echo "=== 0. Freeze the human-selected build configuration ==="
record_or_verify_sha "$SCRIPT_DIR/versions.env" "$MANIFEST_DIR/versions.env.sha256"

echo "=== 1. Freeze bootstrap SIF ==="
BASE_SIF="$BASE_DIR/$BASE_SIF_NAME"
if [ ! -e "$BASE_SIF" ]; then
    BASE_PART="$BASE_SIF.part.$$"
    apptainer pull "$BASE_PART" "$BASE_IMAGE_REF"
    mv "$BASE_PART" "$BASE_SIF"
fi
record_or_verify_sha "$BASE_SIF" "$MANIFEST_DIR/base-sif.sha256"

echo "=== 2. Freeze exact source checkouts ==="
ensure_checkout casacore "$CASACORE_GIT_URL" "$CASACORE_TAG" \
    "$CASACORE_EXPECTED_COMMIT" no
ensure_checkout python-casacore "$PYTHON_CASACORE_GIT_URL" "$PYTHON_CASACORE_TAG" \
    "$PYTHON_CASACORE_EXPECTED_COMMIT" no
ensure_checkout wsclean "$WSCLEAN_GIT_URL" "$WSCLEAN_TAG" \
    "$WSCLEAN_EXPECTED_COMMIT" yes

echo "=== 2.0. Validate the required WSClean patch ==="
WSCLEAN_PATCH_NAME=0003-wsclean-header-only-boost-system.patch
WSCLEAN_PACKAGED_PATCH="$SCRIPT_DIR/patches/$WSCLEAN_PATCH_NAME"
WSCLEAN_STAGED_PATCH="$PATCH_DIR/$WSCLEAN_PATCH_NAME"
test -s "$WSCLEAN_PACKAGED_PATCH" \
    || die "Mandatory WSClean patch is missing: $WSCLEAN_PACKAGED_PATCH"
install -m 0640 "$WSCLEAN_PACKAGED_PATCH" "$WSCLEAN_STAGED_PATCH"
patch -d "$SRC_DIR/wsclean" -p1 --batch --forward --dry-run \
    < "$WSCLEAN_STAGED_PATCH"
sha256sum "$WSCLEAN_STAGED_PATCH" > "$MANIFEST_DIR/wsclean-patch.sha256"

WSCLEAN_RADLER_PATCH_NAME=0004-wsclean-radler-offline-fetchcontent.patch
WSCLEAN_RADLER_PACKAGED_PATCH="$SCRIPT_DIR/patches/$WSCLEAN_RADLER_PATCH_NAME"
WSCLEAN_RADLER_STAGED_PATCH="$PATCH_DIR/$WSCLEAN_RADLER_PATCH_NAME"
test -s "$WSCLEAN_RADLER_PACKAGED_PATCH" \
    || die "Mandatory WSClean Radler patch is missing: $WSCLEAN_RADLER_PACKAGED_PATCH"
install -m 0640 "$WSCLEAN_RADLER_PACKAGED_PATCH" "$WSCLEAN_RADLER_STAGED_PATCH"
patch -d "$SRC_DIR/wsclean" -p1 --batch --forward --dry-run \
    < "$WSCLEAN_RADLER_STAGED_PATCH"
sha256sum "$WSCLEAN_RADLER_STAGED_PATCH" > "$MANIFEST_DIR/wsclean-radler-patch.sha256"

WSCLEAN_ABI_PATCH_NAME=0005-wsclean-consistent-abi-libraries.patch
WSCLEAN_ABI_PACKAGED_PATCH="$SCRIPT_DIR/patches/$WSCLEAN_ABI_PATCH_NAME"
WSCLEAN_ABI_STAGED_PATCH="$PATCH_DIR/$WSCLEAN_ABI_PATCH_NAME"
test -s "$WSCLEAN_ABI_PACKAGED_PATCH" \
    || die "Mandatory WSClean ABI patch is missing: $WSCLEAN_ABI_PACKAGED_PATCH"
install -m 0640 "$WSCLEAN_ABI_PACKAGED_PATCH" "$WSCLEAN_ABI_STAGED_PATCH"
patch -d "$SRC_DIR/wsclean" -p1 --batch --forward --dry-run \
    < "$WSCLEAN_ABI_STAGED_PATCH"
sha256sum "$WSCLEAN_ABI_STAGED_PATCH" > "$MANIFEST_DIR/wsclean-abi-patch.sha256"

# WSClean's pinned aocommon submodule uses CMake FetchContent for these five
# header-only libraries.  Read the requested tags from that exact source
# snapshot instead of maintaining a second, potentially divergent pin list.
echo "=== 2.1. Freeze WSClean FetchContent sources for offline CMake ==="
FETCHCONTENT_SRC_DIR="$SRC_DIR/fetchcontent"
mkdir -p "$FETCHCONTENT_SRC_DIR"
FETCHCONTENT_CMAKE="$SRC_DIR/wsclean/external/aocommon/CMake/FetchXTensor.cmake"
RADLER_FETCHCONTENT_CMAKE="$SRC_DIR/wsclean/external/radler/external/aocommon/CMake/FetchXTensor.cmake"
test -s "$FETCHCONTENT_CMAKE" || die "Missing WSClean FetchXTensor.cmake."
test -s "$RADLER_FETCHCONTENT_CMAKE" || die "Missing Radler FetchXTensor.cmake."
fetchcontent_tag() {
    local name=$1
    local cmake_file=$2
    awk -v key="${name}_GIT_TAG" \
        '$1 == "set(" key { sub(/\)$/, "", $2); print $2; exit }' "$cmake_file"
}
FETCHCONTENT_LOCK_PART="$WORK_DIR/tmp/wsclean-fetchcontent.lock.part.$$"
: > "$FETCHCONTENT_LOCK_PART"
for dependency_name in xtl xsimd xtensor xtensor-blas xtensor-fftw; do
    dependency_tag="$(fetchcontent_tag "$dependency_name" "$FETCHCONTENT_CMAKE")"
    radler_tag="$(fetchcontent_tag "$dependency_name" "$RADLER_FETCHCONTENT_CMAKE")"
    test -n "$dependency_tag" \
        || die "Missing ${dependency_name}_GIT_TAG in $FETCHCONTENT_CMAKE."
    [ "$dependency_tag" = "$radler_tag" ] \
        || die "WSClean and Radler request different $dependency_name revisions."

    dependency_checkout="$FETCHCONTENT_SRC_DIR/$dependency_name"
    if [ ! -d "$dependency_checkout/.git" ]; then
        [ ! -e "$dependency_checkout" ] \
            || die "$dependency_checkout exists but is not a Git checkout."
        dependency_checkout_part="$WORK_DIR/tmp/fetchcontent-${dependency_name}.part.$$"
        git clone "https://github.com/xtensor-stack/${dependency_name}.git" \
            "$dependency_checkout_part"
        git -C "$dependency_checkout_part" checkout --detach "$dependency_tag"
        mv "$dependency_checkout_part" "$dependency_checkout"
    fi
    dependency_commit="$(git -C "$dependency_checkout" rev-parse HEAD)"
    if [[ "$dependency_tag" =~ ^[0-9a-f]{40}$ ]]; then
        [ "$dependency_commit" = "$dependency_tag" ] \
            || die "$dependency_name is not at its pinned commit $dependency_tag."
    else
        tagged_commit="$(git -C "$dependency_checkout" rev-parse "${dependency_tag}^{commit}")"
        [ "$dependency_commit" = "$tagged_commit" ] \
            || die "$dependency_name is not at tag $dependency_tag."
    fi
    git -C "$dependency_checkout" diff --quiet
    git -C "$dependency_checkout" diff --cached --quiet
    [ -z "$(git -C "$dependency_checkout" status --porcelain --untracked-files=all)" ] \
        || die "$dependency_name FetchContent checkout is not clean."
    test -d "$dependency_checkout/include" \
        || die "$dependency_name FetchContent checkout lacks include/."
    printf '%s %s %s\n' "$dependency_name" "$dependency_tag" "$dependency_commit" \
        >> "$FETCHCONTENT_LOCK_PART"
done
write_or_compare "$FETCHCONTENT_LOCK_PART" \
    "$MANIFEST_DIR/wsclean-fetchcontent.lock"

echo "=== 3. Download pinned casacore measures data ==="
CASACORE_OFFLINE_DIR="$OFFLINE_DIR/casacore"
mkdir -p "$CASACORE_OFFLINE_DIR"
MEASURES_ARCHIVE="$CASACORE_OFFLINE_DIR/$WSRT_MEASURES_FILENAME"
if [ ! -e "$MEASURES_ARCHIVE" ]; then
    MEASURES_PART="$MEASURES_ARCHIVE.part.$$"
    curl -fL --retry 3 --retry-delay 5 --connect-timeout 30 \
        -o "$MEASURES_PART" "$WSRT_MEASURES_URL"
    printf '%s  %s\n' "$WSRT_MEASURES_SHA256" "$MEASURES_PART" | sha256sum -c -
    MEASURES_LIST="$WORK_DIR/measures-archive-list.$$"
    tar -tzf "$MEASURES_PART" > "$MEASURES_LIST"
    grep -q '^ephemerides/' "$MEASURES_LIST"
    grep -q '^geodetic/' "$MEASURES_LIST"
    rm -f "$MEASURES_LIST"
    mv "$MEASURES_PART" "$MEASURES_ARCHIVE"
fi
printf '%s  %s\n' "$WSRT_MEASURES_SHA256" "$MEASURES_ARCHIVE" | sha256sum -c -
sha256sum "$MEASURES_ARCHIVE" > "$MANIFEST_DIR/casacore-measures.sha256"

echo "=== 4. Validate and patch the supplied DStools dev snapshot ==="
DSTOOLS_DEV="$SRC_DIR/dstools-dev"
DSTOOLS_BUILD="$SRC_DIR/dstools-build"
if [ ! -d "$DSTOOLS_DEV" ]; then
    cat >&2 <<MSG
Missing $DSTOOLS_DEV

From the local workstation, transfer the fixed dev snapshot directly to /fred:

  rsync -av \\
    --exclude='__pycache__' --exclude='*.pyc' \\
    --exclude='.DS_Store' --exclude='.venv' \\
    '$DSTOOLS_LOCAL_SNAPSHOT/' \\
    YOUR_OZSTAR_USER@ozstar.swin.edu.au:'$DSTOOLS_DEV/'

Then rerun this script.
MSG
    exit 1
fi

for source_file in README.md pyproject.toml poetry.lock; do
    case "$source_file" in
        README.md) expected_hash="$DSTOOLS_README_SHA256" ;;
        pyproject.toml) expected_hash="$DSTOOLS_PYPROJECT_SHA256" ;;
        poetry.lock) expected_hash="$DSTOOLS_POETRYLOCK_SHA256" ;;
    esac
    actual_hash="$(sha256sum "$DSTOOLS_DEV/$source_file" | cut -d' ' -f1)"
    [ "$actual_hash" = "$expected_hash" ] \
        || die "$source_file SHA-256 mismatch: got=$actual_hash expected=$expected_hash"
done

write_tree_manifest "$DSTOOLS_DEV" "$MANIFEST_DIR/dstools-upstream-source-files.sha256"
sha256sum "$MANIFEST_DIR/dstools-upstream-source-files.sha256" \
    > "$MANIFEST_DIR/dstools-upstream-source-tree.sha256"
ACTUAL_DSTOOLS_TREE_HASH="$(cut -d' ' -f1 "$MANIFEST_DIR/dstools-upstream-source-tree.sha256")"
[ "$ACTUAL_DSTOOLS_TREE_HASH" = "$DSTOOLS_SOURCE_TREE_SHA256" ] \
    || die "DStools source-tree SHA-256 mismatch: got=$ACTUAL_DSTOOLS_TREE_HASH expected=$DSTOOLS_SOURCE_TREE_SHA256"

for patch_name in "${DSTOOLS_PATCH_FILES[@]}"; do
    packaged_patch="$SCRIPT_DIR/patches/$patch_name"
    test -s "$packaged_patch" || die "Mandatory patch is missing: $packaged_patch"
    install -m 0640 "$packaged_patch" "$PATCH_DIR/$patch_name"
done
(
    cd "$PATCH_DIR"
    sha256sum "${DSTOOLS_PATCH_FILES[@]}"
) > "$MANIFEST_DIR/patches.sha256"

UPSTREAM_TREE_HASH="$(cut -d' ' -f1 "$MANIFEST_DIR/dstools-upstream-source-tree.sha256")"
PATCHSET_HASH="$(sha256sum "$MANIFEST_DIR/patches.sha256" | cut -d' ' -f1)"
DSTOOLS_BUILD_KEY="$(printf '%s\n%s\n' "$UPSTREAM_TREE_HASH" "$PATCHSET_HASH" | sha256sum | cut -d' ' -f1)"

if [ -d "$DSTOOLS_BUILD" ]; then
    test -f "$DSTOOLS_BUILD/.askap-build-key" \
        || die "$DSTOOLS_BUILD exists without a build key. Move it aside and rerun."
    if [ "$(<"$DSTOOLS_BUILD/.askap-build-key")" != "$DSTOOLS_BUILD_KEY" ]; then
        old_build_key="$(<"$DSTOOLS_BUILD/.askap-build-key")"
        old_build_backup="$WORK_DIR/dstools-build.previous-${old_build_key:0:12}.$(date +%Y%m%dT%H%M%S)"
        echo "DStools patch set changed; preserving the old derived build at $old_build_backup"
        mv "$DSTOOLS_BUILD" "$old_build_backup"
    fi
fi
if [ ! -d "$DSTOOLS_BUILD" ]; then
    DSTOOLS_BUILD_PART="$DSTOOLS_BUILD.part.$$"
    cp -a "$DSTOOLS_DEV" "$DSTOOLS_BUILD_PART"
    for patch_name in "${DSTOOLS_PATCH_FILES[@]}"; do
        patch -d "$DSTOOLS_BUILD_PART" -p1 --batch --forward --dry-run \
            < "$PATCH_DIR/$patch_name"
        patch -d "$DSTOOLS_BUILD_PART" -p1 --batch --forward \
            < "$PATCH_DIR/$patch_name"
    done
    grep -Fq '"-visibility-weighting-mode unit"' "$DSTOOLS_BUILD_PART/dstools/imaging.py"
    grep -Fq 'return len(np.unique(self.times))' "$DSTOOLS_BUILD_PART/dstools/ms.py"
    grep -Fq 'while sel.get_map()' "$DSTOOLS_BUILD_PART/dstools/logger.py"
    grep -Fq 'returncode = process.wait()' "$DSTOOLS_BUILD_PART/dstools/logger.py"
    grep -Fq 'raise subprocess.CalledProcessError' "$DSTOOLS_BUILD_PART/dstools/logger.py"
    printf '%s\n' "$DSTOOLS_BUILD_KEY" > "$DSTOOLS_BUILD_PART/.askap-build-key"
    mv "$DSTOOLS_BUILD_PART" "$DSTOOLS_BUILD"
fi

PATCHED_FILES_MANIFEST="$MANIFEST_DIR/dstools-patched-source-files.sha256"
PATCHED_FILES_NEW="$WORK_DIR/dstools-patched-source-files.sha256.new"
write_tree_manifest "$DSTOOLS_BUILD" "$PATCHED_FILES_NEW"
mv -f "$PATCHED_FILES_NEW" "$PATCHED_FILES_MANIFEST"
PATCHED_TREE_MANIFEST="$MANIFEST_DIR/dstools-patched-source-tree.sha256"
PATCHED_TREE_NEW="$WORK_DIR/dstools-patched-source-tree.sha256.new"
sha256sum "$PATCHED_FILES_MANIFEST" > "$PATCHED_TREE_NEW"
mv -f "$PATCHED_TREE_NEW" "$PATCHED_TREE_MANIFEST"

echo "=== 5. Export DStools lock and resolve controller versions ==="
POETRY_VENV="$WORK_DIR/poetry-export-venv"
if [ ! -x "$POETRY_VENV/bin/poetry" ]; then
    "$HOST_PYTHON" -m venv "$POETRY_VENV"
    "$POETRY_VENV/bin/pip" install --quiet "$POETRY_PIN" "$POETRY_EXPORT_PLUGIN_PIN"
fi

DSTOOLS_REQUIREMENTS="$SCRIPT_DIR/dstools-main-requirements.lock.txt"
DSTOOLS_REQUIREMENTS_NEW="$WORK_DIR/dstools-main-requirements.lock.txt.new"
(
    cd "$DSTOOLS_BUILD"
    "$POETRY_VENV/bin/poetry" export --only main -f requirements.txt \
        --without-hashes -o "$DSTOOLS_REQUIREMENTS_NEW"
)
grep -Eiq '^python-casacore==3\.7\.1' "$DSTOOLS_REQUIREMENTS_NEW" \
    || die "The exported DStools lock no longer selects python-casacore 3.7.1."
write_or_compare "$DSTOOLS_REQUIREMENTS_NEW" "$DSTOOLS_REQUIREMENTS"
sha256sum "$DSTOOLS_REQUIREMENTS" > "$MANIFEST_DIR/dstools-main-requirements.sha256"

test -x "$CONTROLLER_PYTHON_BIN" \
    || die "Controller Python is missing: $CONTROLLER_PYTHON_BIN"
"$CONTROLLER_PYTHON_BIN" -c 'import sys; assert sys.version_info[:2] == (3, 12), sys.version' \
    || die "The controller environment must use Python 3.12."
ASTROQUERY_VERSION="$("$CONTROLLER_PYTHON_BIN" -c 'from importlib.metadata import version; print(version("astroquery"))')"
KEYRING_VERSION="$("$CONTROLLER_PYTHON_BIN" -c 'from importlib.metadata import version; print(version("keyring"))')"
CONTROLLER_PINS="$SCRIPT_DIR/controller-direct-pins.txt"
CONTROLLER_PINS_NEW="$WORK_DIR/controller-direct-pins.txt.new"
printf 'astroquery==%s\nkeyring==%s\n' "$ASTROQUERY_VERSION" "$KEYRING_VERSION" \
    > "$CONTROLLER_PINS_NEW"
write_or_compare "$CONTROLLER_PINS_NEW" "$CONTROLLER_PINS"
sha256sum "$CONTROLLER_PINS" > "$MANIFEST_DIR/controller-direct-pins.sha256"

echo "=== 6. Resolve the complete Python dependency closure ==="
RUNTIME_REQUIREMENTS="$SCRIPT_DIR/python-runtime-requirements.lock.txt"
WHEELHOUSE="$OFFLINE_DIR/wheels"
LOGIN_BUILT_WHEELHOUSE="$OFFLINE_DIR/login-built-wheels"
LOGIN_BUILT_WHEEL_MANIFEST="$MANIFEST_DIR/login-built-wheels.sha256"
CASA_ARCHIVE="$OFFLINE_DIR/casa/casa-data.tar"
NEED_RESOLVER=0
test -s "$RUNTIME_REQUIREMENTS" || NEED_RESOLVER=1
test -s "$WHEELHOUSE/.requirements.sha256" || NEED_RESOLVER=1
test -s "$CASA_ARCHIVE" || NEED_RESOLVER=1
test -s "$MANIFEST_DIR/casa-data-versions.txt" || NEED_RESOLVER=1

RESOLVE_VENV="$WORK_DIR/python-resolve-venv"
if [ "$NEED_RESOLVER" -eq 1 ]; then
    # astroplan 0.10.1 is the only selected main dependency that PyPI publishes
    # solely as an sdist.  RM-lite 2025.5.1 has an upstream metadata typo: it
    # declares `tdqm`, although its code imports `tqdm`; the obsolete alias does
    # not support Python 3.12.  Prepare both exceptional wheels on the login node
    # and admit them only after pure-Python and provenance checks.
    REBUILD_LOGIN_WHEELS=0
    if [ -d "$LOGIN_BUILT_WHEELHOUSE" ]; then
        test -s "$LOGIN_BUILT_WHEEL_MANIFEST" \
            || die "$LOGIN_BUILT_WHEELHOUSE exists without its SHA-256 manifest. Move it aside and rerun."
        sha256sum -c "$LOGIN_BUILT_WHEEL_MANIFEST"
        test -n "$(find "$LOGIN_BUILT_WHEELHOUSE" -maxdepth 1 -type f -iname 'astroplan-*.whl' -print -quit)" \
            || REBUILD_LOGIN_WHEELS=1
        test -n "$(find "$LOGIN_BUILT_WHEELHOUSE" -maxdepth 1 -type f -iname 'rm_lite-*+askap1-*.whl' -print -quit)" \
            || REBUILD_LOGIN_WHEELS=1
    else
        REBUILD_LOGIN_WHEELS=1
    fi

    if [ "$REBUILD_LOGIN_WHEELS" -eq 1 ]; then
        ASTROPLAN_LOCK_LINES="$WORK_DIR/astroplan-lock-lines.$$"
        grep -Ei '^astroplan==[^[:space:];]+' "$DSTOOLS_REQUIREMENTS" \
            > "$ASTROPLAN_LOCK_LINES" || true
        [ "$(wc -l < "$ASTROPLAN_LOCK_LINES")" -eq 1 ] \
            || die "Expected exactly one Poetry-locked astroplan requirement."
        ASTROPLAN_SPEC="$(sed -E 's/^([^[:space:];]+).*/\1/I' "$ASTROPLAN_LOCK_LINES")"
        rm -f "$ASTROPLAN_LOCK_LINES"

        LOGIN_BUILT_WHEELHOUSE_PART="$OFFLINE_DIR/login-built-wheels.part.$$"
        SOURCE_WHEEL_VENV="$WORK_DIR/source-wheel-build-venv"
        rm -rf "$LOGIN_BUILT_WHEELHOUSE_PART" "$SOURCE_WHEEL_VENV"
        mkdir -p "$LOGIN_BUILT_WHEELHOUSE_PART"
        "$HOST_PYTHON" -m venv "$SOURCE_WHEEL_VENV"

        EXISTING_ASTROPLAN_WHEEL="$(find "$LOGIN_BUILT_WHEELHOUSE" -maxdepth 1 -type f \
            -iname 'astroplan-*.whl' -print -quit 2>/dev/null || true)"
        if [ -n "$EXISTING_ASTROPLAN_WHEEL" ]; then
            cp "$EXISTING_ASTROPLAN_WHEEL" "$LOGIN_BUILT_WHEELHOUSE_PART/"
        else
            "$SOURCE_WHEEL_VENV/bin/pip" wheel --no-deps \
                --wheel-dir "$LOGIN_BUILT_WHEELHOUSE_PART" "$ASTROPLAN_SPEC"
        fi

        mapfile -t ASTROPLAN_WHEELS < <(
            find "$LOGIN_BUILT_WHEELHOUSE_PART" -maxdepth 1 -type f \
                -iname 'astroplan-*.whl' -print
        )
        [ "${#ASTROPLAN_WHEELS[@]}" -eq 1 ] \
            || die "Expected exactly one locally built astroplan wheel."
        validate_pure_python_wheel "${ASTROPLAN_WHEELS[0]}"

        RM_LITE_LOCK_LINES="$WORK_DIR/rm-lite-lock-lines.$$"
        grep -Ei '^rm-lite==[^[:space:];]+' "$DSTOOLS_REQUIREMENTS" \
            > "$RM_LITE_LOCK_LINES" || true
        [ "$(wc -l < "$RM_LITE_LOCK_LINES")" -eq 1 ] \
            || die "Expected exactly one Poetry-locked rm-lite requirement."
        RM_LITE_SPEC="$(sed -E 's/^([^[:space:];]+).*/\1/I' "$RM_LITE_LOCK_LINES")"
        RM_LITE_VERSION="${RM_LITE_SPEC#*==}"
        rm -f "$RM_LITE_LOCK_LINES"

        RM_LITE_DOWNLOAD="$WORK_DIR/rm-lite-official.$$"
        rm -rf "$RM_LITE_DOWNLOAD"
        mkdir -p "$RM_LITE_DOWNLOAD"
        "$SOURCE_WHEEL_VENV/bin/pip" download --no-deps --only-binary=:all: \
            --dest "$RM_LITE_DOWNLOAD" "$RM_LITE_SPEC"
        mapfile -t RM_LITE_OFFICIAL_WHEELS < <(
            find "$RM_LITE_DOWNLOAD" -maxdepth 1 -type f -iname 'rm_lite-*.whl' -print
        )
        [ "${#RM_LITE_OFFICIAL_WHEELS[@]}" -eq 1 ] \
            || die "Expected exactly one official rm-lite wheel."

        RM_LITE_EXPECTED_SHA256="$("$HOST_PYTHON" - "$DSTOOLS_BUILD/poetry.lock" "$RM_LITE_VERSION" <<'PY'
import sys
import tomllib

with open(sys.argv[1], "rb") as stream:
    lock = tomllib.load(stream)
version = sys.argv[2]
matches = []
for package in lock["package"]:
    if package["name"] == "rm-lite" and package["version"] == version:
        matches.extend(
            item["hash"].removeprefix("sha256:")
            for item in package.get("files", [])
            if item["file"].endswith("-py3-none-any.whl")
        )
if len(matches) != 1:
    raise SystemExit("expected one locked rm-lite py3-none-any wheel hash")
print(matches[0])
PY
)"
        printf '%s  %s\n' "$RM_LITE_EXPECTED_SHA256" "${RM_LITE_OFFICIAL_WHEELS[0]}" \
            | sha256sum -c -

        PATCHED_RM_LITE_WHEEL="$(patch_rm_lite_wheel \
            "${RM_LITE_OFFICIAL_WHEELS[0]}" "$LOGIN_BUILT_WHEELHOUSE_PART" "$RM_LITE_VERSION")"
        validate_pure_python_wheel "$PATCHED_RM_LITE_WHEEL"
        rm -rf "$RM_LITE_DOWNLOAD"

        if [ -d "$LOGIN_BUILT_WHEELHOUSE" ]; then
            rm -rf "$LOGIN_BUILT_WHEELHOUSE"
        fi
        mv "$LOGIN_BUILT_WHEELHOUSE_PART" "$LOGIN_BUILT_WHEELHOUSE"
        find "$LOGIN_BUILT_WHEELHOUSE" -maxdepth 1 -type f -print0 \
            | LC_ALL=C sort -z | xargs -0 sha256sum \
            > "$LOGIN_BUILT_WHEEL_MANIFEST"
    fi

    RESOLVER_REQUIREMENTS="$WORK_DIR/dstools-main-requirements.resolver.txt"
    [ "$(grep -Eic '^tdqm==' "$DSTOOLS_REQUIREMENTS")" -eq 1 ] \
        || die "Expected exactly one locked tdqm alias introduced by rm-lite."
    grep -Eiv '^tdqm==' "$DSTOOLS_REQUIREMENTS" > "$RESOLVER_REQUIREMENTS"
    grep -Eiq '^tqdm==' "$RESOLVER_REQUIREMENTS" \
        || die "The corrected resolver requirements do not contain tqdm."

    rm -rf "$RESOLVE_VENV"
    "$HOST_PYTHON" -m venv "$RESOLVE_VENV"
    "$RESOLVE_VENV/bin/pip" install --only-binary=:all: \
        --find-links "$LOGIN_BUILT_WHEELHOUSE" \
        -r "$RESOLVER_REQUIREMENTS" \
        "astroquery==$ASTROQUERY_VERSION" \
        "keyring==$KEYRING_VERSION" \
        "${PYTHON_BUILD_REQUIREMENTS[@]}"
    "$RESOLVE_VENV/bin/pip" check

    RESOLVED_ALL="$WORK_DIR/python-resolved-all.txt"
    "$RESOLVE_VENV/bin/pip" freeze > "$RESOLVED_ALL"
    grep -Eiq '^python-casacore==3\.7\.1$' "$RESOLVED_ALL" \
        || die "Resolver did not select python-casacore 3.7.1."
    grep -Eiq "^numpy==${NUMPY_VERSION//./\\.}$" "$RESOLVED_ALL" \
        || die "Resolver did not select NumPy $NUMPY_VERSION."
    grep -Eiq '^rm-lite==[^[:space:]]+\+askap1$' "$RESOLVED_ALL" \
        || die "Resolver did not select the corrected +askap1 rm-lite wheel."
    if grep -Eiq '^tdqm==' "$RESOLVED_ALL"; then
        die "The incompatible tdqm alias entered the resolved environment."
    fi

    RUNTIME_REQUIREMENTS_NEW="$WORK_DIR/python-runtime-requirements.lock.txt.new"
    # python-casacore is built from source later. NumPy comes from the exact
    # conda environment so pip cannot overwrite it with a second binary build.
    awk 'BEGIN { IGNORECASE=1 } !/^python-casacore==/ && !/^numpy==/' "$RESOLVED_ALL" \
        > "$RUNTIME_REQUIREMENTS_NEW"
    if grep -Ev '^[A-Za-z0-9_.-]+==[^[:space:]]+$' "$RUNTIME_REQUIREMENTS_NEW" | grep -q .; then
        grep -Ev '^[A-Za-z0-9_.-]+==[^[:space:]]+$' "$RUNTIME_REQUIREMENTS_NEW" >&2
        die "Combined Python lock contains a non-version-pinned requirement."
    fi
    write_or_compare "$RUNTIME_REQUIREMENTS_NEW" "$RUNTIME_REQUIREMENTS"
fi
if grep -Eiq '^(python-casacore|numpy)==' "$RUNTIME_REQUIREMENTS"; then
    die "The pip runtime lock must not contain python-casacore or NumPy."
fi
sha256sum "$RUNTIME_REQUIREMENTS" > "$MANIFEST_DIR/python-runtime-requirements.sha256"

echo "=== 7. Download and verify the offline Python wheelhouse ==="
RUNTIME_LOCK_HASH="$(sha256sum "$RUNTIME_REQUIREMENTS" | cut -d' ' -f1)"
if [ -d "$WHEELHOUSE" ]; then
    test -s "$WHEELHOUSE/.requirements.sha256" \
        || die "$WHEELHOUSE exists but is incomplete. Move it aside and rerun."
    [ "$(<"$WHEELHOUSE/.requirements.sha256")" = "$RUNTIME_LOCK_HASH" ] \
        || die "$WHEELHOUSE was created from a different Python lock. Use a new BUILD_ROOT."
    sha256sum -c "$MANIFEST_DIR/wheelhouse.sha256"
else
    test -x "$RESOLVE_VENV/bin/pip" || die "Resolver venv is required to create the wheelhouse."
    WHEELHOUSE_PART="$OFFLINE_DIR/wheels.part.$$"
    mkdir -p "$WHEELHOUSE_PART"
    "$RESOLVE_VENV/bin/pip" download --no-deps --only-binary=:all: \
        --find-links "$LOGIN_BUILT_WHEELHOUSE" \
        --dest "$WHEELHOUSE_PART" -r "$RUNTIME_REQUIREMENTS"
    if find "$WHEELHOUSE_PART" -maxdepth 1 -type f -iname 'python_casacore*' | grep -q .; then
        die "A python-casacore wheel entered the wheelhouse; source build isolation has been violated."
    fi

    VERIFY_VENV="$WORK_DIR/wheelhouse-verify-venv"
    rm -rf "$VERIFY_VENV"
    "$HOST_PYTHON" -m venv "$VERIFY_VENV"
    "$VERIFY_VENV/bin/pip" install --no-index --no-deps \
        --find-links "$WHEELHOUSE_PART" -r "$RUNTIME_REQUIREMENTS"
    printf '%s\n' "$RUNTIME_LOCK_HASH" > "$WHEELHOUSE_PART/.requirements.sha256"
    mv "$WHEELHOUSE_PART" "$WHEELHOUSE"
    find "$WHEELHOUSE" -maxdepth 1 -type f -print0 \
        | LC_ALL=C sort -z | xargs -0 sha256sum \
        > "$MANIFEST_DIR/wheelhouse.sha256"
fi

echo "=== 8. Download and freeze CASA runtime data ==="
mkdir -p "$OFFLINE_DIR/casa"
if [ -e "$CASA_ARCHIVE" ] && [ ! -s "$MANIFEST_DIR/casa-data-versions.txt" ]; then
    die "$CASA_ARCHIVE exists without casa-data-versions.txt. Use a new BUILD_ROOT or move the incomplete archive aside."
fi
if [ ! -e "$CASA_ARCHIVE" ]; then
    test -x "$RESOLVE_VENV/bin/python" || die "Resolver venv is required to download CASA data."
    CASA_STAGE="$WORK_DIR/casa-data.part.$$"
    mkdir -p "$CASA_STAGE"
    "$RESOLVE_VENV/bin/python" -m casaconfig \
        --noconfig --nositeconfig --measurespath "$CASA_STAGE" --update-all
    "$RESOLVE_VENV/bin/python" -m casaconfig \
        --noconfig --nositeconfig --measurespath "$CASA_STAGE" --current-data \
        > "$MANIFEST_DIR/casa-data-versions.txt"
    test -s "$CASA_STAGE/readme.txt" || die "CASA data download did not create readme.txt."
    test -d "$CASA_STAGE/ephemerides" || die "CASA data is missing ephemerides/."
    test -d "$CASA_STAGE/geodetic" || die "CASA data is missing geodetic/."
    CASA_ARCHIVE_PART="$CASA_ARCHIVE.part.$$"
    tar -C "$CASA_STAGE" -cf "$CASA_ARCHIVE_PART" .
    tar -tf "$CASA_ARCHIVE_PART" >/dev/null
    mv "$CASA_ARCHIVE_PART" "$CASA_ARCHIVE"
fi
test -s "$MANIFEST_DIR/casa-data-versions.txt" \
    || die "CASA data version manifest is missing."
record_or_verify_sha "$CASA_ARCHIVE" "$MANIFEST_DIR/casa-data.sha256"

echo "=== 8.1. Download and freeze Astropy's observatory site registry ==="
ASTROPY_SITES="$OFFLINE_DIR/astropy/sites.json"
mkdir -p "$(dirname "$ASTROPY_SITES")"
if [ ! -e "$ASTROPY_SITES" ]; then
    ASTROPY_SITES_PART="$WORK_DIR/tmp/astropy-sites.json.part.$$"
    # OzSTAR login nodes have been verified to reach GitHub Raw quickly;
    # data.astropy.org can stall at TCP connect. Bound both attempts.
    if curl --fail --location --silent --show-error \
        --connect-timeout 5 --max-time 20 --retry 0 \
        https://raw.githubusercontent.com/astropy/astropy-data/refs/heads/gh-pages/coordinates/sites.json \
        --output "$ASTROPY_SITES_PART"; then
        ASTROPY_SITES_SOURCE=https://raw.githubusercontent.com/astropy/astropy-data/refs/heads/gh-pages/coordinates/sites.json
    elif curl --fail --location --silent --show-error \
        --connect-timeout 5 --max-time 20 --retry 0 \
        https://data.astropy.org/coordinates/sites.json \
        --output "$ASTROPY_SITES_PART"; then
        ASTROPY_SITES_SOURCE=https://data.astropy.org/coordinates/sites.json
    else
        rm -f -- "$ASTROPY_SITES_PART"
        die "Could not download Astropy's observatory site registry from either official source."
    fi
    printf '%s\n' "$ASTROPY_SITES_SOURCE" \
        > "$MANIFEST_DIR/astropy-sites-source-url.txt"
    mv "$ASTROPY_SITES_PART" "$ASTROPY_SITES"
fi
test -s "$MANIFEST_DIR/astropy-sites-source-url.txt" \
    || die "Astropy site registry source URL is missing."
record_or_verify_sha "$ASTROPY_SITES" "$MANIFEST_DIR/astropy-sites.sha256"
"$HOST_PYTHON" - "$ASTROPY_SITES" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as stream:
    registry = json.load(stream)
assert isinstance(registry, dict) and registry
names = {
    alias.casefold()
    for key, entry in registry.items()
    for alias in (key, entry["name"], *entry.get("aliases", []))
}
for site in ("GMRT", "vla", "MeerKAT", "ASKAP"):
    assert site.casefold() in names, f"Astropy site registry lacks {site}"
print("Astropy site registry contains GMRT, VLA, MeerKAT, and ASKAP")
PY

echo "=== 9. Create a relocatable Python 3.12 / Boost.Python environment ==="
PYTHON_OFFLINE_DIR="$OFFLINE_DIR/python"
PYTHON_ENV_ARCHIVE="$PYTHON_OFFLINE_DIR/askap-python.tar.gz"
CONTAINER_HOME_DIR="$BUILD_ROOT/config/container-home"
mkdir -p "$PYTHON_OFFLINE_DIR"
mkdir -p "$CONTAINER_HOME_DIR/.conda"
if [ ! -e "$PYTHON_ENV_ARCHIVE" ]; then
    # Remove only temporary archive names created by interrupted earlier runs.
    for stale_conda_archive in \
        "$PYTHON_OFFLINE_DIR"/askap-python.tar.gz.part.* \
        "$PYTHON_OFFLINE_DIR"/askap-python.part.*.tar.gz; do
        [ -e "$stale_conda_archive" ] || continue
        rm -f -- "$stale_conda_archive"
    done
    CONDA_STAGE=""
    for candidate_stage in "$WORK_DIR"/conda-askap.part.*; do
        [ -d "$candidate_stage" ] || continue
        if [ -x "$candidate_stage/bin/python" ] \
            && "$candidate_stage/bin/python" -c \
                "import sys, numpy; assert sys.version_info[:2] == (3, 12); assert numpy.__version__ == '$NUMPY_VERSION'" \
            && find "$candidate_stage/lib" -maxdepth 1 \( -type f -o -type l \) \
                -name 'libboost_python3*.so*' -print -quit | grep -q .; then
            [ -z "$CONDA_STAGE" ] \
                || die "Multiple complete conda staging environments exist under $WORK_DIR."
            CONDA_STAGE="$candidate_stage"
        fi
    done
    if [ -z "$CONDA_STAGE" ]; then
        CONDA_STAGE="$WORK_DIR/conda-askap.part.$$"
    else
        echo "Reusing complete conda staging environment: $CONDA_STAGE"
    fi
    # conda-pack selects the archive format from the final suffix, so the
    # temporary name must still end in .tar.gz.
    CONDA_ARCHIVE_PART="$PYTHON_OFFLINE_DIR/askap-python.part.$$.tar.gz"
    # micromamba 1.4.6 unconditionally registers a newly created prefix in
    # $HOME/.conda/environments.txt.  Map the container home to BUILD_ROOT so
    # that registration cannot touch the user's small real home directory.
    apptainer exec --cleanenv \
        --home "$CONTAINER_HOME_DIR:/home/askap-user" \
        --bind "$BUILD_ROOT:$BUILD_ROOT" "$BASE_SIF" \
        bash -c '
            set -Eeuo pipefail
            mamba_root=$1
            prefix=$2
            channel=$3
            explicit_file=$4
            output_archive=$5
            shift 5
            export MAMBA_ROOT_PREFIX="$mamba_root"
            if [ ! -x "$prefix/bin/python" ]; then
                micromamba create -y -p "$prefix" -c "$channel" "$@"
            fi
            micromamba env export -p "$prefix" --explicit > "$explicit_file"
            micromamba run -p "$prefix" conda-pack -p "$prefix" -o "$output_archive"
        ' _ "$CACHE_DIR/mamba" "$CONDA_STAGE" "$CONDA_CHANNEL" \
        "$MANIFEST_DIR/conda-explicit.txt" "$CONDA_ARCHIVE_PART" \
        "${CONDA_BOOTSTRAP_SPECS[@]}"
    find "$CONDA_STAGE/lib" -maxdepth 1 \( -type f -o -type l \) \
        -name 'libboost_python3*.so*' -print -quit | grep -q . \
        || die "The conda environment lacks a Python 3 compatible Boost.Python library."
    CONDA_ARCHIVE_LIST="$WORK_DIR/conda-archive-list.$$"
    tar -tzf "$CONDA_ARCHIVE_PART" > "$CONDA_ARCHIVE_LIST"
    grep -q 'bin/conda-unpack$' "$CONDA_ARCHIVE_LIST"
    rm -f "$CONDA_ARCHIVE_LIST"
    mv "$CONDA_ARCHIVE_PART" "$PYTHON_ENV_ARCHIVE"
    rm -rf "$CONDA_STAGE"
fi
test -s "$MANIFEST_DIR/conda-explicit.txt" \
    || die "$PYTHON_ENV_ARCHIVE exists without its exact conda package manifest. Use a new BUILD_ROOT."
record_or_verify_sha "$PYTHON_ENV_ARCHIVE" "$MANIFEST_DIR/conda-environment.sha256"

echo "=== 10. Build a local Jammy APT repository ==="
APT_REPO="$OFFLINE_DIR/apt"
printf '%s\n' "${APT_BUILD_PACKAGES[@]}" > "$SCRIPT_DIR/apt-build-packages.txt"
if [ -d "$APT_REPO" ]; then
    test -s "$APT_REPO/Packages" || die "$APT_REPO exists but has no Packages index."
    test -s "$MANIFEST_DIR/apt-repository.sha256" \
        || die "$APT_REPO exists without its SHA-256 manifest. Use a new BUILD_ROOT."
    sha256sum -c "$MANIFEST_DIR/apt-repository.sha256"
else
    # A failed preparation may leave only script-owned temporary repositories.
    # They are not trusted as complete dependency closures and are rebuilt.
    for stale_apt_repo in "$OFFLINE_DIR"/apt.part.*; do
        [ -d "$stale_apt_repo" ] || continue
        rm -rf -- "$stale_apt_repo"
    done
    APT_REPO_PART="$OFFLINE_DIR/apt.part.$$"
    mkdir -p "$APT_REPO_PART"
    apptainer exec --fakeroot --writable-tmpfs --cleanenv --no-home \
        --bind "$BUILD_ROOT:$BUILD_ROOT" "$BASE_SIF" \
        bash -c '
            set -Eeuo pipefail
            build_root=$1
            output_dir=$2
            mapfile -t packages < "$build_root/recipe/apt-build-packages.txt"
            apt-get update
            apt-get -y --download-only --no-install-recommends install "${packages[@]}"
            cp /var/cache/apt/archives/*.deb "$output_dir/"

            # Build a standard flat-repository Packages index directly from
            # the downloaded .deb control records.  Installing dpkg-dev only
            # for dpkg-scanpackages is unsafe in an Apptainer overlay because
            # package upgrades can require cross-filesystem renames.
            cd "$output_dir"
            : > Packages
            : > package-inventory.txt
            for package_file in ./*.deb; do
                dpkg-deb -f "$package_file" >> Packages
                file_name=${package_file#./}
                file_size=$(stat -c %s "$package_file")
                file_md5=$(md5sum "$package_file" | cut -d" " -f1)
                file_sha1=$(sha1sum "$package_file" | cut -d" " -f1)
                file_sha256=$(sha256sum "$package_file" | cut -d" " -f1)
                printf "Filename: ./%s\nSize: %s\nMD5sum: %s\nSHA1: %s\nSHA256: %s\n\n" \
                    "$file_name" "$file_size" "$file_md5" "$file_sha1" "$file_sha256" \
                    >> Packages
                dpkg-deb -f "$package_file" Package Version Architecture \
                    >> package-inventory.txt
                printf "\n" >> package-inventory.txt
            done
            grep -q "^Package: " Packages
            gzip -9c Packages > Packages.gz

            # Prove that the local repository alone resolves the requested
            # package set against the unchanged bootstrap image.
            find /etc/apt/sources.list.d -type f -delete
            printf "%s\n" "deb [trusted=yes] file:$output_dir ./" > /etc/apt/sources.list
            apt-get -o Acquire::Languages=none update
            apt-get --simulate --no-download --no-install-recommends \
                install "${packages[@]}"
        ' _ "$BUILD_ROOT" "$APT_REPO_PART"
    test -n "$(find "$APT_REPO_PART" -maxdepth 1 -name '*.deb' -print -quit)" \
        || die "Offline APT preparation produced no .deb files."
    mv "$APT_REPO_PART" "$APT_REPO"
    find "$APT_REPO" -maxdepth 1 -type f -print0 \
        | LC_ALL=C sort -z | xargs -0 sha256sum \
        > "$MANIFEST_DIR/apt-repository.sha256"
fi

echo "=== 11. Archive source trees without following symlinks ==="
# Apptainer %files dereferences links while copying directories.  The
# casacore checkout intentionally tracks casacore -> ., which makes that
# operation fail with a cyclic-link error.  Tar preserves the link itself.
SOURCE_ARCHIVE_DIR="$OFFLINE_DIR/sources"
mkdir -p "$SOURCE_ARCHIVE_DIR"
for source_name in casacore wsclean python-casacore dstools-build; do
    source_archive="$SOURCE_ARCHIVE_DIR/${source_name}.tar"
    if [ "$source_name" = dstools-build ]; then
        source_archive_key="$MANIFEST_DIR/dstools-build-source-archive.key"
        if [ -e "$source_archive" ] && \
           { [ ! -e "$source_archive_key" ] || [ "$(<"$source_archive_key")" != "$DSTOOLS_BUILD_KEY" ]; }; then
            archive_backup="$WORK_DIR/dstools-build.previous.$(date +%Y%m%dT%H%M%S).tar"
            echo "Preserving the previous DStools source archive at $archive_backup"
            mv "$source_archive" "$archive_backup"
        fi
    fi
    if [ ! -e "$source_archive" ]; then
        source_archive_part="$WORK_DIR/tmp/${source_name}.tar.part.$$"
        tar -C "$SRC_DIR" -cf "$source_archive_part" "$source_name"
        mv "$source_archive_part" "$source_archive"
    fi
    tar -tf "$source_archive" >/dev/null
    if [ "$source_name" = dstools-build ]; then
        sha256sum "$source_archive" \
            > "$MANIFEST_DIR/${source_name}-source-archive.sha256"
        printf '%s\n' "$DSTOOLS_BUILD_KEY" > "$source_archive_key"
    else
        record_or_verify_sha "$source_archive" \
            "$MANIFEST_DIR/${source_name}-source-archive.sha256"
    fi
done
for dependency_name in xtl xsimd xtensor xtensor-blas xtensor-fftw; do
    source_archive="$SOURCE_ARCHIVE_DIR/fetchcontent-${dependency_name}.tar"
    if [ ! -e "$source_archive" ]; then
        source_archive_part="$WORK_DIR/tmp/fetchcontent-${dependency_name}.tar.part.$$"
        tar -C "$FETCHCONTENT_SRC_DIR" -cf "$source_archive_part" "$dependency_name"
        mv "$source_archive_part" "$source_archive"
    fi
    tar -tf "$source_archive" >/dev/null
    record_or_verify_sha "$source_archive" \
        "$MANIFEST_DIR/fetchcontent-${dependency_name}-source-archive.sha256"
done

echo "=== 12. Freeze every compute-node build input ==="
BUILD_INPUT_MANIFEST="$MANIFEST_DIR/build-input-files.sha256"
find \
    "$BASE_DIR" \
    "$OFFLINE_DIR" \
    "$SRC_DIR" \
    "$SCRIPT_DIR" \
    "$PATCH_DIR" \
    "$MANIFEST_DIR" \
    -type f \
    ! -name '.DS_Store' \
    ! -name 'build-input-files.sha256' \
    ! -name 'candidate-sif.sha256' \
    ! -name 'candidate-inspect.json' \
    ! -name 'candidate-definition.txt' \
    -print0 \
    | LC_ALL=C sort -z \
    | xargs -0 sha256sum \
    > "$BUILD_INPUT_MANIFEST"

sha256sum -c "$BUILD_INPUT_MANIFEST" >/dev/null

echo
echo "=== Login-node preparation is complete ==="
echo "All persistent inputs are under: $BUILD_ROOT"
echo "Combined Python lock:          $RUNTIME_REQUIREMENTS"
echo "Offline wheelhouse:            $WHEELHOUSE"
echo "Offline APT repository:        $APT_REPO"
echo "CASA runtime data archive:     $CASA_ARCHIVE"
echo "Astropy site registry:         $ASTROPY_SITES"
echo "Next command: cd $SCRIPT_DIR && sbatch build_sif.sbatch"
