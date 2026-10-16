# 1. Installation

[Index](../../README.md) · [中文](../zh-CN/INSTALLATION.md) · [Usage](USAGE.md)

This guide is done once. It puts the code on OzSTAR, creates the Python
environment, stores your CASDA password and installs the retrieval script on
your destination machine. Afterwards, continue with [Usage](USAGE.md).

Values such as `YOUR_OZSTAR_USER` and paths beginning with `/PATH/TO/` are
placeholders: replace them with your own (see the table in the
[README](../../README.md)). The module names `gcc/13.3.0`, `python/3.12.3` and
`apptainer` are OzSTAR's; on another site, use the equivalent modules.

## 1. Requirements

| Where | What runs there | What it needs |
| --- | --- | --- |
| OzSTAR login node | Catalogue queries, CASDA downloads, submitting and following Slurm jobs | Python ≥3.10 with the packages in `requirements.txt`; network access to CASDA |
| OzSTAR compute nodes | DStools processing inside Apptainer | A DStools image (`.sif`); no internet access needed |
| Destination machine (Ada in our setup) | Copying finished batch results with `pull_products.py` | Python ≥3.7, `ssh`, `rsync`; `sshpass` only if you log in with a password file |

Accounts you need:

- an **OzSTAR account** in a project with Slurm time and enough `/fred` storage;
- a **CASDA account** (your OPAL login), used only for downloads;
- **ssh access** from the destination machine to OzSTAR.

Compute nodes are treated as offline. Everything that needs the network
(installing packages, searching CASDA and downloading data) happens on the
login node. The retrieval script uses only the Python standard library.

## 2. Obtain the code

