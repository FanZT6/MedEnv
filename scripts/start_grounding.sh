#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"
export CUDA_VISIBLE_DEVICES="${GROUNDING_GPUS:-}"
exec python services/grounding_server.py \
  --model_dir "${GROUNDING_MODEL_PATH:-models/grounding-dino-base}" \
  --host "${SERVICE_HOST:-127.0.0.1}" --port "${GROUNDING_PORT:-30050}" "$@"
