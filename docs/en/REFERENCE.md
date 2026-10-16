# 3. How it works

[Index](../../README.md) · [中文](../zh-CN/REFERENCE.md) · [Installation](INSTALLATION.md) · [Usage](USAGE.md)

This page explains what happens behind the commands: how observations are
chosen, how each target is processed, how jobs are sized, and which files are
kept. You do not need it for a first run, but it helps when you interpret
results or change settings.

## 1. Responsibilities

| Component | Runs on | Responsibility |
| --- | --- | --- |
| `askap-batch` | login node | Reads the catalogue, builds the observation index and plans source ranges |
| `askap-source` | login node | Queries one position, writes the candidate table and plans the selection |
| Controller (`run --submit`) | login node | Downloads data, submits Slurm jobs, follows them and continues interrupted work |
| Worker (inside each Slurm job) | compute node | Unpacks archives and runs DStools in the image, recording each target's result |
| `pull_products.py` | destination machine (Ada) | Copies published batch results and uploads verification receipts |

Both commands share the code in `askap_auto/` and read `config.py` and
`dstools-config.py` every time they start; the program itself has no default
values. A plan records the settings and selection it was made with. When a
command differs from the unfinished plan, it lists the differences and replaces
the plan only after you confirm, and only once its Slurm jobs have stopped; the
old plan is kept as history. Each submitted job also keeps its own copy of the
settings it runs with. Every command is logged in `logs/commands/`. In these
guides, *default* means the value in the two files as distributed.

Terms used in these guides:

| Term | Meaning |
| --- | --- |
| SB | Scheduling block: one ASKAP observation, identified by its SB number |
| Beam | ASKAP observes with 36 beams at once; CASDA stores the visibilities of each beam as a separate dataset |
| Dataset | The visibility archive of one SB and beam: a measurement set (MS) packed as `.tar` |
| Target | One source in one dataset |
| Partner | Another catalogue source in the same dataset; partners share the download and the sky model |
| Attempt | One try at processing a target; retries and reruns create new attempts |
| Plan | The targets one command will process, with the settings used; recorded in `state/runs/<run-id>/` |
| Dynamic spectrum (DS) | The target's brightness as a function of time and frequency, written by `dstools-extract-ds` |
| SIF | The Apptainer image that contains DStools and WSClean |

## 2. Input, filters and observation identity

