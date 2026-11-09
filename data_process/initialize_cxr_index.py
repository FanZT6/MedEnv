#!/usr/bin/env python3
"""Initialize a CXR report-only index from authorized metadata and report text.

No JPG/DICOM pixels, image tree, file index or image-derived labels are used.
This is a new prerequisite adapter, not recovered historical initialization SQL.
A new standalone report DB is recommended. Existing cxr schemas are never replaced.
"""
import argparse
import csv
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_EVEN
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile

import duckdb

from import_hosp_note import quote_identifier
from cxr_reports import read_report_records, validate_report_database

VERSION = "2-report-only"
REQUIRED_COLUMNS = {"subject_id", "study_id", "dicom_id", "studydate", "studytime"}
TIME_PATTERN = re.compile(r"([0-9]{1,6})(?:\.([0-9]{1,18}))?\Z")
ID_PATTERN = re.compile(r"[1-9][0-9]{0,18}\Z")
DICOM_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*\Z")
NORMALIZED_COLUMNS = {
    "subject_id": "BIGINT", "study_id": "BIGINT", "dicom_id": "VARCHAR",
    "ViewPosition": "VARCHAR", "StudyDate": "VARCHAR", "StudyTime": "VARCHAR",
    "study_ts": "TIMESTAMP",
}


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def source_identity(path):
    info = path.stat()
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def parse_study_timestamp(date_value, time_value, time_format="mimic-csv"):
    """Parse CSV HHMMSS[.fraction], never inventing a time for missing data.

    mimic-csv explicitly treats short integer parts as numeric serialization of
    HHMMSS with leading zeros lost, NOT as reduced-precision DICOM TM values.
    dicom-tm requires all six digits and at most six fractional digits instead.
    Long CSV decimal tails are rounded to microseconds using decimal half-even
    rounding; float arithmetic and stripping decimal points are not used.
    """
    if time_format not in ("mimic-csv", "dicom-tm"):
        raise ValueError("Unknown time format")
    date_text, time_text = (date_value or "").strip(), (time_value or "").strip()
    if not re.fullmatch(r"[0-9]{8}", date_text):
        raise ValueError("StudyDate must be YYYYMMDD")
    try:
        day = datetime.strptime(date_text, "%Y%m%d")
    except ValueError as exc:
        raise ValueError("StudyDate is not a valid calendar date") from exc
    match = TIME_PATTERN.fullmatch(time_text)
    if not match:
        raise ValueError("Missing or invalid StudyTime")
    integer, fraction = match.group(1), match.group(2) or ""
    if time_format == "dicom-tm" and (len(integer) != 6 or len(fraction) > 6):
        raise ValueError("dicom-tm requires HHMMSS with at most six fractional digits")
    integer = integer.zfill(6)
    hour, minute, second = int(integer[:2]), int(integer[2:4]), int(integer[4:6])
    if hour > 23 or minute > 59 or second > 59:
        # Python/DuckDB cannot represent leap seconds. Do not clamp them to 59.
        raise ValueError("StudyTime is outside supported HH/MM/SS ranges")
    micros = int((Decimal("0." + (fraction or "0")) * 1_000_000)
                 .quantize(Decimal("1"), rounding=ROUND_HALF_EVEN))
    try:
        return day.replace(hour=hour, minute=minute, second=second) + timedelta(microseconds=micros)
    except (ValueError, OverflowError) as exc:
        raise ValueError("StudyTime rounds outside the supported date range") from exc


