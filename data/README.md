# Benchmark data

[Inference guide](../docs/RUNNING.md) · [Configuration](../docs/CONFIGURATION.md)

## Access

Evaluation uses a prepared OSCE benchmark based on MIMIC records and chest X-rays. Obtain the required access through [MIMIC-IV](https://physionet.org/content/mimiciv/) and the relevant NOTE, ED and CXR datasets, including [MIMIC-CXR-JPG](https://physionet.org/content/mimic-cxr-jpg/) when using images. This repository publicly documents and releases the MIMIC processing code in [`data_process/`](../data_process/README.md), including import, extraction, conversion and sampling; it does not distribute clinical cases or offer a public data download. The released CXR preprocessing is report-only and does not recreate the historical image-backed benchmark exactly. Prepare inputs privately with your own authorized access, or obtain the prepared benchmark through an authorized data channel.

Set `MEDENV_BENCH_ROOT` in `.env` to the prepared benchmark directory. It can be outside the repository. `data/bench/`, retrieval assets, model weights and generated outputs are Git-ignored. MIMIC-derived data and trajectories remain subject to the applicable data-use agreements.

Keep `README.md` and `synthetic/` when cleaning this directory. Private `bench/` data can be moved outside the repository or deleted if no longer needed; evaluation then requires an external benchmark path. Removing `retrieval/` requires replacing or rebuilding the corpus/index and updating the retrieval configuration. Do not include either directory in a public archive merely because Git ignores it.

## Input structure

```text
bench/
├── with_img/
│   ├── ed_hosp/<case_id>/
│   │   ├── ehr_<case_id>.json
│   │   └── img/<image>.jpg
│   └── hosp_only/<case_id>/...
└── without_img/
    ├── ed_hosp/<case_id>/ehr_<case_id>.json
    └── hosp_only/<case_id>/ehr_<case_id>.json
```

The loader reads `ehr_*.json`. Each file contains an `ehr` object and an optional `knowledge` list:

```json
{
  "ehr": {
    "Patient_info": {"age": 40, "gender": "synthetic"},
    "History": {"Chief_Complaint": "A fictional symptom."},
    "Physical_Examination_Findings": "Synthetic examination findings.",
    "Test_Results-Labs": [],
    "Test_Results-Microbiology": [],
    "Test_Results-Imaging": [],
    "CXR": [],
    "Final_Result": "Synthetic condition A"
  },
  "knowledge": []
}
```

`Final_Result` supplies the reference diagnosis for scoring. The Doctor's opening message uses demographics and chief complaint. Other evidence is accessed through tools.

For a case with CXR, each `CXR` study can contain `report_text` and `dicoms`; each DICOM entry supplies `jpg_path`, relative to the case directory, and an optional `view_position`:

```json
{"report_text": "Synthetic report.", "dicoms": [{"jpg_path": "img/example.jpg", "view_position": "AP"}]}
```

Check all four subsets before evaluation:

```bash
python scripts/validate_data.py --bench-root /path/to/bench
```

## Synthetic cases

`synthetic/` contains one fictional case per subset and two test-pattern images. They exercise loading, image transport and tool interfaces without patient data. They are not suitable for reporting diagnostic performance.

```bash
bash scripts/run_all.sh --case_dir data/synthetic --dry_run
```

## Retrieval corpus

The retrieval service uses an E5/FAISS index and a JSONL corpus whose row order matches the index. Use an appropriately licensed clinical corpus, such as permitted material from the [clinical guidelines dataset](https://huggingface.co/datasets/epfl-llm/guidelines), and follow its source-specific terms.

For a guidelines-format JSONL file, convert and build the index:

```bash
python scripts/convert_guidelines.py \
  --input /path/to/open_guidelines.jsonl \
  --output data/retrieval/guidelines.jsonl
python scripts/build_retrieval_index.py \
  --input_jsonl data/retrieval/guidelines.jsonl \
  --output_dir data/retrieval/guidelines_index \
  --model_name intfloat/e5-base-v2
```

This produces `e5_Flat.index`, `corpus_passages.jsonl` and `docids.jsonl`. The service uses the index and corpus; `docids.jsonl` maps passages back to source documents. Point `.env` to the index/corpus and use the same embedding model when starting the service:

```bash
RETRIEVAL_INDEX=data/retrieval/guidelines_index/e5_Flat.index
RETRIEVAL_CORPUS=data/retrieval/guidelines_index/corpus_passages.jsonl
RETRIEVER_MODEL=intfloat/e5-base-v2
```