The input table needs a name, right ascension and declination (ICRS) for each
source, optionally with proper motion and epoch ([Usage §5](USAGE.md#columns-units-and-proper-motion)).
The pipeline stores the table as it was read (`state/input_catalogue.csv`),
together with its checksum and column mapping. When the file, the mapping or
the units change, the next command reads the new version, keeps the previous
one in `logs/catalogues/` and refreshes the observation index.

For each source, observations are chosen in three steps:

1. **Search.** A CASDA TAP query returns visibility datasets whose beam centre
   lies within `INITIAL_RADIUS_DEG` of the source and whose exposure exceeds
   `EXPOSURE_MIN`.
2. **Check.** Every candidate is tested against the conditions below; any
   failed test is listed in its `Eligibility_Reason`.
3. **Select.** In each SB, the eligible beam nearest to the source is selected.
   If two datasets are equally near, the one with the smaller dataset identity
   is taken. This choice is reproducible but carries no scientific preference.
   Batch runs process these selections; a single-source plan can expand
   chosen SBs to all their eligible beams with `--all-beams-for-sb`.

| Condition | Default |
| --- | --- |
| Data | Released (public) ASKAP visibilities |
| Quality | `GOOD` or `UNCERTAIN` (`QUALITY_LEVELS`) |
| Exposure | Strictly longer than 500 s (`EXPOSURE_MIN`; `None`/`null` disables) |
| SB range | No minimum (`MINIMUM_SB`), none excluded (`EXCLUDED_SBS`) |
| Archive size | At most 50 × 1024 × 1024 KB, i.e. 50 GiB (`MAX_FILE_SIZE_KB`; can be disabled) |
| Initial search radius | 1° (`INITIAL_RADIUS_DEG`) |
| Results per query | 5000 (`QUERY_LIMIT`); reaching it is an error: raise the limit and query again |
| Distance from beam centre | Within the frequency-scaled radius below |

The acceptance radius scales with frequency, approximating half the primary
beam width:

~~~text
radius_deg = 0.5 × REFERENCE_FWHM_DEG × REFERENCE_FREQUENCY_GHZ / frequency_GHz
~~~

With the defaults (`REFERENCE_FWHM_DEG = 1.2`, `REFERENCE_FREQUENCY_GHZ = 1.4`),
an observation centred at 0.8875 GHz gets a radius of 0.5 × 1.2 × 1.4 / 0.8875
≈ 0.95°. `RADIUS_FREQUENCY_MODE = "central"` uses the middle of the band;
`"top"` uses its upper edge, which gives a smaller radius. The initial search
radius only limits what CASDA returns: with the defaults the acceptance radius
exceeds 1° only when the frequency used is below about 0.84 GHz. If you raise the
acceptance radius, raise `INITIAL_RADIUS_DEG` too. A record with invalid
coordinates or frequencies has an empty `Within_Radius` and is not eligible.

Query results are saved and reused while the query settings and the catalogue
are unchanged. Any change to either makes the next `query` or `run` query every
source again; `--update-query` does the same on request, to find newly released
data.

The single-source table lists every candidate with its rejection reasons.
Datasets that CASDA itself filtered out (for example, too short an exposure)
do not appear in it.

A dataset's identity combines its SB, beam, archive file name, CASDA product
ID and frequency range into a 64-character key (`Record_ID` or
`Observation_Key`). Directory labels have the form
`SBxxxxx_beamyy--<first 12 characters of the key>`; the suffix distinguishes
different products of the same SB and beam, so do not compare labels by their
first part alone.

## 3. Science processing and target isolation

For each dataset in a batch run:

1. **Download** (login node): the `.tar` archive and its `.checksum` file are
   fetched from CASDA.
2. **Unpack** (compute node): the archive's structure is checked, then the
   original measurement set is extracted.
3. **Shared model**: a copy of the MS goes through `dstools-askap-preprocess`
   and `dstools-create-model` (WSClean imaging and deconvolution of the field).
   This is done once and shared by all targets in the dataset.
4. **Per target**, in a private copy of the preprocessed MS and model:
   - `dstools-insert-model` predicts the sky model with a region around the
     target masked out (`MASK_RADIUS_ARCSEC`), i.e. the model of every other
     source in the field;
   - `dstools-subtract-model --split-ms` subtracts it, writing the residuals to
     a separate `.subtracted.ms`;
   - `dstools-extract-ds` extracts the dynamic spectrum at the target's
     position, corrected for proper motion, from the residual `DATA` column;
   - Stokes I and V cutouts are cut from the field images made in step 3,
     centred on the target.
5. **Publish and clean up**: results are published and the temporary copies
   removed (§5).

Retries and single-source runs follow the same steps, but each target gets its
own private model instead of a shared one.

These settings are in `dstools-config.py`:

| Setting | Value in the file and meaning |
| --- | --- |
| `MODEL` | Every `dstools-create-model` option: the DStools defaults, except `band=AK_low`, `iterations=100000`, `multiscale=True`, `minuvw-m=200`, which reproduce our original processing |
| `MASK_RADIUS_ARCSEC` | `None`: DStools' beam-based default radius around the target |
| `EXTRACT_MINUV_M` | `500`: shortest baseline, in metres, used for the dynamic spectrum |
| `EXTRACT_NOFLAG` | `False`: flagged data stay excluded |
| `EXTRACT_BASELINE_AVERAGE` | `True`: average over baselines |
| `CROP_ARCMIN` | `10`: cutout side length, in arcminutes |

Every `MODEL` value is passed to DStools explicitly, so the processing does
not depend on the defaults of the DStools version in the image. `None` leaves
an option to DStools (it then computes, for example, the image size and pixel
size), and `False` for a switch means it is not given. The extraction always
passes `--datacolumn data` (the residuals) and `--baseline-average` or
`--no-baseline-average`. The first job of each plan checks that the image
accepts every option used. Output paths and prefixes are managed by the
pipeline.

**Primary-beam images** are optional. For a single target use
`PRIMARY_BEAM`; for several targets use `PRIMARY_BEAM_BY_SOURCE`, a dictionary
from normalised source name to image path. The files must already exist on
OzSTAR; their checksums are recorded in the plan.

**Time and proper motion.** The start time of the observation is taken from
CASDA when available; otherwise it is read from the measurement set's `TIME`
column, honouring its unit and time scale. Positions are propagated from the
catalogue epoch to that time with Astropy (no distance or radial velocity is
assumed). A moving source without a usable time fails at `proper_motion`;
partners without motion are processed normally.

**Products.** For each target and dataset:

| File | Content |
| --- | --- |
| `<SB>_<beam>.ds`, e.g. `SB12345_beam00.ds` | Dynamic spectrum |
| `wsclean-MFS-I-image.fits`, `wsclean-MFS-V-image.fits` | Stokes I and V cutouts, `CROP_ARCMIN` across, with a celestial WCS |
| `observation.json` | Provenance: source, SB, beam, attempt, settings version, image checksum |

In published batch results the two cutouts sit in a `wsclean_model/`
subfolder and keep WSClean's file names, but they are cutouts, not full-field
images. "Complete" means these files were produced and saved; whether the data
are scientifically usable is for you to judge.

## 4. Parallelism, time estimates and recovery

Independent datasets are processed in parallel *slots*; the targets of one
dataset are processed one after another. The default is 4 slots × 8 threads,
i.e. 32 CPUs; a job with fewer datasets requests fewer slots. Memory
(`MEMORY_GB`, default 160 GiB per job) is divided among the slots in use, and
WSClean is given 75% of a slot's share. `TMP_GB` (default 10) is the local
scratch space requested from Slurm; measurement sets and models live in the
work directory, so plan your `/fred` storage accordingly.

Job durations are estimated from fixed workloads:

| Setting | Default | Meaning |
| --- | --- | --- |
| `HOURS_PER_OBSERVATION` | 2 | Preparing a dataset and building its model |
| `HOURS_PER_TARGET` | 0.5 | Each additional target in that dataset |
| `OVERHEAD_HOURS` | 2 | Fixed overhead per job |
| `RESERVE_MINUTES` | 20 | Time kept at the end of a job for an orderly stop |
| `WALLTIME_HOURS` | 48 | Upper limit for any job |
| `MAX_OBSERVATIONS_PER_JOB` | 40 | Datasets per job |
| `MAX_CONTINUATIONS` | 20 | Follow-up jobs after a job reaches its time limit |

Datasets are distributed over the slots; a job requests the load of its
busiest slot plus the larger of the overhead and the reserve, capped at
`WALLTIME_HOURS`. For example, one dataset with three targets requests
2 + 3 × 0.5 + 2 = 5.5 h. If a usable shared model already exists, its 2 h are
not counted. With the defaults, one job handles at most 88 targets of the same
dataset; larger groups are split across jobs. These estimates are not derived
from the exposure time of the observations; `EXPOSURE_MIN` only filters
observations.

**Continuations.** When a job comes within `RESERVE_MINUTES` of its end, the
worker stops the running command, marks that target `partial` and starts
nothing new. The controller then submits a follow-up job for the unfinished
targets, up to `MAX_CONTINUATIONS` times.
Finished targets, a completed shared model and a finished dynamic spectrum are
reused, but the interrupted command starts again from the beginning. If a
single step needs more time than a job provides (typically model building for
a long observation), every follow-up job repeats it until the limit is reached
and the targets become `env_failed` with "Bounded walltime continuation
exhausted". In that case, raise `HOURS_PER_OBSERVATION` in `config.py` and run
the command again; the targets are retried automatically.

**Failed jobs.** If a Slurm job ends without the worker's completion record
(for example, a node failure, a cancelled job, too little memory, or a module
or image problem), the controller records its unfinished targets as
`env_failed` at step `worker`. The next command gives them a new attempt
without using up a retry. After a node failure (`NODE_FAIL`, `BOOT_FAIL`) the
controller continues with the next job; after any other such ending, and when
the worker reports that space ran out or that the image check failed, it stops
and names the cause and the Slurm log, because the next job would most likely
fail in the same way. Jobs that reach their time limit (`TIMEOUT`) or are
preempted (`PREEMPTED`) are continued instead, as described above. Processing
failures, such as a failing DStools step, cutout or unpack, are recorded as
`failed` and retried only with `--retry-failed`
([Usage §4](USAGE.md#retry-failures-or-recompute-successes)).

The controller must keep running to prepare later downloads and jobs. If it
stops, run the same command with the same options again, or
`run --resume --submit`: it continues the plan and re-attaches to jobs already
in Slurm. If the outcome of a submission
is uncertain, it stops instead of submitting again and asks you to check Slurm
first ([Usage §4](USAGE.md#resolve-an-uncertain-submission)).

## 5. State, results and cleanup

`state/sb_processing.sqlite` holds the authoritative processing state. The CSV
and JSON files are views exported from it; `progress` refreshes them and
imports transfer receipts.

| Path in the work directory | Content |
| --- | --- |
| `state/observation_progress.csv` | Current and earlier source–observation states, steps, errors, logs and product paths |
| `state/processing_summary.json` | Counts of queries, observations, shared groups and statuses |
| `state/input_catalogue.csv` | The catalogue as read, with normalised names and parsed coordinates (batch) |
| `state/crosscatalogue_sources.csv` | Query status of every source (batch) |
| `state/queries/<Source_ID>/` | All candidates of one source (batch) |
| `query/observations.csv` | Candidate table (single source) |
| `logs/commands/` | Every `query` and `run` command, with its effective settings, their origins and timestamps |
| `logs/catalogues/` | Earlier versions of the catalogue as read (batch) |
| `state/runs/<run-id>/execution-plan.csv` | Planned targets and their attempt IDs, for every plan including replaced ones |
| `state/runs/<run-id>/effective-settings.json` | Effective settings and their origins |
| `state/jobs/` | Job manifests, Slurm scripts, submission and outcome records |
| `logs/` | Slurm output and error files |

Where the data live:

~~~text
Batch WORK_ROOT/                         Single-source WORK_ROOT/
├── state/      state and tables          ├── state/
├── logs/       Slurm output, commands,   ├── logs/
│               catalogue versions        │
├── staging/    downloads, shared models, ├── query/          candidate table
│               per-target work copies    ├── observations/   all data and results
└── products/   published results         └── products/       (unused)
                (PRODUCT_ROOT)
~~~

What is kept:

| File | Batch | Single source |
| --- | --- | --- |
| DS and I/V cutouts | Published under `PRODUCT_ROOT` | In the attempt directory |
| Downloaded archive (`.tar`) | Deleted after unpacking | Kept |
| `.checksum` file | Kept | Kept |
| Original MS and shared model | Deleted once every target of the dataset has succeeded | Kept |
| Per-target work copy, model and full images | Deleted after the result is published | Kept |
| Interrupted or failed downloads (no `download-ready.json` for them) | Deleted before the next download; errors stay in `download.log` | Same as batch |
| Half-unpacked MS (not named in `raw-ready.json`) | Deleted before unpacking again | Same as batch |
| Unfinished shared model build (`build-*` without a valid `ready.json`) | Deleted before building again | (no shared model) |
| Work copies of an interrupted attempt (`interrupted-work-*`) | Deleted when the target is processed again | Same as batch |
| Work copies of attempts that were replaced or stopped by the environment (`superseded`, `env_failed`) | Deleted when the target is prepared again | Same as batch |
| Inputs of processing failures (`failed`, `exhausted`) | Kept for diagnosis; a retry with a new download removes the failed attempt's work copies | Kept |
| State, logs and provenance | Kept | Kept |

A complete download (named in `download-ready.json`, with unchanged file
sizes) or unpacked MS (named in `raw-ready.json`) is reused instead of being
fetched or unpacked again. Leftovers are deleted only by the controller on the
login node or by the job that processes the dataset, so no running job loses
data it uses.

Published batch results:

~~~text
PRODUCT_ROOT/<Source_Name>/<dataset-label>
    -> PRODUCT_ROOT/.versions/<source-id>/<dataset-id>/<attempt-id>/
~~~

`.versions` holds the results themselves and never changes them; the visible
path is a link to the current result. Do not delete `.versions` because it is
hidden. If a rerun fails, the old result stays current; the link switches only
when the new result has succeeded. When copying the whole product tree, keep
both the links and `.versions`.

Single-source data are under `observations/<dataset-label>/`, with each
attempt in `attempts/<attempt-id>/`: `products/` (DS), `cutouts/` (I/V),
`work/` (processing copies and full images, including
`work/wsclean_model/`) and `logs/`. A failed cleanup is recorded separately
and never revokes a saved result.

## 6. Ada transfer and storage

`pull_products.py` selects only complete, published batch results whose three
files (DS, I, V) are present. It lists them with SHA256 checksums, copies
missing or different files, verifies every copy and checks that the results on
OzSTAR did not change during the transfer. Only then does it record the results
as complete on the destination and upload a receipt; `progress` on OzSTAR
imports the receipt and marks the results `verified`.

The destination keeps the files under `.versions/`, links to the current
results, and `state/transfer_ledger.sqlite` with manifests and receipts.
Repeated pulls reuse files that already match. Nothing is deleted on either
side.

To free space on OzSTAR, delete published results only after they have been
retrieved and verified (`Transfer_Status` = `verified`). If results that were
never retrieved are missing, the next plan stops with "Completed products
unexpectedly missing" until you rerun them explicitly with `--rerun-completed`.

Downloads are limited by `STAGING_BUDGET_BYTES` (default 2 TiB, counting
everything under `WORK_ROOT/staging/`) and by the free disk space.
Data of processing failures are kept and count towards the budget; leftovers
of interrupted or replaced attempts are deleted automatically (§5). If a
download, unpack or copy would exceed the budget or the free space, the
affected targets are recorded as `env_failed` and the controller stops with a
message. Free space or raise `STAGING_BUDGET_BYTES`, then run the command
again; the targets are retried automatically, without a retry option.
In single-source mode the data are kept under `observations/`, outside the
budget, so only the free disk space limits them.
