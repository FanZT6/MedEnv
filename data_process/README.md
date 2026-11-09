# MedEnv diagnosis preprocessing — report-only release

This folder organizes the recovered January/February diagnosis pipeline.
**CXR input means report text, not images.** No JPG/DICOM files, image labels,
image-copying tools or image-file index are required. No model/API is called.

Code release only: obtain the clinical data under your own access permissions.
**Generated OSCE/EHR cases, source IDs, timestamps, report text, knowledge corpora
and selection manifests are private data, not files to publish.**
Portable paths are not patient de-identification.

## Pipeline and recovered scripts

| Stage | Script | Historical source / role |
|---|---|---|
| Optional HOSP/NOTE/ED import | import_hosp_note.py | Adapted early HOSP/NOTE importer; creates a new DB |
| CXR metadata + report index | initialize_cxr_index.py, cxr_reports.py | New prerequisite adapter; not recovered historical SQL |
| Read-only preflight | check_database.py | Required schemas and report-index format |
| Diagnosis case extraction | osce_extractor.py | February 15 combined ED/HOSP extractor |
| Batch extraction | build_osce_dataset.py | Historical batch builder |
| Runtime EHR conversion | convert_osce_to_ehr.py | February 12 conversion |
| ICD-stratified selection | sample_benchmark.py | Historical stratification/seeded sampling; copies JSON, never moves inputs |
| Release validation | check_release.py, tests/ | Code-only allowlist, path/secret heuristics and invented fixtures |

Historical filenames and verified source hashes are in source_manifest.json.
Older duplicate extractors, destructive data_split.py, image refilling/counting
helpers, patient-agent/training code and one-off later dataset repacking are not
part of this preprocessing release. Original files are preserved separately.

## Quick start

Python 3.10+ for these scripts; the parent agent may require a newer version.
From the MedEnv repository root:

~~~bash
python -m pip install -r data_process/requirements.txt
MEDENV_TEST_REPO_ROOT="$PWD" python -m unittest discover -s data_process/tests -v
python data_process/check_release.py
~~~

All real inputs and generated outputs should live outside the checkout.
Copy config.example.sh outside the repository, replace the placeholders, then:

~~~bash
source /path/to/private/medenv-data-config.sh
bash data_process/run_pipeline.sh
~~~

Before using the wrapper, prepare the two databases below. The wrapper never
initializes or overwrites databases automatically. It requires a NEW output root.

### 1. Clinical database

Reuse an authorized existing HOSP/NOTE/ED DuckDB without modifying it, or create
a new one:

~~~bash
python data_process/import_hosp_note.py \
  --db /path/to/private/clinical.duckdb \
  --hosp-dir /path/to/authorized/mimic-iv/hosp \
  --note-dir /path/to/authorized/mimic-iv-note/note \
  --ed-dir /path/to/authorized/mimic-iv-ed/ed
~~~

A NOTE ZIP can replace --note-dir via --note-zip. The importer streams ZIP
members, preserves ICD code leading zeros and refuses existing output databases.

Required tables: hosp.admissions, patients, diagnoses_icd, d_icd_diagnoses and
emar or pharmacy. Optional clinical fields use labevents/d_labitems,
microbiologyevents, transfers, services, omr, orders, discharge/radiology notes
and ED tables. Missing optional tables do not establish clinical completeness.
ED schemas mimiciv_ed and ed are supported.

### 2. Separate CXR report index

Create a NEW report database from authorized metadata plus report text:

~~~bash
python data_process/initialize_cxr_index.py \
  --db /path/to/private/cxr-reports.duckdb \
  --metadata-csv /path/to/authorized/mimic-cxr-metadata.csv.gz \
  --reports-json /path/to/authorized/cxr-reports.json \
  --dry-run
# Review the aggregate audit, then repeat without --dry-run.
~~~

JSON accepts a list, or a dictionary of lists, with study_id and report fields.
A report may be a string or list of strings. Identical duplicates are counted
and collapsed; conflicting duplicates, invalid identifiers and subject mismatches
fail. Partial report coverage is explicitly counted; absent/empty reports are not
fabricated. The report JSON is a user-supplied input, not bundled with this code.

Alternatively use --reports-dir /path/to/authorized/cxr/files. Only report TXT
files at pXX/p<subject>/s<study>.txt are read. No image files are accessed.
This native-TXT adapter is new; using a different report corpus does not promise
the same historical cases or text.

The index contains cxr.metadata_raw, metadata_ts2, reports and index_audit.
Metadata preserves the original date/time strings and validates IDs and
subject/study linkage. DICOM IDs here only deduplicate metadata rows; there are no
image paths or pixel inputs. Source hashes and aggregate coverage are recorded.

StudyTime uses numeric HHMMSS[.fraction]: 73005.1 -> 07:30:05.100000.
Leading zeros are restored; decimal fractions are retained with half-even
microsecond rounding. Missing/invalid times fail, never become midnight.
Strict full-precision DICOM TM can be requested with --time-format dicom-tm.

Existing cxr schemas are refused, including in dry-run mode. Adding an index to
a clinical DB with no cxr schema is supported transactionally, but a separate DB
is recommended to leave the old clinical DB unchanged. --cxr-db must point to this
validated report-only index: the known incompatible historical image index is
deliberately rejected.

### 3. Preflight and extract

