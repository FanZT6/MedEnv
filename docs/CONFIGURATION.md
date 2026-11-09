# Configuration and code customization

[Home](../README.md) · [Inference guide](RUNNING.md)

Use `.env` for deployment settings and command-line flags for experiment settings. Edit the Python code when changing agent behavior, tool implementations or scoring.

## Where to make changes

| Setting or behavior | File and entry point |
|---|---|
| Models, API keys, URLs, GPU groups, data/index paths | [`.env.example`](../.env.example), copied to `.env` |
| Case selection, repetitions, sampling and inference budgets | [`medenv/cli.py`](../medenv/cli.py): `build_parser()` |
| Enabled tools and environment construction | [`medenv/cli.py`](../medenv/cli.py): `run_configuration()`, `main()` |
| Doctor's instructions | [`system_prompts.py`](../rllm/agents/system_prompts.py): `DOCTOR_SYSTEM_PROMPT`, `DOCTOR_SYSTEM_PROMPT_wo_IMG` |
| Patient evidence routing and response prompts | [`dialog_tool.py`](../rllm/tools/med_tool/dialog_tool.py): `ROUTER_SYSTEM_PROMPT`, `PATIENT_SYSTEM_PROMPT`, `PATIENT_SYSTEM_PROMPT_KB` |
| Examination response prompts | [`exam_result_tool.py`](../rllm/tools/med_tool/exam_result_tool.py): `EXAM_SECTION_ROUTER_PROMPT`, `EXTRACT_RESULT_FOR_DOCTOR_PROMPT`, `KB_APPROXIMATE_EXAM_PROMPT` |
| Case fields and opening patient message | [`medenv/data.py`](../medenv/data.py): `load_cases_from_bench()`, `build_tasks()` |
| Tool registration | [`rllm/tools/__init__.py`](../rllm/tools/__init__.py): `DEFAULT_TOOLS` |
| Final-diagnosis judging and reward weights | [`med_diagnosis_reward.py`](../rllm/rewards/med_diagnosis_reward.py): `JUDGE_SYSTEM_PROMPT`, `med_diagnosis_reward()` |
| Metric aggregation | [`medenv/evaluation.py`](../medenv/evaluation.py): `evaluate_doctor_results()` |

## Models, endpoints and GPUs

Edit `.env`, keeping the server model name, client model name and endpoint aligned:

```bash
DOCTOR_MODEL_PATH=/path/to/doctor-model
TOKENIZER_PATH=/path/to/doctor-model
DOCTOR_MODEL=doctor_agent
DOCTOR_PORT=31000
DOCTOR_BASE_URL=http://127.0.0.1:31000/v1
DOCTOR_GPUS=0,1
PATIENT_GPUS=2
JUDGE_GPUS=3
MAX_MODEL_LEN=32768
GPU_MEMORY_UTILIZATION=0.85
```

Use the same pattern for `PATIENT_PORT` / `RLLM_PATIENT_BASE_URL` and `JUDGE_PORT` / `JUDGE_BASE_URL`. When using an existing API, configure its served model ID and credentials; the local `*_MODEL_PATH` values are used only by the launchers. Patient and examiner requests share the `RLLM_PATIENT_*` settings.

Additional vLLM options can be supplied after the role:

```bash
bash scripts/start_vllm.sh doctor --max-num-seqs 16
```

## Inference budget and sampling

```bash
bash scripts/run_bench.sh \
  --subdir with_img/ed_hosp \
  --max_cases 100 --repeat_k 3 \
  --max_steps 20 --n_parallel_agents 4 \
  --temperature 0.2 --top_p 0.9 \
  --max_prompt_length 8192 --max_response_length 8192 \
  --env_timeout 600
```

`max_steps` limits interaction turns. On the last turn, the Doctor is instructed to submit a diagnosis from the evidence already collected. `env_timeout` is the timeout for an environment operation. `max_response_length` is a token budget for the entire trajectory, including Doctor turns and environment replies. If a trajectory ends with `TRUNCATION` before a diagnosis, increase this budget while staying within the model server's context limit, and check `unscored_runs` in `metrics.json`. Patient/examiner generation has its own parameters inside the tool implementations; the CLI sampling flags control the Doctor.

To change a default permanently, edit the corresponding argument in `build_parser()`:

```python
parser.add_argument("--max_steps", type=int, default=20)
parser.add_argument("--temperature", type=float, default=0.2)
```

