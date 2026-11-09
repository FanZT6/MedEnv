"""Invented metadata and text fixtures; no image access or model calls."""
import csv
from datetime import datetime
import gzip
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPT_DIR))
import duckdb
import initialize_cxr_index as initializer
from initialize_cxr_index import initialize_index, parse_study_timestamp
from cxr_reports import open_report_database
from osce_extractor import extract_cxr_for_hadm
from test_pipeline import run_cli

FIELDS = ["subject_id", "study_id", "dicom_id", "ViewPosition", "StudyDate", "StudyTime"]


class CxrInitializationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="medenv-report-index-test-")
        self.root = Path(self.tmp.name)
        self.db = self.root / "clinical.duckdb"
        self.metadata = self.root / "metadata.csv.gz"
        self.reports = self.root / "reports.json"
        self.rows = [
            ["90000001", "99000001", "SYNTHETIC-A", "AP", "20200101", "110000.125"],
            ["90000001", "99000002", "synthetic-b", "PA", "20200101", "120000.0"],
        ]
        self.write_metadata()
        self.reports.write_text(json.dumps({"train": [
            {"study_id": 99000001, "report": "Invented observation A."},
            {"study_id": 99000002, "report": "Invented observation B."}]}))

    def tearDown(self):
        self.tmp.cleanup()

    def write_metadata(self):
        with gzip.open(self.metadata, "wt", encoding="utf-8", newline="") as out:
            writer = csv.writer(out)
            writer.writerow(FIELDS)
            writer.writerows(self.rows)

    def create_hosp(self):
        with duckdb.connect(str(self.db)) as conn:
            conn.execute("CREATE SCHEMA hosp")
            conn.execute("CREATE TABLE hosp.admissions(hadm_id BIGINT, subject_id BIGINT)")
            conn.execute("INSERT INTO hosp.admissions VALUES (98000001, 90000001)")

    def initialize(self, **kwargs):
        kwargs.setdefault("reports_json", self.reports)
        return initialize_index(self.db, self.metadata, **kwargs)

    def test_timestamp_leading_zero_fraction_and_precision(self):
        examples = {
            "73005.1": datetime(2020, 1, 1, 7, 30, 5, 100000),
            "000009.000001": datetime(2020, 1, 1, 0, 0, 9, 1),
            "9.125": datetime(2020, 1, 1, 0, 0, 9, 125000),
            "110000.123456": datetime(2020, 1, 1, 11, 0, 0, 123456),
            "110000.73999999999": datetime(2020, 1, 1, 11, 0, 0, 740000),
            "110000.0000005": datetime(2020, 1, 1, 11),
            "235959.9999999": datetime(2020, 1, 2),
            "0": datetime(2020, 1, 1),
        }
        for value, expected in examples.items():
            with self.subTest(value=value):
                self.assertEqual(parse_study_timestamp("20200101", value), expected)

    def test_timestamp_missing_invalid_or_partial_dicom_is_rejected(self):
        for value in (None, "", "NaN", "12:30:00", "240000", "126000", "120060", "1234567", "1e5", "12."):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_study_timestamp("20200101", value)
        for value in (None, "", "20200230", "20201301", "20200101.0"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_study_timestamp(value, "120000")
        for value in ("12", "1230", "73005.0", "123000.1234567"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_study_timestamp("20200101", value, "dicom-tm")
        self.assertEqual(parse_study_timestamp("20200101", "073005.25", "dicom-tm"), datetime(2020,1,1,7,30,5,250000))

    def test_dry_run_does_not_create_new_database(self):
        self.assertEqual(self.initialize(dry_run=True)["status"], "validated_only")
        self.assertFalse(self.db.exists())
        self.assertFalse(list(self.root.glob("medenv-cxr-init-*")))

    def test_dry_run_does_not_modify_existing_database(self):
        self.create_hosp()
        before = self.db.read_bytes()
        self.initialize(dry_run=True)
        self.assertEqual(self.db.read_bytes(), before)

    def test_existing_cxr_schema_is_never_overwritten(self):
        self.initialize()
        before = self.db.read_bytes()
        for dry_run in (False, True):
            with self.assertRaisesRegex(ValueError, "already exists"):
                self.initialize(dry_run=dry_run)
        self.assertEqual(self.db.read_bytes(), before)

    def test_missing_timestamp_does_not_create_partial_database(self):
        self.rows[1][-1] = ""
        self.write_metadata()
        with self.assertRaisesRegex(ValueError, "StudyTime"):
            self.initialize()
        self.assertFalse(self.db.exists())

    def test_duplicate_dicom_case_variants_are_rejected(self):
        self.rows[1][2] = "synthetic-a"
        self.write_metadata()
        with self.assertRaisesRegex(ValueError, "duplicate dicom_id"):
            self.initialize()
        self.assertFalse(self.db.exists())

    def test_study_subject_conflict_is_rejected(self):
        self.rows[1][:2] = ["90000002", "99000001"]
        self.write_metadata()
        with self.assertRaisesRegex(ValueError, "multiple subjects"):
            self.initialize()

    def test_database_symlink_is_rejected(self):
        other = self.root / "other.duckdb"
        self.db.symlink_to(other)
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.initialize()
        self.assertFalse(other.exists())

    def test_publication_failure_rolls_back_existing_database(self):
        self.create_hosp()
        original = initializer.create_indexes
        calls = []

        def fail_second(conn):
            calls.append(True)
            if len(calls) == 2:
                raise ValueError("injected publication failure")
            original(conn)

        with patch.object(initializer, "create_indexes", side_effect=fail_second):
            with self.assertRaisesRegex(ValueError, "injected publication failure"):
                self.initialize()
        with duckdb.connect(str(self.db), read_only=True) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM hosp.admissions").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT count(*) FROM information_schema.schemata WHERE schema_name='cxr'").fetchone()[0], 0)

    def test_empty_missing_column_or_duplicate_header_rejected(self):
        contents = ["subject_id,study_id,dicom_id,StudyDate,StudyTime\n",
                    "subject_id,study_id,dicom_id\n90000001,99000001,a\n",
                    "subject_id,study_id,dicom_id,StudyDate,StudyTime,STUDYTIME\n"]
        for content in contents:
            with self.subTest(content=content):
                with gzip.open(self.metadata, "wt") as out:
                    out.write(content)
                with self.assertRaises(ValueError):
                    self.initialize()
                self.assertFalse(self.db.exists())

    def test_malformed_csv_row_and_invalid_numeric_identifier_are_rejected(self):
        with gzip.open(self.metadata, "wt") as out:
            out.write(",".join(FIELDS) + "\n90000001,99000001,synthetic-a\n")
        with self.assertRaisesRegex(ValueError, "column count"):
            self.initialize()
        for identifier in ("90000001.0", "-1", "0", "9223372036854775808"):
            with self.subTest(identifier=identifier):
                self.rows[0][0] = identifier
                self.write_metadata()
                with self.assertRaisesRegex(ValueError, "invalid positive BIGINT"):
                    self.initialize()

    def test_concurrent_new_destination_is_not_overwritten(self):
        original = initializer.create_indexes

        def create_other_destination(conn):
            original(conn)
            self.db.write_bytes(b"another-writer-output")

        with patch.object(initializer, "create_indexes", side_effect=create_other_destination):
            with self.assertRaises(FileExistsError):
                self.initialize()
        self.assertEqual(self.db.read_bytes(), b"another-writer-output")


    def test_new_index_contains_reports_not_files_and_keeps_provenance(self):
        result = self.initialize()
        self.assertEqual(result["reports"]["nonempty_studies"], 2)
        self.assertFalse(result["images_used"])
        self.assertEqual(self.db.stat().st_mode & 0o777, 0o600)
        with open_report_database(self.db) as conn:
            tables = {r[0] for r in conn.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='cxr'").fetchall()}
            self.assertEqual(tables, {"metadata_raw", "metadata_ts2", "reports", "index_audit"})
            self.assertEqual(conn.execute("SELECT study_ts FROM cxr.metadata_ts2 ORDER BY study_id").fetchone()[0],
                             datetime(2020, 1, 1, 11, 0, 0, 125000))
            audit = conn.execute("SELECT audit_json FROM cxr.index_audit").fetchone()[0]
            self.assertNotIn(str(self.root), audit)
            self.assertNotIn("90000001", audit)
        self.assertFalse(list(self.root.rglob("*.jpg")))

    def test_study_selection_boundary_and_chronological_order(self):
        self.create_hosp()
        self.initialize()
        with open_report_database(self.db) as conn:
            selected = extract_cxr_for_hadm(conn, 98000001, datetime(2020,1,1,10),
                                           datetime(2020,1,1,12), cxr_conn=conn)
            self.assertEqual([r["study_id"] for r in selected["studies"]], [99000001])
            all_reports = extract_cxr_for_hadm(conn, 98000001, datetime(2020,1,1,10),
                                              datetime(2020,1,1,13), cxr_conn=conn)
            self.assertEqual([r["study_id"] for r in all_reports["studies"]], [99000001, 99000002])
            latest = extract_cxr_for_hadm(conn, 98000001, datetime(2020,1,1,10),
                                         datetime(2020,1,1,13), max_studies=1, cxr_conn=conn)
            self.assertEqual(latest["studies"][0]["study_id"], 99000002)

    def test_numeric_serialization_is_not_midnight(self):
        self.rows[0][-1] = "90000.73999999999"
        self.rows[1][-1] = "093005.125"
        self.write_metadata()
        result = self.initialize()
        self.assertEqual(result["leading_zero_padded_times"], 1)
        self.assertEqual(result["long_fraction_times_rounded"], 1)
        with open_report_database(self.db) as conn:
            self.assertEqual(conn.execute("SELECT study_ts FROM cxr.metadata_ts2 ORDER BY study_id").fetchone()[0],
                             datetime(2020,1,1,9,0,0,740000))

    def test_partial_reports_are_counted_and_identical_duplicates_collapsed(self):
        self.reports.write_text(json.dumps([{"study_id": 99000001, "report": "A"},
                                          {"study_id": "99000001", "report": "A"},
                                          {"study_id": 99000002, "report": ""}]))
        audit = self.initialize()["reports"]
        self.assertEqual(audit["nonempty_studies"], 1)
        self.assertEqual(audit["metadata_studies_without_report"], 1)
        self.assertEqual(audit["identical_duplicate_items"], 1)
        self.assertEqual(audit["blank_items"], 1)

    def test_conflicting_duplicate_or_wrong_subject_report_fails(self):
        variants = [
            [{"study_id": 99000001, "report": "A"}, {"study_id": 99000001, "report": "B"}],
            [{"study_id": 99000001, "subject_id": 42, "report": "A"}],
            [{"study_id": 99111111, "report": "A"}],
            [{"study_id": 99000001, "report": {"unexpected": "object"}}],
            [{"study_id": 99000001, "report": ""}],
        ]
        for records in variants:
            self.reports.write_text(json.dumps(records))
            with self.assertRaises(ValueError):
                self.initialize()
            self.assertFalse(self.db.exists())

    def test_native_report_txt_works_without_images(self):
        root = self.root / "reports"
        folder = root / "p90" / "p90000001"
        folder.mkdir(parents=True)
        (folder / "s99000001.txt").write_text("Invented native report.")
        result = initialize_index(self.db, self.metadata, reports_dir=root)
        self.assertEqual(result["reports"]["nonempty_studies"], 1)
        self.assertEqual(result["reports"]["source"], "mimic-cxr-txt")

    def test_report_symlink_escape_is_rejected(self):
        root = self.root / "reports"
        folder = root / "p90" / "p90000001"
        folder.mkdir(parents=True)
        other = self.root / "outside.txt"
        other.write_text("Invented report.")
        (folder / "s99000001.txt").symlink_to(other)
        with self.assertRaisesRegex(ValueError, "escapes"):
            initialize_index(self.db, self.metadata, reports_dir=root)

    def test_legacy_index_refused_and_not_changed(self):
        self.create_hosp()
        with duckdb.connect(str(self.db)) as conn:
            conn.execute("CREATE SCHEMA cxr")
            conn.execute("CREATE TABLE cxr.metadata_ts2(study_ts TIMESTAMP)")
        before = self.db.read_bytes()
        with self.assertRaises(ValueError):
            open_report_database(self.db)
        self.assertEqual(self.db.read_bytes(), before)

    def test_initializer_cli_and_help(self):
        args = ["--db", self.db, "--metadata-csv", self.metadata, "--reports-json", self.reports]
        preview = run_cli("initialize_cxr_index.py", *args, "--dry-run")
        self.assertEqual(json.loads(preview.stdout)["status"], "validated_only")
        self.assertFalse(self.db.exists())
        run_cli("initialize_cxr_index.py", *args)
        run_cli("initialize_cxr_index.py", *args, expected=2)
        for script in SCRIPT_DIR.glob("*.py"):
            if script.name in {"cxr_reports"}:
                continue
            if "def main(" in script.read_text():
                run_cli(script.name, "--help")


if __name__ == "__main__":
    unittest.main()
