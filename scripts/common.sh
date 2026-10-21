#!/usr/bin/env bash
set -euo pipefail
MEDENV_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$MEDENV_ROOT"
if [[ -f .env ]]; then
  set -a
  source .env
  set +a
fi
export PYTHONPATH="${MEDENV_ROOT}${PYTHONPATH:+:${PYTHONPATH}}"

export NO_PROXY="${NO_PROXY:+${NO_PROXY},}localhost,127.0.0.1,::1"
export no_proxy="${no_proxy:+${no_proxy},}localhost,127.0.0.1,::1"
