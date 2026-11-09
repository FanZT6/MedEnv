#!/usr/bin/env python3
"""Check this code-only folder, NOT the whole repository or its Git history."""
import argparse
import hashlib
import json
from pathlib import Path
import re

APPROVED = {
    ".gitignore", "README.md", "VALIDATION.md", "source_manifest.json",
    "requirements.txt", "config.example.sh", "run_pipeline.sh",
    "import_hosp_note.py", "initialize_cxr_index.py", "cxr_reports.py",
    "check_database.py", "osce_extractor.py", "build_osce_dataset.py",
    "convert_osce_to_ehr.py", "sample_benchmark.py", "check_release.py",
    "tests/test_pipeline.py", "tests/test_cxr_index.py", "tests/test_report_runtime.py",
}
PATTERNS = {
    "personal_filesystem_root": re.compile(r"/(?:Users|home|data)/[A-Za-z][A-Za-z0-9_.-]*/"),
    "private_server_address": re.compile(r"(?<![0-9])(?:10\.[0-9]{1,3}|192\.168|172\.(?:1[6-9]|2[0-9]|3[01]))\.[0-9]{1,3}\.[0-9]{1,3}(?![0-9])"),
    "private_key_material": re.compile(r"-----BEGIN (?:OPENSSH |RSA |EC )?PRIVATE KEY-----"),
    "api_token": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b|\bghp_[A-Za-z0-9]{30,}\b"),
}


def check(root):
    root = Path(root)
    files, errors = {}, []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        if path.is_symlink():
            errors.append({"file": relative, "issue": "symlink"})
            continue
        if not path.is_file():
            continue
        if relative not in APPROVED:
            errors.append({"file": relative, "issue": "not in code-only allowlist"})
            continue
        raw = path.read_bytes()
        try:
            text = raw.decode("utf-8")
        except UnicodeError:
            errors.append({"file": relative, "issue": "not UTF-8 source"})
            continue
        files[relative] = hashlib.sha256(raw).hexdigest()
        for issue, pattern in PATTERNS.items():
            if pattern.search(text):
                errors.append({"file": relative, "issue": issue})
    for missing in sorted(APPROVED - set(files)):
        errors.append({"file": missing, "issue": "missing required source"})
    return {"scope": "data_process source folder only; heuristic scan, not a complete security guarantee",
            "source_files": len(files), "sha256": files, "errors": errors}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent)
    args = parser.parse_args()
    report = check(args.root)
    print(json.dumps(report, indent=2))
    raise SystemExit(bool(report["errors"]))


if __name__ == "__main__":
    main()
