"""Portable entry point for the OSCE benchmark."""
import argparse
import asyncio
import json
import os
from datetime import datetime
from pathlib import Path

from .data import SUBSETS, SUBDIR_NO_CXR, SUBDIR_WITH_CXR, prepare_med_data
from .serialization import redact_secrets


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default=os.getenv("DOCTOR_MODEL", "doctor_agent"))
    parser.add_argument("--tokenizer_path", default=os.getenv("TOKENIZER_PATH", ""))
    parser.add_argument("--base_url", default=os.getenv("DOCTOR_BASE_URL", "http://127.0.0.1:30000/v1"))
    parser.add_argument("--api_key", default=os.getenv("DOCTOR_API_KEY", "None"))
    parser.add_argument("--case_dir", default=os.getenv("MEDENV_BENCH_ROOT", "data/bench"))
    parser.add_argument("--subdir", choices=SUBSETS)
    parser.add_argument("--no_cxr", action="store_true", help="Default to without_img/hosp_only")
    parser.add_argument("--cxr_mode", choices=("reports", "vision"), default="reports",
                        help="reports: text model baseline; vision: send images to a vision model")
    parser.add_argument("--max_cases", type=int, default=10, help="Cases per subset; <=0 selects all")
    parser.add_argument("--repeat_k", type=int, default=1)
    parser.add_argument("--n_parallel_agents", type=int, default=8)
    parser.add_argument("--max_steps", type=int, default=15)
    parser.add_argument("--env_timeout", type=float, default=300)
    parser.add_argument("--temperature", type=float, default=0.6)
    parser.add_argument("--top_p", type=float, default=0.95)
    parser.add_argument("--max_prompt_length", type=int, default=4096)
    parser.add_argument("--max_response_length", type=int, default=8192)
    parser.add_argument("--parser_name", choices=("qwen", "r1"), default="qwen")
    parser.add_argument("--judge_model", default=os.getenv("JUDGE_MODEL", ""))
    parser.add_argument("--judge_base_url", default=os.getenv("JUDGE_BASE_URL", ""))
    parser.add_argument("--judge_api_key", default=os.getenv("JUDGE_API_KEY", ""))
    parser.add_argument("--disable_retrieval", action="store_true")
    parser.add_argument("--disable_grounding", action="store_true")
    parser.add_argument("--output_dir", default="", help="Default: outputs/<timestamp>/<subset>")
    parser.add_argument("--dry_run", action="store_true", help="Validate selected data without loading models or calling services")
    return parser


