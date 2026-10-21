# Adapted for MedEnv (2026): portable benchmark-only packaging; see NOTICE and docs/CHANGES.md.
#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import json
import os
from pathlib import Path

import numpy as np
import torch
from tqdm import tqdm

try:
    import faiss  # faiss-cpu / faiss-gpu
except ImportError as e:
    raise RuntimeError("FAISS not found. Please install: pip install faiss-cpu (or faiss-gpu)") from e

from transformers import AutoTokenizer, AutoModel


def mean_pooling(last_hidden_state: torch.Tensor, attention_mask: torch.Tensor) -> torch.Tensor:
    # last_hidden_state: [B, L, H]
    mask = attention_mask.unsqueeze(-1).type_as(last_hidden_state)  # [B, L, 1]
    summed = torch.sum(last_hidden_state * mask, dim=1)             # [B, H]
    counts = torch.clamp(mask.sum(dim=1), min=1e-9)                 # [B, 1]
    return summed / counts


def l2_normalize(x: torch.Tensor) -> torch.Tensor:
    return torch.nn.functional.normalize(x, p=2, dim=1)


def split_to_passages(text: str, tokenizer, chunk_tokens: int) -> list[str]:
    """
    把超长文本切成多个 passage，尽量在段落边界切。
    chunk_tokens 是“近似 token 上限”（不含 special tokens），避免被 max_length 截断丢信息。
    """
    text = (text or "").strip()
    if not text:
        return [""]

    # 先按空行分段；如果没有空行，也会得到一个段
    paras = [p.strip() for p in text.split("\n\n") if p.strip()]
    passages = []
    cur = []
    cur_tok = 0

    for p in paras:
        p_tok = len(tokenizer.encode(p, add_special_tokens=False))
        # 段落本身就很长：再粗暴按行切
        if p_tok > chunk_tokens:
            lines = [ln.strip() for ln in p.split("\n") if ln.strip()]
            for ln in lines:
                ln_tok = len(tokenizer.encode(ln, add_special_tokens=False))
                if cur_tok + ln_tok > chunk_tokens and cur:
                    passages.append("\n".join(cur))
                    cur, cur_tok = [], 0
                cur.append(ln)
                cur_tok += ln_tok
            continue

        if cur_tok + p_tok > chunk_tokens and cur:
            passages.append("\n\n".join(cur))
            cur, cur_tok = [], 0

        cur.append(p)
        cur_tok += p_tok

    if cur:
        passages.append("\n\n".join(cur))

    return passages


@torch.no_grad()
def encode_texts_e5(model, tokenizer, texts: list[str], device: str, max_length: int, fp16: bool) -> np.ndarray:
    """
    E5 推荐：passage 侧加前缀 'passage: '，query 侧加 'query: '。
    这里做 passage 编码。
    """
    inputs = tokenizer(
        texts,
        padding=True,
        truncation=True,
        max_length=max_length,
        return_tensors="pt",
    )
    inputs = {k: v.to(device) for k, v in inputs.items()}

    if fp16 and device.startswith("cuda"):
        with torch.autocast(device_type="cuda", dtype=torch.float16):
            outputs = model(**inputs)
            emb = mean_pooling(outputs.last_hidden_state, inputs["attention_mask"])
    else:
        outputs = model(**inputs)
        emb = mean_pooling(outputs.last_hidden_state, inputs["attention_mask"])

    emb = l2_normalize(emb)
    return emb.detach().cpu().numpy().astype("float32")


