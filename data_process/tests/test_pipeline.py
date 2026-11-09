"""Invented clinical fixtures only. No image files, real patients or API calls."""
import csv
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime

SCRIPT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPT_DIR))
import duckdb
from build_osce_dataset import get_all_eligible_hadm_ids, process_one
from check_database import inspect_database
from convert_osce_to_ehr import transform_one, process_file_single_output, _dump_single_dict
from import_hosp_note import import_csv
from initialize_cxr_index import initialize_index
from cxr_reports import open_report_database
from osce_extractor import (build_osce_case, compute_diag_window_or_raise,
                           extract_cxr_for_hadm, extract_labs, get_first_hosp_med_time)
from sample_benchmark import parse_category

CASE_IDS = tuple(90_000_000 + i for i in range(1, 5))
SUBSETS = ("with_img/ed_hosp", "with_img/hosp_only",
           "without_img/ed_hosp", "without_img/hosp_only")
START = datetime(2020, 1, 1, 10)
END = datetime(2020, 1, 1, 12)


def make_database(path, cxr_db):
    with duckdb.connect(str(path)) as conn:
        conn.execute("CREATE SCHEMA hosp; CREATE SCHEMA mimiciv_ed; CREATE SCHEMA note")
        conn.execute("CREATE TABLE hosp.admissions(hadm_id BIGINT, subject_id BIGINT, admittime TIMESTAMP, dischtime TIMESTAMP)")
        conn.execute("CREATE TABLE hosp.patients(subject_id BIGINT, gender VARCHAR, anchor_age INTEGER)")
        conn.execute("CREATE TABLE hosp.emar(hadm_id BIGINT, charttime TIMESTAMP)")
        conn.execute("CREATE TABLE hosp.pharmacy(hadm_id BIGINT, starttime TIMESTAMP)")
        conn.execute("CREATE TABLE hosp.diagnoses_icd(hadm_id BIGINT, icd_code VARCHAR, icd_version INTEGER, seq_num INTEGER)")
        conn.execute("CREATE TABLE hosp.d_icd_diagnoses(icd_code VARCHAR, icd_version INTEGER, long_title VARCHAR)")
        conn.execute("INSERT INTO hosp.d_icd_diagnoses VALUES ('J189', 10, 'Synthetic fixture label')")
        conn.execute("CREATE TABLE mimiciv_ed.edstays(hadm_id BIGINT, stay_id BIGINT, intime TIMESTAMP, outtime TIMESTAMP)")
        conn.execute("CREATE TABLE hosp.labevents(hadm_id BIGINT, itemid BIGINT, charttime TIMESTAMP, value VARCHAR)")
        conn.execute("CREATE TABLE note.radiology(note_id VARCHAR, hadm_id BIGINT, charttime TIMESTAMP, storetime TIMESTAMP, text VARCHAR)")
        for index, identifier in enumerate(CASE_IDS):
            subject = index + 1
            conn.execute("INSERT INTO hosp.admissions VALUES (?, ?, ?, '2020-01-02 10:00:00')", [identifier, subject, START])
            conn.execute("INSERT INTO hosp.patients VALUES (?, 'F', 40)", [subject])
            conn.execute("INSERT INTO hosp.emar VALUES (?, ?)", [identifier, END])
            conn.execute("INSERT INTO hosp.diagnoses_icd VALUES (?, 'J189', 10, 1)", [identifier])
            if index in (0, 2):
                conn.execute("INSERT INTO mimiciv_ed.edstays VALUES (?, ?, '2020-01-01 09:00:00', '2020-01-01 11:00:00')", [identifier, subject])
        conn.execute("INSERT INTO note.radiology VALUES ('synthetic-note', ?, '2020-01-01 11:00:00', '2020-01-01 13:00:00', 'Invented observation.')", [CASE_IDS[0]])
    metadata = path.parent / "synthetic-metadata.csv"
    with metadata.open("w", newline="") as out:
        writer = csv.writer(out)
        writer.writerow(["subject_id", "study_id", "dicom_id", "StudyDate", "StudyTime"])
        writer.writerows([[1, 1, "fixture-a", "20200101", "110000.125"],
                          [2, 2, "fixture-b", "20200101", "110000"],
                          [1, 3, "fixture-end", "20200101", "120000"]])
    reports = path.parent / "synthetic-reports.json"
    reports.write_text(json.dumps([{"study_id": sid, "report": "Invented report observation."} for sid in (1, 2, 3)]))
    initialize_index(cxr_db, metadata, reports_json=reports)


