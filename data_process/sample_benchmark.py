#!/usr/bin/env python3
"""Copy a reproducible ICD-stratified sample without moving source cases."""
import argparse
import os
import sys
import json
import re
import random
import shutil
from collections import defaultdict
from tqdm import tqdm
from pathlib import Path

SKIP_UNCLASSIFIED = True  # 是否跳过 Unclassified 类别

# 只扫描这些子目录（None=扫描 ROOT_DIR 下全部，排除 bench）。设为 ["with_img"] 可避免扫描 without_img 等大目录，加快速度
WALK_SUBDIRS = None  # 例如 ["with_img"]
# 阶段 1 遍历目录时，每扫描多少个目录打印一次进度（避免长时间无输出像卡住）
WALK_PROGRESS_EVERY_NDIRS = 5000

# ---------- ICD-9 类别（按三位码范围） ----------
ICD9_CATEGORY_LIST = [
    ("infectious_and_parasitic_diseases", (1, 139)),
    ("neoplasms", (140, 239)),
    ("endocrine_metabolic_diseases_and_immunity", (240, 279)),
    ("blood_and_blood_forming_organs", (280, 289)),
    ("mental_disorders", (290, 319)),
    ("nervous_system_and_sense_organs", (320, 389)),
    ("circulatory_system", (390, 459)),
    ("respiratory_system", (460, 519)),
    ("digestive_system", (520, 579)),
    ("genitourinary_system", (580, 629)),
    ("complications_of_pregnancy_childbirth_puerperium", (630, 679)),
    ("skin_and_subcutaneous_tissue", (680, 709)),
    ("musculoskeletal_and_connective_tissue", (710, 739)),
    ("congenital_anomalies", (740, 759)),
    ("perinatal_conditions", (760, 779)),
    ("symptoms_signs_ill_defined", (780, 799)),
    ("injury_and_poisoning", (800, 999)),
    ("external_causes_E_and_V_codes", None),  # E/V 单独处理
]

# ---------- ICD-10 类别（按字母+两位数字范围） ----------
ICD10_CATEGORY_LIST = [
    ("Certain infectious and parasitic diseases", ("A00", "B99")),
    ("Neoplasms", ("C00", "D48")),
    ("Diseases of the blood and blood-forming organs", ("D50", "D89")),
    ("Endocrine, nutritional and metabolic diseases", ("E00", "E90")),
    ("Mental and behavioural disorders", ("F00", "F99")),
    ("Diseases of the nervous system", ("G00", "G99")),
    ("Diseases of the eye and adnexa", ("H00", "H59")),
    ("Diseases of the ear and mastoid process", ("H60", "H95")),
    ("Diseases of the circulatory system", ("I00", "I99")),
    ("Diseases of the respiratory system", ("J00", "J99")),
    ("Diseases of the digestive system", ("K00", "K93")),
    ("Diseases of the skin and subcutaneous tissue", ("L00", "L99")),
    ("Diseases of the musculoskeletal system and connective tissue", ("M00", "M99")),
    ("Diseases of the genitourinary system", ("N00", "N99")),
    ("Pregnancy, childbirth and the puerperium", ("O00", "O99")),
    ("Certain conditions originating in the perinatal period", ("P00", "P96")),
    ("Congenital malformations, deformations and chromosomal abnormalities", ("Q00", "Q99")),
    ("Symptoms, signs and abnormal clinical and laboratory findings", ("R00", "R99")),
    ("Injury, poisoning and certain other consequences of external causes", ("S00", "T98")),
    ("External causes of morbidity and mortality", ("V01", "Y98")),
    ("Factors influencing health status and contact with health services", ("Z00", "Z99")),
]

# ========== ICD-10 辅助：把 A00 这种转成可比较的整数 key ==========
def parse_icd10_key(icd_code: str):
    if not icd_code:
        return None
    code = icd_code.strip().upper()
    m = re.match(r"^([A-Z])(\d{2})", code)
    if not m:
        return None
    letter = m.group(1)
    num = int(m.group(2))
    return (ord(letter) - ord("A")) * 100 + num

