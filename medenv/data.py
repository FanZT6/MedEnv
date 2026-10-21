# Adapted for MedEnv (2026): portable benchmark-only packaging; see NOTICE and docs/CHANGES.md.
# prepare_med_data_bench.py
# 适配 osce_data/bench 数据格式：按 ehr_<id>.json 读取，图像路径与 case 目录拼接
from __future__ import annotations

import glob
import json
import os
from typing import Any, Dict, List, Tuple

# bench 下按是否有 CXR 使用的子目录（相对 bench 根目录）
SUBDIR_WITH_CXR = "with_img/ed_hosp"
SUBDIR_NO_CXR = "without_img/hosp_only"
SUBSETS = (SUBDIR_WITH_CXR, "with_img/hosp_only", SUBDIR_NO_CXR, "without_img/ed_hosp")


def load_json(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def discover_ehr_paths(bench_root: str, subdir: str = "with_img/ed_hosp", max_cases: int = -1) -> List[str]:
    """
    在 bench 根目录下发现所有 ehr_<id>.json 路径。
    bench_root: 例如 data/bench
    subdir: 相对 bench_root 的子目录，例如 with_img/ed_hosp
    """
    if subdir not in SUBSETS:
        raise ValueError(f"Unknown subset: {subdir}")
    search_dir = os.path.join(bench_root, subdir)
    if not os.path.isdir(search_dir):
        raise FileNotFoundError(f"Missing benchmark subset: {search_dir}")
    pattern = os.path.join(search_dir, "*", "ehr_*.json")
    paths = sorted(glob.glob(pattern))
    if not paths:
        raise ValueError(f"No ehr_*.json cases found in {search_dir}")
    if max_cases > 0:
        paths = paths[:max_cases]
    return paths


def load_cases_from_bench(
    bench_root: str,
    subdir: str = "with_img/ed_hosp",
    max_cases: int = -1,
) -> List[Dict[str, Any]]:
    """
    从 bench 目录加载 case：每个 case 对应一个 ehr_<id>.json；
    图像路径解析为相对于该 case 目录的绝对路径（case_dir + jpg_path）。
    """
    ehr_paths = discover_ehr_paths(bench_root, subdir=subdir, max_cases=max_cases)
    cases: List[Dict[str, Any]] = []

    for ehr_path in ehr_paths:
        case_dir = os.path.dirname(ehr_path)
        case_id = os.path.splitext(os.path.basename(ehr_path))[0].replace("ehr_", "")

        obj = load_json(ehr_path)
        if isinstance(obj, dict) and "ehr" in obj and isinstance(obj["ehr"], dict):
            ehr = obj["ehr"].copy()
            knowledge = obj.get("knowledge") or []
        else:
            ehr = obj if isinstance(obj, dict) else {}
            knowledge = []

        # 将 CXR 中 dicoms 的 jpg_path 解析为基于 case 目录的绝对路径
        if "CXR" in ehr and isinstance(ehr["CXR"], list):
            for cxr_item in ehr["CXR"]:
                if not isinstance(cxr_item, dict) or "dicoms" not in cxr_item:
                    continue
                for d in cxr_item["dicoms"]:
                    if isinstance(d, dict) and "jpg_path" in d:
                        rel_path = d["jpg_path"]
                        d["jpg_path_abs"] = os.path.abspath(os.path.join(case_dir, rel_path))
                        # 保留原 jpg_path 便于兼容
                        # d["jpg_path"] 不变

        gold = (ehr.get("Final_Result") or "") if isinstance(ehr, dict) else ""

        cases.append({
            "case_id": case_id,
            "ehr": ehr,
            "knowledge": knowledge,
            "gold_diagnosis": gold,
            "ehr_path": ehr_path,
            "case_dir": case_dir,
        })

    return cases


def build_tasks(
    cases: List[Dict[str, Any]],
    repeat_k: int = 1,
) -> List[Dict[str, Any]]:
    tasks: List[Dict[str, Any]] = []
    repeat_k = int(repeat_k)
    if repeat_k < 1:
        raise ValueError("repeat_k must be positive")

    for rep in range(repeat_k):
        for c in cases:
            ehr = c.get("ehr", {})
            info = ehr.get("Patient_info") or {}
            history = ehr.get("History") or {}
            chief = history.get("Chief_Complaint", "")

            tasks.append({
                "case_id": c["case_id"],
                "context": {
                    "ehr": ehr,
                    "knowbase": c.get("knowledge"),
                    "case_dir": c.get("case_dir"),
                },
                "question": (
                    f"I am a {info.get('age', '')}-year-old {info.get('gender', '')} patient. "
                    f"I haven't been feeling well recently. "
                    f"My main issue is: {chief}."
                ),
                "ground_truth": c.get("gold_diagnosis", ""),
                "data_source": "medical",
                "_case_group": c["case_id"],
                "_repeat_id": rep,
            })

    # print(f"###### tasks ######:\n{tasks[0]}")
    return tasks


def prepare_med_data(
    case_dir: str,
    max_cases: int = -1,
    repeat_k: int = 1,
    subdir: str = "with_img/ed_hosp",
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Any]:
    """
    适配 bench 的 prepare_med_data 接口。
    case_dir: 此处作为 bench 根目录使用，例如 data/bench
    subdir: 相对 bench 根目录的子路径，默认 with_img/ed_hosp
    返回: (tasks, cases, None)
    """
    cases = load_cases_from_bench(case_dir, subdir=subdir, max_cases=max_cases)
    tasks = build_tasks(cases, repeat_k=repeat_k)
    return tasks, cases, None
