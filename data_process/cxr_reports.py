"""Report-only adapters. Never read image bytes or require an image filesystem."""
import hashlib
import json
from pathlib import Path

import duckdb


def nonempty_report(value):
    if isinstance(value, str):
        return value.strip().lower() not in ("", "none", "null", "[]")
    if isinstance(value, (list, tuple)):
        return any(nonempty_report(item) for item in value)
    return False


def report_text(value):
    if value is None:
        return ""
    if isinstance(value, str):
        return value if nonempty_report(value) else ""
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return "\n".join(item for item in value if nonempty_report(item))
    raise ValueError("Report must be a string or a list of strings")


def _positive_id(value):
    text = str(value)
    if not text.isascii() or not text.isdigit() or not 0 < int(text) <= 2**63 - 1:
        raise ValueError("Invalid report study/subject identifier")
    return int(text)


def read_report_records(studies, *, reports_json=None, reports_dir=None):
    """Validate one nonempty report per study; conflicting duplicates fail.

    studies maps study_id to subject_id from validated metadata. Partial report
    corpora are explicitly counted. Unknown studies fail. Native TXT support is
    a packaging addition; the JSON adapter follows the historical schema.
    """
    if bool(reports_json) == bool(reports_dir):
        raise ValueError("Supply exactly one report input")
    result, duplicates, blank = {}, 0, 0
    digest = hashlib.sha256()
    if reports_json:
        raw = Path(reports_json).expanduser().resolve(strict=True).read_bytes()
        digest.update(raw)
        obj = json.loads(raw)
        if isinstance(obj, list):
            records = obj
        elif isinstance(obj, dict) and all(isinstance(v, list) for v in obj.values()):
            records = [r for group in obj.values() for r in group]
        else:
            raise ValueError("Report JSON must be a list or a dictionary of lists")
        for number, item in enumerate(records, start=1):
            if not isinstance(item, dict) or "study_id" not in item or "report" not in item:
                raise ValueError(f"Report item {number}: expected study_id and report")
            sid = _positive_id(item["study_id"])
            if sid not in studies:
                raise ValueError(f"Report item {number}: no matching metadata study")
            if item.get("subject_id") is not None and _positive_id(item["subject_id"]) != studies[sid]:
                raise ValueError(f"Report item {number}: subject/study mismatch")
            text = report_text(item["report"])
            if not text:
                blank += 1
                continue
            if sid in result:
                if result[sid] != text:
                    raise ValueError(f"Report item {number}: conflicting text for the same study")
                duplicates += 1
            result[sid] = text
        source = "study-json"
    else:
        root = Path(reports_dir).expanduser().resolve(strict=True)
        if not root.is_dir():
            raise ValueError("Report root must be a directory")
        for sid, subject in sorted(studies.items()):
            rel = Path(f"p{str(subject)[:2]}") / f"p{subject}" / f"s{sid}.txt"
            path = root / rel
            if not path.exists():
                continue
            resolved = path.resolve(strict=True)
            if not resolved.is_relative_to(root) or not resolved.is_file():
                raise ValueError("Report file escapes configured root or is not a regular file")
            raw = resolved.read_bytes()
            digest.update(str(rel).encode() + b"\0" + raw + b"\0")
            text = report_text(raw.decode("utf-8"))
            if text:
                result[sid] = text
            else:
                blank += 1
        source = "mimic-cxr-txt"
    if not result:
        raise ValueError("No nonempty reports matched metadata")
    audit = {"nonempty_studies": len(result), "metadata_studies_without_report": len(studies) - len(result),
             "identical_duplicate_items": duplicates, "blank_items": blank,
             "source": source, "input_sha256": digest.hexdigest(),
             "report_completion_time_available": False}
    return [(sid, text, source) for sid, text in sorted(result.items())], audit


def validate_report_database(conn):
    """Reject the incompatible legacy image index, not just missing tables."""
    columns = {}
    for schema, table, column in conn.execute(
            "SELECT table_schema, table_name, column_name FROM information_schema.columns").fetchall():
        columns.setdefault(f"{schema}.{table}", set()).add(column.lower())
    for table, expected in {
        "cxr.metadata_ts2": {"subject_id", "study_id", "dicom_id", "study_ts", "studydate", "studytime"},
        "cxr.reports": {"study_id", "report_text", "report_source"},
        "cxr.index_audit": {"audit_json"},
    }.items():
        if not expected <= columns.get(table, set()):
            raise ValueError("Missing report-only CXR index; run initialize_cxr_index.py in a NEW database")
    audits = conn.execute("SELECT audit_json FROM cxr.index_audit").fetchall()
    if len(audits) != 1 or json.loads(audits[0][0]).get("initializer_version") != "2-report-only":
        raise ValueError("CXR index is not the validated report-only format; legacy study_ts must not be reused")


def open_report_database(path):
    if not Path(path).is_file():
        raise ValueError("CXR report database does not exist")
    conn = duckdb.connect(str(path), read_only=True)
    try:
        validate_report_database(conn)
    except BaseException:
        conn.close()
        raise
    return conn
