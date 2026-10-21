#!/usr/bin/env python3
"""Copy authorized local data and verify every file, without adding it to Git."""
import argparse
import hashlib
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def source_open(path):
    # NOATIME also avoids needless source-filesystem journal traffic on Linux.
    try:
        return os.fdopen(os.open(path, os.O_RDONLY | getattr(os, "O_NOATIME", 0)), "rb")
    except PermissionError:
        return path.open("rb")


def digest(path):
    h = hashlib.sha256()
    with source_open(path) as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def copy_verified(source, target):
    if source.is_symlink() or target.is_symlink():
        raise ValueError(f"Refusing symlink: {source.name}")
    target.parent.mkdir(parents=True, exist_ok=True)
    expected = digest(source)
    if target.exists():
        if digest(target) != expected:
            raise ValueError(f"Existing destination differs; not overwriting: {target}")
    else:
        temporary = target.with_name(target.name + ".partial")
        if temporary.is_symlink():
            raise ValueError(f"Refusing symlink: {temporary}")
        with source_open(source) as src, temporary.open("wb") as dst:
            for block in iter(lambda: src.read(4 * 1024 * 1024), b""):
                dst.write(block)
        if digest(temporary) != expected:
            raise IOError(f"Copy checksum mismatch: {target}")
        temporary.replace(target)
    return {"bytes": target.stat().st_size, "sha256": expected}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bench-source", type=Path)
    parser.add_argument("--retrieval-source", type=Path, help="Guidelines directory containing guidelines_index/")
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    args = parser.parse_args()
    if not args.bench_source and not args.retrieval_source:
        parser.error("Provide --bench-source and/or --retrieval-source")
    items = []
    if args.bench_source:
        source = args.bench_source.resolve()
        if not source.is_dir():
            parser.error("Benchmark source does not exist")
        for path in sorted(source.rglob("*")):
            if path.is_symlink():
                parser.error("Benchmark source contains symlinks; materialize them first")
            if path.is_file():
                items.append((path, Path("bench") / path.relative_to(source)))
    if args.retrieval_source:
        source = args.retrieval_source.resolve()
        required = ["guidelines_index/corpus_passages.jsonl", "guidelines_index/e5_Flat.index", "guidelines_index/docids.jsonl"]
        optional = ["open_guidelines.jsonl", "guidelines.jsonl"]
        for relative in required + optional:
            path = source / relative
            if not path.is_file():
                if relative in required:
                    parser.error(f"Missing retrieval asset: {relative}")
                continue
            items.append((path, Path("retrieval") / relative))
    records = []
    for index, (source, relative) in enumerate(items, 1):
        target = args.data_dir / relative
        if source.resolve() == target.resolve():
            parser.error("Source and destination must differ")
        record = copy_verified(source, target)
        records.append({"path": str(relative), **record})
        if index % 1000 == 0:
            print(f"Verified {index}/{len(items)} files", flush=True)
    manifests = args.data_dir / "private"
    manifests.mkdir(parents=True, exist_ok=True)
    label = "all" if args.bench_source and args.retrieval_source else "bench" if args.bench_source else "retrieval"
    (manifests / f"import-{label}.json").write_text(json.dumps({"files": records, "total_bytes": sum(x["bytes"] for x in records)}, indent=2))
    print(json.dumps({"verified_files": len(records), "bytes": sum(x["bytes"] for x in records)}))


if __name__ == "__main__":
    main()
