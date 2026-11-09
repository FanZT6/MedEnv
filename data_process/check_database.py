#!/usr/bin/env python3
"""Read-only schema preflight. Prints no patient rows and makes no completeness claim."""
import argparse
import json
from pathlib import Path
import duckdb

from osce_extractor import detect_ed_schema
from cxr_reports import validate_report_database, open_report_database


def inspect_database(conn, require_ed=False, require_notes=False, require_cxr=False, cxr_conn=None):
    columns = {}
    for schema, table, column in conn.execute(
        "SELECT table_schema, table_name, column_name FROM information_schema.columns"
    ).fetchall():
        columns.setdefault(f"{schema}.{table}", set()).add(column.lower())
    errors, warnings = [], []
    required = {
        "hosp.admissions": {"hadm_id", "subject_id", "admittime"},
        "hosp.patients": {"subject_id"},
        "hosp.diagnoses_icd": {"hadm_id", "icd_code", "icd_version", "seq_num"},
        "hosp.d_icd_diagnoses": {"icd_code", "icd_version", "long_title"},
    }
    for table, expected in required.items():
        missing = expected - columns.get(table, set())
        if missing:
            errors.append(f"{table}: missing {', '.join(sorted(missing))}")
    timing = any(expected <= columns.get(table, set()) for table, expected in {
        "hosp.emar": {"hadm_id", "charttime"},
        "hosp.pharmacy": {"hadm_id", "starttime"},
    }.items())
    if not timing:
        errors.append("Need hosp.emar(hadm_id, charttime) or hosp.pharmacy(hadm_id, starttime)")
    ed_schema = detect_ed_schema(conn)
    ed_ready = bool(ed_schema and {"hadm_id", "stay_id", "intime", "outtime"} <= columns.get(f"{ed_schema}.edstays", set()))
    notes_ready = {"hadm_id", "text"} <= columns.get("note.discharge", set())
    cxr_ready = False
    if require_cxr or cxr_conn is not None:
        try:
            validate_report_database(cxr_conn if cxr_conn is not None else conn)
            cxr_ready = True
        except ValueError:
            pass
    for name, ready, mandatory in (("ED linkage", ed_ready, require_ed),
                                   ("discharge notes", notes_ready, require_notes),
                                   ("validated CXR metadata + reports", cxr_ready, require_cxr)):
        if not ready:
            (errors if mandatory else warnings).append(f"Missing/incomplete {name}")
    return {"schema_ok": not errors, "errors": errors, "warnings": warnings,
            "ed_schema": ed_schema, "cxr_report_index_ready": cxr_ready,
            "tables": sorted(columns),
            "scope": "Schema only; no population scan, timestamp validation, or file existence audit"}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", required=True)
    ap.add_argument("--require-ed", action="store_true")
    ap.add_argument("--require-notes", action="store_true")
    ap.add_argument("--require-cxr", action="store_true")
    ap.add_argument("--cxr-db", help="Separate report-only DB; implies --require-cxr")
    args = ap.parse_args()
    if not Path(args.db).is_file():
        ap.error("--db must be an existing file")
    cxr_conn = None
    try:
        cxr_conn = open_report_database(args.cxr_db) if args.cxr_db else None
        with duckdb.connect(args.db, read_only=True) as conn:
            result = inspect_database(conn, args.require_ed, args.require_notes,
                                      args.require_cxr or bool(args.cxr_db), cxr_conn)
    except ValueError as exc:
        ap.error(str(exc))
    finally:
        if cxr_conn is not None:
            cxr_conn.close()
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["schema_ok"] else 1)


if __name__ == "__main__":
    main()
