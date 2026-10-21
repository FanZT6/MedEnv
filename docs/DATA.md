# Data access, scope and provenance

## Benchmark snapshot

The local packaging source is the prepared `osce_data/bench` referenced by MedGym's `README_TEST_OSCE.md`. It contains 5,405 cases across all four subsets, 10,810 EHR/OSCE JSON files, 883 JPEG files and one `manifest.jsonl`. The original benchmark payload occupies 1,844,690,746 bytes (about 1.72 GiB). Public aggregate statistics are in `data/dataset_summary.json`; per-file names and SHA-256 hashes are kept locally under `data/private/`.

```text
bench/
  manifest.jsonl
  with_img/{ed_hosp,hosp_only}/<case_id>/
    ehr_<case_id>.json
    osce_<case_id>.json
    img/...
  without_img/{ed_hosp,hosp_only}/<case_id>/
    ehr_<case_id>.json
    osce_<case_id>.json
```

The archive retains all 883 image files. The flattened EHR inputs reference 869 distinct images; the remaining 14 images are retained byte-for-byte with the original export. No referenced image is missing.

The runtime reads the wrapper's `ehr` dictionary and optional `knowledge` list. Relevant fields include `Patient_info`, `History`, examination and test results, `CXR`, and `Final_Result`. The CXR `dicoms[].jpg_path` values resolve relative to each case folder. The richer OSCE files and original manifest are copied byte-for-byte; historical path strings inside the manifest are provenance, not runtime dependencies.

## Access and redistribution

MIMIC data require the appropriate PhysioNet credentialing, training and data-use agreements. The [PhysioNet Credentialed Health Data License](https://physionet.org/content/mimic-cxr-jpg/view-license/2.1.0/) restricts sharing access to credentialed data. Code publication does not make patient records, derived case text, admission identifiers, radiographs or generated trajectories public-domain material. Therefore `data/bench/`, manifests identifying individual cases, and outputs are excluded from the public release.

Users must obtain access through the official [MIMIC-IV](https://physionet.org/content/mimiciv/) and [MIMIC-CXR-JPG](https://physionet.org/content/mimic-cxr-jpg/) project pages and acquire the prepared OSCE export through an authorized channel. This repository packages an existing prepared export; it does **not** claim to regenerate the 5,405-case selection from raw MIMIC. The original raw-to-OSCE cohort construction pipeline, extraction revision and full source dataset versions were not supplied by the reference run instructions. Reproducing that cohort from raw tables requires those upstream details.

The public synthetic fixtures are written specifically for interface tests. They contain no real patient identifiers, clinical records or radiographs; the images are tiny generated white test patterns. They cannot support scientific benchmark scores.

## Retrieval assets

The existing run configuration uses E5/FAISS over a local clinical-guidelines corpus. The importer copies:

- `guidelines_index/e5_Flat.index` (1,035,936,813 bytes)
- `guidelines_index/corpus_passages.jsonl` (475,101,955 bytes)
- `guidelines_index/docids.jsonl` (27,459,732 bytes)
- `guidelines.jsonl` (463,125,688 bytes, when present)
- `open_guidelines.jsonl` (877,724,423 bytes, when present)

The source dataset card identifies the EPFL Clinical Guidelines/Meditron corpus and source-specific redistribution terms. See the [upstream dataset card](https://huggingface.co/datasets/epfl-llm/guidelines). The local `LICENSE` beside these assets is empty, so it is not evidence of an unrestricted blanket license. Exact corpus preprocessing provenance is not fully recorded. These assets are retained locally and excluded from public Git, wheels, source distributions and release ZIPs. Public users may import their authorized copy or obtain permitted upstream material and rebuild with the included conversion/index scripts.

The BM25 index, unrelated training datasets, historical patient trajectories, model weights and original virtual environment are not needed by the documented bench execution and are not copied.

## Integrity

`scripts/import_data.py` does not overwrite differing destination files. It copies materialized files, compares source/destination SHA-256 digests, and writes a private manifest. Repeat imports skip only byte-identical files. `scripts/validate_data.py` checks all four subsets, JSON pairs, final labels and image references; `--checksums` verifies the imported bytes again. Copying is local; these tools do not upload datasets.
