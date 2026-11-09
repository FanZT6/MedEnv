#!/usr/bin/env python3
"""Batch diagnosis extraction with optional CXR REPORTS, never image files.

Legacy directory names with_img/without_img are retained for MedEnv compatibility.
In this release they indicate nonempty CXR report presence, not image availability.
Use a NEW output root; historical cases are not rewritten.
"""
import argparse
import json
import os
import sys
import tempfile
import threading
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Tuple

import duckdb

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from osce_extractor import build_osce_case
from cxr_reports import nonempty_report, open_report_database


def has_table(conn: duckdb.DuckDBPyConnection, schema: str, table: str) -> bool:
    r = conn.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_schema=? AND table_name=?",
        (schema, table),
    ).fetchall()
    return bool(r)


def get_all_eligible_hadm_ids(conn: duckdb.DuckDBPyConnection) -> List[int]:
    """Enumerate medication-timed candidates without ambiguous ED linkage.

    An admission with multiple linked ED rows is neither ED+HOSP nor HOSP-only.
    It must stay excluded, matching the extractor's unique-ED-stay rule. Other
    case-level extraction errors remain explicit failures, not silent skips.
    """
    ed_schema = next((s for s in ["mimiciv_ed", "ed"] if has_table(conn, s, "edstays")), None)
    edstays_tbl = f"{ed_schema}.edstays" if ed_schema else None
    emar_tbl = "hosp.emar" if has_table(conn, "hosp", "emar") else None
    pharm_tbl = "hosp.pharmacy" if has_table(conn, "hosp", "pharmacy") else None

    t0_cond = ""
    if emar_tbl and pharm_tbl:
        t0_cond = f"""
            (EXISTS (SELECT 1 FROM {emar_tbl} e WHERE e.hadm_id = a.hadm_id AND e.charttime IS NOT NULL AND e.charttime > a.admittime)
             OR EXISTS (SELECT 1 FROM {pharm_tbl} p WHERE p.hadm_id = a.hadm_id AND p.starttime IS NOT NULL AND p.starttime > a.admittime))
        """
    elif emar_tbl:
        t0_cond = f"EXISTS (SELECT 1 FROM {emar_tbl} e WHERE e.hadm_id = a.hadm_id AND e.charttime IS NOT NULL AND e.charttime > a.admittime)"
    elif pharm_tbl:
        t0_cond = f"EXISTS (SELECT 1 FROM {pharm_tbl} p WHERE p.hadm_id = a.hadm_id AND p.starttime IS NOT NULL AND p.starttime > a.admittime)"
    else:
        return []

    ids: List[int] = []

    # ED+HOSP: hadm_id in edstays with exactly 1 stay
    if edstays_tbl:
        q_ed = f"""
            SELECT a.hadm_id FROM hosp.admissions a
            WHERE a.hadm_id IN (
                SELECT hadm_id FROM {edstays_tbl} WHERE hadm_id IS NOT NULL
                GROUP BY hadm_id HAVING COUNT(*) = 1
            )
            AND a.admittime IS NOT NULL AND {t0_cond}
            ORDER BY a.hadm_id
        """
        ids.extend([row[0] for row in conn.execute(q_ed).fetchall()])

        # HOSP-only means NO linked ED row. Excluding only the unique-ED group
        # would incorrectly route multi-ED admissions here, although the
        # extractor rejects them under the existing 1_stays_per_hadm rule.
        q_hosp = f"""
            SELECT a.hadm_id FROM hosp.admissions a
            WHERE NOT EXISTS (
                SELECT 1 FROM {edstays_tbl} e WHERE e.hadm_id = a.hadm_id
            )
            AND a.admittime IS NOT NULL AND {t0_cond}
            ORDER BY a.hadm_id
        """
        ids.extend([row[0] for row in conn.execute(q_hosp).fetchall()])
    else:
        # No ED module: all are hosp-only
        q = f"""
            SELECT hadm_id FROM hosp.admissions a
            WHERE admittime IS NOT NULL AND {t0_cond}
            ORDER BY hadm_id
        """
        ids.extend([row[0] for row in conn.execute(q).fetchall()])

    return ids


def _has_output(hadm_id: int, out_root: str) -> bool:
    """Return True if osce_{hadm_id}.json already exists in out_root."""
    sid = str(hadm_id)
    for sub1 in ["with_img", "without_img"]:
        for sub2 in ["ed_hosp", "hosp_only"]:
            path = os.path.join(out_root, sub1, sub2, sid, f"osce_{sid}.json")
            if os.path.isfile(path):
                return True
    return False


def _get_existing_hadm_ids(out_root: str) -> set:
    """Return set of hadm_ids that already have output in out_root."""
    existing: set = set()
    for sub1 in ["with_img", "without_img"]:
        for sub2 in ["ed_hosp", "hosp_only"]:
            parent = os.path.join(out_root, sub1, sub2)
            if not os.path.isdir(parent):
                continue
            try:
                for name in os.listdir(parent):
                    json_path = os.path.join(parent, name, f"osce_{name}.json")
                    if name.isdigit() and os.path.isfile(json_path):
                        existing.add(int(name))
            except OSError:
                pass
    return existing


