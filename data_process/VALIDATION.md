# Preprocessing release validation

Date: 2026-10-22. Scope: the report-only data_process package, with a follow-up
multi-ED candidate-routing correction. The three earlier runtime compatibility
edits are unchanged by this correction. This is not a clinical-data rebuild,
new benchmark evaluation or complete public-release approval.

## Source recovery

The five primary historical input script hashes were checked again against the
target server and match source_manifest.json. The January/February extractor,
batch builder, converter, sampler and importer are distinguished from newly
written prerequisites and safety wrappers.

Compared with the preceding packaged version, AST checks find 72 unchanged
extractor functions, 10 unchanged converter functions and 7 unchanged sampler
functions. Modified functions concern the CXR branch/assembly/CLI, preservation
of report time metadata and report-only copying. No new diagnosis, medication
cutoff, ED eligibility or ICD classification rule is claimed.

## Candidate-routing correction

The batch HOSP-only branch previously excluded only admissions with exactly one
ED row. This incorrectly admitted multi-ED admissions that the extractor already
rejects. HOSP-only now requires no linked ED row (NOT EXISTS). The ED+HOSP branch
still requires exactly one row. Ambiguous admissions are excluded before
extraction; no source records are deleted. Unrelated extraction failures still
return a nonzero exit rather than being silently skipped.

A read-only, full-candidate comparison on the existing clinical database found:

- Before: 497,038 candidates; after: 496,593.
- Exactly 445 removed, all and only previously selected multi-ED admissions.
- No added or duplicate candidates; retained candidate order unchanged.
- 178,030 unique-ED candidates and 318,563 no-ED candidates retained.
- Database size and modification time unchanged. No real case text or IDs were
  emitted, and no real benchmark was regenerated.

## Test scope

Synthetic-only tests cover:

- Correct leading-zero/fraction timestamp parsing; invalid timestamps fail.
- CXR metadata and report JSON/native TXT indexing without any image files.
- Duplicate/conflicting/missing report handling and subject/study linkage.
- Dry run, existing-schema refusal, transaction rollback and concurrent-output
  protection; known incompatible legacy CXR indices are refused.
- Preserved EMAR priority/pharmacy fallback, ED linkage and window boundaries.
- Multi-ED rejection before batching, alternate ED schema, unlinked ED rows,
  absent ED module, preserved ordering and uninterrupted downstream processing.
- Complete batch -> EHR -> deterministic non-destructive benchmark sampling.
- Report provenance/time fields retained; no image paths emitted or copied.
- Report-only runtime behavior without dicoms/images, plus retained vision mode.
- CLI dry run without images; no model, GPU, patient agent, judge or API calls.

Local runtime: Python 3.14.7 / DuckDB 1.5.6: 48 tests passed.
Target runtime: Python 3.10.19 / DuckDB 1.4.3: 48 tests passed in the isolated
release candidate, including the current loader and CLI dry-run integration.
Set MEDENV_TEST_REPO_ROOT to the actual MedEnv checkout for full CLI integration;
the README command does so explicitly. No integration test was skipped.

An independent raw CSV.GZ / NOTE ZIP / ED / CXR-report test included five invented
admissions, one with ambiguous ED linkage. Import, report indexing, preflight,
OSCE extraction, EHR conversion, sampling and final-benchmark validation finished
successfully. The final four subsets each contained one valid case (4 OSCE + 4
EHR files), excluded the ambiguous admission and had zero image references.
All four report-mode runtime dry runs passed; vision mode rejected report-only
input as expected. Original synthetic raw-file hashes were unchanged.

The report-tool unit tests stub only the Tool/ToolOutput base classes to avoid
loading model dependencies. Loader/CLI integration uses invented records and the
current MedEnv helpers. No real report text or patient IDs are test fixtures.

## Other release findings remain open

This targeted fix does not resolve every finding from the broader release review:

- Header-only clinical CSV time columns can be inferred as VARCHAR; schema
  preflight currently checks required columns but not their types.
- The sampler does not yet reject all OSCE/EHR identity mismatches, and the parent
  validator does not enforce nonempty reports in the legacy with_img group.
- A parent-repository report-tool test still expects the obsolete images output.
  Its offline suite remains 15 passed / 1 error out of 16 tests; preprocessing-test
  success is not a substitute for parent-suite success.
- Parent CI and private-artifact ignore rules still need integration review.

These issues are documented rather than silently described as fixed. The
positive synthetic roundtrip and candidate comparison establish the stated
checks, not that every possible raw input or the entire repository is ready to
publish without further work.

## Release checks

check_release.py checks an explicit 19-file allowlist and common personal-path,
private-address, private-key and API-token patterns. It prints filenames and
hashes, never matching secret values. Python caches are runtime artifacts and are
excluded from archives. Configuration uses placeholders only.

This is a heuristic source-folder check, not a guarantee about arbitrary secrets,
the surrounding project, Git history, ignored data or third-party rights.
No source database, real report corpus, selection manifest or case JSON is
included in the code package. Paths made portable are not patient anonymization.

## Scientific limitations retained

CXR filtering still uses study acquisition time, not report signing/completion
time. Ordinary radiology filtering retains charttime, not storetime. The code now
preserves the distinction rather than inventing missing report times.

The historical prepared index had demonstrated timestamp parsing discrepancies.
The new report-only initializer uses corrected parsing in a separate DB; it does
not modify that index or already extracted cases. New report grouping and ordering
are documented, so this is not an exact frozen-benchmark reproduction claim.

Full-population rebuilding, original report-corpus availability, prospective
information safety, a new patient-disjoint split, model scores and full-repository
public-release approval are not established by these tests.
