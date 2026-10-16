# 4. SIF build (optional)

[Index](../../README.md) · [中文](../zh-CN/SIF_BUILD.md) · [Installation](INSTALLATION.md)

You only need this page if you have no suitable DStools image. If your group
or the maintainer gives you one, use it and skip this page. Updating this
project's code does not require a new image as long as the DStools commands and
options it uses stay compatible.

The image must provide the DStools commands, WSClean at
`/opt/wsclean/bin/wsclean` and a Python with casacore and Astropy at
`/opt/askap/bin/python` ([Installation §3](INSTALLATION.md#dstools-image-sif)).
The recipe below builds exactly such an image, fully offline on a compute node,
from pinned and checksummed inputs. Our results were produced with an image
built this way.

## 1. Recipe

The recipe is in [New_dstools_build](../../New_dstools_build/):

| File | Purpose |
| --- | --- |
| `versions.env` | Pinned versions of every source and dependency, host modules and the build directory |
| `prep_sources.sh` | Runs on a login node: downloads and freezes every build input |
| `askap-dstools.def` | Offline Apptainer definition |
| `build_sif.sbatch` | Runs on a compute node: compiles everything and assembles the image |
| `patches/` | Source patches applied during the build; upload the whole folder |

The patches matter for the results, so know what they do:

| Patch | Effect |
| --- | --- |
| `0001-visibility-weighting-unit` | DStools imaging calls WSClean with `-visibility-weighting-mode unit` |
| `0002-unique-integrations` | DStools counts integrations as the number of distinct time stamps |
| `0003`, `0004`, `0005` | WSClean build fixes (Boost, offline dependencies, consistent OpenSSL libraries); no effect on results |
| `0006-dstools-subprocess-drain-and-check` | DStools waits for each external command to finish and stops with an error if it fails |

An image built from other sources without equivalent changes may give
different results, or may report success when an internal step failed.

Before using the recipe, edit these placeholders on your own computer:

| Where | What to set |
| --- | --- |
| `versions.env`: `BUILD_ROOT` | A new, empty build directory, e.g. `/fred/YOUR_PROJECT/YOUR_OZSTAR_USER/dstools/sif-build-new` |
| `build_sif.sbatch`: `BUILD_ROOT=` and the `#SBATCH --output` / `--error` paths | The same directory, so that the two files agree |
| `versions.env`: `CONTROLLER_PYTHON_BIN` | Your host Python (Python 3.12 with this project's requirements); the versions of its `astroquery` and `keyring` are copied into the image |
| `versions.env`: `HOST_*_MODULE` | Module names, if your site differs from OzSTAR |

These settings apply only to the new build; they do not change any image
already in use.

DStools itself is not downloaded by the recipe. Obtain the exact DStools source
snapshot from the maintainer: the recipe checks it against the file hashes in
`versions.env` and refuses any other version. Do not put a newer DStools source
into the pinned recipe. Whenever the source, patches or dependencies change,
use a new build directory and keep earlier images and their manifests.

## 2. Upload recipe and source

From your own computer, after editing the placeholders. Replace the example
directory with your own new `BUILD_ROOT`:

~~~bash
BUILD_ROOT=/fred/YOUR_PROJECT/YOUR_OZSTAR_USER/dstools/sif-build-new
ssh YOUR_OZSTAR_USER@ozstar.swin.edu.au \
  "mkdir -p '$BUILD_ROOT/recipe' '$BUILD_ROOT/src/dstools-dev' '$BUILD_ROOT/logs'"
rsync -av New_dstools_build/ "YOUR_OZSTAR_USER@ozstar.swin.edu.au:$BUILD_ROOT/recipe/"
rsync -av /path/to/exact/dstools-dev/ \
  "YOUR_OZSTAR_USER@ozstar.swin.edu.au:$BUILD_ROOT/src/dstools-dev/"
~~~

`/path/to/exact/dstools-dev/` is your local copy of the DStools snapshot. The
recipe is the `New_dstools_build/` folder of this repository; with the clone on
OzSTAR you can copy it there instead of using the first `rsync`:
`cp -r New_dstools_build/. "$BUILD_ROOT/recipe/"`.

## 3. Prepare on login, build on compute

On an OzSTAR login node, run these commands one at a time, and continue only if
the previous one succeeded:

~~~bash
BUILD_ROOT=/fred/YOUR_PROJECT/YOUR_OZSTAR_USER/dstools/sif-build-new
cd "$BUILD_ROOT/recipe"
bash prep_sources.sh
sha256sum -c --quiet "$BUILD_ROOT/manifests/build-input-files.sha256"
sbatch build_sif.sbatch
~~~

`prep_sources.sh` needs network access: it downloads every input, applies and
checks the patches, and writes the input manifest. The Slurm job then builds
the image without any network access, using `apptainer build --fakeroot` (no
root account needed, but your site must allow fakeroot builds). The job
requests 8 CPUs, 32 GB of memory, 32 GB of local disk and 12 hours.

Inputs, caches, logs, manifests and images all stay under `BUILD_ROOT`. If you
change the recipe after preparing, run the preparation again; never edit the
recorded hashes to get past a mismatch. If the build fails, read
`BUILD_ROOT/logs/build-<jobid>.err`.

## 4. Keep and adopt a replacement

A successful build leaves a candidate image in `BUILD_ROOT/artifacts/`; its path
and SHA256 are in `manifests/candidate-sif.sha256`, and the build log in
`logs/`. Before the job finishes, it runs short tests as an ordinary user
(casacore, Astropy and CASA data are readable, and failing sub-commands are
reported). These do not prove scientific correctness.

After confirming that the image works with the required commands and with real
data, keep it under a distinct file name, set its path as `CONTAINER` in
`config.py` and run your command again; unfinished work then uses the new
image. Do not overwrite an image file while jobs that use it are queued or
running: each job checks the image's checksum before it starts and stops with
"SIF changed after this run was prepared".
