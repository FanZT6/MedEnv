# Modified for MedEnv; see NOTICE.
#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import json
from pathlib import Path

from tqdm import tqdm


def convert_jsonl(input_path: str, output_path: str, start_id: int = 0) -> None:
    input_path = Path(input_path)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    written = 0
    skipped = 0

    with input_path.open("r", encoding="utf-8") as fin, output_path.open("w", encoding="utf-8") as fout:
        for idx, line in tqdm(enumerate(fin, start=start_id), desc="Converting", unit="lines"):
            line = line.strip()
            if not line:
                skipped += 1
                continue

            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                skipped += 1
                continue

            clean_text = obj.get("clean_text")
            if clean_text is None:
                # 如果某些行缺 clean_text，就用空串（也可以改成 continue 跳过）
                clean_text = ""

            out_obj = {
                "id": str(idx),
                "contents": clean_text
            }

            fout.write(json.dumps(out_obj, ensure_ascii=False) + "\n")
            written += 1

    print(f"Done. Written: {written}, Skipped: {skipped}")
    print(f"Output saved to: {output_path}")


def main():
    parser = argparse.ArgumentParser(description="Convert open_guidelines.jsonl to {id, contents} JSONL format.")
    parser.add_argument(
        "--input",
        default="data/retrieval/open_guidelines.jsonl",
        help="Input JSONL file path"
    )
    parser.add_argument(
        "--output",
        default="data/retrieval/guidelines.jsonl",
        help="Output JSONL file path"
    )
    parser.add_argument("--start_id", type=int, default=0, help="Starting id number (default: 0)")
    args = parser.parse_args()

    convert_jsonl(args.input, args.output, args.start_id)


if __name__ == "__main__":
    main()
