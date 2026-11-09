#!/usr/bin/env python3
"""Adapt the historical HOSP/NOTE CSV importer to explicit, non-overwriting paths.

Creates a NEW private DuckDB. Optional ED-directory support is a packaging addition.
For CXR reports, run initialize_cxr_index.py in a separate new private database.
"""
import argparse
import csv
import gzip
import os
from pathlib import Path
import shutil
import tempfile
import zipfile

import duckdb


def quote_identifier(value):
    return '"' + value.replace('"', '""') + '"'


def import_csv(conn, schema, path):
    path = Path(path)
    table = path.name.removesuffix(".gz").removesuffix(".csv")
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8-sig", newline="") as src:
        columns = next(csv.reader(src))
    # ICD identifiers are strings, including leading zeroes (not numeric values).
    types = {name: "VARCHAR" for name in columns if name.lower() == "icd_code"}
    relation = conn.read_csv(str(path), header=True, sample_size=-1, dtype=types or None)
    relation.create_view("_medenv_csv_import", replace=True)
    conn.execute(f"CREATE SCHEMA IF NOT EXISTS {quote_identifier(schema)}")
    conn.execute(
        f"CREATE TABLE {quote_identifier(schema)}.{quote_identifier(table)} "
        "AS SELECT * FROM _medenv_csv_import"
    )
    conn.execute("DROP VIEW _medenv_csv_import")
    print(f"Imported {schema}.{table}")


def csv_files(directory):
    root = Path(directory)
    files = sorted([*root.glob("*.csv.gz"), *root.glob("*.csv")])
    if not files:
        raise ValueError(f"No CSV or CSV.GZ files in {root}")
    return files


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", required=True, help="New DuckDB output path (must not exist)")
    ap.add_argument("--hosp-dir", required=True, help="Authorized MIMIC-IV hosp CSV directory")
    notes = ap.add_mutually_exclusive_group()
    notes.add_argument("--note-dir", help="Extracted NOTE CSV directory")
    notes.add_argument("--note-zip", help="NOTE archive with note/*.csv.gz members")
    ap.add_argument("--ed-dir", help="Optional extracted MIMIC-IV-ED CSV directory")
    ap.add_argument("--temp-dir", help="Private scratch directory with sufficient disk space")
    ap.add_argument("--threads", type=int, default=4)
    args = ap.parse_args()
    destination = Path(args.db).expanduser().resolve()
    if destination.exists() or destination.is_symlink():
        ap.error("--db already exists; the importer never replaces existing databases")
    if args.threads < 1:
        ap.error("--threads must be positive")
    directories = [("hosp", args.hosp_dir)]
    if args.note_dir:
        directories.append(("note", args.note_dir))
    if args.ed_dir:
        directories.append(("mimiciv_ed", args.ed_dir))
    sources = [(schema, csv_files(directory)) for schema, directory in directories]
    if args.note_zip and not Path(args.note_zip).is_file():
        ap.error("--note-zip does not exist")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if args.temp_dir:
        Path(args.temp_dir).mkdir(parents=True, exist_ok=True)
    # Never leave a partial database at the final destination on import failure.
    with tempfile.TemporaryDirectory(prefix="medenv-import-", dir=destination.parent) as staged:
        staged_db = Path(staged) / "database.duckdb"
        conn = duckdb.connect(str(staged_db))
        try:
            conn.execute(f"SET threads={args.threads}")
            with tempfile.TemporaryDirectory(prefix="medenv-scratch-", dir=args.temp_dir) as scratch:
                conn.execute("SET temp_directory=?", [scratch])
                for schema, files in sources:
                    for path in files:
                        import_csv(conn, schema, path)
                if args.note_zip:
                    with zipfile.ZipFile(args.note_zip) as archive:
                        members = [m for m in archive.namelist()
                                   if m.endswith((".csv.gz", ".csv"))
                                   and "note" in Path(m).parts[:-1] and "__MACOSX" not in m]
                        if not members:
                            raise ValueError("No note/*.csv(.gz) members in archive")
                        for member in members:
                            # Use a basename in fresh scratch space, never archive paths.
                            extracted = Path(scratch) / Path(member).name
                            with archive.open(member) as src, extracted.open("xb") as dst:
                                shutil.copyfileobj(src, dst)
                            import_csv(conn, "note", extracted)
                            extracted.unlink()
        finally:
            conn.close()
        # Atomic no-clobber publication of the newly created file.
        os.link(staged_db, destination)
    print("Created new private database. Run check_database.py before extraction.")


if __name__ == "__main__":
    main()