def run_configuration(args):
    subdir = args.subdir or (SUBDIR_NO_CXR if args.no_cxr else SUBDIR_WITH_CXR)
    no_cxr = subdir.startswith("without_img/")
    if args.no_cxr and not no_cxr:
        raise ValueError("--no_cxr conflicts with an image-containing --subdir")
    names = ["ask_patient", "diagnosis", "retrieve"]
    if not no_cxr:
        names.append("cxr")
    names.append("request_exam")
    if not no_cxr:
        names.append("cxr_grounding")
    if args.disable_retrieval:
        names.remove("retrieve")
    if args.disable_grounding and "cxr_grounding" in names:
        names.remove("cxr_grounding")
    return subdir, no_cxr, names


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    for field in ("repeat_k", "n_parallel_agents", "max_steps", "env_timeout", "max_prompt_length", "max_response_length"):
        if getattr(args, field) <= 0:
            parser.error(f"--{field} must be positive")
    try:
        subdir, no_cxr, names = run_configuration(args)
        tasks, cases, _ = prepare_med_data(args.case_dir, args.max_cases, args.repeat_k, subdir)
    except (ValueError, FileNotFoundError) as error:
        parser.error(str(error))
    image_count = 0
    for case in cases:
        if not case["gold_diagnosis"]:
            parser.error(f"Missing Final_Result in case {case['case_id']}")
        for study in case["ehr"].get("CXR", []):
            for dicom in study.get("dicoms", []):
                path = dicom.get("jpg_path_abs")
                if not path or not Path(path).is_file():
                    parser.error(f"Missing CXR image in case {case['case_id']}")
                image_count += 1
    if args.dry_run:
        print(json.dumps({"subset": subdir, "cases": len(cases), "tasks": len(tasks),
                          "image_references": image_count, "tools": names, "cxr_mode": args.cxr_mode}, indent=2))
        return
    if not args.tokenizer_path:
        parser.error("--tokenizer_path or TOKENIZER_PATH is required for model evaluation")

    from transformers import AutoTokenizer
    from rllm.agents.med_agent import MedicalAgent
    from rllm.agents.system_prompts import DOCTOR_SYSTEM_PROMPT, DOCTOR_SYSTEM_PROMPT_wo_IMG
    from rllm.engine.agent_execution_engine import AgentExecutionEngine
    from rllm.environments.medgym.medgym_env import MedicalDialogueEnv
    from rllm.rewards.med_diagnosis_reward import med_diagnosis_reward
    from .evaluation import evaluate_doctor_results

    for key in ("NO_PROXY", "no_proxy"):
        existing = os.environ.get(key, "")
        os.environ[key] = ",".join(filter(None, [existing, "localhost", "127.0.0.1", "::1"]))
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    os.environ.setdefault("RETRIEVAL_SERVER_URL", "http://127.0.0.1:8003")
    os.environ.setdefault("RLLM_PATIENT_BASE_URL", "http://127.0.0.1:30001/v1")
    os.environ.setdefault("RLLM_PATIENT_MODEL", "patient_agent")
    os.environ["MEDENV_CXR_MODE"] = args.cxr_mode
    output = Path(args.output_dir or f"outputs/{datetime.now():%Y%m%d_%H%M%S_%f}/{subdir}").resolve()
    output.mkdir(parents=True, exist_ok=True)
    os.environ["MEDENV_TRAJECTORY_DIR"] = str(output / "trajectories")
    if args.judge_model:
        for task in tasks:
            task.update(judge_model_name=args.judge_model, judge_base_url=args.judge_base_url or args.base_url,
                        judge_api_key=args.judge_api_key or args.api_key)
    config = {**vars(args), "subdir": subdir, "tools": names, "case_count": len(cases), "task_count": len(tasks)}
    (output / "config.json").write_text(json.dumps(redact_secrets(config), indent=2))
    tokenizer = AutoTokenizer.from_pretrained(args.tokenizer_path)
    engine = AgentExecutionEngine(
        agent_class=MedicalAgent,
        agent_args={"tools": names, "parser_name": args.parser_name,
                    "system_prompt": DOCTOR_SYSTEM_PROMPT_wo_IMG if no_cxr else DOCTOR_SYSTEM_PROMPT},
        env_class=MedicalDialogueEnv,
        env_args={"tools": names, "reward_fn": med_diagnosis_reward, "max_steps": args.max_steps,
                  "timeout": args.env_timeout,
                  "context_injected_tool_names": ["cxr", "request_exam", "cxr_grounding"]},
        engine_name="openai",
        rollout_engine_args={"base_url": args.base_url, "api_key": args.api_key, "model": args.model,
                             "use_chat_completions": args.cxr_mode == "vision" and not no_cxr},
        tokenizer=tokenizer,
        sampling_params={"temperature": args.temperature, "top_p": args.top_p, "model": args.model},
        max_steps=args.max_steps, max_response_length=args.max_response_length,
        max_prompt_length=args.max_prompt_length, n_parallel_agents=args.n_parallel_agents,
    )
    try:
        results = asyncio.run(engine.execute_tasks(tasks))
        report = evaluate_doctor_results(results, tasks)
        (output / "metrics.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    finally:
        for env in engine.envs:
            if env is not None and env.process.is_alive():
                env.close()
        engine.shutdown()
    print(f"Results: {output}")