ICD10_RANGES = []
for name, (s, e) in ICD10_CATEGORY_LIST:
    ICD10_RANGES.append((name, parse_icd10_key(s), parse_icd10_key(e), s, e))

# ========== ICD-9 辅助：取前三位数字做类别判断 ==========
def icd9_three_digit(code: str):
    if not code:
        return None
    c = code.strip().upper().replace(".", "")
    if c.startswith(("E", "V")):
        return c[0]  # 用 'E' 或 'V' 标记
    m = re.match(r"^(\d{3})", c)
    if not m:
        return None
    return int(m.group(1))

def classify_icd9(code: str):
    x = icd9_three_digit(code)
    if x in ("E", "V"):
        return "external_causes_E_and_V_codes"
    if isinstance(x, int):
        for name, rng in ICD9_CATEGORY_LIST:
            if rng is None:
                continue
            start, end = rng
            if start <= x <= end:
                return name
    return "Unclassified"

def classify_icd10(code: str):
    k = parse_icd10_key(code)
    if k is None:
        return "Unclassified"
    for name, start_k, end_k, s, e in ICD10_RANGES:
        if start_k <= k <= end_k:
            return name
    return "Unclassified"

def parse_category(icd_code, icd_version):
    try:
        v = int(icd_version)
    except Exception:
        return "Unclassified"

    if v == 9:
        cat = classify_icd9(icd_code)
        if cat == "external_causes_E_and_V_codes":
            return "ICD-9 external causes (E/V)"
        for name, rng in ICD9_CATEGORY_LIST:
            if name == cat and rng is not None:
                return f"ICD-9 {name} ({rng[0]}-{rng[1]})"
        return f"ICD-9 {cat}"

    if v == 10:
        cat = classify_icd10(icd_code)
        for name, (s, e) in ICD10_CATEGORY_LIST:
            if name == cat:
                return f"ICD-10 {name} ({s}-{e})"
        return f"ICD-10 {cat}"

    return "Unclassified"

OSCE_FILE_PATTERN = re.compile(r"^osce_\d{8}\.json$")

def process_json_file(file_path):
    """
    返回 meta dict:
    - category
    - icd_version, icd_code, icd_title
    """
    try:
        with open(file_path, "r") as f:
            data = json.load(f)

        data = data.get("OSCE_Examination", {}) or {}
        diag = data.get("Principal_Diagnosis", {}) or {}
        icd_version = diag.get("icd_version")
        icd_code = diag.get("icd_code")
        icd_title = diag.get("icd_title")

        if icd_version is None or not icd_code:
            return None

        category = parse_category(str(icd_code), icd_version)
        return {
            "icd_version": int(icd_version),
            "icd_code": str(icd_code),
            "icd_title": icd_title,
            "category": category,
        }
    except Exception as e:
        return {"_ERROR_": True, "file_path": file_path, "error": str(e)}

def ensure_parent_dir(path: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)


