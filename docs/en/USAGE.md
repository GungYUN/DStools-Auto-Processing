# 2. Usage and parameters

[Index](../../README.md) · [中文](../zh-CN/USAGE.md) · [Installation](INSTALLATION.md) · [How it works](REFERENCE.md)

This guide walks through a batch run (§2) and a single-source run (§3), then
covers monitoring, changing settings and retries (§4), settings in general (§5)
and retrieving results (§6). Complete the [Installation](INSTALLATION.md) first.

Both workflows use the same three steps:

| Step | Command | What it does |
| --- | --- | --- |
| 1. Find observations | `query` | Searches CASDA and records the candidate observations. Downloads nothing, submits nothing. |
| 2. Plan | `run` | Shows which targets and observations would be processed and the download size, and records this plan. Downloads nothing. |
| 3. Execute | `run --submit` | Downloads the data, submits Slurm jobs and follows them to the end. |

`run --prepare-download` is an intermediate option: it downloads the data for
the next job and writes its Slurm script without submitting it.

> **Settings are read again by every command.** Both commands read `config.py`
> and `dstools-config.py` each time they start. To continue interrupted work,
> run the **same command with the same options** again, or `run --resume`,
> which repeats the options the unfinished plan was started with. If neither
> the settings nor the options have changed, the command continues the plan and
> takes over Slurm jobs that are already submitted. If anything differs (an
> edited settings file, or different options such as `--begin`/`--end`,
> `--source`, `--all-beams-for-sb` or `--retry-failed`), the command lists the
> differences and asks `Replace the unfinished plan? [y/N]`. Answer `y` to plan
> again with the current values; finished results are kept. Any other answer
> keeps the unfinished plan unchanged. While a Slurm job of the old plan is
> still queued or running, the command refuses and asks you to wait or to
> cancel the job first. When the previous plan has finished, there is nothing
> to replace and the command runs without asking. Every command, with its
> effective settings and their origins, is recorded in
> `WORK_ROOT/logs/commands/`.

The process started by `run --submit` (the *controller*) keeps running on the
login node until all jobs have finished, which can take days. Start it inside
`tmux` so that it survives when you disconnect (§1).

## 1. Session environment

At the start of every login session, on an OzSTAR login node (replace the
placeholders):

~~~bash
module load gcc/13.3.0 python/3.12.3 apptainer
cd /fred/YOUR_PROJECT/YOUR_OZSTAR_USER/DStools-Auto-Processing
ASKAP_HOST_PYTHON=/fred/YOUR_PROJECT/YOUR_OZSTAR_USER/askap-python/bin/python
~~~

