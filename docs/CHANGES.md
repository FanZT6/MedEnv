# Changes from the supplied MedGym checkout

The reference was `examples/MedGym/README_TEST_OSCE.md` and its actual Python entry point. The source directory had no Git metadata, so no upstream commit can be asserted. `docs/source_manifest.json` records source paths and hashes for copied code. This is a benchmark-focused extraction, not a full replacement for rLLM.

## Packaging

- Extracted the evaluation engine, agent, environment, prompts, tool parser, medical tools and reward into a vendored `rllm` namespace. Avoid installing this distribution alongside an unrelated `rllm` distribution in the same environment.
- Removed eager imports of unrelated RL, math, code, web and distributed-training dependencies from package initializers. Retained the actual Apache-2.0 license instead of the inconsistent MIT metadata.
- Added the `medenv-bench`/`python -m medenv` CLI, four-subset selection, dry runs, explicit output paths, environment configuration and compatibility wrappers.
- Included retrieval/grounding servers and index conversion/build scripts. Standardized retrieval port 8003. CPU service defaults avoid overlapping the three LLM GPUs.
- Copied the complete prepared benchmark and retrieval dependencies locally with hashes. Excluded restricted/third-party data from every public packaging path.

## Runtime corrections

- CXRTool now consumes `jpg_path_abs` or resolves a case-relative image path. The original loader created the absolute field, but the CXR tool opened the relative path against the process working directory.
- The no-image environment now injects context into `request_exam`, as the image branch already did.
- A `diagnosis` action now terminates and computes the reward inside the environment, using its `final_response`. Previously only `finish` invoked the terminal reward; the execution engine stopped on `diagnosis` without that scoring step.
- The run summary reads structured actions and terminal `is_correct`, and recognizes boxed diagnoses. The original summary expected JSON diagnoses and counted tool names in the string representation of entire trajectories, which could include the hidden gold label.
- Report mode explicitly suppresses image bytes for text-only Qwen3-8B. Vision mode sends multimodal payloads over Chat Completions. The original tokenized completions path reduced multimodal content to text. Both modes retain report text from the original CXR tool; this is documented as part of the protocol.
- The same configurable maximum step count is passed to the engine and environment. Empty subsets, missing labels and invalid concurrency/repeat values fail clearly.
- Grounding API request schemas now resolve under FastAPI, and post-processing uses the Transformers 4.57 `threshold`/`text_labels` interface. PNG fixtures retain the correct MIME type. Localhost calls bypass inherited network proxies.
- Trajectories use portable output directories and redact credential fields, including Judge keys embedded in task metadata.

These corrections affect results compared with the unmodified scripts. Re-run experiments and record the MedEnv revision, model revisions, subset, mode, tool set and data hashes; do not mix old and new scores as though the implementations were identical. Clinical validity and model quality are outside the software smoke tests.
