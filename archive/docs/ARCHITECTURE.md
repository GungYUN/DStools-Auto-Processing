# Architecture and persisted contracts

`askap_auto` has no import-time batch configuration or site path resolution.
`cli.py` loads batch `config.py` only for askap-batch. Single-source settings
come from explicit CLI/defaults/optional independent JSON. `settings.py` resolves
and validates values; `catalogue.py` normalises catalogue/position input;
`query.py` preserves candidates before eligibility and nearest selection;
`casda_client.py` retains the proven signed-URL and ephemeral-keyring adapter.
`archive.py` performs login downloads and command-line compute extraction.
`science.py` builds long-form DStools commands and isolates raw/master/target
copies. `controller.py`, `worker.py` and `state.py` manage jobs and recovery.

## Identity and paths

Processing_Index is the original 1-based row position, never a persistent key.
Source identity hashes the original name; a workspace's full input file and
column/coordinate mapping are frozen. Reorder, rename or input changes require
another workspace. Rows are validated before any network request. Whitespace
runs become one underscore; supported source names are 1–120 ASCII letters,
digits, dot, underscore, plus and hyphen, starting with a letter/digit and
without `..`. Normalised collisions are rejected with row numbers.

Dataset identity hashes SB, beam, filename, publisher DID and wavelength bounds
normalised to metres. The directory label is `SBxxxxx_beamyy--<12-key-chars>`;
the suffix distinguishes different datasets with the same SB/beam. The full key
is always recorded. This intentionally refines the design documents' simpler
example paths, so distinct versions never overwrite one another.

Batch:

```text
WORK/state/{input_catalogue.csv,input_catalogue.json,crosscatalogue.json,
            sb_processing.sqlite,observation_progress.csv,processing_summary.json}
WORK/state/queries/<source-id>/versions/<revision>/candidates.ecsv
WORK/state/runs/<run-id>/{execution-plan.json,execution-plan.csv,effective-settings.json}
WORK/state/jobs/<job-id>.{json,sbatch,submission.json,outcome.json}
WORK/staging/_datasets/<dataset-label>/{download,raw,download-ready.json,raw-ready.json}
WORK/staging/_shared/<model-key>/{build-<id>,ready.json,model.log}
WORK/staging/<source>/<dataset-label>/attempts/<id>/{work,products,cutouts,logs}
PRODUCT/.versions/<source-id>/<dataset-key>/<attempt-id>/<DS,I,V,observation.json>
PRODUCT/<source>/<dataset-label> -> immutable successful version
```

Single source:

```text
WORK/query/{observations.csv,observations-columns.md,query-snapshot.json,versions/}
WORK/state/source.json
WORK/observations/<dataset-label>/{download,raw,attempts/<id>/}
# attempts contain work/input.ms, work/input.subtracted.ms,
# work/wsclean_model (full and unmasked), work/insertion_model,
# products/*.ds, cutouts/I,V, logs, observation.json.
```

Product publication uses a temporary copy, immutable version directory and
atomic current-link replacement. Failed reruns cannot delete old successes.
Transfer understands these managed links and copies immutable backing files;
ordinary recursive tools may need `-L` to follow links. Never delete `.versions`
merely because it is hidden. The current SQLite success pointer is updated only
after all required files have been saved. Cleanup errors are recorded separately.

## Query and motion

Public eligibility precedes nearest-beam selection. Candidates rejected by
client-side filters stay in the table; server-excluded rows cannot be listed.
Invalid position/frequency has blank Within_Radius and is ineligible. Ties use
separation then stable dataset key; exact metadata duplicates collapse. In
all-beams mode, different datasets for the same SB/beam are reported as ambiguous
before downloading. Expanding SB200 does not remove other SBs from selection.

Only an explicit query update discovers new observations. Ordinary batch runs
reuse the matching index. Changed selection conditions require --update-query;
query failures retain old rows for audit but exclude them from current scheduling.
Single run always reads the saved query, even if archive URLs must be restaged.
CSV edits never alter the execution plan.