def copy_report_case(source, destination):
    """Copy only the validated OSCE/EHR pair, never image trees or local extras."""
    source, destination = Path(source), Path(destination)
    files = [*source.glob("osce_*.json"), *source.glob("ehr_*.json")]
    if (len(list(source.glob("osce_*.json"))) != 1
            or len(list(source.glob("ehr_*.json"))) != 1):
        raise ValueError("Expected exactly one OSCE and one converted EHR JSON")
    def image_reference(obj):
        if isinstance(obj, dict):
            return any(k in {"jpg_path", "jpg_path_abs", "image_base64"}
                       or image_reference(v) for k, v in obj.items())
        return isinstance(obj, list) and any(image_reference(v) for v in obj)
    for path in files:
        if path.is_symlink() or not path.resolve().is_relative_to(source.resolve()):
            raise ValueError("Case files must be real files inside the source case")
        if image_reference(json.loads(path.read_text(encoding="utf-8"))):
            raise ValueError("Image-backed legacy JSON is not report-only; rebuild into a new output root")
    destination.mkdir(parents=False, exist_ok=False)
    for path in files:
        shutil.copy2(path, destination / path.name)

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--input-root", required=True, help="Private root containing with_img/ and without_img/")
    ap.add_argument("--output-root", required=True, help="New private benchmark root, outside input root")
    ap.add_argument("--per-category", type=int, default=150)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--dry-run", action="store_true", help="Show selection counts without copying or writing")
    args = ap.parse_args()
    source_root = Path(args.input_root).expanduser().resolve()
    output_root = Path(args.output_root).expanduser().resolve()
    if not source_root.is_dir():
        ap.error("--input-root must be an existing directory")
    if output_root.is_relative_to(source_root) or source_root.is_relative_to(output_root):
        ap.error("Input and output roots must be separate, non-nested directories")
    if output_root.exists() or output_root.is_symlink():
        ap.error("--output-root already exists; choose a new directory")
    if args.per_category < 1:
        ap.error("--per-category must be positive")
    ROOT_DIR, BENCH_DIR = str(source_root), str(output_root)
    SAMPLE_PER_CATEGORY, RANDOM_SEED = args.per_category, args.seed
    rng = random.Random(RANDOM_SEED)

    # 1) 收集 osce_########.json，且不进入 bench
    print("Phase 1: Walking directory tree (may be slow on large disks)...", flush=True)
    if WALK_SUBDIRS is not None:
        print(f"  Restricting to subdirs: {WALK_SUBDIRS}", flush=True)
    case_json_files = []
    n_dirs_walked = 0
    for root, dirs, files in os.walk(ROOT_DIR):
        if os.path.normpath(root) == os.path.normpath(ROOT_DIR):
            dirs[:] = [d for d in dirs if d in ("with_img", "without_img")]
        if "bench" in dirs:
            dirs.remove("bench")
        if WALK_SUBDIRS is not None and os.path.normpath(root) == os.path.normpath(ROOT_DIR):
            dirs[:] = [d for d in dirs if d in WALK_SUBDIRS]

        n_dirs_walked += 1
        if WALK_PROGRESS_EVERY_NDIRS > 0 and n_dirs_walked % WALK_PROGRESS_EVERY_NDIRS == 0:
            print(f"  ... walked {n_dirs_walked} dirs, found {len(case_json_files)} osce json so far", flush=True)

        for fn in files:
            if OSCE_FILE_PATTERN.match(fn):
                case_json_files.append(os.path.join(root, fn))

    case_json_files.sort()
    print(f"Found osce case json files: {len(case_json_files)} (walked {n_dirs_walked} dirs)", flush=True)

    # 2) 扫描分类：category -> set(case_dir)
    print("Phase 2: Scanning & classifying JSON...", flush=True)
    category_to_case_dirs = defaultdict(set)
    case_dir_meta = {}  # case_dir -> meta（用于 manifest）

    missing_diag = 0
    unclassified = 0
    errors = []

    for fp in tqdm(
        case_json_files,
        desc="Scanning & classifying",
        unit="case",
        mininterval=5.0,
        file=sys.stdout,
    ):
        meta = process_json_file(fp)
        if meta is None:
            missing_diag += 1
            continue
        if meta.get("_ERROR_"):
            errors.append(meta)
            continue

        cat = meta["category"]
        if SKIP_UNCLASSIFIED and "Unclassified" in cat:
            unclassified += 1
            continue

        case_dir = os.path.dirname(fp)  # 关键：case 文件夹
        category_to_case_dirs[cat].add(case_dir)

        # 一个 case_dir 只有一个 osce json 的话，这里直接存即可；
        # 若意外重复，以第一次为准
        case_dir_meta.setdefault(case_dir, {
            **meta,
            "osce_json": os.path.basename(fp),
            "osce_json_relpath": os.path.relpath(fp, ROOT_DIR),
        })

    # 3) 每类随机采样
    print("Phase 3: Sampling by category...", flush=True)
    sampled_case_dirs = []
    per_cat_stat = {}

    for cat, dir_set in category_to_case_dirs.items():
        dirs_list = sorted(dir_set)
        n_total = len(dirs_list)
        n_take = min(SAMPLE_PER_CATEGORY, n_total)
        if n_take <= 0:
            continue
        picked = rng.sample(dirs_list, n_take)
        sampled_case_dirs.extend(picked)
        per_cat_stat[cat] = (n_total, n_take)

    if errors:
        raise SystemExit(f"{len(errors)} malformed case file(s); no benchmark was written")
    if not sampled_case_dirs:
        raise SystemExit("No classifiable OSCE cases found; no benchmark was written")
    if args.dry_run:
        for cat, (available, selected) in sorted(per_cat_stat.items()):
            print(f"{cat}: {selected}/{available}")
        print(f"Dry run: {len(sampled_case_dirs)} cases; no files written or moved")
        return

    # Keep the historical selection algorithm; copy instead of destructive move.
    print("Phase 4: Copying sampled case dirs...", flush=True)
    os.makedirs(BENCH_DIR, exist_ok=False)
    copy_fail = []

    manifest_path = os.path.join(BENCH_DIR, "manifest.jsonl")
    with open(manifest_path, "x", encoding="utf-8") as mf:
        for src_case_dir in tqdm(
            sampled_case_dirs,
            desc="Copying sampled case dirs",
            unit="case",
            mininterval=2.0,
            file=sys.stdout,
        ):
            rel_case = os.path.relpath(src_case_dir, ROOT_DIR)
            dst_case_dir = os.path.join(BENCH_DIR, rel_case)

            try:
                ensure_parent_dir(dst_case_dir)
                copy_report_case(src_case_dir, dst_case_dir)

                meta = case_dir_meta.get(src_case_dir, {})
                out = {
                    "src_case_relpath": rel_case,
                    "dst_case_relpath": os.path.relpath(dst_case_dir, BENCH_DIR),
                    "seed": RANDOM_SEED,
                    "sample_per_category": SAMPLE_PER_CATEGORY,
                    "category": meta.get("category"),
                    "icd_version": meta.get("icd_version"),
                    "icd_code": meta.get("icd_code"),
                    "icd_title": meta.get("icd_title"),
                    "osce_json": meta.get("osce_json"),
                    "osce_json_relpath": meta.get("osce_json_relpath"),
                }
                mf.write(json.dumps(out, ensure_ascii=False) + "\n")
            except Exception as e:
                copy_fail.append((src_case_dir, dst_case_dir, str(e)))

    # 5) 输出统计
    print("\n================= Summary =================")
    print(f"Total osce json scanned: {len(case_json_files)}")
    print(f"Cases missing Principal_Diagnosis: {missing_diag}")
    if SKIP_UNCLASSIFIED:
        print(f"Cases skipped due to Unclassified: {unclassified}")
    print(f"Categories found (after filtering): {len(category_to_case_dirs)}")
    print(f"Sample per category: {SAMPLE_PER_CATEGORY}")
    print(f"Random seed: {RANDOM_SEED}")
    print(f"Total selected case dirs (to copy): {len(sampled_case_dirs)}")
    print(f"Bench dir: {BENCH_DIR}")
    print(f"Manifest: {manifest_path}")

    print("\n========== Per-category sampling ==========")
    for cat, (n_total, n_take) in sorted(per_cat_stat.items(), key=lambda x: x[1][1], reverse=True):
        print(f"{cat:70s}  total_cases={n_total:6d}  sampled={n_take:6d}")

    if errors:
        print(f"\n[Warning] {len(errors)} files had JSON errors. Showing first 5:")
        for e in errors[:5]:
            print(f"- {e.get('file_path')}: {e.get('error')}")

    if copy_fail:
        print(f"\n[Warning] {len(copy_fail)} case dirs failed to copy. Showing first 5:")
        for src, dst, msg in copy_fail[:5]:
            print(f"- {src} -> {dst}: {msg}")
        raise SystemExit(1)

if __name__ == "__main__":
    main()