Clone the repository directly on OzSTAR, into a directory that compute nodes
can read (for example under `/fred`), because Slurm jobs run the code from
there. Your settings live in `config.py` and `dstools-config.py` inside this
directory (see [Usage §2](USAGE.md#2-batch-catalogue-processing)).

On a login node (replace the placeholders):

~~~bash
mkdir -p /fred/YOUR_PROJECT/YOUR_OZSTAR_USER
cd /fred/YOUR_PROJECT/YOUR_OZSTAR_USER
git clone https://github.com/GungYUN/DStools-Auto-Processing.git
cd DStools-Auto-Processing
~~~

To update later, run `git pull` in this directory. Because you edit `config.py`
and `dstools-config.py` in place, Git may refuse to update while they have
local changes. Keep your edits aside during the update and then check both
files for conflicts:

~~~bash
git stash
git pull
git stash pop
~~~

An update can add new settings: `git pull` brings them into both files and
`git stash pop` puts your own values back. If you resolve a conflict in a
settings file, keep the new lines: a missing setting stops every command with
"Settings missing …" and its name, and `git show HEAD:config.py` (or
`HEAD:dstools-config.py`) shows the repository version to copy it from. Do not update the code while processing started from
this directory is still running: its Slurm jobs use the code directory.

## 3. Host Python and SIF

### Python environment

The *host Python* is the Python on OzSTAR that runs the commands of this
project and the worker inside each Slurm job. DStools itself runs inside the
SIF with its own Python, so the two are independent.

Create the environment once, on a login node, in a location compute nodes can
read:

~~~bash
module load gcc/13.3.0 python/3.12.3 apptainer
cd /fred/YOUR_PROJECT/YOUR_OZSTAR_USER/DStools-Auto-Processing
python -m venv /fred/YOUR_PROJECT/YOUR_OZSTAR_USER/askap-python
/fred/YOUR_PROJECT/YOUR_OZSTAR_USER/askap-python/bin/python -m pip install -r requirements.txt
~~~

The guides refer to this interpreter through a shell variable,
`ASKAP_HOST_PYTHON`, set at the start of each session ([Usage §1](USAGE.md#1-session-environment)).
Check the installation:

~~~bash
ASKAP_HOST_PYTHON=/fred/YOUR_PROJECT/YOUR_OZSTAR_USER/askap-python/bin/python
"$ASKAP_HOST_PYTHON" -m pip check
"$ASKAP_HOST_PYTHON" askap-batch --help
"$ASKAP_HOST_PYTHON" askap-source --help
~~~

Slurm jobs use the same interpreter that started the command. To use a
different one, set `PYTHON` in `config.py`; it applies to both commands.
If you already have a Python ≥3.10 environment with these packages, you can
use it instead.

### DStools image (SIF)

Processing needs an Apptainer image containing DStools and WSClean. Use an
image provided by your group or the maintainer, or build one with
[SIF build](SIF_BUILD.md). You will enter its path as `CONTAINER` in
`config.py`. Updating this project's code does not require a new image as long
as the DStools commands and options it uses stay compatible.

The image must provide:

- the commands `dstools-askap-preprocess`, `dstools-create-model`,
  `dstools-insert-model`, `dstools-subtract-model` and `dstools-extract-ds`;
- WSClean at `/opt/wsclean/bin/wsclean`;
- a Python with casacore and Astropy at `/opt/askap/bin/python` (the
  `CONTAINER_PYTHON` setting).

The first job of each plan checks that the image accepts every command-line
option the plan uses. It does not check the image's scientific behaviour: our
results were produced with an image built from the recipe in
`New_dstools_build/`, which applies DStools patches that change the processing
(listed in [SIF build](SIF_BUILD.md#1-recipe)). An image built differently may
give different results.

## 4. Directories and input

Keep the code, the processing data and your input tables in separate places,
for example:

| Content | Example |
| --- | --- |
| Code (including your `config.py` and `dstools-config.py`) | `/fred/YOUR_PROJECT/YOUR_OZSTAR_USER/DStools-Auto-Processing` |
| Batch work directory | `/fred/YOUR_PROJECT/YOUR_OZSTAR_USER/DStools_work/catalogue` |
| Single-source work directory | `/fred/YOUR_PROJECT/YOUR_OZSTAR_USER/DStools_work/MySource` |
| Input catalogue | `/fred/YOUR_PROJECT/YOUR_OZSTAR_USER/catalogues/targets.csv` |

A *work directory* holds everything one catalogue or one target produces:
downloaded archives, measurement sets, models, state, logs and results. Rules:

- Use one work directory per catalogue (batch) or per target (single source),
  and never mix batch and single-source work in one directory.
- Corrections can be made in the same work directory: edit the catalogue or
  a settings file and run the command again; if an unfinished plan differs, the
  command asks before replacing it. Earlier catalogue versions are kept in
  `logs/catalogues/` and every command in `logs/commands/`.
- If you used the earlier pipeline for ultracool dwarfs (UCDs), do not reuse
  its work directories.
- Use absolute paths without spaces, made of letters, digits, `_`, `.`, `+`
  and `-` only: DStools runs some internal shell commands that cannot handle
  other characters.
- Each downloaded archive can be tens of gigabytes (the default size cap is
  50 GiB), and the default staging budget is 2 TiB. Check your `/fred` quota
  before processing many observations.

The catalogue needs only a name, right ascension and declination per source.
Optional proper-motion and epoch columns are described in
[Usage §2](USAGE.md#prepare-the-source-table).

## 5. CASDA credentials

Searching CASDA needs no account, but downloading does. Use your CASDA (OPAL)
login, which is unrelated to your OzSTAR password. Put the user name in
`config.py` as `CASDA_USERNAME`; both commands use it.

The password is not written in `config.py`, and the program never asks for it
while processing, because downloads run unattended. Store it once with the
command below, explained in [Usage §1](USAGE.md#store-your-casda-password-once).
It asks for the password without showing it, saves it in a file readable only
by you, and tests the login:

~~~bash
"$ASKAP_HOST_PYTHON" askap-batch set-casda-password
~~~

The password is only read on the login node and is never written to settings
records, command logs or Slurm scripts.

## 6. Place the retrieval script on Ada

Batch results are copied to your destination machine (Ada in our setup) with
`pull_products.py`. Any Linux or macOS machine that can `ssh` to OzSTAR works.
On that machine:

~~~bash
ADA_PROJECT=/PATH/TO/ada/DStools-Auto-Processing
mkdir -p "$ADA_PROJECT/bin"
scp -4 YOUR_OZSTAR_USER@ozstar.swin.edu.au:/fred/YOUR_PROJECT/YOUR_OZSTAR_USER/DStools-Auto-Processing/pull_products.py \
  "$ADA_PROJECT/bin/"
python3 "$ADA_PROJECT/bin/pull_products.py" --help
~~~

(`-4` forces IPv4; drop it if your network does not need it.)

The script logs in to OzSTAR with `ssh` and `rsync`. SSH keys or an agent are
recommended. If you must use your OzSTAR password, store it in a separate file
with the hidden-input method of §5 (for example
`~/.config/askap/ozstar-transfer-password`, mode 600; this is the OzSTAR
password, not the CASDA one) and pass it with `--password-file`. This also
requires `sshpass`.

Next, read [Usage](USAGE.md) and choose the batch or single-source workflow.