def build_index(args):
    in_path = Path(args.input_jsonl)
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    device = "cuda" if torch.cuda.is_available() and not args.force_cpu else "cpu"
    print(f"[Info] device = {device}")

    tokenizer = AutoTokenizer.from_pretrained(args.model_name)
    model = AutoModel.from_pretrained(args.model_name)
    model.eval()
    model.to(device)

    # 先创建 faiss index（IndexFlatIP + L2 normalize => cosine）
    index = None
    docids_path = out_dir / "docids.jsonl"
    corpus_path = out_dir / "corpus_passages.jsonl"

    total_docs = 0
    total_passages = 0

    # 写出：每个 passage 一行（方便后续根据检索结果回读文本）
    f_docids = docids_path.open("w", encoding="utf-8")
    f_corpus = corpus_path.open("w", encoding="utf-8")

    batch_texts = []
    batch_meta = []  # (passage_id, orig_id, passage_idx)

    def flush_batch(nonlocal_index):
        nonlocal total_passages, index
        if not batch_texts:
            return

        # E5 passage 前缀
        enc_texts = [f"passage: {t}" for t in batch_texts]
        embs = encode_texts_e5(
            model=model,
            tokenizer=tokenizer,
            texts=enc_texts,
            device=device,
            max_length=args.max_length,
            fp16=(not args.no_fp16),
        )

        if index is None:
            dim = embs.shape[1]
            index = faiss.IndexFlatIP(dim)

        index.add(embs)  # 顺序加入，行号就是向量id
        start_row = total_passages
        for i, meta in enumerate(batch_meta):
            passage_id, orig_id, pidx, text = meta
            row_id = start_row + i

            # 保存 row_id -> passage_id 映射
            f_docids.write(json.dumps({
                "row_id": row_id,
                "passage_id": passage_id,
                "orig_id": orig_id,
                "passage_idx": pidx
            }, ensure_ascii=False) + "\n")

            # 保存 passage 文本（用于检索后取回）
            f_corpus.write(json.dumps({
                "passage_id": passage_id,
                "contents": text
            }, ensure_ascii=False) + "\n")

        total_passages += len(batch_texts)
        batch_texts.clear()
        batch_meta.clear()

    # 逐行读入 jsonl，流式处理
    with in_path.open("r", encoding="utf-8") as f:
        for line in tqdm(f, desc="Reading & chunking", unit="lines"):
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            orig_id = str(obj.get("id"))
            contents = obj.get("contents", "")

            total_docs += 1

            # 切成多个 passage（避免 512 截断）
            passages = split_to_passages(contents, tokenizer, args.chunk_tokens)

            for pidx, ptext in enumerate(passages):
                passage_id = f"{orig_id}_{pidx}"
                batch_texts.append(ptext)
                batch_meta.append((passage_id, orig_id, pidx, ptext))

                if len(batch_texts) >= args.batch_size:
                    flush_batch(index)

    # flush 最后不足一批的
    flush_batch(index)

    f_docids.close()
    f_corpus.close()

    if index is None:
        raise RuntimeError("No passages found. Check your input file.")

    # 保存 index
    index_path = out_dir / args.index_name
    faiss.write_index(index, str(index_path))

    print("\n[Done]")
    print(f"  docs processed      : {total_docs}")
    print(f"  passages generated  : {total_passages}")
    print(f"  FAISS index saved   : {index_path}")
    print(f"  docids mapping saved: {docids_path}")
    print(f"  corpus passages saved: {corpus_path}")
    print("\n[Reminder] 用 E5 检索时，query 要加前缀：'query: ' 并做同样的 pooling + L2 normalize。")


def parse_args():
    ap = argparse.ArgumentParser("Build FAISS index for guidelines.jsonl (id, contents) using E5.")
    ap.add_argument("--input_jsonl", type=str, required=True, help="Path to guidelines.jsonl (one json per line).")
    ap.add_argument("--output_dir", type=str, required=True, help="Directory to save index and mappings.")
    ap.add_argument("--model_name", type=str, default="intfloat/e5-base-v2", help="Embedding model name.")
    ap.add_argument("--index_name", type=str, default="e5_Flat.index", help="Output FAISS index filename.")
    ap.add_argument("--batch_size", type=int, default=64, help="Encoding batch size.")
    ap.add_argument("--max_length", type=int, default=512, help="Tokenizer max_length (truncation).")
    ap.add_argument("--chunk_tokens", type=int, default=380, help="Chunk size in tokens to avoid truncation.")
    ap.add_argument("--no_fp16", action="store_true", help="Disable fp16 autocast on CUDA.")
    ap.add_argument("--force_cpu", action="store_true", help="Force CPU even if CUDA is available.")
    return ap.parse_args()


if __name__ == "__main__":
    args = parse_args()
    build_index(args)


"""
python ./examples/search/guidelines/build_guidelines_faiss_index.py \
  --input_jsonl ./examples/search/guidelines/guidelines.jsonl \
  --output_dir  ./examples/search/guidelines/guidelines_index \
  --max_length 512 \
  --chunk_tokens 380
"""
