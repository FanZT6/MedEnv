"""Compatibility imports for the original benchmark loader."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from medenv.data import SUBDIR_NO_CXR, SUBDIR_WITH_CXR, build_tasks, discover_ehr_paths, load_cases_from_bench, prepare_med_data