def metadata_rows(path):
    """Yield case-insensitive field dictionaries without inferring numeric types."""
    opener = gzip.open if path.suffix.lower() == ".gz" else open
    with opener(path, "rt", encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        names = reader.fieldnames or []
        lowered = [name.lower() for name in names]
        if len(set(lowered)) != len(lowered):
            raise ValueError("Metadata contains duplicate column names")
        if not REQUIRED_COLUMNS <= set(lowered):
            raise ValueError("Metadata is missing required identifier/date/time columns")
        for number, row in enumerate(reader, start=2):
            if None in row or any(value is None for value in row.values()):
                raise ValueError(f"Metadata CSV row {number}: inconsistent column count")
            yield number, {key.lower(): value for key, value in row.items()}


def normalize_metadata(metadata, output, time_format):
    """Validate linkage and acquisition timestamps; never open any image files."""
    count = padded = rounded = 0
    seen_dicoms, study_subjects = set(), {}
    before = source_identity(metadata)
    with output.open("x", encoding="utf-8", newline="") as target:
        writer = csv.writer(target)
        writer.writerow(NORMALIZED_COLUMNS)
        for number, row in metadata_rows(metadata):
            subject, study, dicom = (row[key].strip() for key in ("subject_id", "study_id", "dicom_id"))
            if any(not ID_PATTERN.fullmatch(value) or int(value) > 2**63 - 1
                   for value in (subject, study)):
                raise ValueError(f"Metadata CSV row {number}: invalid positive BIGINT identifier")
            if not DICOM_PATTERN.fullmatch(dicom):
                raise ValueError(f"Metadata CSV row {number}: unsafe or empty dicom_id")
            key = dicom.lower()
            if key in seen_dicoms:
                raise ValueError(f"Metadata CSV row {number}: duplicate dicom_id (case-insensitive)")
            seen_dicoms.add(key)
            if study_subjects.setdefault(study, subject) != subject:
                raise ValueError(f"Metadata CSV row {number}: study_id belongs to multiple subjects")
            try:
                timestamp = parse_study_timestamp(row["studydate"], row["studytime"], time_format)
            except ValueError as exc:
                raise ValueError(f"Metadata CSV row {number}: {exc}") from exc
            time_text = row["studytime"].strip()
            padded += len(time_text.split(".", 1)[0]) < 6
            rounded += "." in time_text and len(time_text.split(".", 1)[1]) > 6
            writer.writerow([subject, study, key, row.get("viewposition", ""),
                             row["studydate"], row["studytime"],
                             timestamp.isoformat(sep=" ", timespec="microseconds")])
            count += 1
            if count % 50_000 == 0:
                print(f"Validated {count:,} metadata rows", file=sys.stderr, flush=True)
    if not count:
        raise ValueError("Metadata contains no rows")
    if source_identity(metadata) != before:
        raise ValueError("Metadata input changed during validation; retry with frozen inputs")
    return {"metadata_rows": count,
            "studies": len(study_subjects), "subjects": len(set(study_subjects.values())),
            "leading_zero_padded_times": padded, "long_fraction_times_rounded": rounded}


def create_indexes(conn):
    conn.execute("CREATE UNIQUE INDEX cxr_metadata_dicom_unique ON cxr.metadata_ts2(dicom_id)")
    conn.execute("CREATE INDEX cxr_metadata_subject_time ON cxr.metadata_ts2(subject_id, study_ts)")
    conn.execute("CREATE UNIQUE INDEX cxr_reports_study_unique ON cxr.reports(study_id)")


def assert_no_cxr_schema(conn):
    if conn.execute("SELECT count(*) FROM information_schema.schemata WHERE catalog_name=current_database() AND lower(schema_name)='cxr'").fetchone()[0]:
        raise ValueError("The cxr schema already exists; initialization never replaces it. Use a separate database.")


def initialize_index(db, metadata_csv, *, reports_json=None, reports_dir=None,
                     time_format="mimic-csv", temp_dir=None, dry_run=False):
    destination = Path(db).expanduser().absolute()
    if destination.is_symlink():
        raise ValueError("--db must not be a symlink")
    if not destination.parent.is_dir():
        raise ValueError("--db parent directory must already exist")
    existed = destination.exists()
    identity = source_identity(destination) if existed else None
    if existed:
        with duckdb.connect(str(destination), read_only=True) as conn:
            assert_no_cxr_schema(conn)
    metadata = Path(metadata_csv).expanduser().resolve(strict=True)
    if not metadata.is_file():
        raise ValueError("Expected a metadata CSV file")
    if bool(reports_json) == bool(reports_dir):
        raise ValueError("Supply exactly one of reports_json or reports_dir")
    # New DB publication uses an atomic, non-overwriting hard link, so staging
    # must be on the destination filesystem. Existing DB additions use a transaction.
    scratch_parent = destination.parent if not existed and not dry_run else temp_dir
    with tempfile.TemporaryDirectory(prefix="medenv-cxr-init-", dir=scratch_parent) as scratch:
        scratch = Path(scratch)
        staged_db, normalized = scratch / "index.duckdb", scratch / "normalized.csv"
        metadata_identity = source_identity(metadata)
        audit = normalize_metadata(metadata, normalized, time_format)
        audit.update({"initializer_version": VERSION, "time_format": time_format,
                      "timestamp_precision": "microseconds; decimal half-even rounding",
                      "metadata_sha256": sha256_file(metadata),
                      "time_basis": "study acquisition, NOT report completion or signing",
                      "images_used": False})
        with duckdb.connect(str(staged_db)) as conn:
            conn.execute("SET threads=4")
            conn.execute("CREATE SCHEMA cxr")
            conn.execute("CREATE TABLE cxr.metadata_raw AS SELECT * FROM read_csv(?, header=true, all_varchar=true)", [str(metadata)])
            conn.read_csv(str(normalized), header=True, dtype=NORMALIZED_COLUMNS).create_view("_normalized")
            conn.execute("CREATE TABLE cxr.metadata_ts2 AS SELECT * FROM _normalized")
            conn.execute("DROP VIEW _normalized")
            if source_identity(metadata) != metadata_identity:
                raise ValueError("Metadata input changed during import; retry with frozen inputs")
            for table in ("metadata_raw", "metadata_ts2"):
                if conn.execute(f"SELECT count(*) FROM cxr.{table}").fetchone()[0] != audit["metadata_rows"]:
                    raise ValueError("Metadata/index row counts do not agree")
            studies = dict(conn.execute("SELECT DISTINCT study_id, subject_id FROM cxr.metadata_ts2").fetchall())
            report_rows, report_audit = read_report_records(
                studies, reports_json=reports_json, reports_dir=reports_dir)
            conn.execute("""CREATE TABLE cxr.reports(
                study_id BIGINT, report_text VARCHAR, report_source VARCHAR)""")
            conn.executemany("INSERT INTO cxr.reports VALUES (?, ?, ?)", report_rows)
            audit["reports"] = report_audit
            audit["tables"] = ["metadata_raw", "metadata_ts2", "reports", "index_audit"]
            conn.execute("CREATE TABLE cxr.index_audit(created_at_utc TIMESTAMP, audit_json VARCHAR)")
            conn.execute("INSERT INTO cxr.index_audit VALUES (?, ?)",
                         [datetime.now(timezone.utc).replace(tzinfo=None), json.dumps(audit, sort_keys=True)])
            create_indexes(conn)
            validate_report_database(conn)
        os.chmod(staged_db, 0o600)
        if dry_run:
            return {"status": "validated_only", **audit}
        if not existed:
            os.link(staged_db, destination)  # fails if another process created the target
        else:
            if destination.is_symlink() or source_identity(destination) != identity:
                raise ValueError("Destination changed during validation; retry with no concurrent writers")
            with duckdb.connect(str(destination)) as conn:
                stage_literal = "'" + str(staged_db).replace("'", "''") + "'"
                conn.execute(f"ATTACH {stage_literal} AS cxr_staging (READ_ONLY)")
                conn.execute("BEGIN TRANSACTION")
                try:
                    assert_no_cxr_schema(conn)
                    conn.execute("CREATE SCHEMA cxr")
                    for table in audit["tables"]:
                        q = quote_identifier(table)
                        conn.execute(f"CREATE TABLE cxr.{q} AS SELECT * FROM cxr_staging.cxr.{q}")
                    create_indexes(conn)
                    conn.execute("COMMIT")
                except BaseException:
                    conn.execute("ROLLBACK")
                    raise
        return {"status": "initialized", **audit}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="New DuckDB, or existing HOSP/NOTE database with no cxr schema")
    parser.add_argument("--metadata-csv", required=True, help="Authorized MIMIC-CXR-JPG metadata.csv[.gz]")
    reports = parser.add_mutually_exclusive_group(required=True)
    reports.add_argument("--reports-json", help="Authorized study_id/report JSON, list or dictionary of lists")
    reports.add_argument("--reports-dir", help="Authorized report files/ root containing pXX/p<subject>/s<study>.txt")
    parser.add_argument("--time-format", choices=("mimic-csv", "dicom-tm"), default="mimic-csv")
    parser.add_argument("--temp-dir", help="Private scratch parent for existing-DB validation (new DB uses destination parent)")
    parser.add_argument("--dry-run", action="store_true", help="Validate metadata and reports without changing/creating the target DB")
    args = parser.parse_args()
    try:
        result = initialize_index(args.db, args.metadata_csv,
                                  reports_json=args.reports_json, reports_dir=args.reports_dir,
                                  time_format=args.time_format, temp_dir=args.temp_dir, dry_run=args.dry_run)
    except ValueError as exc:
        parser.exit(2, f"CXR initialization failed: {exc}\n")
    except (OSError, RuntimeError, duckdb.Error, csv.Error, EOFError) as exc:
        # Database/CSV exception strings can include clinical rows or paths.
        parser.exit(2, f"CXR initialization failed ({type(exc).__name__}); check private inputs, paths and database locks. No replacement was attempted.\n")
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