~~~bash
python data_process/check_database.py \
  --db "$MEDENV_DB" --cxr-db "$MEDENV_CXR_DB" --require-ed --require-notes

python data_process/build_osce_dataset.py \
  --db "$MEDENV_DB" --cxr-db "$MEDENV_CXR_DB" \
  --out "$MEDENV_OSCE_ROOT" --workers 4
~~~

For a smoke test add --limit 10 --workers 1 and use a new private output.
Optional --knowledge_json attaches the existing authorized ICD knowledge corpus.
To intentionally omit the CXR-specific route use --without-cxr-reports instead
of --cxr-db. Ordinary NOTE radiology records remain available.

Batch outputs retain the legacy directory names with_img/ed_hosp,
with_img/hosp_only, without_img/ed_hosp and without_img/hosp_only for loader
compatibility. **In this release with_img means a nonempty CXR report, not
presence of image files.** No img directory is created. Redaction-empty reports
are not counted. This grouping is not identical to the historical image grouping.

Batch resume skips existing OSCE files by existence, not configuration hash.
Use a fresh root after any input/configuration change. Skip/failure messages and
hadmid_done.log are private artifacts; a nonzero exit must be investigated.

Candidate routing follows the extractor's existing ED-linkage requirement:
exactly one linked ED row is ED+HOSP; no linked ED rows is HOSP-only. Multiple
linked ED rows are excluded from both groups before extraction, not relabeled as
HOSP-only. Source rows are never deleted, and genuine extraction failures still
produce a nonzero exit. This correction preserves medication cutoffs and the
relative order of retained candidates; it does not rewrite existing benchmarks.

### 4. Convert and sample

~~~bash
python data_process/convert_osce_to_ehr.py \
  --input_dir "$MEDENV_OSCE_ROOT" --recursive --ehr_alongside --workers 4

python data_process/sample_benchmark.py \
  --input-root "$MEDENV_OSCE_ROOT" --output-root "$MEDENV_BENCH_ROOT" \
  --per-category 150 --seed 42 --dry-run
# Review the counts, then repeat without --dry-run.

python scripts/validate_data.py --bench-root "$MEDENV_BENCH_ROOT" --cxr-mode reports
~~~

Conversion retains CXR study_id, study_ts, report_source and report text.
Unknown report_charttime/report_storetime remain null. Ordinary NOTE report
note_id/charttime/storetime are retained. Image references are discarded.

Sampling keeps the historical ICD-9/ICD-10 strata and seeded algorithm, copies
only the OSCE/EHR JSON pair, and refuses legacy image-backed JSON.
It never copies arbitrary case-folder contents or moves original cases.
The private manifest records destination-relative paths. Input/output roots
must be separate and non-nested; existing outputs are refused.
This is NOT a patient-disjoint train/test splitter. A seed alone cannot reproduce
a frozen benchmark without the exact source population and selection manifest.

The existing parent validator requires all four subsets to be nonempty.
A deliberately CXR-free or very small sample may not meet that benchmark-specific
requirement even when its individual records are valid.

## Time semantics and inherited scientific limitations

- The clinical cutoff is the first post-admission EMAR timestamp; pharmacy is a
  fallback when no qualifying EMAR time exists, not a joint minimum.
- The legacy diagnostic_hours argument does NOT enforce a fixed two-hour window.
- Labs and imaging/CXR use [ED arrival when linked, otherwise admission, t0_med).
- CXR reports are linked to studies selected by **study acquisition time**.
  The newest 10 qualifying report-bearing studies are retained by default and
  returned chronologically, so the last is latest. Multiple metadata views do not
  multiply the report. Acquisition time is NOT report completion/signing time.
- Ordinary NOTE radiology selection retains the historical charttime filter.
  storetime is preserved but is not newly substituted as its eligibility boundary.
  Thus this package does not establish prospective report availability.
- The inherited ED extractor reads full ED-stay records; history sections are
  parsed retrospectively from discharge notes. Diagnosis-title redaction is a
  heuristic, not proof of no leakage. These non-CXR clinical rules are retained.
- Required CXR extraction errors now fail explicitly instead of silently turning
  into missing CXR. Empty/unavailable report bodies remain explicit.
- The corrected timestamp parser and report-only grouping can change case
  membership compared with the old image-based extraction. No old index, extracted
  case or historical score was rewritten. This release is not a bit-identical
  reproduction claim for the previous benchmark.

Time-field references:
[MIMIC-CXR-JPG metadata](https://physionet.org/content/mimic-cxr-jpg/2.0.0/) and
[MIMIC-IV radiology notes](https://mimic.mit.edu/docs/iv/modules/note/radiology.html).

## Open-source boundary

Only this code, synthetic tests, placeholder configuration and source provenance
belong in Git. No clinical cases, real metadata/report fixtures, images,
credentials, database files, knowledge files or real selected-case lists are
included. Check the Git staged diff and Git history for the WHOLE MedEnv project
before publication; check_release.py checks this folder only and is heuristic.

The parent code LICENSE/NOTICE applies where appropriate. It does not grant
redistribution rights to clinical data, reports, knowledge corpora or weights.
The runtime report-mode compatibility changes are limited to cxr_tool.py,
medenv/cli.py and scripts/validate_data.py; the legacy vision branch is retained.
See VALIDATION.md for the actual test scope.
