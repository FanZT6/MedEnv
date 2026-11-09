#!/usr/bin/env bash
set -euo pipefail

if [[ "${1:-}" == "--help" ]]; then
  printf '%s\n' \
    'Use a private configuration based on config.example.sh.' \
    'Required: MEDENV_DB, MEDENV_OSCE_ROOT. Optional: MEDENV_BENCH_ROOT.' \
    'See README.md for data-access requirements and extraction limitations.'
  exit 0
fi
if [[ $# -ne 0 ]]; then
  printf '%s\n' 'This wrapper uses MEDENV_* environment variables; see --help.' >&2
  exit 2
fi

: "${MEDENV_DB:?Set MEDENV_DB to an authorized, existing DuckDB file}"
: "${MEDENV_OSCE_ROOT:?Set MEDENV_OSCE_ROOT to a new private output directory}"
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
medenv_python="${MEDENV_PYTHON:-python3}"
medenv_workers="${MEDENV_WORKERS:-4}"
if [[ -e "$MEDENV_OSCE_ROOT" ]]; then
  printf '%s\n' 'The wrapper requires a new output root. Use individual commands to inspect/resume a prior run.' >&2
  exit 2
fi

check_args=(--db "$MEDENV_DB")
if [[ -n "${MEDENV_CXR_JPG_ROOT:-}" || -n "${MEDENV_CXR_REPORT_JSON:-}" ]]; then
  printf '%s\n' 'Obsolete CXR configuration: initialize a report-only DB and use MEDENV_CXR_DB.' >&2
  exit 2
fi
if [[ -n "${MEDENV_CXR_DB:-}" && "${MEDENV_WITHOUT_CXR_REPORTS:-0}" == "1" ]]; then
  printf '%s\n' 'Choose either MEDENV_CXR_DB or MEDENV_WITHOUT_CXR_REPORTS=1.' >&2
  exit 2
fi
if [[ -n "${MEDENV_CXR_DB:-}" ]]; then
  check_args+=(--cxr-db "$MEDENV_CXR_DB")
elif [[ "${MEDENV_WITHOUT_CXR_REPORTS:-0}" != "1" ]]; then
  printf '%s\n' 'Set MEDENV_CXR_DB, or explicitly set MEDENV_WITHOUT_CXR_REPORTS=1.' >&2
  exit 2
fi
"$medenv_python" "$script_dir/check_database.py" "${check_args[@]}"
build_args=(--db "$MEDENV_DB" --out "$MEDENV_OSCE_ROOT" --workers "$medenv_workers")
if [[ -n "${MEDENV_CXR_DB:-}" ]]; then
  build_args+=(--cxr-db "$MEDENV_CXR_DB")
else
  build_args+=(--without-cxr-reports)
fi
if [[ -n "${MEDENV_KNOWLEDGE_JSON:-}" ]]; then
  build_args+=(--knowledge_json "$MEDENV_KNOWLEDGE_JSON")
fi
"$medenv_python" "$script_dir/build_osce_dataset.py" "${build_args[@]}"
"$medenv_python" "$script_dir/convert_osce_to_ehr.py" \
  --input_dir "$MEDENV_OSCE_ROOT" --recursive --ehr_alongside --workers "$medenv_workers"
if [[ -n "${MEDENV_BENCH_ROOT:-}" ]]; then
  "$medenv_python" "$script_dir/sample_benchmark.py" \
    --input-root "$MEDENV_OSCE_ROOT" --output-root "$MEDENV_BENCH_ROOT" \
    --per-category "${MEDENV_SAMPLE_PER_CATEGORY:-150}" --seed "${MEDENV_SAMPLE_SEED:-42}"
fi
