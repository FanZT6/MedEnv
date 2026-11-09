#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Batch transform OSCE_Examination-style JSON into:
{
  "ehr": {...},
  "knowledge": [...]
}

Batch mode:
- process osce_*.json and osce_*.jsonl under input_dir (optionally recursive)
- write one output per input file, preserving relative paths
- IMPORTANT: each output file is a SINGLE dict (not a list)

Single-file mode:
- process one input file and write one output file
- IMPORTANT: output is a SINGLE dict (not a list)

Input formats supported for each file:
- JSON object
- JSON array
- JSONL

In single-dict output mode:
- Multiple samples per file are refused instead of silently discarding later cases.
- Existing output files are never overwritten.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("osce_transform")


# ----------------------------
# Core transformation functions
# ----------------------------

def _safe_get(d: Any, key: str, default=None):
    if isinstance(d, dict):
        return d.get(key, default)
    return default


def _extract_exam_fields(imaging_item: Dict[str, Any]) -> Dict[str, Any]:
    exam_code = None
    exam_name = None
    for det in imaging_item.get("details", []) or []:
        if not isinstance(det, dict):
            continue
        if det.get("field_name") == "exam_code":
            exam_code = det.get("field_value")
        elif det.get("field_name") == "exam_name":
            exam_name = det.get("field_value")

    return {
        "exam_name": exam_name,
        "exam_code": exam_code,
        "text": imaging_item.get("text"),
        "note_id": imaging_item.get("note_id"),
        "charttime": imaging_item.get("charttime"),
        "storetime": imaging_item.get("storetime"),
    }


def _extract_cxr_list(cxr: Any) -> List[Dict[str, Any]]:
    """Preserve report provenance/times; deliberately discard image references."""
    from cxr_reports import nonempty_report
    if not isinstance(cxr, dict):
        return []
    out = []
    for st in cxr.get("studies", []) or []:
        if not isinstance(st, dict) or not nonempty_report(st.get("report_text")):
            continue
        out.append({key: st.get(key) for key in
                    ("study_id", "study_ts", "report_text", "report_source",
                     "report_charttime", "report_storetime", "time_basis")})
    return out


def _collect_knowledge_blocks(knowledge_root: Any) -> List[Dict[str, Any]]:
    """
    Collect all matched_knowledge anywhere under "knowledge".
    Keep only required fields and deduplicate by name.
    """
    wanted = ["name", "introduction", "signs_and_symptoms", "causes", "diagnosis", "prognosis"]
    collected: List[Dict[str, Any]] = []

    def walk(node: Any):
        if isinstance(node, dict):
            if "matched_knowledge" in node and isinstance(node["matched_knowledge"], list):
                for item in node["matched_knowledge"]:
                    if not isinstance(item, dict):
                        continue
                    collected.append({k: item.get(k) for k in wanted})
            else:
                for v in node.values():
                    walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(knowledge_root)

    # dedupe by name (keep first)
    seen = set()
    deduped = []
    for item in collected:
        key = item.get("name") or json.dumps(item, sort_keys=True, ensure_ascii=False)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)

    return deduped


def transform_one(sample: Dict[str, Any]) -> Dict[str, Any]:
    root = sample.get("OSCE_Examination", sample)

    meta = _safe_get(root, "Meta")
    objective = _safe_get(root, "Objective_for_Doctor")

    patient = _safe_get(root, "Patient_Actor", {})
    patient_info = _safe_get(patient, "Demographics", {})
    history_src = _safe_get(patient, "History", {})
    symptoms_src = _safe_get(patient, "Symptoms", {})

    ehr_history = {
        "Chief_Complaint": _safe_get(symptoms_src, "Chief_Complaint"),
        "HPI": _safe_get(history_src, "HPI"),
        "Past_Medical_History": _safe_get(history_src, "Past_Medical_History"),
        "Social_History": _safe_get(history_src, "Social_History"),
    }

    pe = _safe_get(root, "Physical_Examination_Findings", {})
    pe_text = _safe_get(pe, "general_exam")

    tests = _safe_get(root, "Test_Results", {})
    labs = _safe_get(tests, "Labs", []) or []
    micro = _safe_get(tests, "Microbiology", []) or []

    imaging_src = _safe_get(tests, "Imaging", []) or []
    imaging_out = []
    if isinstance(imaging_src, list):
        for img in imaging_src:
            if isinstance(img, dict):
                imaging_out.append(_extract_exam_fields(img))

    cxr_out = _extract_cxr_list(_safe_get(tests, "CXR", {}))

    ed = _safe_get(tests, "ED", {})
    medrecon_out = {
        "medrecon": _safe_get(ed, "medrecon", []) or [],
        "pyxis": _safe_get(ed, "pyxis", []) or [],
    }

    diagnosis = _safe_get(root, "Principal_Diagnosis", {})
    final_result = _safe_get(diagnosis, "icd_title", None)

    ehr = {
        "Meta": meta,
        "Patient_info": patient_info,
        "Objective_for_Doctor": objective,
        "History": ehr_history,
        "Physical_Examination_Findings": pe_text,
        "Test_Results-Labs": labs,
        "Test_Results-Microbiology": micro,
        "Test_Results-Imaging": imaging_out,
        "CXR": cxr_out,
        "Medrecon": medrecon_out,
        "Final_Result": final_result,
    }

    knowledge_out = _collect_knowledge_blocks(_safe_get(root, "knowledge", {}))

    return {"ehr": ehr, "knowledge": knowledge_out}


