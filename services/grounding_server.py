# Adapted for MedEnv (2026): portable benchmark-only packaging; see NOTICE and docs/CHANGES.md.
#!/usr/bin/env python3
"""
Grounding DINO 检测 API 服务：加载 IDEA-Research/grounding-dino-base，
对外提供 POST /ground，接收 image_base64 + text_prompts，返回带框检测结果。
用法（在 rllm 仓库根目录下）：
  python services/grounding_server.py [--host 0.0.0.0] [--port 30050] [--model_dir ...]
"""

import argparse
import base64
import io
import logging
import time
from typing import Any, List

logger = logging.getLogger(__name__)

# 延迟导入，避免未用 API 时占显存
_processor = None
_model = None
_device = "cpu"


def _normalize_text_prompt(text_prompts: List[str]) -> str:
    """Grounding DINO：小写，每个 query 以点结尾。"""
    parts = [str(x).strip().lower() for x in (text_prompts or []) if str(x).strip()]
    if not parts:
        return ""
    return ". ".join(parts) + "."


def _load_model(model_dir: str, device: str):
    global _processor, _model, _device
    if _processor is not None and _model is not None:
        return
    import torch
    from transformers import AutoModelForZeroShotObjectDetection, AutoProcessor

    _device = device if device else ("cuda" if torch.cuda.is_available() else "cpu")
    logger.info("Loading Grounding DINO from %s on %s", model_dir, _device)
    _processor = AutoProcessor.from_pretrained(model_dir, local_files_only=True)
    _model = AutoModelForZeroShotObjectDetection.from_pretrained(
        model_dir, local_files_only=True
    ).to(_device)
    _model.eval()


def run_grounding(
    image_base64: str,
    text_prompts: List[str],
    box_threshold: float = 0.4,
    text_threshold: float = 0.3,
    model_dir: str = "",
    device: str = "",
) -> dict[str, Any]:
    """对一张图做 grounding 检测，返回 detections 列表。"""
    from PIL import Image

    import torch

    _load_model(model_dir or "IDEA-Research/grounding-dino-base", device)
    text_prompt = _normalize_text_prompt(text_prompts)
    if not text_prompt:
        return {"error": "text_prompts empty", "detections": []}

    raw = base64.b64decode(image_base64)
    image = Image.open(io.BytesIO(raw)).convert("RGB")

    # processor 期望 text 为 list of list: [["a cat", "a dog"]]
    text_labels = [[p.strip() for p in text_prompts if p.strip()]] if text_prompts else []
    if not text_labels or not text_labels[0]:
        return {"error": "text_prompts empty", "detections": []}

    inputs = _processor(images=image, text=text_labels, return_tensors="pt").to(_model.device)
    with torch.no_grad():
        outputs = _model(**inputs)

    results = _processor.post_process_grounded_object_detection(
        outputs,
        inputs.input_ids,
        threshold=box_threshold,
        text_threshold=text_threshold,
        target_sizes=[image.size[::-1]],
    )
    result = results[0]
    detections = []
    boxes = result.get("boxes")
    scores = result.get("scores")
    labels = result.get("text_labels", result.get("labels"))
    if boxes is not None and scores is not None:
        boxes_list = boxes.detach().cpu().tolist() if hasattr(boxes, "detach") else (boxes or [])
        scores_list = scores.detach().cpu().tolist() if hasattr(scores, "detach") else (scores or [])
        labels_list = [str(x) for x in labels] if labels is not None else ["object"] * len(scores_list)
        for box, score, label in zip(boxes_list, scores_list, labels_list):
            detections.append({
                "box": [round(x, 2) for x in box],
                "score": round(float(score), 3),
                "label": label,
            })

    return {"detections": detections, "text_prompts": text_prompts}


def create_app(model_dir: str, device: str):
    from fastapi import FastAPI, HTTPException
    from pydantic import BaseModel

    app = FastAPI(title="Grounding DINO API", version="0.1")

    class GroundRequest(BaseModel):
        image_base64: str
        text_prompts: List[str]
        box_threshold: float = 0.4
        text_threshold: float = 0.3

    @app.post("/ground")
    def ground(req: GroundRequest):
        t0 = time.time()
        try:
            out = run_grounding(
                image_base64=req.image_base64,
                text_prompts=req.text_prompts,
                box_threshold=req.box_threshold,
                text_threshold=req.text_threshold,
                model_dir=model_dir,
                device=device,
            )
            out["latency_s"] = round(time.time() - t0, 3)
            return out
        except Exception as e:
            logger.exception("grounding failed")
            raise HTTPException(status_code=500, detail=str(e))

    @app.get("/health")
    def health():
        return {"status": "ok"}

    return app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=30050)
    parser.add_argument("--model_dir", default="IDEA-Research/grounding-dino-base")
    parser.add_argument("--device", default="")
    args = parser.parse_args()

    import uvicorn

    app = create_app(model_dir=args.model_dir, device=args.device)
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
