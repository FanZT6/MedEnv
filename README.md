<div align="center">

# MedEnv

### Scaling Multimodal Virtual Medical Environments for Long-Horizon Diagnosis

**MICCAI 2026**

[Paper](https://papers.miccai.org/miccai-2026/paper/5084_paper.pdf) · [中文说明](README_zh.md) · [Inference Guide](docs/RUNNING.md) · [Configuration](docs/CONFIGURATION.md)

**EHR-grounded · Multimodal · Interactive · Scalable**

</div>

## Table of Contents

- [Overview](#overview)
  - [Environment & Interaction](#environment--interaction)
  - [Agent Training & Results](#agent-training--results)
- [Getting Started](#getting-started)
  - [Installation](#installation)
  - [Data Preparation](#data-preparation)
  - [Quickstart](#quickstart)
- [Repository Structure](#repository-structure)
- [Public release and MIMIC data](#public-release-and-mimic-data)
- [Citation](#citation)
- [License](#license)

## Overview

**MedEnv** turns real-world clinical records into interactive environments for long-horizon diagnosis. Rather than answering a fixed medical question, a doctor agent actively interviews patients, orders examinations, retrieves guidelines and inspects chest X-rays before making a diagnosis. Patient and examiner simulators combine patient-specific EHR evidence with disease-level knowledge to support consistent multi-turn interactions. The paper uses this environment to train **Diagnosis-R1**, a diagnostic agent that learns informative evidence acquisition through confidence-based process supervision.

### Environment & Interaction

<p align="center">
  <img src="https://github.com/user-attachments/assets/a53a72ce-aa5f-4a99-ab8c-03c7614bd01b" width="680" alt="MedEnv environment connecting a doctor agent to patient and examiner simulators, guideline retrieval and vision tools.">
</p>

| Component | Role |
|---|---|
| Patient simulator | Answers questions about history, symptoms and personal observations. |
| Examiner simulator | Returns physical examination, laboratory and imaging evidence. |
| Retrieval & vision tools | Support guideline lookup, CXR inspection and visual grounding. |
| Evidence routing | Checks the case EHR first, then disease knowledge; falls back when neither can answer. |

![Dataset construction and multi-simulator interaction](https://github.com/user-attachments/assets/042c5728-1ccf-4ddd-b2b8-5daff3e4beca)

*From EHRs to role-specific evidence and multi-turn tool calls. The supplied illustration contains earlier dataset counts; the poster reports **496K+ trajectories** and **about 4.9K disease-level knowledge entries**.*

- **Sources:** MIMIC-IV, MIMIC-IV-Note and available paired MIMIC-CXR studies.
- **Case construction:** organize diagnostic-phase evidence, remove explicit diagnosis statements from early notes and retain diagnosis labels for evaluation.
- **Evaluation:** final diagnostic accuracy, efficient turns and average interaction length; trajectories expose repeated questions and premature diagnostic closure.

### Agent Training & Results

The paper trains Qwen3-VL-8B-Instruct with approximately 3K expert trajectories for cold start, then applies GRPO with outcome rewards and dense process rewards tied to confidence gains in the reference diagnosis. Eight stochastic rollouts filter out uniformly correct or incorrect cases.

**Diagnostic accuracy (%) reported in the poster:**

| Model | No image | Image |
|---|---:|---:|
| Qwen3-VL-8B | 10.28 | 12.16 |
| MedGemma-27B | 27.75 | 28.95 |
| Gemini-3-Flash | 34.15 | 33.97 |
| GPT 5 | 34.84 | 35.86 |
| Qwen3.5-397B-A17B | 35.99 | 37.56 |
| **Diagnosis-R1** | **41.95** | **41.15** |

**Training ablation:** base → SFT → outcome-only RL → Diagnosis-R1 improves no-image accuracy from **10.28 → 27.54 → 31.51 → 41.95%**. Scaling gains broadly saturate near 50K trajectories; image input does not consistently improve accuracy across models.

## Getting Started

### Installation

Use **Python 3.11** and a PyTorch build appropriate for your hardware.

```bash
git clone https://github.com/FanZT6/MedEnv.git
cd MedEnv
python -m venv .venv
source .venv/bin/activate
python -m pip install -e .
cp .env.example .env
```

For local retrieval and grounding servers, install the optional dependencies with `python -m pip install -e '.[retrieval,grounding]'`. Model serving uses a [separate vLLM environment](docs/RUNNING.md#3-start-the-model-services).

### Data Preparation

1. Obtain authorized MIMIC access and prepare cases with the [processing pipeline](data_process/README.md).
2. Set `MEDENV_BENCH_ROOT=/path/to/bench` in `.env`.
3. Check the [input format](data/README.md#input-structure) and validate your cases:

```bash
python scripts/validate_data.py --bench-root /path/to/bench
```

The historical evaluation benchmark contains **5,405 cases**:

| Subset | Cases |
|---|---:|
| `with_img/ed_hosp` | 401 |
| `with_img/hosp_only` | 17 |
| `without_img/ed_hosp` | 1,517 |
| `without_img/hosp_only` | 3,470 |

The release includes **four fictional cases** for interface checks. Real benchmark data are not bundled; the released report-only CXR processing does not reproduce the historical image-backed benchmark exactly.

### Quickstart

**Check the pipeline without model services:**

```bash
bash scripts/run_all.sh --case_dir data/synthetic --dry_run
```

**Run evaluation:** configure model paths, tokenizer and endpoints in `.env`, then follow the [service setup](docs/RUNNING.md#3-start-the-model-services) and [retrieval / grounding setup](docs/RUNNING.md#4-start-retrieval-and-grounding).

```bash
# CXR-report baseline: 10 cases from with_img/ed_hosp.
bash scripts/run_bench.sh --cxr_mode reports --max_cases 10 --n_parallel_agents 1

# No-CXR cohort: without_img/hosp_only.
bash scripts/run_bench.sh --no_cxr --max_cases 10 --n_parallel_agents 1

# Every case in all four subsets.
bash scripts/run_all.sh --n_parallel_agents 1
```

| Setting | Usage |
|---|---|
| Multimodal inference | Use a vision-capable Doctor and matching tokenizer; pass `--cxr_mode vision`. Both CXR modes provide report information; vision adds images. |
| Sample budget | `--max_cases 0` selects a whole subset; `--repeat_k` controls repeated runs. |
| Saved results | `outputs/<run>/<subset>/`: `config.json`, `metrics.json` and `trajectories/`. |
| Customization | [Models, prompts, tools and scoring](docs/CONFIGURATION.md) |

## Repository Structure

```text
medenv/        Benchmark CLI, case loading and metrics
rllm/          Doctor agent, environment, tools and scoring
services/      Retrieval and visual-grounding servers
scripts/       Service launchers, evaluation and visualization
data_process/  MIMIC processing code and documentation
data/          Input format and fictional test cases
docs/          Inference guides, configuration and figures
tests/         Offline tests
```

## Public release and MIMIC data

| Publicly released | Not distributed as benchmark data |
|---|---|
| Processing code: database import, diagnosis-window extraction, OSCE construction, EHR conversion and ICD-stratified sampling | Raw MIMIC tables, clinical notes, reports and images |
| Inference / evaluation code and fictional software fixtures | Patient/admission/study identifiers, processed cases, selection manifests and case-level trajectories |

- **Access:** obtain your own authorization for [MIMIC-IV](https://physionet.org/content/mimiciv/), [MIMIC-IV-Note](https://physionet.org/content/mimic-iv-note/), [MIMIC-IV-ED](https://physionet.org/content/mimic-iv-ed/) and [MIMIC-CXR](https://physionet.org/content/mimic-cxr/) as required. Use [MIMIC-CXR-JPG](https://physionet.org/content/mimic-cxr-jpg/) when applicable.
- **Data use:** MIMIC-derived records and trajectories remain subject to the applicable access conditions and data-use agreements. De-identification or JSON conversion does not grant redistribution rights; Apache-2.0 covers code only.
- **Local assets:** keep real inputs, databases and derived outputs outside the checkout. Git ignores local benchmark, retrieval and output directories, but does **not** filter manual ZIPs or uploads. Preserve `data/README.md` and `data/synthetic/`; `outputs/` may be deleted when run records are no longer needed.
- **Details:** [Processing pipeline](data_process/README.md) · [Data guide](data/README.md) · [Release audit](docs/DATA_PRIVACY_AUDIT_zh.md).

## Citation

```bibtex
@inproceedings{FanZhi_MedEnv_MICCAI2026,
  title     = {MedEnv: Scaling Multimodal Virtual Medical Environments for Long-Horizon Diagnosis},
  author    = {Fan, Zhiting and Ren, Xuxiang and Wang, Yuan and Chen, Ruizhe and
               Meng, Zijie and Hu, Keli and Liu, Jiaxiang and Wu, Jian and
               Zhai, Weiqi and Xu, Hongxia and Liu, Zuozhu},
  booktitle = {Medical Image Computing and Computer Assisted Intervention -- MICCAI 2026},
  year      = {2026}
}
```

## License

Code: [Apache-2.0](LICENSE). Attribution: [NOTICE](NOTICE). Datasets and model weights retain their respective access conditions and licenses.
