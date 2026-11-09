# Copy outside the repository and replace placeholders locally. Never commit it.
export MEDENV_PYTHON=python3
export MEDENV_DB=/path/to/private/database.duckdb
export MEDENV_OSCE_ROOT=/path/to/private/osce
export MEDENV_BENCH_ROOT=/path/to/private/bench
export MEDENV_WORKERS=4
export MEDENV_SAMPLE_PER_CATEGORY=150
export MEDENV_SAMPLE_SEED=42

# Initialize from authorized metadata CSV and report JSON or native TXT first.
# This DB is separate from the clinical DB; no image files/root are needed.
export MEDENV_CXR_DB=/path/to/private/cxr-reports.duckdb
# Alternatively omit the CXR-specific route explicitly (ordinary NOTE remains):
# unset MEDENV_CXR_DB
# export MEDENV_WITHOUT_CXR_REPORTS=1
# Optional knowledge is never generated or silently downloaded:
# export MEDENV_KNOWLEDGE_JSON=/path/to/authorized/knowledge.json