def run_cli(script, *args, expected=0):
    result = subprocess.run([sys.executable, str(SCRIPT_DIR / script), *map(str, args)],
                            capture_output=True, text=True, timeout=120)
    if result.returncode != expected:
        raise AssertionError(f"{script}: exit={result.returncode}\n{result.stdout}\n{result.stderr}")
    return result


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="medenv-report-test-")
        self.root = Path(self.tmp.name)
        self.db, self.cxr_db = self.root / "clinical.duckdb", self.root / "reports.duckdb"
        make_database(self.db, self.cxr_db)
        self.conn = duckdb.connect(str(self.db))

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_emar_priority_and_admission_boundary(self):
        self.conn.execute("INSERT INTO hosp.emar VALUES (?, ?)", [CASE_IDS[0], START])
        self.conn.execute("INSERT INTO hosp.pharmacy VALUES (?, '2020-01-01 11:00:00')", [CASE_IDS[0]])
        self.assertEqual(get_first_hosp_med_time(self.conn, CASE_IDS[0], START), END)

    def test_pharmacy_fallback_and_batch_candidates(self):
        self.conn.execute("DROP TABLE hosp.emar")
        self.conn.execute("INSERT INTO hosp.pharmacy VALUES (?, ?)", [CASE_IDS[0], END])
        self.assertEqual(get_first_hosp_med_time(self.conn, CASE_IDS[0], START), END)
        self.assertEqual(get_all_eligible_hadm_ids(self.conn), [CASE_IDS[0]])

    def test_missing_medication_rejected(self):
        self.conn.execute("DELETE FROM hosp.emar")
        with self.assertRaisesRegex(ValueError, "t0_med is NULL"):
            compute_diag_window_or_raise(self.conn, CASE_IDS[0], 2)

    def test_multiple_ed_stays_rejected(self):
        self.conn.execute("INSERT INTO mimiciv_ed.edstays SELECT * FROM mimiciv_ed.edstays WHERE hadm_id=?", [CASE_IDS[0]])
        self.assertEqual(get_all_eligible_hadm_ids(self.conn),
                         [CASE_IDS[2], CASE_IDS[1], CASE_IDS[3]])
        with self.assertRaisesRegex(ValueError, "1_stays_per_hadm"):
            compute_diag_window_or_raise(self.conn, CASE_IDS[0], 2)

    def test_ed_candidate_order_and_unlinked_rows_preserved(self):
        expected = [CASE_IDS[0], CASE_IDS[2], CASE_IDS[1], CASE_IDS[3]]
        self.assertEqual(get_all_eligible_hadm_ids(self.conn), expected)
        self.conn.execute("INSERT INTO mimiciv_ed.edstays VALUES (NULL, 99, '2020-01-01 09:00:00', '2020-01-01 11:00:00')")
        self.assertEqual(get_all_eligible_hadm_ids(self.conn), expected)
        self.assertEqual(len(set(expected)), len(expected))

    def test_multi_ed_exclusion_with_ed_schema_alias(self):
        self.conn.execute("CREATE SCHEMA ed")
        self.conn.execute("CREATE TABLE ed.edstays AS SELECT * FROM mimiciv_ed.edstays")
        self.conn.execute("DROP TABLE mimiciv_ed.edstays")
        self.conn.execute("INSERT INTO ed.edstays SELECT * FROM ed.edstays WHERE hadm_id=?", [CASE_IDS[0]])
        self.assertEqual(get_all_eligible_hadm_ids(self.conn),
                         [CASE_IDS[2], CASE_IDS[1], CASE_IDS[3]])
        with self.assertRaisesRegex(ValueError, "1_stays_per_hadm"):
            compute_diag_window_or_raise(self.conn, CASE_IDS[0], 2)

    def test_no_ed_module_preserves_hospital_candidates(self):
        self.conn.execute("DROP TABLE mimiciv_ed.edstays")
        self.assertEqual(get_all_eligible_hadm_ids(self.conn), list(CASE_IDS))

    def test_wrapper_completes_with_ambiguous_ed_admission_excluded(self):
        ambiguous_id = 90_000_005
        self.conn.execute("INSERT INTO hosp.admissions SELECT ?, subject_id, admittime, dischtime FROM hosp.admissions WHERE hadm_id=?",
                          [ambiguous_id, CASE_IDS[0]])
        self.conn.execute("INSERT INTO hosp.emar VALUES (?, ?)", [ambiguous_id, END])
        self.conn.execute("INSERT INTO hosp.diagnoses_icd VALUES (?, 'J189', 10, 1)", [ambiguous_id])
        for stay_id in (50, 51):
            self.conn.execute("INSERT INTO mimiciv_ed.edstays VALUES (?, ?, '2020-01-01 09:00:00', '2020-01-01 11:00:00')",
                              [ambiguous_id, stay_id])
        self.assertNotIn(ambiguous_id, get_all_eligible_hadm_ids(self.conn))
        self.conn.close()
        before = self.db.read_bytes()
        environment = {k: v for k, v in os.environ.items() if not k.startswith("MEDENV_")}
        out, bench = self.root / "ambiguity-osce", self.root / "ambiguity-bench"
        environment.update(MEDENV_PYTHON=sys.executable, MEDENV_DB=str(self.db),
                           MEDENV_CXR_DB=str(self.cxr_db), MEDENV_WORKERS="2",
                           MEDENV_OSCE_ROOT=str(out), MEDENV_BENCH_ROOT=str(bench))
        result = subprocess.run(["bash", str(SCRIPT_DIR / "run_pipeline.sh")], env=environment,
                                capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("4 OK, 0 SKIP", result.stdout)
        self.assertEqual(len(list(bench.rglob("osce_*.json"))), 4)
        self.assertEqual(len(list(bench.rglob("ehr_*.json"))), 4)
        self.assertFalse(list(out.rglob(f"osce_{ambiguous_id}.json")))
        self.assertFalse(list(bench.rglob(f"ehr_{ambiguous_id}.json")))
        self.assertEqual(before, self.db.read_bytes())

    def test_hours_parameter_does_not_change_endpoint(self):
        result = compute_diag_window_or_raise(self.conn, CASE_IDS[1], 0.01)
        self.assertIsNone(result[4])
        self.assertEqual(result[2], END)

    def test_lab_window_left_closed_right_open(self):
        for ts, value in (("2020-01-01 09:59:59", "before"), ("2020-01-01 10:00:00", "start"),
                          ("2020-01-01 11:59:59", "inside"), ("2020-01-01 12:00:00", "end")):
            self.conn.execute("INSERT INTO hosp.labevents VALUES (?, 1, ?, ?)", [CASE_IDS[0], ts, value])
        self.assertEqual([r["value"] for r in extract_labs(self.conn, CASE_IDS[0], START, END, {})], ["start", "inside"])

    def test_cxr_report_endpoint_and_no_images(self):
        with open_report_database(self.cxr_db) as cxr:
            result = extract_cxr_for_hadm(self.conn, CASE_IDS[0], START, END, cxr_conn=cxr)
            self.assertEqual([st["study_id"] for st in result["studies"]], [1])
            self.assertNotIn("dicoms", result["studies"][0])
            self.assertIsNone(result["studies"][0]["report_storetime"])
            self.assertFalse(result["report_availability_verified"])
            ok, _ = process_one(self.conn, CASE_IDS[0], str(self.root / "osce"), cxr_conn=cxr)
        self.assertTrue(ok)
        obj = json.loads(next((self.root / "osce").rglob("osce_*.json")).read_text())
        self.assertIn("Invented report observation", obj["OSCE_Examination"]["Test_Results"]["CXR"]["studies"][0]["report_text"])
        self.assertFalse(list(self.root.rglob("*.jpg")))
        self.assertFalse(list(self.root.rglob("img")))

    def test_requested_cxr_failures_are_not_silently_ignored(self):
        with duckdb.connect(":memory:") as bad:
            with self.assertRaises(duckdb.Error):
                build_osce_case(self.conn, CASE_IDS[0], cxr_conn=bad)
        self.conn.close()
        with self.assertRaises(ValueError):
            open_report_database(self.db)

    def test_case_overwrite_refused(self):
        out = self.root / "osce"
        self.assertTrue(process_one(self.conn, CASE_IDS[0], str(out))[0])
        with self.assertRaises(FileExistsError):
            process_one(self.conn, CASE_IDS[0], str(out))

    def test_conversion_preserves_report_times_without_image_paths(self):
        with open_report_database(self.cxr_db) as cxr:
            obj = build_osce_case(self.conn, CASE_IDS[0], cxr_conn=cxr)
        converted = transform_one(obj)
        self.assertEqual(set(converted), {"ehr", "knowledge"})
        ehr = converted["ehr"]
        self.assertEqual(ehr["Final_Result"], "Synthetic fixture label")
        self.assertNotIn("actions", ehr)
        self.assertNotIn("_debug", ehr)
        self.assertEqual(ehr["Meta"]["diagnostic_window"]["start"], "2020-01-01 09:00:00")
        self.assertEqual(ehr["CXR"][0]["study_id"], 1)
        self.assertEqual(ehr["CXR"][0]["time_basis"], "study_acquisition")
        self.assertNotIn("jpg_path", json.dumps(converted, default=str))
        self.assertEqual(str(ehr["Test_Results-Imaging"][0]["storetime"]), "2020-01-01 13:00:00")

    def test_conversion_rejects_multiple_malformed_and_existing_outputs(self):
        path = self.root / "input.json"
        for content in ('[{"OSCE_Examination": {}}, {"OSCE_Examination": {}}]',
                        '{"ehr": {}}', '{"OSCE_Examination": {}}\n{broken'):
            path.write_text(content)
            with self.assertRaises(ValueError):
                process_file_single_output(path)
        with self.assertRaises(FileExistsError):
            _dump_single_dict(path, {})

    def test_icd_categories_and_import_preserve_leading_zero(self):
        self.assertTrue(parse_category("0019", 9).startswith("ICD-9 infectious"))
        self.assertTrue(parse_category("J18.9", 10).startswith("ICD-10 Diseases of the respiratory"))
        source = self.root / "fixture.csv"
        source.write_text("icd_code,icd_version\n0019,9\n0021,9\n")
        import_csv(self.conn, "testing", source)
        self.assertEqual(self.conn.execute("SELECT icd_code FROM testing.fixture ORDER BY icd_code").fetchall(), [("0019",), ("0021",)])
        with self.assertRaises(duckdb.CatalogException):
            import_csv(self.conn, "testing", source)

    def test_preflight_external_report_database(self):
        self.assertTrue(inspect_database(self.conn)["schema_ok"])
        self.assertFalse(inspect_database(self.conn, require_notes=True)["schema_ok"])
        self.assertFalse(inspect_database(self.conn, require_cxr=True)["schema_ok"])
        with open_report_database(self.cxr_db) as cxr:
            self.assertTrue(inspect_database(self.conn, require_cxr=True, cxr_conn=cxr)["schema_ok"])

    def test_full_cli_and_deterministic_non_destructive_sampling(self):
        self.conn.close()
        out = self.root / "osce"
        run_cli("build_osce_dataset.py", "--db", self.db, "--out", out, "--cxr-db", self.cxr_db, "--workers", 2)
        self.assertEqual(len(list(out.rglob("osce_*.json"))), 4)
        before = {p: p.read_bytes() for p in out.rglob("osce_*.json")}
        run_cli("convert_osce_to_ehr.py", "--input_dir", out, "--recursive", "--ehr_alongside", "--workers", 2)
        for path, data in before.items():
            self.assertEqual(path.read_bytes(), data)
        for subset in SUBSETS:
            self.assertEqual(len(list((out / subset).rglob("ehr_*.json"))), 1)
        dry = self.root / "dry"
        run_cli("sample_benchmark.py", "--input-root", out, "--output-root", dry, "--dry-run")
        self.assertFalse(dry.exists())
        first, second = self.root / "bench-one", self.root / "bench-two"
        for target in (first, second):
            run_cli("sample_benchmark.py", "--input-root", out, "--output-root", target, "--per-category", 2)
        self.assertEqual((first / "manifest.jsonl").read_bytes(), (second / "manifest.jsonl").read_bytes())
        self.assertEqual(len(list(out.rglob("ehr_*.json"))), 4)
        self.assertFalse(list(first.rglob("*.jpg")))
        run_cli("sample_benchmark.py", "--input-root", out, "--output-root", first, expected=2)
        run_cli("sample_benchmark.py", "--input-root", out, "--output-root", out / "nested", expected=2)
        repository = Path(os.environ.get("MEDENV_TEST_REPO_ROOT", SCRIPT_DIR.parent))
        for name, relative in (("fixture_validator", "scripts/validate_data.py"), ("fixture_loader", "medenv/data.py")):
            path = repository / relative
            if path.is_file():
                spec = importlib.util.spec_from_file_location(name, path)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                if name == "fixture_validator":
                    stats, errors = module.validate(out)
                    self.assertEqual(errors, [])
                else:
                    for subset in SUBSETS:
                        cases = module.load_cases_from_bench(str(out), subdir=subset)
                        self.assertEqual(len(cases), 1)

    def test_single_case_and_explicit_no_cxr_cli(self):
        self.conn.close()
        single = self.root / "single" / "osce_fixture.json"
        run_cli("osce_extractor.py", "--db", self.db, "--hadm_id", CASE_IDS[0], "--out", single, "--cxr-db", self.cxr_db)
        self.assertTrue(single.is_file())
        run_cli("osce_extractor.py", "--db", self.db, "--hadm_id", CASE_IDS[0], "--out", single, "--cxr-db", self.cxr_db, expected=2)
        out = self.root / "without-cxr"
        run_cli("build_osce_dataset.py", "--db", self.db, "--out", out, "--without-cxr-reports", "--workers", 1)
        self.assertFalse(list((out / "with_img").rglob("osce_*.json")))
        run_cli("build_osce_dataset.py", "--db", self.db, "--out", self.root / "bad", expected=2)

    def test_wrapper_and_repeated_output_protection(self):
        self.conn.close()
        environment = {k: v for k, v in os.environ.items() if not k.startswith("MEDENV_")}
        environment.update(MEDENV_PYTHON=sys.executable, MEDENV_DB=str(self.db),
                           MEDENV_CXR_DB=str(self.cxr_db), MEDENV_WORKERS="1",
                           MEDENV_OSCE_ROOT=str(self.root / "wrapper"),
                           MEDENV_BENCH_ROOT=str(self.root / "bench"))
        command = ["bash", str(SCRIPT_DIR / "run_pipeline.sh")]
        first = subprocess.run(command, env=environment, capture_output=True, text=True, timeout=120)
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertEqual(len(list((self.root / "bench").rglob("ehr_*.json"))), 4)
        second = subprocess.run(command, env=environment, capture_output=True, text=True, timeout=120)
        self.assertEqual(second.returncode, 2)

    def test_current_cli_dry_run_without_images(self):
        repository = Path(os.environ.get("MEDENV_TEST_REPO_ROOT", SCRIPT_DIR.parent / "integration"))
        if not (repository / "medenv/cli.py").is_file():
            self.skipTest("Surrounding runtime fixture not present")
        self.conn.close()
        out = self.root / "cli-cases"
        run_cli("build_osce_dataset.py", "--db", self.db, "--cxr-db", self.cxr_db,
                "--out", out, "--workers", 1)
        run_cli("convert_osce_to_ehr.py", "--input_dir", out, "--recursive", "--ehr_alongside", "--workers", 1)
        env = dict(os.environ, PYTHONPATH=str(repository))
        command = [sys.executable, "-c", "from medenv.cli import main; main()", "--dry_run",
                   "--case_dir", str(out), "--subdir", "with_img/ed_hosp"]
        result = subprocess.run(command, cwd=repository, env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        audit = json.loads(result.stdout)
        self.assertEqual(audit["cases"], 1)
        self.assertEqual(audit["image_references"], 0)
        self.assertIn("cxr", audit["tools"])
        self.assertNotIn("cxr_grounding", audit["tools"])
        vision = subprocess.run(command + ["--cxr_mode", "vision"], cwd=repository, env=env,
                                capture_output=True, text=True, timeout=60)
        self.assertNotEqual(vision.returncode, 0)
        self.assertIn("requires image-backed", vision.stderr)

    def test_importer_cli_never_overwrites_database(self):
        source = self.root / "csv"
        source.mkdir()
        (source / "patients.csv").write_text("subject_id,gender\n1,F\n")
        db = self.root / "imported.duckdb"
        run_cli("import_hosp_note.py", "--db", db, "--hosp-dir", source)
        run_cli("import_hosp_note.py", "--db", db, "--hosp-dir", source, expected=2)


if __name__ == "__main__":
    unittest.main()