def has_cxr_reports(case):
    cxr = case.get("OSCE_Examination", {}).get("Test_Results", {}).get("CXR", {})
    return any(nonempty_report(st.get("report_text")) for st in cxr.get("studies", []))


def is_ed_hosp(case: Dict[str, Any]) -> bool:
    """Return True if case is ED+HOSP (has stay_id)."""
    meta = case.get("OSCE_Examination", {}).get("Meta", {})
    return meta.get("stay_id") is not None


def process_one(
    conn: duckdb.DuckDBPyConnection,
    hadm_id: int,
    out_root: str,
    knowledge_json: Optional[str] = None,
    cxr_conn=None,
) -> Tuple[bool, str]:
    """
    Process one hadm_id. Return (success, message).
    On success, writes report-only JSON to the compatible subset folder.
    """
    try:
        case = build_osce_case(
            conn,
            hadm_id=hadm_id,
            diagnostic_hours=2.0,
            knowledge_json=knowledge_json,
            cxr_conn=cxr_conn,
        )
    except Exception as e:
        return False, str(e)

    with_img = has_cxr_reports(case)
    ed_hosp = is_ed_hosp(case)

    sub1 = "with_img" if with_img else "without_img"
    sub2 = "ed_hosp" if ed_hosp else "hosp_only"
    case_dir = os.path.join(out_root, sub1, sub2, str(hadm_id))
    json_path = os.path.join(case_dir, f"osce_{hadm_id}.json")
    if os.path.lexists(case_dir):
        raise FileExistsError(f"Case directory already exists: {case_dir}")
    parent = os.path.dirname(case_dir)
    os.makedirs(parent, exist_ok=True)
    # Commit the complete JSON case atomically.
    with tempfile.TemporaryDirectory(prefix=".case-build-", dir=parent) as staging:
        staged_case = os.path.join(staging, str(hadm_id))
        os.makedirs(staged_case)
        with open(os.path.join(staged_case, f"osce_{hadm_id}.json"), "x", encoding="utf-8") as f:
            json.dump(case, f, ensure_ascii=False, indent=2, default=str)
            f.write("\n")
        os.rename(staged_case, case_dir)

    return True, json_path


def _process_chunk(
    hadm_ids: List[int],
    db_path: str,
    out_root: str,
    cxr_db: Optional[str],
    knowledge_json: Optional[str],
) -> Tuple[int, int, List[Tuple[int, str]], List[int]]:
    """Process a chunk of hadm_ids. Return (ok_count, fail_count, errors, succeeded_hadm_ids)."""
    conn = duckdb.connect(db_path, read_only=True)
    cxr_conn = open_report_database(cxr_db) if cxr_db else None
    ok, fail = 0, 0
    errors: List[Tuple[int, str]] = []
    succeeded: List[int] = []
    for hadm_id in hadm_ids:
        if _has_output(hadm_id, out_root):
            ok += 1
            succeeded.append(hadm_id)
            continue
        try:
            success, msg = process_one(
                conn, hadm_id, out_root, knowledge_json, cxr_conn
            )
            if success:
                ok += 1
                succeeded.append(hadm_id)
            else:
                fail += 1
                if len(errors) < 20:
                    errors.append((hadm_id, msg))
        except Exception as e:
            fail += 1
            if len(errors) < 20:
                errors.append((hadm_id, str(e)))
    if cxr_conn is not None:
        cxr_conn.close()
    conn.close()
    return (ok, fail, errors, succeeded)


