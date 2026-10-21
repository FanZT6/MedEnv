# Adapted for MedEnv (2026): portable benchmark-only packaging; see NOTICE and docs/CHANGES.md.
#!/usr/bin/env python3
"""Convert an rLLM medical trajectory JSON into an offline HTML reader."""

from __future__ import annotations

import argparse
import html
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any


def esc(value: object) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def parse_arguments(value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {"value": parsed}
        except json.JSONDecodeError:
            return {"raw": value}
    return {}


def first_tool_call(action: object) -> dict[str, Any]:
    if isinstance(action, list) and action:
        call = action[0] if isinstance(action[0], dict) else {}
    elif isinstance(action, dict):
        call = action
    else:
        return {"name": "plain_text", "arguments": {"content": str(action or "")}}
    function = call.get("function") or {}
    return {
        "id": call.get("id"),
        "name": function.get("name") or "unknown",
        "arguments": parse_arguments(function.get("arguments")),
    }


def observation_text(observation: object) -> tuple[str, str, str]:
    if isinstance(observation, dict):
        if "question" in observation:
            return "patient", "Patient / environment reply", str(observation.get("question") or "")
        if "tool_outputs" in observation:
            outputs = observation.get("tool_outputs") or {}
            parts = []
            source = "tool"
            for value in outputs.values():
                text = str(value)
                try:
                    parsed = json.loads(text)
                    if isinstance(parsed, dict):
                        exam = parsed.get("exam_type") or parsed.get("query")
                        result = parsed.get("result")
                        src = parsed.get("source")
                        if src:
                            source = str(src)
                        if exam:
                            parts.append(f"Requested: {exam}")
                        if result is not None:
                            parts.append(str(result))
                        else:
                            parts.append(json.dumps(parsed, ensure_ascii=False, indent=2))
                        continue
                except json.JSONDecodeError:
                    pass
                parts.append(text)
            label = "Recorded result" if source not in {"none", "tool"} else "Environment result"
            return "tool", label, "\n\n".join(parts)
        if "case_id" in observation:
            return "opening", "Case opening", str(observation.get("question") or "")
        return "environment", "Environment observation", json.dumps(observation, ensure_ascii=False, indent=2)
    return "environment", "Environment observation", str(observation or "")


def extract_final(step: dict[str, Any]) -> str:
    response = (step.get("info") or {}).get("response")
    calls = response if isinstance(response, list) else []
    for call in calls:
        function = (call or {}).get("function") or {}
        if function.get("name") not in {"finish", "diagnosis"}:
            continue
        args = parse_arguments(function.get("arguments"))
        value = args.get("response") or args.get("result") or ""
        match = re.search(r"\\boxed\{([^}]+)\}", str(value))
        return match.group(1) if match else str(value)
    metadata = (step.get("info") or {}).get("metadata") or {}
    return str(metadata.get("model_output_excerpt") or "")


def tool_title(name: str, args: dict[str, Any]) -> str:
    if name == "ask_patient":
        return str(args.get("question") or "Ask patient")
    if name == "request_exam":
        return str(args.get("exam_type") or "Request exam")
    if name == "retrieve":
        return str(args.get("query") or "Retrieve knowledge")
    if name in {"diagnosis", "finish"}:
        return str(args.get("result") or args.get("response") or "Submit diagnosis")
    return str(args.get("content") or name)


def build_model(payload: dict[str, Any]) -> dict[str, Any]:
    trajectory = payload.get("trajectory") or {}
    steps = trajectory.get("steps") or []
    initial_kind, initial_label, initial_text = observation_text((steps[0] if steps else {}).get("observation"))
    interactions = []
    for index, step in enumerate(steps):
        call = first_tool_call(step.get("action"))
        next_observation = steps[index + 1].get("observation") if index + 1 < len(steps) else None
        response_kind, response_label, response_text = observation_text(next_observation)
        interactions.append({
            "step": index + 1,
            "tool": call["name"],
            "arguments": call["arguments"],
            "title": tool_title(call["name"], call["arguments"]),
            "response_kind": response_kind,
            "response_label": response_label,
            "response": response_text if next_observation is not None else "No subsequent observation recorded.",
            "model_response": step.get("model_response") or "",
            "reward": step.get("reward"),
            "done": bool(step.get("done")),
            "metadata": (step.get("info") or {}).get("metadata") or {},
        })
    first_context = ((steps[0].get("observation") or {}).get("context") or {}) if steps else {}
    ehr = first_context.get("ehr") or {}
    meta = ehr.get("Meta") or {}
    patient = ehr.get("Patient_info") or {}
    history = ehr.get("History") or {}
    final = extract_final(steps[-1]) if steps else ""
    final_metadata = ((steps[-1].get("info") or {}).get("metadata") or {}) if steps else {}
    return {
        "saved_at": payload.get("saved_at"),
        "mode": payload.get("mode"),
        "uid": trajectory.get("uid"),
        "name": trajectory.get("name"),
        "reward": trajectory.get("reward"),
        "steps": len(steps),
        "initial": {"kind": initial_kind, "label": initial_label, "text": initial_text},
        "interactions": interactions,
        "counts": Counter(item["tool"] for item in interactions),
        "final": final,
        "reference": ehr.get("Final_Result") or "",
        "judge_consistent": final_metadata.get("judge_consistent"),
        "judge_raw": final_metadata.get("judge_raw_response"),
        "result_reward": final_metadata.get("result_reward"),
        "process_reward": final_metadata.get("process_reward"),
        "meta": meta,
        "patient": patient,
        "objective": ehr.get("Objective_for_Doctor") or "",
        "history": history,
        "physical": ehr.get("Physical_Examination_Findings") or "",
        "ehr": ehr,
    }


def render_interaction(item: dict[str, Any]) -> str:
    tool = item["tool"]
    cls = {
        "ask_patient": "ask",
        "request_exam": "exam",
        "retrieve": "retrieve",
        "diagnosis": "finish",
        "finish": "finish",
    }.get(tool, "other")
    labels = {
        "ask_patient": "问诊",
        "request_exam": "检查",
        "retrieve": "检索",
        "diagnosis": "诊断",
        "finish": "完成",
    }
    metadata = item.get("metadata") or {}
    audit = {
        "reward": item.get("reward"),
        "done": item.get("done"),
        "metadata": metadata,
    }
    return f'''
    <article class="turn turn-{cls}">
      <div class="rail"><span>{item['step']:02d}</span></div>
      <div class="turn-body">
        <header><div><span class="tool tool-{cls}">{esc(labels.get(tool, tool))}</span><h3>{esc(item['title'])}</h3></div><code>{esc(tool)}</code></header>
        <section class="response response-{esc(item['response_kind'])}"><span>{esc(item['response_label'])}</span><p>{esc(item['response'])}</p></section>
        <details><summary>查看模型原始输出与步骤元数据</summary><div class="raw-grid"><div><h4>Model response</h4><pre>{esc(item['model_response'])}</pre></div><div><h4>Step metadata</h4><pre>{esc(json.dumps(audit, ensure_ascii=False, indent=2))}</pre></div></div></details>
      </div>
    </article>'''


def render_html(model: dict[str, Any], source_name: str) -> str:
    counts = model["counts"]
    meta = model["meta"]
    patient = model["patient"]
    history = model["history"]
    judge_text = "一致" if model["judge_consistent"] is True else "未确认"
    interaction_html = "".join(render_interaction(item) for item in model["interactions"])
    return f'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src data:; connect-src 'none'; script-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'">
<title>医学 Agent 轨迹 · {esc(meta.get('hadm_id') or model['uid'])}</title>
<style>
:root{{--ink:#20272d;--muted:#6c767d;--line:#dedbd4;--paper:#fff;--wash:#f2f0eb;--ask:#3977a6;--exam:#9a6a22;--retrieve:#746098;--finish:#55764b;--danger:#b8443f}}*{{box-sizing:border-box}}html{{background:var(--wash);color:var(--ink);font:14px/1.55 Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}}body{{margin:0}}.page{{max-width:1120px;margin:auto;padding:26px 18px 50px}}.hero{{padding:26px;border:1px solid #d9d4cb;border-radius:18px;background:#fff;box-shadow:0 16px 40px rgba(47,43,36,.07)}}.eyebrow{{color:#887e72;font-size:10px;font-weight:800;letter-spacing:.12em}}h1{{margin:4px 0 9px;font-size:30px;letter-spacing:-.035em}}.hero>p{{max-width:780px;margin:0;color:var(--muted)}}.result{{display:grid;grid-template-columns:1.2fr 1fr;gap:10px;margin-top:18px}}.result article{{padding:15px;border-radius:12px;background:#f6f5f1}}.result span,.metric span{{display:block;color:var(--muted);font-size:10px;font-weight:700}}.result strong{{display:block;margin-top:3px;font-size:16px}}.result .success{{border-left:4px solid var(--finish)}}.metrics{{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:8px;margin:12px 0 18px}}.metric{{padding:12px;border:1px solid var(--line);border-radius:11px;background:#fff}}.metric b{{display:block;margin-top:3px;font-size:18px;font-variant-numeric:tabular-nums}}.context,.timeline,.method{{padding:20px;border:1px solid var(--line);border-radius:16px;background:#fff}}.context{{margin-bottom:16px}}.section-head{{display:flex;align-items:flex-end;justify-content:space-between;gap:16px;margin-bottom:13px}}.section-head h2{{margin:0;font-size:18px}}.section-head p{{margin:0;color:var(--muted);font-size:11px}}.case-grid{{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:8px}}.case-grid div{{padding:10px;border-radius:9px;background:#f7f6f2}}.case-grid span{{display:block;color:var(--muted);font-size:9px}}.case-grid b{{font-size:11px}}.opening{{margin-top:10px;padding:13px 15px;border-left:4px solid var(--ask);border-radius:8px;background:#edf4f8}}.opening span{{color:#4a6e84;font-size:9px;font-weight:800;letter-spacing:.08em}}.opening p{{margin:4px 0 0;white-space:pre-wrap}}.trajectory{{position:relative}}.turn{{display:grid;grid-template-columns:48px 1fr;gap:11px;position:relative}}.turn:not(:last-child) .rail:after{{content:"";position:absolute;left:23px;top:41px;bottom:-6px;width:1px;background:#d8d4cc}}.rail{{position:relative;display:flex;justify-content:center}}.rail span{{position:relative;z-index:1;display:flex;width:34px;height:34px;align-items:center;justify-content:center;border:1px solid #d7d2c9;border-radius:50%;background:#fff;color:#737c82;font-size:10px;font-weight:800}}.turn-body{{margin-bottom:13px;overflow:hidden;border:1px solid #dfdbd3;border-radius:13px;background:#fff}}.turn-body header{{display:flex;align-items:flex-start;justify-content:space-between;gap:18px;padding:13px 15px;border-bottom:1px solid #eeeae3}}.turn-body header>div{{display:flex;align-items:center;gap:9px}}.turn-body h3{{margin:0;font-size:13px}}.turn-body header code{{color:#8a9297;font-size:9px}}.tool{{padding:3px 6px;border-radius:5px;color:#fff;font-size:9px;font-weight:800;white-space:nowrap}}.tool-ask{{background:var(--ask)}}.tool-exam{{background:var(--exam)}}.tool-retrieve{{background:var(--retrieve)}}.tool-finish{{background:var(--finish)}}.tool-other{{background:#70787d}}.response{{padding:12px 15px;background:#fbfaf8}}.response>span{{color:#7a8388;font-size:9px;font-weight:800;letter-spacing:.06em;text-transform:uppercase}}.response p{{margin:4px 0 0;white-space:pre-wrap;color:#3c474e;font-size:12px}}details{{border-top:1px solid #ece8e1}}summary{{cursor:pointer;padding:9px 15px;color:#68747b;font-size:10px}}.raw-grid{{display:grid;grid-template-columns:1fr 1fr;gap:8px;padding:0 15px 14px}}.raw-grid div{{min-width:0}}h4{{margin:0 0 5px;font-size:10px}}pre{{max-height:280px;margin:0;overflow:auto;padding:10px;border-radius:8px;background:#252b30;color:#e7ecef;font:10px/1.5 ui-monospace,SFMono-Regular,Consolas,monospace;white-space:pre-wrap;word-break:break-word}}.method{{margin-top:16px}}.method details{{border-top:0;border-bottom:1px solid #ece8e1}}.method summary{{padding-left:0;font-size:11px;font-weight:750}}.method p{{white-space:pre-wrap;color:#465159;font-size:11px}}.source{{margin:16px 2px 0;color:var(--muted);font-size:9px;word-break:break-all}}
@media(max-width:800px){{.page{{padding:12px}}.result,.raw-grid{{grid-template-columns:1fr}}.metrics{{grid-template-columns:repeat(3,minmax(0,1fr))}}.case-grid{{grid-template-columns:1fr 1fr}}.section-head{{align-items:flex-start;flex-direction:column}}}}@media print{{html{{background:#fff}}.page{{max-width:none;padding:0}}.hero,.context,.timeline,.method{{box-shadow:none}}details{{display:none}}}}
</style></head><body><main class="page">
<header class="hero"><span class="eyebrow">RLLM · MEDICAL AGENT TRAJECTORY</span><h1>医学 Agent 诊断轨迹</h1><p>{esc(model['objective'] or '多轮问诊、检查请求与诊断过程')}</p><div class="result"><article class="success"><span>Agent 最终诊断</span><strong>{esc(model['final'] or '未提取到最终诊断')}</strong></article><article><span>参考诊断</span><strong>{esc(model['reference'] or '未提供')}</strong></article></div></header>
<section class="metrics"><article class="metric"><span>步骤</span><b>{model['steps']}</b></article><article class="metric"><span>问诊</span><b>{counts.get('ask_patient',0)}</b></article><article class="metric"><span>检查</span><b>{counts.get('request_exam',0)}</b></article><article class="metric"><span>检索</span><b>{counts.get('retrieve',0)}</b></article><article class="metric"><span>总奖励</span><b>{esc(model['reward'])}</b></article><article class="metric"><span>Judge</span><b>{esc(judge_text)}</b></article></section>
<section class="context"><div class="section-head"><div><span class="eyebrow">CASE CONTEXT</span><h2>病例入口信息</h2></div><p>只展示轨迹开始时 Doctor 可见的入口信息</p></div><div class="case-grid"><div><span>SUBJECT</span><b>{esc(meta.get('subject_id'))}</b></div><div><span>HADM</span><b>{esc(meta.get('hadm_id'))}</b></div><div><span>患者</span><b>{esc(patient.get('age'))} 岁 · {esc(patient.get('gender'))}</b></div><div><span>主诉</span><b>{esc(history.get('Chief_Complaint'))}</b></div></div><div class="opening"><span>INITIAL OBSERVATION</span><p>{esc(model['initial']['text'])}</p></div></section>
<section class="timeline"><div class="section-head"><div><span class="eyebrow">{model['steps']}-STEP TRACE</span><h2>Doctor 与环境交互</h2></div><p>每个动作与下一步 observation 配对显示</p></div><div class="trajectory">{interaction_html}</div></section>
<section class="method"><div class="section-head"><div><span class="eyebrow">AUDIT CONTEXT</span><h2>病例依据与评估说明</h2></div><p>默认折叠，供复核使用</p></div><details><summary>病史与既往史</summary><p><strong>HPI</strong>\n{esc(history.get('HPI'))}\n\n<strong>Past medical history</strong>\n{esc(history.get('Past_Medical_History'))}</p></details><details><summary>体格检查文本</summary><p>{esc(model['physical'])}</p></details><details><summary>轨迹与 Judge 元数据</summary><pre>{esc(json.dumps({'uid':model['uid'],'saved_at':model['saved_at'],'mode':model['mode'],'reward':model['reward'],'judge_consistent':model['judge_consistent'],'judge_raw':model['judge_raw'],'result_reward':model['result_reward'],'process_reward':model['process_reward']},ensure_ascii=False,indent=2))}</pre></details></section>
<footer class="source">源文件：{esc(source_name)} · 页面生成时间：{esc(datetime.now().astimezone().isoformat(timespec='seconds'))}。页面未展示或推断隐藏思维链。</footer>
</main></body></html>'''


def build_artifact(model: dict[str, Any], source_name: str, body: str) -> dict[str, Any]:
    generated_at = datetime.now().astimezone().isoformat(timespec="seconds")
    source = {"id": "trajectory", "label": source_name, "path": source_name}
    return {
        "surface": "report",
        "manifest": {
            "version": 1,
            "surface": "report",
            "title": "医学 Agent 诊断轨迹",
            "generatedAt": generated_at,
            "sources": [source],
            "blocks": [{"id": "trajectory-reader", "type": "html", "layout": "full", "body": body}],
        },
        "snapshot": {
            "version": 1,
            "generatedAt": generated_at,
            "status": "ready",
            "datasets": {
                "step_summary": [
                    {"step": item["step"], "tool": item["tool"], "reward": item["reward"], "done": item["done"]}
                    for item in model["interactions"]
                ]
            },
            "accessIssues": [],
        },
        "sources": [source],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--artifact", type=Path)
    args = parser.parse_args()
    output = args.output or args.input.with_suffix(".html")
    artifact_path = args.artifact or args.input.with_name(args.input.stem + "_artifact.json")
    payload = json.loads(args.input.read_text(encoding="utf-8"))
    model = build_model(payload)
    body = render_html(model, args.input.name)
    artifact = build_artifact(model, args.input.name, body)
    output.write_text(body, encoding="utf-8")
    artifact_path.write_text(json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {output} ({output.stat().st_size / 1024:.1f} KiB)")
    print(f"Wrote {artifact_path} ({artifact_path.stat().st_size / 1024:.1f} KiB)")


if __name__ == "__main__":
    main()
