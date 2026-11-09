#!/usr/bin/env python3
"""Validate benchmark EHR inputs, diagnosis labels and image references."""
import argparse
import json
from pathlib import Path

SUBSETS = ("with_img/ed_hosp", "with_img/hosp_only", "without_img/hosp_only", "without_img/ed_hosp")


def validate(root, cxr_mode="reports"):
    if cxr_mode not in ("reports", "vision"):
        raise ValueError("Unknown CXR mode")
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
                if not ehr.get("Final_Result"):
                    counts["missing_labels"] += 1
                    errors.append(f"Missing label: {path.relative_to(root)}")
                images = list(path.parent.glob("img/**/*"))
                counts["images"] += sum(p.is_file() for p in images)
                for study in ehr.get("CXR", []):
                    if cxr_mode == "reports":
                        text = study.get("report_text")
                        if not (isinstance(text, str) and text.strip()
                                or isinstance(text, list) and any(isinstance(x, str) and x.strip() for x in text)):
                            errors.append(f"Missing CXR report in {path.relative_to(root)}")
                        continue
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
    parser.add_argument("--output", type=Path)
    parser.add_argument("--cxr-mode", choices=("reports", "vision"), default="reports")
    args = parser.parse_args()
    stats, errors = validate(args.bench_root, args.cxr_mode)
    report = {"subsets": stats, "total_cases": sum(s["cases"] for s in stats.values()), "errors": errors}
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
    raise SystemExit(bool(errors))


if __name__ == "__main__":
    main()
