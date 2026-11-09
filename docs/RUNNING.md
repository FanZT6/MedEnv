# Running benchmark inference

[Home](../README.md) · [Configuration examples](CONFIGURATION.md) · [Data format](../data/README.md)

## 1. Install the evaluation client

Use Python 3.11. Run these commands from the repository root after installing the PyTorch build appropriate for your hardware:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[retrieval,grounding]'
cp .env.example .env
```

The client calls model servers through HTTP. Doctor, Patient and Judge weights run in a separate serving environment. For a client using existing retrieval and grounding endpoints, `python -m pip install -e .` is sufficient.

## 2. Prepare the data

Obtain the prepared benchmark through an authorized data channel and set its root in `.env`:

```bash
MEDENV_BENCH_ROOT=/path/to/bench
```

The expected directory structure and JSON fields are described in [data/README.md](../data/README.md). To validate a complete benchmark:

```bash
python scripts/validate_data.py --bench-root /path/to/bench
```

To check the pipeline using the included synthetic cases:

```bash
bash scripts/run_all.sh --case_dir data/synthetic --dry_run
```

A dry run checks the selected cases and image paths without contacting model services. Synthetic cases demonstrate the interface and are not evaluation data.

## 3. Start the model services

Create a separate vLLM environment:

```bash
python -m venv .venv-vllm
source .venv-vllm/bin/activate
python -m pip install vllm==0.11.0
```

Set these values in `.env`:

```bash
DOCTOR_MODEL_PATH=/path/to/doctor-model
TOKENIZER_PATH=/path/to/doctor-model
PATIENT_MODEL_PATH=/path/to/patient-model
JUDGE_MODEL_PATH=/path/to/judge-model
DOCTOR_GPUS=0
PATIENT_GPUS=1
JUDGE_GPUS=2
```

Launch one service per terminal, with the vLLM environment activated:

```bash
bash scripts/start_vllm.sh doctor
bash scripts/start_vllm.sh patient
bash scripts/start_vllm.sh judge
```

| Service | Model name exposed by the API | Default endpoint |
|---|---|---|
| Doctor | `doctor_agent` | `http://127.0.0.1:30000/v1` |
| Patient / examiner | `patient_agent` | `http://127.0.0.1:30001/v1` |
| Judge | `judge_agent` | `http://127.0.0.1:30002/v1` |

The supplied model defaults use `Qwen/Qwen3-8B` for text inference. For image input, replace the Doctor with a vision-capable model and use its tokenizer. Set disjoint GPU groups; a comma-separated value such as `DOCTOR_GPUS=0,1` sets tensor parallelism to two. Adjust `MAX_MODEL_LEN` and `GPU_MEMORY_UTILIZATION` to fit your hardware.

You can use existing compatible servers instead of launching these processes. Set the corresponding model names, base URLs and keys in `.env`. Report mode uses the Doctor's `/v1/completions` endpoint with a locally formatted prompt; vision mode uses `/v1/chat/completions`. Patient and Judge use Chat Completions. See [model configuration](CONFIGURATION.md#models-endpoints-and-gpus).

## 4. Start retrieval and grounding

Return to the evaluation environment (`source .venv/bin/activate`).

**Retrieval:** prepare a corpus and matching E5/FAISS index as described in the [data guide](../data/README.md#retrieval-corpus), set `RETRIEVAL_INDEX` and `RETRIEVAL_CORPUS` in `.env`, then run:

```bash
bash scripts/start_retrieval.sh
```

The embedding model must match the one used to build the index; the default is `intfloat/e5-base-v2`. The service listens on port 8003. `RETRIEVAL_GPUS` is empty for CPU inference; set it to an unused GPU for CUDA embeddings. GPU FAISS is a separate optional server setting.

**Grounding:** place the `IDEA-Research/grounding-dino-base` model files at `GROUNDING_MODEL_PATH`. The server loads from that directory, then listens on port 30050:

```bash
bash scripts/start_grounding.sh
```

`GROUNDING_GPUS` is empty for CPU inference. Grounding is used by `cxr_grounding` in image-containing cohorts. For no-CXR evaluation it is not needed; for image-containing cohorts, `--disable_grounding` explicitly removes that tool. Likewise, `--disable_retrieval` removes retrieval and changes the evaluation setting.

Export the configuration and check service availability:

```bash
set -a
source .env
set +a
python scripts/check_services.py
```

Add `--no-grounding` and/or `--no-retrieval` to the health check when the corresponding tool is disabled. Judge is checked when `JUDGE_MODEL` is nonempty. Setting `JUDGE_MODEL=` selects string-containment scoring instead of LLM judging.

## 5. Run inference

Shell launchers load `.env` automatically. Direct `python -m medenv` commands require exporting the variables first, as shown above.

```bash
# CXR-report baseline: first 10 cases in with_img/ed_hosp.
bash scripts/run_bench.sh --cxr_mode reports --max_cases 10 --n_parallel_agents 1

# Multimodal Doctor: images plus report excerpts.
bash scripts/run_bench.sh --cxr_mode vision --max_cases 10 --n_parallel_agents 1

# No-CXR cohort; defaults to without_img/hosp_only.
bash scripts/run_bench.sh --no_cxr --max_cases 10 --n_parallel_agents 1

# Select a subset and repeat each case three times.
bash scripts/run_bench.sh --subdir without_img/ed_hosp --max_cases 0 --repeat_k 3

# Evaluate every case in every subset.
bash scripts/run_all.sh --n_parallel_agents 1
```

`--max_cases 0` selects all cases. Positive values select a sorted prefix. `--max_steps` defaults to 15; `--n_parallel_agents` defaults to 8. Start with concurrency 1 when checking a service deployment, then increase it to suit capacity.

CXR report mode returns report text. Vision mode supplies the images and a report excerpt of up to 200 characters, so it is not an image-only experiment. The server enforces the vision model's image-token limits. Keep the model, CXR mode, tools, Judge and inference budget fixed when comparing results.

## 6. Inspect the results

Each run creates `outputs/<run>/<subset>/`:

| File | Contents |
|---|---|
| `config.json` | Model, selected tools, inference settings and case counts |
| `metrics.json` | Aggregate metrics and per-case predictions, reference diagnoses and scores |
| `trajectories/*.json` | Doctor actions, tool responses and terminal information |

`accuracy_all_runs` counts correct runs over all runs; `accuracy_scored_runs` uses only runs with terminal scoring. `unscored_runs` reports missing terminal scores. With repeated runs, `empirical_pass_at_k` is the fraction of cases with at least one correct run. `mean_reward` is separate from accuracy and includes the configured process-reward term.

Use `--output_dir outputs/my-run` for a single evaluation. Do not pass a shared `--output_dir` to `run_all.sh`, because later subsets would overwrite the earlier metrics and configuration.

To view one trajectory:

```bash
python scripts/trajectory_to_html.py \
  outputs/my-run/trajectories/<trajectory-file>.json \
  --output outputs/my-run/trajectory.html
```
