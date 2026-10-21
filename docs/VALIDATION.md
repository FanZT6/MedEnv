# Validation record

Packaging date: 2026-10-21.

## Completed software checks

- Installed the package in a fresh Python 3.11.14 environment with CPU PyTorch 2.8.0, Transformers 4.57.6, OpenAI client 2.15.0, HTTPX 0.28.1, NumPy 1.26.4 and Pillow 12.1.0.
- Passed 16 offline regression/integration tests. These cover all four synthetic subsets, repeat selection, image path resolution and PNG encoding, report/vision mode separation, diagnosis termination and reward, no-image examination context, structured metrics, credential redaction, concurrent spawned environments, serialized trajectories, a local HTTP Judge stub, multimodal HTTP transport, importer conflict handling, release filtering and FastAPI grounding request validation.
- Installed the optional retrieval dependencies and checked retrieval, grounding, index-builder and HTML-viewer command entry points. PyArrow is constrained below 23 to retain NumPy 1.26 compatibility.
- Compiled the Python sources and checked Bash launcher syntax.
- Installed the built wheel outside the source tree and successfully ran the CLI against the synthetic data.
- Built source and wheel distributions and scanned their file lists for private dataset/output paths.
- Checked the public Git file set for private data paths, oversized artifacts, machine-specific paths, common credential patterns and patient-ID payloads.

A repeat on the busy data volume hit one 30-second worker-start timeout; that single test passed on retry without a code change. The fresh-environment suite passed all 16 tests in one run.

The tests use synthetic data and local scripted API responses. They do not download model weights or contact a hosted model provider. The CPU environment is for software validation; it does not establish compatibility with every CUDA/vLLM configuration.

## Full-data checks

All 11,699 imported files (4,724,039,357 bytes) passed source/destination SHA-256 comparison. All 5,405 cases passed structural validation and all four full-cohort dry runs completed. The FAISS index contains 337,219 vectors of dimension 768; both corpus and document mappings contain exactly 337,219 valid JSON rows.

Final local data integrity results are recorded in `data/dataset_summary.json` and the ignored `data/private/import-all.json`. The importer checks source and destination SHA-256 values for every copied file. The dataset validator checks JSON parsing, EHR/OSCE pairing, final labels and image references for all four subsets.

## Not measured

The full Doctor/Patient/Judge GPU benchmark, real E5 embedding inference, Grounding DINO model inference, and clinical quality metrics were not run as part of repository packaging. No scientific score is claimed. Model checkpoints must be supplied separately; the historical reference README's local Qwen3-8B path was absent during packaging. Use a valid local checkpoint or obtain the desired model through its upstream distribution, then configure `.env`.

The tests confirm the included workflow's software interfaces, not medical accuracy. See `docs/CHANGES.md` for behavior corrections that require new experiment results rather than reuse of older scores.