All commands below are run from this code directory and start with
`"$ASKAP_HOST_PYTHON"`, the Python environment created during
[installation](INSTALLATION.md#3-host-python-and-sif).

### Store your CASDA password (once)

Downloading needs your CASDA (OPAL) password. It is not written in `config.py`,
and the program never asks for it while processing, so store it once before
your first `run --submit`:

~~~bash
"$ASKAP_HOST_PYTHON" askap-batch set-casda-password
~~~

The command asks for the password twice without showing it and saves it in the
file named by `CASDA_PASSWORD_FILE` in `config.py` (default
`~/.config/askap/casda-password`), readable only by you. If `CASDA_USERNAME` is
already set in `config.py`, it then tests the login; `CASDA authentication
failed` can mean a wrong user name or password, or a network problem. Run it
again whenever your password changes; `askap-source set-casda-password` does the
same, and `--no-check` skips the test. Instead of the file, you can export
`CASDA_PASSWORD` in the shell that runs the controller; it takes precedence over
the file. Without either, downloads stop with "CASDA credentials are missing".
Searching (`query`) needs no password.

### Long runs and Slurm

For long runs, open `tmux` first and run the three lines above inside it:

~~~bash
tmux new -s askap        # start a session called "askap"
# ...work...; detach with Ctrl-b then d
tmux attach -t askap     # reattach later
~~~

A `tmux` session lives on one login node. If there are several login nodes,
note the node name (`hostname`) and log in to that node to reattach.

Optionally, before your first run, check that Slurm accepts the default job
size (32 CPUs, 160 GiB memory) with your account; nothing is submitted:

~~~bash
sbatch --test-only --time=01:00:00 --cpus-per-task=32 --mem=160G --wrap=hostname
# add --partition=... and/or --account=... if your allocation needs them
~~~

## 2. Batch catalogue processing

### Configure the catalogue

Both commands read their settings from two files in the code directory:
`config.py` (data, query, Slurm and account settings) and `dstools-config.py`
(the DStools options and cutout size, [§5](#dstools-options-dstools-configpy)).
Each file lists its settings with a short comment. The program has no built-in
defaults, so keep every setting in these files (`None` where a value is
optional). Open `config.py` in an editor (for example `nano config.py`) and
set at least these four values:

| Setting | Meaning | Example |
| --- | --- | --- |
| `WORK_ROOT` | Batch work directory (created if missing) | `"/fred/YOUR_PROJECT/YOUR_OZSTAR_USER/DStools_work/catalogue"` |
| `CATALOGUE_PATH` | Your source table, CSV or FITS | `"/fred/YOUR_PROJECT/YOUR_OZSTAR_USER/catalogues/targets.csv"` |
| `CONTAINER` | DStools image | `"/PATH/TO/dstools.sif"` |
| `CASDA_USERNAME` | Your CASDA (OPAL) login | `"YOUR_CASDA_USERNAME"` |

The CASDA password does not go in `config.py`; store it once with
`askap-batch set-casda-password` ([§1](#store-your-casda-password-once)).

These are often needed as well:

| Setting | Meaning | Value in the file |
| --- | --- | --- |
| `PYTHON` | Interpreter used inside Slurm jobs; must be readable from compute nodes | the interpreter that runs the command |
| `PARTITION`, `ACCOUNT` | Slurm partition and account, if your allocation requires them | not set |
| `MODULES` | Modules loaded at the start of each job | `["gcc/13.3.0", "python/3.12.3", "apptainer"]` |
| `PRODUCT_ROOT` | Where finished results are published | `WORK_ROOT/products` |
| `BEGIN`, `END` | Rows of the catalogue to process (1-based, both included) | the whole table |

For example, the edited lines might be:

~~~python
WORK_ROOT = "/fred/YOUR_PROJECT/YOUR_OZSTAR_USER/DStools_work/catalogue"
CATALOGUE_PATH = "/fred/YOUR_PROJECT/YOUR_OZSTAR_USER/catalogues/targets.csv"
CONTAINER = "/PATH/TO/dstools.sif"
CASDA_USERNAME = "YOUR_CASDA_USERNAME"
~~~

Leave the other values as they are unless you have a reason to change them;
§5 explains the common settings and the comments in both files describe all of
them. If you find a mistake later, correct the file and run the command again
(§4). Because both files are part of the code directory, see
[Installation §2](INSTALLATION.md#2-obtain-the-code) for updating the
code without losing your edits, and do not commit or push them with your own
paths. To keep settings for several catalogues, you can store further settings
files outside the repository and select one with
`--config /PATH/TO/my_catalogue.py`; the DStools options are then read from
`dstools-config.py` in the same directory, or from the file given with
`--dstools-config`.

### Prepare the source table

The table needs one row per source with a name, right ascension and
declination (ICRS). With the default settings the columns are called `Name`,
`RA` and `DEC`, both coordinates in decimal degrees, as in
[targets.csv](../../examples/targets.csv):

~~~csv
Name,RA,DEC
Example A,180.1494,-28.6159
Example_B,180.25,-28.65
~~~

**Proper motion (optional).** You can add proper motion and the epoch of the
catalogue position for each source. The pipeline then moves every position to
the time of each observation before extracting the dynamic spectrum. Add the
columns to the table and tell the pipeline their names in `config.py`:

~~~csv
Name,RA,DEC,PMRA,PMDEC,EPOCH
Example A,180.1494,-28.6159,-120.5,35.2,J2016.0
Example_B,180.25,-28.65,0,0,J2016.0
~~~

~~~python
CATALOGUE_COLUMNS = {"source_name": "Name", "ra": "RA", "dec": "DEC",
                     "pmra": "PMRA", "pmdec": "PMDEC", "epoch": "EPOCH"}
~~~

By default `PMRA` means μα cos δ and both motions are in mas/yr. The epoch is a
time string such as `J2016.0`; a plain number such as `2016.0` is rejected.
Once a motion column is mapped, every row needs a number, so use `0` for
sources without motion. Without motion columns, positions are used as given;
without an epoch column, all rows use `COORDINATE_EPOCH` (default `J2000`).
Other column names, sexagesimal coordinates, other units and FITS tables are
covered in [§5](#columns-units-and-proper-motion).

If you correct the table later, the next command reads the new version, keeps
the previous one in `WORK_ROOT/logs/catalogues/`, and queries every source
again.

### Query and plan

Find observations for every source in the table:

~~~bash
"$ASKAP_HOST_PYTHON" askap-batch query
~~~

This searches CASDA (no login needed) and prints a summary including the
number of sources and their query status. Large catalogues take a while; if
the command is interrupted, run it again and it continues where it stopped.
Useful results in `WORK_ROOT/state/`:

| File | Content |
| --- | --- |
| `input_catalogue.csv` | Your table as read: normalised names and parsed coordinates. Check this first. |
| `crosscatalogue_sources.csv` | Query status of every source (`available`, `no_observations`, `query_failed`) |
| `observation_progress.csv` | One row per selected source–observation pair |
| `queries/<Source_ID>/observations.csv` | All candidate observations of one source, with the reason for every rejection |

The query always covers the whole table; you choose which rows to process when
you plan. With `BEGIN`/`END` unset, this plans the whole table:

~~~bash
"$ASKAP_HOST_PYTHON" askap-batch run
~~~

To process only some rows, set `BEGIN` and `END` in `config.py` (rows are
counted from 1 in the original table, both ends included), or give them on the
command line, e.g. `run --begin 1 --end 20`. Options given on the command line
apply only to that command, so repeat them on every later command for the same
work, or continue it with `run --resume`. You can also select sources by name with
`--source NAME` (repeatable; see the `Source_Name` column of
`input_catalogue.csv`). This command downloads nothing. It prints:

- `selected_datasets` and `datasets_needing_work`: observations in scope, and
  those still to be processed;
- `target_attempts`: source–observation pairs that will be processed;
- `selected_archive_bytes_for_work`: the approximate download size in bytes;
- the paths of `execution-plan.csv` (one row per planned target) and
  `effective-settings.json` (every setting and where it came from), both in
  `WORK_ROOT/state/runs/<run-id>/`.

Check the download size against your storage quota before executing. If the
plan is not what you want, change the settings or the options and run again.
The preview is an unfinished plan, so the command lists what differs and asks
before replacing it; answer `y`.

**Shared observations.** If another source in the table lies in the same
observation (the same SB and beam) as a planned source, it is processed
together with it, even when it is outside your row range, because the
expensive steps are done once for both. Its other observations are not added.

### Execute or resume

Inside `tmux`:

~~~bash
"$ASKAP_HOST_PYTHON" askap-batch run --submit
~~~

The controller repeats these steps: download the data for the next job on the
login node, submit one Slurm job, check it every 60 seconds (`POLL_SECONDS`),
and prepare the next job when it ends. It prints a line
`Job <id>: N datasets, S slots, H h, C CPUs` at each check, and a final summary
when everything has finished.

To stop the controller, press Ctrl-C. Jobs already submitted keep running. To
continue, run exactly the same command again (with the same options), or

~~~bash
"$ASKAP_HOST_PYTHON" askap-batch run --resume --submit
~~~

which repeats the options recorded with the unfinished plan (it prints them).
`--resume` accepts only `--submit`, `--prepare-download`, `--work-dir`,
`--config` and `--dstools-config`; without `--submit` or `--prepare-download`
it only shows the plan.
Either way the command prints `Continuing run …` and re-attaches to the jobs.
If a settings file was edited in the meantime, it lists the differences and
asks first. Every command you ran is in the `command` field of the files in
`WORK_ROOT/logs/commands/`.

### Refresh the observation index

When the query settings and the catalogue are unchanged, every command reuses
the saved observation index without contacting CASDA. When either has changed,
the next `query` or `run` notices it and queries **every** source again with the
current values, which can take a while for a large catalogue. To find
observations released since the last query without changing anything, or to
repeat only failed queries, run one of:

~~~bash
# Query every source again: finds newly released observations
"$ASKAP_HOST_PYTHON" askap-batch query --update-query
# Repeat only the sources whose query failed
"$ASKAP_HOST_PYTHON" askap-batch query --retry-query-failures
~~~

Use one or the other, not both together. A *failed* query (for example, a
network error) is different from a successful query that found no eligible
observation; `crosscatalogue_sources.csv` shows which is which. New query
results invalidate an unfinished plan, so if one exists, `query` asks before
replacing it; use `progress` (§4) if you only want to look at the state.

> **Put query filters in `config.py`.** Filters given only as options of
> `query` (for example `--exposure-min 300`) apply to that one command. The next
> `run` compares the saved query with `config.py`, finds the difference and
> queries every source again with the values in `config.py`. Set
> `EXPOSURE_MIN`, `QUALITY_LEVELS`, `MINIMUM_SB`, `RA_UNIT` and similar settings
> in `config.py`, or repeat the same `--set` options on every command.

## 3. Single-position processing

### Prepare the settings

Single-source commands read the same `config.py` and `dstools-config.py`:
`CONTAINER`, `CASDA_USERNAME`, `PYTHON`, the query filters, the DStools options
and the resources all apply. The catalogue settings (`CATALOGUE_*`), `BEGIN`/`END`
and `PRODUCT_ROOT` are ignored: the position is given to `query`, and results
stay in the target's own work directory. Always pass `--work-dir` with a
separate directory for each target; otherwise the batch `WORK_ROOT` from
`config.py` would be used.

### Query and inspect the table

~~~bash
"$ASKAP_HOST_PYTHON" askap-source query \
  --ra 180.1494 --dec=-28.6159 --name MySource \
  --work-dir /fred/YOUR_PROJECT/YOUR_OZSTAR_USER/DStools_work/MySource
~~~

The command prints a summary (candidates, eligible records, SBs, default
selection, estimated download bytes) and the path of the observation table,
`WORK_ROOT/query/observations.csv`. Its columns are explained in
`observations-columns.md` in the same folder. Each row is one candidate dataset
with its SB, beam, separation, exposure, quality, public status, whether it is
eligible (and if not, why), and whether it is selected. By default the nearest
eligible beam of each SB is selected. `Record_ID` is the full dataset identity
used by `--observation-key`. Editing this CSV does not change the selection.

Other ways to give the position:

~~~bash
# Sexagesimal: RA in hours, Dec in degrees
"$ASKAP_HOST_PYTHON" askap-source query \
  --ra "12:00:35.856" --dec="-28:36:57.24" --ra-unit hourangle --name MySource \
  --work-dir /fred/YOUR_PROJECT/YOUR_OZSTAR_USER/DStools_work/MySource

# With proper motion (mas/yr; --pmra is μα cos δ) at reference epoch J2016.0
"$ASKAP_HOST_PYTHON" askap-source query \
  --ra 180.1494 --dec=-28.6159 --pmra -120.5 --pmdec 35.2 --coordinate-epoch J2016.0 \
  --name MySource --work-dir /fred/YOUR_PROJECT/YOUR_OZSTAR_USER/DStools_work/MySource
~~~

Write a negative declination with an equals sign (`--dec=-28.6159`) so that it
is not mistaken for an option. Without `--name`, a name is generated from the
coordinates, for example `J120035.856-283657.24`. Further options:
`--pm-unit` and `--no-pmra-includes-cosdec` (§5).

To correct the position, name, motion or epoch, run `query` again with the
corrected values; the next `run` uses them. You can also repeat the query later
to look for new observations. As in batch mode, put query filters in
`config.py`: the next `run` re-queries with the values in `config.py`.

### Plan and execute

~~~bash
# Show the plan
"$ASKAP_HOST_PYTHON" askap-source run \
  --work-dir /fred/YOUR_PROJECT/YOUR_OZSTAR_USER/DStools_work/MySource

# Execute, and continue later with the same command (inside tmux)
"$ASKAP_HOST_PYTHON" askap-source run \
  --work-dir /fred/YOUR_PROJECT/YOUR_OZSTAR_USER/DStools_work/MySource --submit
~~~

Single-source mode keeps everything: the downloaded archives, the original
measurement sets (MS), processing copies, models, full images and cutouts.
Allow several times the download size.

### Optional scope for the first plan

To process **all** eligible beams of particular SBs instead of only the
nearest one, add `--all-beams-for-sb`:

~~~bash
"$ASKAP_HOST_PYTHON" askap-source run \
  --work-dir /fred/YOUR_PROJECT/YOUR_OZSTAR_USER/DStools_work/MySource \
  --all-beams-for-sb 35429 21677 --submit
~~~

The listed SBs (written with or without the `SB` prefix) are expanded to all
their eligible beams; every other SB still uses its nearest eligible beam, so
this is not a whitelist. An unknown SB, an SB without eligible beams, or two
different datasets for the same beam are reported before anything is
downloaded. Repeat the option on every later command for this target, or
continue with `run --resume`; a command without it asks before replacing the
plan with one that has only the nearest beams.

In both modes, `--observation-key ID` (repeatable) restricts a plan to exact
datasets: `Record_ID` in the single-source table, `Observation_Key` in the
batch progress table. It too must be repeated on later commands (or use
`--resume`).

## 4. Progress, recovery and reprocessing

### Inspect status

~~~bash
"$ASKAP_HOST_PYTHON" askap-batch progress \
  --work-dir /fred/YOUR_PROJECT/YOUR_OZSTAR_USER/DStools_work/catalogue
"$ASKAP_HOST_PYTHON" askap-batch failures \
  --work-dir /fred/YOUR_PROJECT/YOUR_OZSTAR_USER/DStools_work/catalogue
~~~

For a single source, use `askap-source` and its work directory. Both commands
leave the plan untouched and can be run while the controller is running.
`progress` refreshes the tables in `WORK_ROOT/state/` and prints a summary with
the count of each status. `failures` lists failed attempts (`env_failed`,
`failed`, `exhausted`), including old ones that have since been retried, so use
the progress table for the current state.

The main table is `state/observation_progress.csv`, one row per
source–observation pair; its columns are explained in
`state/observation-progress-columns.md`. The most useful ones:

| Column | Meaning |
| --- | --- |
| `Status` | `not_started`, `preparing`, `queued`, `running`, `partial` (stopped at the job time limit; continues in the next job), `complete`, `env_failed` (stopped by the environment; the next command retries it), `failed` (processing failed; retried only with `--retry-failed`), `exhausted` (processing failed after both retries; retried only with `--retry-exhausted`), `superseded` (belonged to a plan that was replaced) |
| `Failed_Step`, `Error`, `Log` | Where and why the latest attempt failed, and which log file to read |
| `archive_download` … `cleanup` | One column per step: `Y` done, `N` failed, `NA` not needed, empty not reached |
| `DS`, `I`, `V` | Paths of the finished products |
| `Transfer_Status` | `verified` once the result has been retrieved and checked (§6) |

In Slurm, the pipeline's jobs are called `ASKAP-auto`:
`squeue -u "$USER" --name=ASKAP-auto` lists them, and their output is in
`WORK_ROOT/logs/slurm-<jobid>.out` and `.err`. A job that ends normally does
not mean that every target succeeded; check `Status`.

### Resume after disconnection

If the controller has stopped (you logged out without `tmux`, pressed Ctrl-C,
or the login node restarted), first make sure the old process is really gone,
for example with `pgrep -u "$USER" -af askap-`. Then set up the session (§1)
and run the same command, with the same options, again, or
`run --resume --submit`. It re-attaches to jobs
that are still in Slurm, skips finished targets and continues the plan. Only
one controller can use a work directory at a time; a second one stops with
"Another process owns …". Do not delete lock files while their process is still
running.

### Change settings

To correct a setting, a catalogue entry or the scope of unfinished work:

1. Stop the controller (Ctrl-C). It must not be running when you start the new
   command; otherwise the new command stops with "Another process owns …".
2. Check for jobs: `squeue -u "$USER" --name=ASKAP-auto`. Let them finish, or
   cancel them with `scancel JOBID` if their settings are wrong.
3. Edit `config.py`, `dstools-config.py` or the catalogue, and run the command
   again (or `run --resume`). It lists what differs from the unfinished plan,
   for example:

   ~~~text
   This command differs from it in:
     ACCOUNT: None -> 'myproject'
     MODEL['robust']: 0.5 -> -0.5
   Replace the unfinished plan? [y/N]
   ~~~

   Answer `y` to replace the plan. Any other answer, or none, leaves it
   unchanged.

The previous plan is kept as history; its unfinished targets become
`superseded` and are planned again under the new settings if the command
selects them. Finished results are kept and not recomputed (see
`--rerun-completed` below). A settings change does not retry failed targets by
itself: environment failures (`env_failed`) are retried by any command, and
processing failures (`failed`) only with `--retry-failed` (next section). If
the previous plan has finished, nothing is replaced and the command runs
without asking.

### Retry failures or recompute successes

~~~bash
# Retry processing failures (Status failed)
"$ASKAP_HOST_PYTHON" askap-batch run --retry-failed --submit

# After fixing the cause, retry targets whose retries are used up (Status exhausted)
"$ASKAP_HOST_PYTHON" askap-batch run --retry-exhausted --submit

# Recompute finished results of one source with the current settings
"$ASKAP_HOST_PYTHON" askap-batch run --source Example_A --rerun-completed --submit
~~~

Failures are of two kinds, shown in `Status`:

| Kind | Causes | Retried |
| --- | --- | --- |
| `env_failed` (environment) | CASDA staging or download errors, after `DOWNLOAD_RETRIES` tries (default 3, 30 s apart); not enough disk space or staging budget; a Slurm job that ended without finishing (node failure, cancelled, out of memory, module or image problems); jobs that keep reaching their time limit (`MAX_CONTINUATIONS`) | automatically, by the next command, even an identical one; this does not use up a retry |
| `failed` (processing) | a DStools step failed or produced no output; a cutout failed; a moving source without an observation time; a damaged or unreadable archive | only with `--retry-failed` |

Environment failures are not retried again within the same controller
session. When space runs out, or when the cause would repeat for every job (the
image check failed, or a job failed, was cancelled or ran out of memory), the
controller stops and names the cause and the Slurm log; fix it and run the
command again. After a node failure it carries on with the next job. Login and
credential errors stop the controller immediately, and nothing is marked as
failed.

`--retry-failed` gives each processing failure up to two more attempts
**within the same command**:

1. reprocessing with its own private copy and model, reusing the downloaded
   data;
2. if that fails too, a new download and processing.

After that the target is `exhausted`, and neither an ordinary run nor
`--retry-failed` processes it again. Once you have fixed the cause (for
example, a `MODEL` option), `--retry-exhausted` gives the exhausted targets in
the selection a new round of the same two attempts; if both fail again, they
are `exhausted` again. `--retry-exhausted` processes no other target: failed,
environment-stopped and not yet processed targets are left to the other
commands.

An explicit retry that was stopped by the environment does not count: the
same retry option repeats it. Retries do not share models between sources, so
they cost more compute. The selection (`BEGIN`/`END`, `--begin`/`--end`,
`--source`, `--observation-key`) limits a retry, but targets of the same kind
that share an observation with a selected target are retried as well. To
continue an interrupted retry, repeat the command with the same retry option
(or use `run --resume`); a command without it asks before replacing the retry
plan.

> **Fix the cause before retrying.** Run `failures` first and read the `error`
> of each processing failure. Otherwise both attempts can fail for the same
> reason, and the targets become `exhausted`.

`--rerun-completed` recomputes finished results in new, independent attempts,
for the sources or rows you select only. The old result remains available and
is replaced only when the new attempt succeeds. Changing settings alone never
recomputes finished results; use this option when you want that.

Single-source mode supports the same options. It also keeps the inputs of
processing failures.

### Resolve an uncertain submission

If `sbatch` rejects a job outright (for example, an invalid partition or
account), the command stops and shows Slurm's reason; correct `PARTITION` or
`ACCOUNT` in `config.py` and run again (answer `y` when asked to replace the
plan).

If instead the controller stops between calling `sbatch` and recording the
result, or `sbatch` fails without a clear answer (for example, a time-out), the
pipeline cannot know whether the job exists, so it refuses to submit it again.
The next command then stops with a message containing the pipeline job ID (for
example `20261015T034649-99be0f87b0d3`, which is not the numeric Slurm ID).
Check Slurm:

~~~bash
squeue -u "$USER" --name=ASKAP-auto
sacct -X -u "$USER" --name=ASKAP-auto --starttime=now-2days
~~~

If the job exists, link its Slurm ID:

~~~bash
"$ASKAP_HOST_PYTHON" askap-batch resolve-submission \
  --work-dir /fred/YOUR_PROJECT/YOUR_OZSTAR_USER/DStools_work/catalogue \
  --job PIPELINE_JOB_ID --scheduler-id EXISTING_SLURM_ID
~~~

Only if you are sure that no job was created, replace
`--scheduler-id EXISTING_SLURM_ID` with `--confirmed-not-submitted`. Then run
your command again. Single-source mode uses the same command with
`askap-source`.

## 5. Common settings and options

### Columns, units and proper motion

`CATALOGUE_COLUMNS` tells the pipeline which column of your table holds each
quantity; `None` means the table has no such column:

| Key | Required | Content | Default column |
| --- | --- | --- | --- |
| `source_name` | yes | Source name | `Name` |
| `ra` | yes | Right ascension, ICRS | `RA` |
| `dec` | yes | Declination, ICRS | `DEC` |
| `pmra` | no | Proper motion in right ascension | `None` |
| `pmdec` | no | Proper motion in declination | `None` |
| `epoch` | no | Epoch of the catalogue position | `None` (uses `COORDINATE_EPOCH`) |

Units and formats:

| Setting | Values | Default | Notes |
| --- | --- | --- | --- |
| `RA_UNIT` | `deg` or `hourangle` | `deg` | `hourangle` accepts `12:00:35.856` or `12h00m35.856s` |
| `DEC_UNIT` | `deg` | `deg` | Decimal or sexagesimal degrees, e.g. `-28:36:57.24` |
| `PM_UNIT` | an angular rate known to Astropy, e.g. `mas/yr`, `arcsec/yr` | `mas/yr` | Applies to both motion columns |
| `PMRA_INCLUDES_COSDEC` | `True` or `False` | `True` | `True`: `pmra` is μα cos δ, as in Gaia; `False`: plain μα |
| `COORDINATE_EPOCH` | a time string | `J2000` | Used for all rows when there is no epoch column |
| `CATALOGUE_FORMAT` | `csv` or `fits` | `csv` | |
| `CATALOGUE_FITS_HDU` | HDU number | `1` | FITS tables only |

**Epochs** must be time strings that Astropy understands: Julian epochs
(`J2000`, `J2016.0`), Besselian epochs (`B1950`) or ISO dates
(`2016-01-01T00:00:00`, UTC). Plain numbers such as `2016.0` (decimal year) or
`57388.5` (MJD) are rejected rather than guessed; convert them first, e.g.
`2016.0` → `J2016.0`, or an MJD to an ISO date.

**Motion rules:**

- Unmapped motion columns mean zero motion.
- A mapped motion column must contain a number in every row. An empty or
  invalid cell stops the catalogue import and reports the row number; use `0`
  where motion is unknown or should be ignored.
- Motion is applied from the epoch to the start of each observation, taken
  from CASDA or, if CASDA has no time, from the measurement set. If neither is
  available for a moving source, that target fails at the `proper_motion` step;
  sources without motion are not affected.

A ready-made example with motion and sexagesimal coordinates is
[targets_sexagesimal.csv](../../examples/targets_sexagesimal.csv). Use it with:

~~~python
CATALOGUE_COLUMNS = {"source_name": "Object", "ra": "RightAscension", "dec": "Declination",
                     "pmra": "MuAlphaStar", "pmdec": "MuDelta", "epoch": "Epoch"}
RA_UNIT = "hourangle"
~~~

For FITS input, set `CATALOGUE_FORMAT = "fits"` (and `CATALOGUE_FITS_HDU` if
the table is not in HDU 1) and map the columns in the same way.
[targets.fits](../../examples/targets.fits) holds the same two sources as
`targets.csv`. Masked FITS cells count as empty.

**Source names** are normalised: runs of whitespace become `_`. A name may
contain only ASCII letters, digits and `_ . + -`, must start with a letter or
digit, must not contain `..`, may have at most 120 characters, and must still
be unique after normalisation. `--source` and `PRIMARY_BEAM_BY_SOURCE` use the
normalised names (column `Source_Name` of `state/input_catalogue.csv`). Your
table must not contain columns with these reserved names: `Processing_Index`,
`Source_ID`, `Original_Source_Name`, `Source_Name`, `RA_deg`, `Dec_deg`,
`PMRA_cosdec_masyr`, `PMDEC_masyr`, `Epoch_MJD`, `Epoch_Scale`.

When the table, its mapping or the units change, the next command reads the
new version and queries every source again; earlier versions are kept in
`WORK_ROOT/logs/catalogues/`.

Single-source equivalents: `--ra-unit`, `--pmra`, `--pmdec`, `--pm-unit`,
`--pmra-includes-cosdec` / `--no-pmra-includes-cosdec` and `--coordinate-epoch`.

### Supply settings

Both commands read settings from these places, highest priority first:

1. command-line options;
2. `ASKAP_<SETTING>` environment variables;
3. `config.py` and `dstools-config.py` (or the files given with `--config` and
   `--dstools-config`).

There are no built-in defaults. If a setting is missing from both files and is
not given otherwise, the command stops before doing anything and names it, for
example `Settings missing (there are no built-in defaults): dstools-config.py:
CROP_ARCMIN`. This can happen if a line was lost while resolving an update
conflict ([Installation §2](INSTALLATION.md#2-obtain-the-code)); copy the named
lines, with their comments, from the repository version, for example
`git show HEAD:dstools-config.py`. A setting written in the wrong file is
refused with a message naming the right one.

How to write values in each place:

- **`config.py`, `dstools-config.py`:** upper-case names and Python values,
  e.g. `EXPOSURE_MIN = 300.0`, `EXCLUDED_SBS = [12345]`, `PRIMARY_BEAM = None`.
- **`--set`:** lower-case names and JSON values, e.g. `--set exposure_min=300`,
  `--set excluded_sbs='[12345]'`. `null` switches an optional limit off:
  `--set exposure_min=null`.
- **Environment:** `ASKAP_` plus the upper-case name, with a JSON value, e.g.
  `export ASKAP_EXCLUDED_SBS='[12345]'`.

Command-line options apply only to the command they are given to, and
environment variables only while they are set; for lasting settings use the
files. Each plan records the effective value and origin of every setting in
`effective-settings.json`, and each command records them, with the checksums of
both files, in `logs/commands/`. The comments in the two files describe every
setting.

Common settings:

| Purpose | Setting | Command line | Value in the file |
| --- | --- | --- | --- |
| Work directory | `WORK_ROOT` | `--work-dir` | — |
| DStools image | `CONTAINER` | `--container` | — |
| Minimum exposure (strictly longer than) | `EXPOSURE_MIN` | `--exposure-min` | 500 s |
| Accepted data quality | `QUALITY_LEVELS` | `--set quality_levels='["GOOD"]'` | `GOOD`, `UNCERTAIN` |
| SB limits | `MINIMUM_SB`, `EXCLUDED_SBS` | `--minimum-sb` | none |
| Catalogue rows (batch) | `BEGIN`, `END` | `--begin`, `--end` | whole table |
| Parallel slots × threads | `PARALLEL_SLOTS`, `THREADS_PER_SLOT` | `--parallel-slots`, `--threads-per-slot` | 4 × 8 (at most 32 CPUs) |
| Memory per job | `MEMORY_GB` | `--memory-gb` | 160 GiB |
| Job time limit | `WALLTIME_HOURS` | `--walltime-hours` | 48 h (an upper limit, not every job's request) |
| Cutout size | `CROP_ARCMIN` (dstools-config.py) | `--crop-arcmin` | 10 arcmin |
| Target protection radius | `MASK_RADIUS_ARCSEC` (dstools-config.py) | `--mask-radius-arcsec` | `None`: chosen by DStools |
| Sky-model options | `MODEL` (dstools-config.py) | `--model-option KEY=VALUE` | see below |
| Anything else | (see the two files) | `--set key=value` | see the two files |

Settings without a file name are in `config.py`.

### DStools options (`dstools-config.py`)

`dstools-config.py` holds what DStools computes with: `MODEL` (the
`dstools-create-model` options), `MASK_RADIUS_ARCSEC` (`dstools-insert-model`),
`EXTRACT_MINUV_M`, `EXTRACT_NOFLAG`, `EXTRACT_BASELINE_AVERAGE`,
`PRIMARY_BEAM` and `PRIMARY_BEAM_BY_SOURCE` (`dstools-extract-ds`), and
`CROP_ARCMIN` (the cutouts).

`MODEL` lists every `dstools-create-model` option with the value that is passed
to DStools. The values are the DStools defaults, except four that reproduce
the processing of our original pipeline: `band` `AK_low`, `iterations` 100000,
`multiscale` `True` and `minuvw-m` 200. Every value is passed explicitly, so a
DStools image with different defaults gives the same processing; `None` means
the option is not passed (DStools then computes it, as for `imsize` and
`cell`), and `False` for a switch such as `erode-beam-shape` means it is not
given. To change an option, edit its value, for example
`"robust": -0.5,`, or change it for one command with
`--model-option robust=-0.5`. Option names are the long options of
`dstools-create-model` without the leading `--`. Keep every option in the
dictionary; unknown names are rejected, and integer options take whole numbers
(`"threshold": 3`, not `3.5`). Output paths and prefixes are managed by the
pipeline.

## 6. Retrieve batch products on Ada

On the destination machine (Ada in our setup), after installing the script
([Installation §6](INSTALLATION.md#6-place-the-retrieval-script-on-ada)):

~~~bash
ADA_PROJECT=/PATH/TO/ada/DStools-Auto-Processing

python3 "$ADA_PROJECT/bin/pull_products.py" \
  --user YOUR_OZSTAR_USER --host ozstar.swin.edu.au \
  --remote-work /fred/YOUR_PROJECT/YOUR_OZSTAR_USER/DStools_work/catalogue \
  --destination "$ADA_PROJECT/catalogue" \
  --password-file ~/.config/askap/ozstar-transfer-password
~~~

Omit `--password-file` if you use SSH keys or an agent. `--remote-products`
defaults to `<remote-work>/products`; set it if you changed `PRODUCT_ROOT`.
Use a separate destination for each batch work directory. All finished results
are copied by default; `--source NAME` (repeatable) limits the copy.

The script lists the finished results on OzSTAR with their SHA256 checksums,
copies only missing or different files, verifies every copy, checks that
nothing changed on OzSTAR in the meantime, and uploads a receipt to the work
directory. Running it again copies only what is new. It never deletes anything,
on either machine.

What you get in the destination directory:

~~~text
DESTINATION/
├── <Source_Name>/<dataset-label>   → link to the current result in .versions/
│     <SB>_<beam>.ds                           dynamic spectrum
│     wsclean_model/wsclean-MFS-I-image.fits    Stokes I cutout
│     wsclean_model/wsclean-MFS-V-image.fits    Stokes V cutout
├── .versions/                      the actual files; keep this hidden folder
└── state/                          ledger, manifests and receipts with the provenance of each result
~~~

The two FITS files are the cutouts (`CROP_ARCMIN` across), even though their
names are those of WSClean's full-field images. The provenance of each result
(SB, beam, attempt, settings version, image checksum) is recorded in the
manifests and receipts under `state/`.

Afterwards, run `progress` on OzSTAR: it imports the receipt and marks the
results as `verified` in `Transfer_Status`.

You can also read the connection details from a settings file: copy your
`config.py` to the destination machine and run
`python3 pull_products.py --config /PATH/TO/config.py`. It uses
`OZSTAR_USER`, `WORK_ROOT` and `ADA_ROOT` (the destination), plus
`PRODUCT_ROOT`, `OZSTAR_HOST` and `TRANSFER_PASSWORD_FILE` if set. Command-line
options take precedence over `ASKAP_*` environment variables, which take
precedence over the file. Keep such copies and password files on your own
machines, outside the repository.

Single-source results are not handled by this script. Copy them yourself (for
example with `rsync`) using the paths in `state/observation_progress.csv`.
