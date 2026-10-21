# Running the benchmark

## 1. Evaluation environment

Use Python 3.11. Install a PyTorch build matching the host, then `python -m pip install -e .`. Optional services use `python -m pip install -e '.[retrieval,grounding]'`. The evaluation client does not run the Doctor/Patient/Judge weights itself. The source environment's observed core package versions are in `requirements/benchmark-tested.txt`; these are constraints, not a complete dependency lock.

vLLM should run in a separate environment. The source environment used vLLM 0.11.0; do not force its CUDA dependencies into a different evaluation environment. Model weights are external assets. Configure local model paths or model IDs in `.env`.

## 2. Data

Local data are under `data/bench` and `data/retrieval`. Public clones need an authorized local prepared benchmark, imported with `scripts/import_data.py`. Run `python scripts/validate_data.py --bench-root data/bench`. To recheck every copied byte, add `--checksums data/private/import-all.json` (or the manifest produced by a separate import).

`--dry_run` loads cases and validates image references without loading a tokenizer, connecting to APIs, or requiring ML dependencies. It validates only the selected cases; use `--max_cases 0` and each subset for a complete run.

## 3. Configure and launch services

Copy `.env.example` to `.env` and edit it. Shell launchers load this file; direct Python commands do not. To export it in the current shell:

```bash
set -a
source .env
set +a
```

Start one service per terminal, in the appropriate environment:

```bash
bash scripts/start_vllm.sh doctor
bash scripts/start_vllm.sh patient
bash scripts/start_vllm.sh judge
```

Default names and ports:

| Role | Served model | Endpoint | Default GPU |
|---|---|---|---|
| Doctor | `doctor_agent` | `http://127.0.0.1:30000/v1` | 0 |
| Patient | `patient_agent` | `http://127.0.0.1:30001/v1` | 1 |
| Judge | `judge_agent` | `http://127.0.0.1:30002/v1` | 2 |

The reference README's TP=4/TP=4/single-GPU commands overlap on GPU 1. Set disjoint groups. For example, a 9-GPU machine can use `DOCTOR_GPUS=0,1,2,3`, `PATIENT_GPUS=4,5,6,7`, `JUDGE_GPUS=8`. The launcher derives TP from each list. Three single-GPU defaults are configuration examples, not a guarantee that all models/context lengths fit your hardware. `MAX_MODEL_LEN` and `GPU_MEMORY_UTILIZATION` are configurable. Extra vLLM arguments can be passed after the role.

Keep the port settings (`DOCTOR_PORT`, etc.) and client URLs consistent if changing ports. Extra retrieval/grounding GPUs must also be disjoint; those services default to CPU.

### Retrieval

```bash
python -m pip install -e '.[retrieval]'
bash scripts/start_retrieval.sh
```

Uses the exact copied `corpus_passages.jsonl` and `e5_Flat.index`, E5 model `intfloat/e5-base-v2`, and port **8003**. The index and corpus row order must stay aligned. The default uses CPU FAISS. Setting `RETRIEVAL_GPUS` allows the embedding model to use CUDA; `--faiss_gpu` additionally requires a compatible GPU FAISS installation.

To rebuild from an independently obtained authorized guidelines corpus:

```bash
python scripts/convert_guidelines.py --input data/retrieval/open_guidelines.jsonl --output data/retrieval/guidelines.jsonl
python scripts/build_retrieval_index.py --input_jsonl data/retrieval/guidelines.jsonl --output_dir data/retrieval/guidelines_index --model_name intfloat/e5-base-v2
```

A rebuild with different corpus/model revisions is a different retrieval snapshot. Retain import hashes when reproducing the existing experiment.

### Grounding DINO

Install `.[grounding]`, obtain the `IDEA-Research/grounding-dino-base` model, and set `GROUNDING_MODEL_PATH` to its local directory. The service intentionally loads local files only.

```bash
bash scripts/start_grounding.sh
```

Default endpoint: `http://127.0.0.1:30050/ground`; health endpoint: `/health`. `GROUNDING_GPUS` defaults to empty (CPU). The grounding model is a generic detector inherited from the original workflow; clinical localization quality is not established by this package.

### Health check

```bash
set -a; source .env; set +a
python scripts/check_services.py
```

Use `--no-grounding` for a no-CXR run and `--no-retrieval` only if retrieval is disabled. Judge is checked when `JUDGE_MODEL` is set. Empty `JUDGE_MODEL` selects the original containment-rule reward; using a Judge selects LLM scoring. Both are recorded by the reward metadata.

## 4. Evaluation

```bash
bash scripts/run_bench.sh --max_cases 10 --n_parallel_agents 1
bash scripts/run_bench.sh --no_cxr --max_cases 10 --n_parallel_agents 1
bash scripts/run_all.sh --n_parallel_agents 1
```

The compatibility command also works:

```bash
python examples/MedGym/run_med_with_tool.py --model doctor_agent --tokenizer_path /path/to/Qwen3-8B --base_url http://127.0.0.1:30000/v1 --case_dir data/bench --judge_model judge_agent --judge_base_url http://127.0.0.1:30002/v1
```

`--subdir` selects any of the four subdirectories. `--max_cases <= 0` selects all, otherwise cases are sorted and truncated. `--repeat_k` repeats that selected cohort. The original loader consumes `ehr_*.json`; `osce_*.json` and the manifest are retained for provenance but are not Doctor inputs. Ground truth remains in environment task metadata for scoring and patient simulation; the initial Doctor message contains demographics and chief complaint.

Tool sets preserve the original ordering:

| Cohort | Tools |
|---|---|
| Without images | `ask_patient`, `diagnosis`, `retrieve`, `request_exam` |
| With images | `ask_patient`, `diagnosis`, `retrieve`, `cxr`, `request_exam`, `cxr_grounding` |

`--cxr_mode reports` is the Qwen3-8B text baseline. `--cxr_mode vision` sends image content through Chat Completions and requires a vision-capable Doctor. CXR tools expose report information in both modes: report mode includes the full `report_text`, while the multimodal agent sends the tool summary with a report excerpt (up to 200 characters) alongside images. This follows the original tool/agent formatting. Token budgets in vision mode count text; the model server enforces the actual image-token/context limit. The client does not estimate visual token usage.

Optional `--disable_retrieval` / `--disable_grounding` change the tool set and are ablations. `--max_steps` defaults to 15 and applies to both environment and engine. `--env_timeout` defaults to 300 seconds per environment operation. Default API endpoints are local. Keep credentialed patient payloads within your authorized computing environment.

## 5. Results

Each run uses a unique directory under `outputs/`; `--output_dir` overrides it for a single run. Do not reuse one explicit output directory across `run_all.sh` subsets, since `metrics.json` and `config.json` would be overwritten.

`metrics.json` reports actual terminal environment `is_correct` values. Unscored runs remain visible and count as failures in `accuracy_all_runs`. `accuracy_scored_runs` uses only scored runs. `empirical_pass_at_k` is the fraction of cases with at least one successful repeat, not the combinatorial unbiased estimator. `lenient_match_runs` is a separate string-matching diagnostic. Reward retains the original result-plus-process formula; process reward uses the original lexical overlap heuristic and is not a calibrated clinical confidence score.

Trajectory/config serialization redacts API-key fields. Trajectories, metrics and HTML exports still contain patient-derived text and belong in the ignored output directory.
