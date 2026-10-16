Current interface update (2026-10-16): both commands read editable config.py on
every invocation. Site JSON was removed; changed configuration replaces idle
plans while preserving history. Command/settings logs live in logs/commands/.
The sections below retain the earlier implementation and site-acceptance history.

# Implementation status — 2026-10-15

Implemented locally: configuration-free askap_auto core; batch and single-source
entry points; CSV/FITS and immutable input identities; candidate query/selection;
verified CASDA download adapter; MS time/proper motion; shared/private processing;
explicit retention; versioned publication; SQLite/progress; Slurm/recovery;
bounded science retry and explicit rerun; Ada SHA256/receipts; deployment/examples/docs.

Validation: 149 current offline tests (48 core, 7 signed-RA query, 4 Stage 2 driver, 7 release-filter,
7 Stage 3 driver, 13 archive-link, 10 raw-link checker, 13 recovery, 9 model-type
and 11 UCDs CLI/resource contract regressions, plus 13 Stage4A and 7 Stage4B
checks) passed locally;
66 historical regression tests passed previously. The final core-only check
also passed after strengthening its actual-versus-configured-slot case.
Python grammar and CLI/shell checks passed. The original production sibling
Ozstar_ASKAP_UCDs is unchanged. Historical copied implementation is in archive/legacy/.

Public-release preparation replaced personal paths/accounts in current and
archived files. Original files are retained locally and excluded from Git;
published numerical evidence is labelled as path-redacted. Host Python now
defaults to the invoking interpreter, with an explicit override supported.
The added portability regression passed together with all existing tests.
Science, resource and time-estimation defaults are unchanged.

User-supplied Ozstar log confirms deployment to the new directory and login-node
environment/CLI checks plus all 46 mocked tests passed. Subsequent site logs
confirm Stage 1/2 acceptance and Stage 3A/3B real single/shared processing PASS,
including CASDA download, actual SIF/MS/DStools, Slurm and retained-file checks.
The user accepted the recorded Stage4A numerical differences and closed that
review. Stage4B real Ada transfer and Ozstar receipt import also passed.
Separate Stage4C site validation is skipped for this release based on the UCD
baseline, current local regressions and Stage1–4B site results. It is optional,
not a release prerequisite; current-version four-slot/recovery site coverage
remains unexecuted rather than PASS.
See docs/VALIDATION.md for evidence, interface decisions and exact remaining checks.

Deployment convention updated: host Python is
`/fred/YOUR_PROJECT/YOUR_OZSTAR_USER/env/askap-python/bin/python`; recommended remote code directory
is `/fred/YOUR_PROJECT/YOUR_OZSTAR_USER/DStools_Auto_Processing`. See docs/DEPLOYMENT_zh.md for the
environment checks and staged pilot acceptance procedure.

Stage 1 site acceptance: user supplied Ozstar output confirms all 19 catalogue
checks passed in validation/stage1/run-vrkpl7qn. Stage 2 real-query/plan acceptance
script and reference-position CSV are ready; four local driver checks passed,
with mocked CASDA boundaries. User-supplied Stage 2 retry log confirms 16 site
checks PASS, zero uncovered cases, in stage2/run-sxr6_93m.

First Stage 2 site run: four real CASDA queries succeeded, then cached public-state
audit failed. Reproduced masked/empty release-date handling across ECSV round trips;
public_indices now normalizes missing dates on a copy before native filtering.
Seven targeted regressions passed locally; the subsequent actual Stage 2 site
run confirmed public-state consistency and all remaining plan/selection checks.

Stage 3 preparation/download/run/check helpers and reference PM metadata are
ready. Seven driver checks passed with real file/SQLite protocols and mocked
science/transport. The user now reports Stage 3A PASS in
`/fred/YOUR_PROJECT/YOUR_OZSTAR_USER/DStools_Auto_Processing_validation/stage3/single_SB74372_beam28`.
The user also supplied Stage 3B PASS and shared-model history in
`/fred/YOUR_PROJECT/YOUR_OZSTAR_USER/DStools_Auto_Processing_validation/stage3/shared3_SB74372_beam28`.
Preprocess/create-model each ran once (19.798/5121.703 seconds), with one
40960-MiB slot and the accepted SIF. Subsequent Stage4A site evidence confirmed
cleanup/accounting and compared the single/shared products; the user accepted
the recorded numerical differences. See docs/STAGE4_PLAN_zh.md.

Stage 3 first site attempt: preparation and authenticated archive/checksum download
passed. Processing stopped before tar execution because the new precheck rejected
all links. User header inspection confirms 88 members with one direct internal
hardlink under ASKAP_METADATA, targeting a regular archived file in the same MS.
The extractor and raw-MS checker now support this relationship, including payload
SHA/size and same-inode verification. Other unsupported links remain rejected.

