#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"
role="${1:-}"
case "$role" in
  doctor) model="${DOCTOR_MODEL_PATH:-Qwen/Qwen3-8B}"; name="${DOCTOR_MODEL:-doctor_agent}"; port="${DOCTOR_PORT:-30000}"; gpus="${DOCTOR_GPUS:-0}" ;;
  patient) model="${PATIENT_MODEL_PATH:-Qwen/Qwen3-8B}"; name="${RLLM_PATIENT_MODEL:-patient_agent}"; port="${PATIENT_PORT:-30001}"; gpus="${PATIENT_GPUS:-1}" ;;
  judge) model="${JUDGE_MODEL_PATH:-Qwen/Qwen3-8B}"; name="${JUDGE_MODEL:-judge_agent}"; port="${JUDGE_PORT:-30002}"; gpus="${JUDGE_GPUS:-2}" ;;
  *) echo 'Usage: bash scripts/start_vllm.sh doctor|patient|judge [vLLM options...]' >&2; exit 2 ;;
esac
shift
# Reject overlaps in the declared three-role GPU layout.
DOCTOR_GPUS="${DOCTOR_GPUS:-0}" PATIENT_GPUS="${PATIENT_GPUS:-1}" JUDGE_GPUS="${JUDGE_GPUS:-2}" python - <<'PY'
import os
seen = {}
for role in ('DOCTOR_GPUS', 'PATIENT_GPUS', 'JUDGE_GPUS'):
    for gpu in os.environ[role].split(','):
        gpu = gpu.strip()
        if not gpu or gpu in seen:
            raise SystemExit(f'Invalid/overlapping GPU allocation: {role}, {seen.get(gpu)}, GPU {gpu}')
        seen[gpu] = role
PY
IFS=',' read -r -a gpu_list <<< "$gpus"
export CUDA_VISIBLE_DEVICES="$gpus"
exec python -m vllm.entrypoints.openai.api_server \
  --model "$model" --served-model-name "$name" \
  --host "${SERVICE_HOST:-127.0.0.1}" --port "$port" \
  --dtype bfloat16 --max-model-len "${MAX_MODEL_LEN:-32768}" \
  --tensor-parallel-size "${#gpu_list[@]}" \
  --gpu-memory-utilization "${GPU_MEMORY_UTILIZATION:-0.9}" "$@"