## Doctor and simulator prompts

Edit the two Doctor prompts in `rllm/agents/system_prompts.py`. For example, to add the same instruction to both CXR and no-CXR settings, append this after their definitions:

```python
instruction = "\nReview the available evidence before submitting the final diagnosis.\n"
DOCTOR_SYSTEM_PROMPT += instruction
DOCTOR_SYSTEM_PROMPT_wo_IMG += instruction
```

Keep the tool-call format and final-diagnosis format compatible with the parser. `--parser_name qwen` uses Qwen tool-call formatting; `--parser_name r1` selects the alternative parser in [`rllm/parser/tool_parser.py`](../rllm/parser/tool_parser.py). A different model's tool-call format may require a new parser and registration in [`rllm/parser/__init__.py`](../rllm/parser/__init__.py).

Patient response rules live in `PATIENT_SYSTEM_PROMPT` and `PATIENT_SYSTEM_PROMPT_KB` in `dialog_tool.py`. Examination rules live in the prompt constants in `exam_result_tool.py`. Keep EHR-supported answers and knowledge-based supplementation distinguishable when changing these prompts.

## Tools and retrieval

For an ablation, use the existing switches:

```bash
bash scripts/run_bench.sh --disable_retrieval --disable_grounding
```

For a new tool, implement the `Tool` interface from [`tool_base.py`](../rllm/tools/tool_base.py), register the class in `rllm/tools/__init__.py`, and add its name in `run_configuration()`. Update the Doctor prompts to describe it. If it requires the case EHR, also add its name to `context_injected_tool_names` in `medenv/cli.py`.

For example, the registry entry and selected tool name must match:

```python
# rllm/tools/__init__.py — after importing MyExamTool
DEFAULT_TOOLS["my_exam"] = MyExamTool  # before register_all(DEFAULT_TOOLS)

# medenv/cli.py — in run_configuration(), before returning names
names.append("my_exam")
```

The retrieval tool's default result count is `RetrieveTool.__init__(max_results=5)` in [`retrieve_tool.py`](../rllm/tools/med_tool/retrieve_tool.py). Change that default to adjust requests without an explicit `top_k`; the agent can also supply `top_k` in a tool call:

```json
{"name": "retrieve", "arguments": {"query": "diagnostic criteria", "top_k": 10}}
```

`RETRIEVAL_INDEX`, `RETRIEVAL_CORPUS` and `RETRIEVER_MODEL` select the search assets. Changing the embedding model requires rebuilding the index with that model. The corpus row order must match the index.

For grounding, adjust `box_threshold` and `text_threshold` defaults in [`cxr_grounding_tool.py`](../rllm/tools/med_tool/cxr_grounding_tool.py), in both the tool schema and `forward()`, or supply them in the tool call:

```json
{"name": "cxr_grounding", "arguments": {"text_prompts": ["lung"], "box_threshold": 0.35, "text_threshold": 0.25}}
```

## Scoring

Set `JUDGE_MODEL`, `JUDGE_BASE_URL` and `JUDGE_API_KEY` in `.env` to select the Judge. Edit `JUDGE_SYSTEM_PROMPT` in `rllm/rewards/med_diagnosis_reward.py` to change its criteria; the API response must remain `YES` or `NO`. Set `JUDGE_MODEL=` for string-containment scoring.

To use only the final-diagnosis reward, add this import in `medenv/cli.py`:

```python
from functools import partial
```

Then replace the `reward_fn` entry in the `env_args` passed to `AgentExecutionEngine`:

```python
"reward_fn": partial(med_diagnosis_reward, result_weight=1.0, process_weight=0.0),
```

This changes `mean_reward`, not the definition of diagnostic accuracy. `is_correct` is determined by the final Judge or containment rule. The process term uses a lexical-overlap heuristic; it is separate from the final correctness score.

## Data fields and opening message

`load_cases_from_bench()` maps each EHR file to a case; `build_tasks()` constructs the initial patient message and environment context. For example, replace the `question` entry in `build_tasks()` to change the opening message:

```python
"question": (
    f"Age: {info.get('age', '')}; gender: {info.get('gender', '')}. "
    f"Chief complaint: {chief}."
),
```

Keep `Final_Result` in the environment's reference label, outside the Doctor's opening message. Custom EHR schemas must also match the section names used by the patient and examination tools.
