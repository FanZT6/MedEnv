#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"
# Run every case in all four subsets, sequentially. Extra options apply to each run.
for subset in with_img/ed_hosp with_img/hosp_only without_img/hosp_only without_img/ed_hosp; do
  python -m medenv --max_cases 0 --subdir "$subset" "$@"
done
