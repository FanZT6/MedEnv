#!/usr/bin/env python3
"""Validate all four subsets, JSON pairs, labels, image references and optional hashes."""
import argparse
import hashlib
import json
from pathlib import Path

SUBSETS = ("with_img/ed_hosp", "with_img/hosp_only", "without_img/hosp_only", "without_img/ed_hosp")


def validate(root):
    stats, errors = {}, []
    for subset in SUBSETS:
        directory = root / subset
        files = sorted(directory.glob("*/ehr_*.json"))
        counts = {"cases": len(files), "images": 0, "image_references": 0, "missing_labels": 0}
        if not files:
            errors.append(f"Empty or missing subset: {subset}")
        for path in files:
            try:
                obj = json.loads(path.read_text())
                ehr = obj.get("ehr", obj)
                case_id = path.stem.removeprefix("ehr_")
                osce = path.with_name(f"osce_{case_id}.json")
                json.loads(osce.read_text())
                if not ehr.get("Final_Result"):
                    counts["missing_labels"] += 1
                    errors.append(f"Missing label: {path.relative_to(root)}")
                images = list(path.parent.glob("img/**/*"))
                counts["images"] += sum(p.is_file() for p in images)
                for study in ehr.get("CXR", []):
                    for dicom in study.get("dicoms", []):
                        image = path.parent / dicom["jpg_path"]
                        counts["image_references"] += 1
                        if not image.is_file():
                            errors.append(f"Missing image in {path.relative_to(root)}")
                        elif not image.resolve().is_relative_to(root.resolve()):
                            errors.append(f"Image outside benchmark: {path.relative_to(root)}")
            except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
                errors.append(f"Invalid case {path.relative_to(root)}: {type(error).__name__}")
        stats[subset] = counts
    return stats, errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bench-root", type=Path, default=Path("data/bench"))
    parser.add_argument("--checksums", type=Path, help="Private import manifest to recheck")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    stats, errors = validate(args.bench_root)
    if args.checksums:
        manifest = json.loads(args.checksums.read_text())
        data_root = args.checksums.resolve().parent.parent
        for item in manifest["files"]:
            path = data_root / item["path"]
            if not path.resolve().is_relative_to(data_root):
                errors.append("Checksum manifest path escapes data root")
                continue
            if not path.is_file():
                errors.append(f"Missing imported file: {item['path']}")
                continue
            h = hashlib.sha256()
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
                    h.update(block)
            if h.hexdigest() != item["sha256"]:
                errors.append(f"Checksum mismatch: {item['path']}")
    report = {"subsets": stats, "total_cases": sum(s["cases"] for s in stats.values()), "errors": errors}
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
    raise SystemExit(bool(errors))


if __name__ == "__main__":
    main()