Coordinates are ICRS, decimal or explicitly unit-qualified sexagesimal. Proper
motion is converted to mas/year with the stated cos(dec) convention, then
propagated with Astropy from the supplied reference epoch. No fabricated distance
or radial velocity is added. CASDA t_min MJD UTC has priority; otherwise SIF
casacore reads the earliest finite main-table TIME, honours QuantumUnits and
MEASINFO Ref, and converts MJD to UTC. No old date CSV or fixed fallback exists.
Missing time blocks only targets requiring nonzero motion.

## Model isolation and resources

Shared master starts from a real raw-MS copy and performs preprocess/create only.
Each target receives a preprocessed MS and full-model copy; insertion uses a
target's existing private model. Single-source preservation uses a further
insertion copy to retain its full unmasked model. Nothing inserts/subtracts/crops the shared master. All copies
are real copies, never hard links. Target position is shared by insert/extract/crop.
Subtraction always creates a split .subtracted.ms; extraction is restricted to
its residual DATA column. Cutouts are always written separately.

Model identity includes dataset, SIF SHA256, every model option, thread/memory
allocation and external mask content hashes. Each run freezes all settings and
override sources, plus external file hashes. A changed SIF/input file causes a
clear error; queued tasks never silently adopt new settings. Actual SIF --help
is checked on the compute node before execution. DStools-internal temporary MS
may still be removed by DStools itself; the pipeline guarantees retention of its
managed inputs/intermediates, not every internal third-party temporary file.

Default commands use the production UCDs inputs and the same accepted SIF
defaults; optional model overrides are emitted only when changed. Actual slot
memory divides allocated MiB by job.slots, with the same 75% WSClean limit as
UCDs. The frozen configured cap stays intact; job manifests and shared build
history record the runtime layout. See UCD_PARAMETER_AUDIT_zh.md.

Default 4 slots ×8 threads, 160 GiB total and 10 GB Slurm tmp. Persistent MS/models
stay in WORK. WSClean (including insert-model's internal invocation) gets a slot
CPU/memory wrapper; OpenBLAS uses one thread. Job duration is max lane load plus
overhead/exit reserve, bounded at 48 hours. Large target groups split across jobs.
A single workspace has one controller and one compute job at a time.

## Recovery and status

SQLite keys targets by source_id/dataset_key and stores independent attempts,
steps, run plans, job records and transfer receipts. CSV is a consistent,
atomically replaced export under an export lock; workers update only SQLite.
Status=complete can coexist with a failed Latest_Attempt_Status after rerun.
Completed_Attempt_ID and the DS/I/V paths refer to the retained successful result.
Y=success, N=failure, blank=not run, NA=not applicable. Model reuse is explicit.

Normal runs skip successful and determined-failed records. --retry-existing
allows one private rebuild (retry 1), then one fresh download/private rebuild
(retry 2), then exhausted. Batch retry 2 removes only the preceding private
attempt's working data; shared inputs remain protected. Single-source retries
retain all earlier failed data. --rerun-completed starts an independent attempt
for selected successful targets; range-external successful partners are not
included. Editing scientific options does not schedule reruns.

Interrupted mutating stages restart from clean private copies; previous work is
retained under interrupted-work directories. A committed extracted DS permits
continuing cutout/publication. Ready shared models are reusable. A killed
worker is reconciled using outcome files or two consecutive terminal sacct
states; failed/empty accounting responses never prove completion. Walltime
partial jobs have bounded login-controller continuation.

The sbatch intent is committed before submission, and the scheduler receipt is
saved separately from worker state. An interrupted submission without receipt
is uncertain and never automatically resubmitted. resolve-submission explicitly
links an existing Slurm ID or accepts the user's assertion of no submission.
Database failures stop the controller; scientific failures allow independent
observations to proceed. No automatic transfer-after-processing or deletion of
transferred results is enabled.
