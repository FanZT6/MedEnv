#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"
export CUDA_VISIBLE_DEVICES="${RETRIEVAL_GPUS:-}"
exec python services/retrieval_server.py \
  --index_path "${RETRIEVAL_INDEX:-data/retrieval/guidelines_index/e5_Flat.index}" \
  --corpus_path "${RETRIEVAL_CORPUS:-data/retrieval/guidelines_index/corpus_passages.jsonl}" \
  --retriever_name e5 --retriever_model "${RETRIEVER_MODEL:-intfloat/e5-base-v2}" \
  --topk 10 --pooling_method mean --batch_size 64 \
  --host "${SERVICE_HOST:-127.0.0.1}" --port "${RETRIEVAL_PORT:-8003}" "$@"