The dedicated single-case `retry` helper freezes corrective attempts using the original
settings/SIF/selection and retained archive pair, preserves original evidence, and
automatically updates the case's current run identity. It cannot contact CASDA or
automatically redownload; retained paths/SHA are frozen and rechecked by the worker.
Repeated preparation and interrupted metadata updates are covered by regression.
The first corrected attempt subsequently reached the CLI failure described below.

The user's first retry passed extraction/preprocess, then create-model exited 2
because threshold "3.0" was not an integer CLI token. Default commands now use
the same production UCDs options and accepted-tool defaults; whole-valued frozen
overrides serialize as integers without changing stored numbers. Real Click
parsing compares all effective inputs for create/insert/subtract/extract.

Removed redundant default flags, tool savelogs/out-dir/name, the extra batch
insertion-model copy, duplicate child binds and duplicate config MODEL defaults.
Per-slot memory now uses the actual job slot count as UCDs does. Single-source
preservation and the confirmed catalogue/epoch/versioning design remain intact.
The existing retry helper also recognizes the diagnosed integer-threshold failure
and freezes the bounded retry2 with retained downloads/raw MS and prior evidence.
See docs/UCD_PARAMETER_AUDIT_zh.md and docs/STAGE3_zh.md section 6.2.

After these corrections, the user supplied `STAGE3 PASS: required steps, retained
files and fingerprints passed` for the single-source case. This accepts the
workflow/file-retention contract; DS/FITS scientific content is not inspected by
that checker. No further science-parameter changes are required for Stage 3B.

Stage 3B subsequently passed. Core normal-path implementation/site acceptance is
complete; remaining release checks reuse the existing products before extending
to actual parallel/recovery runs. See docs/STAGE4_PLAN_zh.md for scope and order.

Stage4A acceptance script and job instructions are ready (docs/STAGE4A_zh.md).
It inspects the existing products in the accepted SIF, exercises real CLI skip
on SQLite/metadata backups, and calls the production real-MS time fallback.
No production modules or scientific defaults changed. Offline tests cover
numerical differences, serialized-file differences, tampering, WCS/identity
mismatches, metadata immutability, read-only binds and accounting gaps.
The user supplied the actual Stage4A site result on 2026-10-15:
10 PASS, 0 FAIL, 1 REVIEW in stage4a/run-vsulw3ip (Slurm 18585962).
Cleanup, identity/SIF, both actual-CLI skip checks, real MS time fallback,
both Stage3 accounting checks and original-state protection passed.
The complete comparison is now supplied in tests/stage4a-comparison.json.
All twelve product fingerprints, DS axes/attributes, cutout WCS and nonfinite
patterns agree. Relative RMS differences are DS flux 0.208944%, Stokes I
1.103531%, and V 0.000061866%. DS/I differences cannot simply be dismissed as
last-bit rounding. The user explicitly accepted these recorded differences on
2026-10-15 and requested moving on without further numerical review.
tests/stage4a-acceptance.json records that decision; the raw REVIEW is unchanged.
No science parameters or thresholds changed. See docs/STAGE4A_REVIEW_zh.md.

Stage4B driver/instructions are ready (docs/STAGE4B_zh.md), reusing pull_products
for a fresh nine-file transfer, zero-repeat pull, local verification and two
remote receipt imports. It adds no science computation or corruption probe.
Seven local transport-boundary tests passed, including actual SQLite/receipt
import and shell argument handling. The user subsequently supplied the real Ada
Astro-environment result: all five checks PASS, STAGE4B PASS in
`/PATH/TO/ada/DStools_Auto_Processing_validation/stage4b/run-v16e3ccc`.
The first pull transferred nine files and the repeat transferred zero. Local
SHA/size, ledger, receipts and links passed; Ozstar imported both new receipts
and verified all three target transfers. This records the supplied console
evidence; the full report.json has not yet been copied locally.

After the user's request to reconsider Stage4C, comparison confirmed the same
lane/Slurm/recovery design, with adapted controller/worker/state implementations
for run/attempt identities and immutable versions. Fourteen existing relevant
offline tests were rerun successfully, covering real local thread concurrency,
resources, submission safeguards, continuation, bounded retry and old-success
protection. External scheduling/science boundaries remain mocked. These are a
subset of the 141 tests, not additional tests. Separate Stage4C site runs are
optional and skipped for this release; no extra science runs or fault injection
are required before normal use. See docs/STAGE4_PLAN_zh.md.

Current edition split: dedicated public checkout with generic environment settings and a fresh Git repository; private records and inherited history were retained separately.
The latest signed-CASDA-RA query regression adds seven passing checks. All 149 current tests passed after this split; time-estimation rules were not changed.