# ----------------------------
# IO helpers
# ----------------------------

def _load_input_auto(path: Path) -> Tuple[List[Dict[str, Any]], str]:
    """
    Return: (samples, detected_format)
      detected_format in {"json", "jsonl"}

    Accept JSON object / JSON array / JSONL
    """
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return [], "json"

    # If extension says jsonl, trust it
    if path.suffix.lower() == ".jsonl":
        samples: List[Dict[str, Any]] = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            if not isinstance(obj, dict):
                raise ValueError("Each JSONL row must be an object")
            samples.append(obj)
        return samples, "jsonl"

    # Try JSON parse
    try:
        obj = json.loads(text)
        if isinstance(obj, list):
            if not all(isinstance(x, dict) for x in obj):
                raise ValueError("Each JSON array item must be an object")
            return obj, "json"
        if isinstance(obj, dict):
            return [obj], "json"
        return [], "json"
    except json.JSONDecodeError:
        # Fallback JSONL
        samples: List[Dict[str, Any]] = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            if not isinstance(obj, dict):
                raise ValueError("Each JSONL row must be an object")
            samples.append(obj)
        return samples, "jsonl"


def _dump_single_dict(path: Path, obj: Dict[str, Any]):
    """Write a SINGLE dict JSON file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def _dump_jsonl(path: Path, items: List[Dict[str, Any]]):
    """Write JSONL file (used only in merge mode if --jsonl)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as f:
        for item in items:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")


def _iter_json_files(input_dir: Path, recursive: bool) -> List[Path]:
    patterns = ["osce_*.json", "osce_*.jsonl"]
    files: List[Path] = []
    if recursive:
        for pat in patterns:
            files.extend(input_dir.rglob(pat))
    else:
        for pat in patterns:
            files.extend(input_dir.glob(pat))
    # stable order
    files = sorted({p.resolve() for p in files})
    return files


# ----------------------------
# Processing
# ----------------------------

def process_file_single_output(in_path: Path) -> Optional[Dict[str, Any]]:
    """
    Load a file (json/jsonl/array supported) and return ONE transformed dict.
    Refuse multiple samples; the MedEnv case layout is one object per file.
    """
    samples, fmt = _load_input_auto(in_path)
    if not samples:
        return None
    if len(samples) != 1:
        raise ValueError("Expected one case per file; split multi-case input explicitly")
    if not isinstance(samples[0].get("OSCE_Examination"), dict):
        raise ValueError("Input must contain an OSCE_Examination object")
    return transform_one(samples[0])