def main():
    ap = argparse.ArgumentParser(description="Batch build OSCE JSON for all eligible cases")
    ap.add_argument("--db", required=True, help="DuckDB file, e.g., mimic_hosp_note.db")
    ap.add_argument("--out", required=True, help="Private output root directory")
    cxr_args = ap.add_mutually_exclusive_group(required=True)
    cxr_args.add_argument("--cxr-db", help="Validated report-only DB")
    cxr_args.add_argument("--without-cxr-reports", action="store_true",
                          help="Explicitly omit the CXR-specific report route")
    ap.add_argument("--knowledge_json", default=None, help="Optional authorized knowledge JSON")
    ap.add_argument("--limit", type=int, default=0, help="Limit number of cases (0=no limit)")
    ap.add_argument("--skip", type=int, default=0, help="Skip first N hadm_ids")
    ap.add_argument("--workers", type=int, default=4, help="Parallel workers (default: 4)")
    ap.add_argument("--hadmid_log", default="", help="Log file for all done hadm_ids (default: {out}/hadmid_done.log)")
    ap.add_argument("--regenerate_hadmid_log", action="store_true",
                    help="Only scan --out for existing osce_*.json and write hadmid_done.log (then exit)")
    args = ap.parse_args()
    if args.workers < 1 or args.limit < 0 or args.skip < 0:
        ap.error("workers must be positive; limit and skip must be nonnegative")
    for path in (args.knowledge_json,):
        if path and not os.path.isfile(path):
            ap.error(f"Optional input file does not exist: {path}")
    if args.cxr_db:
        with open_report_database(args.cxr_db):
            pass

    if not os.path.isfile(args.db):
        print(f"DB not found: {args.db}")
        sys.exit(1)

    hadmid_log = args.hadmid_log or os.path.join(args.out, "hadmid_done.log")

    if args.regenerate_hadmid_log:
        existing = _get_existing_hadm_ids(args.out)
        log_dir = os.path.dirname(hadmid_log)
        if log_dir:
            os.makedirs(log_dir, exist_ok=True)
        with open(hadmid_log, "w", encoding="utf-8") as f:
            for hid in sorted(existing):
                f.write(f"{hid}\n")
        print(f"Regenerated hadmid log: {hadmid_log} ({len(existing)} entries)")
        return

    conn = duckdb.connect(args.db, read_only=True)
    hadm_ids = get_all_eligible_hadm_ids(conn)
    conn.close()

    if args.skip:
        hadm_ids = hadm_ids[args.skip:]
    if args.limit:
        hadm_ids = hadm_ids[: args.limit]

    existing = _get_existing_hadm_ids(args.out)
    all_eligible = hadm_ids
    hadm_ids = [x for x in hadm_ids if x not in existing]
    n_skip_existing = len(all_eligible) - len(hadm_ids)
    total_eligible = len(all_eligible)
    total = len(hadm_ids)

    if n_skip_existing:
        print(f"Skipping {n_skip_existing} already in {args.out}")
    print(f"Processing {total} hadm_ids -> {args.out} (workers={min(args.workers, total or 1)})")

    workers = min(args.workers, total, os.cpu_count() or 1) if total else 0
    knowledge_json = args.knowledge_json

    print("  with_img/ed_hosp, with_img/hosp_only, without_img/ed_hosp, without_img/hosp_only")
    for image_group in ("with_img", "without_img"):
        for admission_group in ("ed_hosp", "hosp_only"):
            os.makedirs(os.path.join(args.out, image_group, admission_group), exist_ok=True)

    # Chunk hadm_ids for workers
    def _chunk(lst: List[int], n: int) -> List[List[int]]:
        k, m = divmod(len(lst), n)
        return [lst[i * k + min(i, m) : (i + 1) * k + min(i + 1, m)] for i in range(n)]

    all_succeeded: List[int] = []
    ok = 0
    fail = 0
    all_errors: List[Tuple[int, str]] = []

    chunks = _chunk(hadm_ids, workers) if hadm_ids else []
    chunks = [c for c in chunks if c]  # drop empty
    if not chunks:
        print("No hadm_ids to process.")
    else:
        # Count osce_*.json files in output dir for progress
        def _count_json() -> int:
            n = 0
            try:
                for _root, _dirs, files in os.walk(args.out):
                    n += sum(1 for f in files if f.startswith("osce_") and f.endswith(".json"))
            except Exception:
                pass
            return n

        stop_progress = threading.Event()

        def _progress_loop() -> None:
            while not stop_progress.wait(timeout=60):
                cnt = _count_json()
                pct = 100.0 * cnt / total_eligible if total_eligible else 0
                print(f"  [{time.strftime('%H:%M:%S')}] Progress: {cnt}/{total_eligible} cases ({pct:.1f}%)", flush=True)

        progress_thread = threading.Thread(target=_progress_loop, daemon=True)
        progress_thread.start()

        with ProcessPoolExecutor(max_workers=workers) as ex:
            futures = {
                ex.submit(
                    _process_chunk,
                    ch,
                    args.db,
                    args.out,
                    args.cxr_db,
                    knowledge_json,
                ): ch
                for ch in chunks
            }
            done = 0
            for fut in as_completed(futures):
                ch = futures[fut]
                try:
                    o, f, errs, succ = fut.result()
                    ok += o
                    fail += f
                    all_errors.extend(errs)
                    all_succeeded.extend(succ)
                except Exception as e:
                    fail += len(ch)
                    all_errors.append((ch[0], str(e)))
                done += 1
                print(f"  [{done}/{len(chunks)}] chunks done, {ok} OK, {fail} SKIP so far")

        stop_progress.set()

    for hadm_id, msg in all_errors[:20]:
        print(f"  SKIP hadm_id={hadm_id}: {msg}")
    print(f"\nDone: {ok} OK, {fail} SKIP")

    # Write all done hadm_ids to log file
    all_done = existing | set(all_succeeded)
    log_dir = os.path.dirname(hadmid_log)
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)
    with open(hadmid_log, "w", encoding="utf-8") as f:
        for hid in sorted(all_done):
            f.write(f"{hid}\n")
    print(f"hadmid log: {hadmid_log} ({len(all_done)} entries)")
    if fail:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
