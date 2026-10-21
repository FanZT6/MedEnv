# OSCE benchmark compatibility entry point

The original `run_med_with_tool.py` command is preserved here as a wrapper.
Use the [root README](../../README.md), [Chinese instructions](../../README_zh.md)
and [complete run guide](../../docs/RUNNING.md) for portable setup commands.

Doctor, Patient and Judge default to ports 30000, 30001 and 30002.
Retrieval uses 8003 and Grounding DINO uses 30050. Configure paths and GPUs
in `.env` copied from `.env.example`.

```bash
bash scripts/run_bench.sh --max_cases 10
bash scripts/run_bench.sh --no_cxr --max_cases 10
bash scripts/run_all.sh --n_parallel_agents 1
```

All commands above run from the repository root. Real data belong under
`data/bench/` and are not included in the public Git repository.