def _process_one_ehr_alongside(path_str: str) -> Tuple[str, Optional[str]]:
    """Process one osce_*.json and write ehr_*.json in same dir. Return (path_str, None) or (path_str, error)."""
    fp = Path(path_str)
    try:
        out_obj = process_file_single_output(fp)
        if out_obj is None:
            return (path_str, "empty/unparseable")
        ehr_name = "ehr_" + fp.stem[5:] + ".json"
        out_path = fp.parent / ehr_name
        _dump_single_dict(out_path, out_obj)
        return (path_str, None)
    except Exception as e:
        return (path_str, str(e))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", "-i", type=str, help="single input file (.json/.jsonl)")
    ap.add_argument("--input_dir", type=str, help="input directory containing json/jsonl files")
    ap.add_argument("--output", "-o", type=str, help="single output file (single-file mode or merge mode)")
    ap.add_argument("--output_dir", type=str, help="output directory (for batch mode)")
    ap.add_argument("--recursive", action="store_true", help="recursively search input_dir")

    # Merge mode: optional; if enabled, collect all outputs and write a merged file
    ap.add_argument("--merge", action="store_true", help="merge outputs into one file (requires --output)")
    ap.add_argument("--jsonl", action="store_true", help="in merge mode, write merged output as JSONL")

    # osce_data mode: for each osce_<hadm_id>.json, write ehr_<hadm_id>.json in the SAME folder
    ap.add_argument("--ehr_alongside", action="store_true",
                    help="batch mode: input osce_<id>.json -> output ehr_<id>.json in same dir (for osce_data)")
    ap.add_argument("--workers", type=int, default=4,
                    help="parallel workers for --ehr_alongside (default 4)")

    ap.add_argument("--loglevel", type=str, default="INFO", help="DEBUG/INFO/WARNING/ERROR")
    args = ap.parse_args()
    if bool(args.input) == bool(args.input_dir):
        ap.error("Provide exactly one of --input or --input_dir")
    if args.workers < 1:
        ap.error("--workers must be positive")
    if args.ehr_alongside and (not args.input_dir or args.merge or args.output_dir or args.output):
        ap.error("--ehr_alongside requires --input_dir and no other output mode")
    if args.merge and (not args.input_dir or not args.output):
        ap.error("--merge requires --input_dir and --output")
    if args.jsonl and not args.merge:
        ap.error("--jsonl is only supported with --merge")

    logging.basicConfig(
        level=getattr(logging, args.loglevel.upper(), logging.INFO),
        format="%(levelname)s: %(message)s"
    )

    # ----------------------------
    # Batch mode
    # ----------------------------
    if args.input_dir:
        in_dir = Path(args.input_dir).expanduser().resolve()
        if not in_dir.exists() or not in_dir.is_dir():
            raise SystemExit(f"input_dir not found or not a directory: {in_dir}")

        files = _iter_json_files(in_dir, recursive=args.recursive)
        if not files:
            raise SystemExit(f"No osce_*.json/.jsonl files found under: {in_dir}")

        # ehr_alongside: only process osce_<hadm_id>.json and write ehr_<hadm_id>.json in same dir
        if args.ehr_alongside:
            files = [p for p in files if p.name.startswith("osce_") and p.suffix.lower() == ".json"]
            if not files:
                logger.warning(f"No osce_*.json files found under: {in_dir}")
                return
            logger.info(f"Found {len(files)} osce_*.json files (ehr_alongside mode).")
        else:
            logger.info(f"Found {len(files)} files.")

        merged: List[Dict[str, Any]] = []
        err_count = 0

        if args.ehr_alongside and args.workers > 1:
            # Parallel ehr_alongside
            n_workers = min(args.workers, len(files), os.cpu_count() or 1)
            logger.info(f"Using {n_workers} workers.")
            ok, err_count = 0, 0
            with ProcessPoolExecutor(max_workers=n_workers) as ex:
                futures = {ex.submit(_process_one_ehr_alongside, str(p)): p for p in files}
                for i, fut in enumerate(as_completed(futures)):
                    path_str, err = fut.result()
                    if err is None:
                        ok += 1
                    else:
                        err_count += 1
                        logger.warning(f"Skip {path_str}: {err}")
                    if (ok + err_count) % 5000 == 0 and (ok + err_count) > 0:
                        logger.info(f"  Processed {ok + err_count}/{len(files)} ...")
            logger.info(f"Done. Wrote {ok} ehr_*.json, skipped {err_count}.")
        else:
            for fp in files:
                try:
                    out_obj = process_file_single_output(fp)
                    if out_obj is None:
                        logger.warning(f"Skip empty/unparseable: {fp}")
                        err_count += 1
                        continue

                    if args.merge:
                        merged.append(out_obj)
                    elif args.ehr_alongside:
                        # Write ehr_<hadm_id>.json in the same folder as osce_<hadm_id>.json
                        ehr_name = "ehr_" + fp.stem[5:] + ".json"  # strip "osce_"
                        out_path = fp.parent / ehr_name
                        _dump_single_dict(out_path, out_obj)
                        logger.info(f"Wrote: {out_path}")
                    else:
                        if not args.output_dir:
                            raise SystemExit("Batch mode without --merge requires --output_dir")

                        out_dir = Path(args.output_dir).expanduser().resolve()
                        rel = fp.relative_to(in_dir)

                        # Keep same extension as requested (default: JSON)
                        out_suffix = ".json"  # single-dict mode always outputs JSON dict
                        out_path = out_dir / rel
                        out_path = out_path.with_suffix(out_suffix)

                        _dump_single_dict(out_path, out_obj)
                        logger.info(f"Wrote: {out_path}")

                except Exception as e:
                    err_count += 1
                    logger.exception(f"Failed processing {fp}: {e}")

        if err_count:
            raise SystemExit(f"{err_count} conversion(s) failed; existing outputs were retained")
        if args.merge:
            if not args.output:
                raise SystemExit("Merge mode requires --output")

            out_path = Path(args.output).expanduser().resolve()
            if args.jsonl:
                _dump_jsonl(out_path, merged)
            else:
                out_path.parent.mkdir(parents=True, exist_ok=True)
                with out_path.open("x", encoding="utf-8") as f:
                    json.dump(merged, f, ensure_ascii=False, indent=2)

            logger.info(f"Merged output written: {out_path}")

        return

    # ----------------------------
    # Single file mode
    # ----------------------------
    if args.input:
        in_path = Path(args.input).expanduser().resolve()
        if not in_path.exists() or not in_path.is_file():
            raise SystemExit(f"input file not found: {in_path}")

        out_obj = process_file_single_output(in_path)
        if out_obj is None:
            raise SystemExit(f"input file empty/unparseable: {in_path}")

        if not args.output:
            raise SystemExit("single-file mode requires --output")

        out_path = Path(args.output).expanduser().resolve()
        _dump_single_dict(out_path, out_obj)
        logger.info(f"Wrote: {out_path}")
        return

    raise SystemExit("Please provide --input or --input_dir")


if __name__ == "__main__":
    main()
