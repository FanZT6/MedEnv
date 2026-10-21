# MedEnv

An OSCE benchmark for medical agents, packaged from the MedGym evaluation workflow. It includes a simulated patient, examination requests, diagnosis scoring, clinical retrieval, and optional chest X-ray tools.

[中文说明](README_zh.md) · [Run guide](docs/RUNNING.md) · [Data access](docs/DATA.md) · [Changes from MedGym](docs/CHANGES.md)

## Included

- All four benchmark subsets supported by a single CLI, plus compatibility entry points under `examples/MedGym/`.
- A vendored evaluation-only `rllm` runtime; no dependency on the original checkout or RL training stack.
- Doctor/Patient/Judge launchers, E5/FAISS retrieval server and index builder, Grounding DINO server, and an HTML trajectory viewer.
- Synthetic fixtures, offline tests, GitHub Actions, package metadata, and a public-release scanner.

The local distribution can contain the complete authorized benchmark under `data/bench` and retrieval assets under `data/retrieval`. **These directories are excluded from Git and public packages.** Public clones include synthetic examples and data import tools. Real MIMIC-derived records and images require credentialed access; the software license does not grant data redistribution rights. See [data access and provenance](docs/DATA.md).

| Subset | Cases in the local source snapshot |
|---|---:|
| `with_img/ed_hosp` | 401 |
| `with_img/hosp_only` | 17 |
| `without_img/hosp_only` | 3,470 |
| `without_img/ed_hosp` | 1,517 |
| **Total** | **5,405** |

There are 883 source images. The reference README selected only `with_img/ed_hosp` and `without_img/hosp_only`; both remain the defaults for their respective modes.

## Quick start

Python 3.11 is the reference environment. Install the appropriate PyTorch build for your machine, then:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e .
cp .env.example .env

# No model, network connection, GPU, or restricted data required:
python -m medenv --case_dir data/synthetic --dry_run
python scripts/validate_data.py --bench-root data/synthetic
python -m unittest discover -s tests -v
```

Import an already-authorized local copy of the prepared OSCE benchmark:

```bash
python scripts/import_data.py --bench-source /path/to/osce_data/bench
python scripts/validate_data.py --bench-root data/bench
```

For model evaluation, configure `.env`, start the services as described in [the run guide](docs/RUNNING.md), then:

```bash
# Qwen3-8B text baseline on the image-containing cohort; CXR tool returns reports.
bash scripts/run_bench.sh --max_cases 10 --cxr_mode reports

# Cohort without CXR
bash scripts/run_bench.sh --no_cxr --max_cases 10

# Every case in every subset
bash scripts/run_all.sh --n_parallel_agents 1
```

`--max_cases 0` means all cases; the CLI default is 10. `--repeat_k` repeats each case. Results are saved under `outputs/<run>/<subset>/` as trajectories, redacted configuration, and metrics.

**Vision evaluation:** use a vision-capable Doctor and its tokenizer with `--cxr_mode vision`. Qwen3-8B is the text baseline. Report mode and vision mode are different experimental settings; both retain the CXR report excerpt supplied by the original tool. Vision mode is therefore not an image-only experiment. Disabling retrieval or grounding is an explicit ablation and is recorded in the run configuration.

## Repository layout

```text
medenv/                 CLI, benchmark loading, metrics, serialization
rllm/                   Vendored evaluation runtime
services/               Retrieval and grounding APIs
scripts/                Launch, import, validation, release and visualization tools
examples/MedGym/        Compatible original command names
data/synthetic/         Four entirely synthetic cases and test-pattern images
data/bench/             Local credentialed benchmark (Git-ignored)
data/retrieval/         Local third-party corpus/index (Git-ignored)
data/private/           Local SHA-256 import manifests (Git-ignored)
docs/                   Setup, data and migration documentation
tests/                  Offline regression and integration tests
```

## Publish the code

```bash
git init -b main                      # only when .git is absent
git add .
python scripts/check_release.py --archive dist/MedEnv-public.zip
python -m pip install build
python -m build
```

Review the staged files, then commit and push to your own GitHub repository. `check_release.py` scans tracked and untracked public files, rejects restricted data paths and suspicious credentials, and can export a public source ZIP. The ZIP excludes the ignored local datasets. Review any additional files before publishing; pattern scans do not prove that arbitrary new content is shareable.

## License and attribution

The supplied upstream Apache-2.0 license is retained in [LICENSE](LICENSE); adaptations and origin are described in [NOTICE](NOTICE) and [the source manifest](docs/source_manifest.json). The old `pyproject.toml` claimed MIT while its actual `LICENSE` was Apache-2.0; this package follows the supplied license file. Third-party datasets and model weights retain their own terms. Models and virtual environments are not bundled.
