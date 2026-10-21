"""Report actual environment scoring separately from text matching."""
import json
import re


def final_answer(result):
    for step in reversed(result.steps):
        action = step.action
        if isinstance(action, list):
            for call in reversed(action):
                function = call.get("function", {})
                if function.get("name") in {"diagnosis", "finish"}:
                    arguments = function.get("arguments", {})
                    if isinstance(arguments, str):
                        try:
                            arguments = json.loads(arguments)
                        except json.JSONDecodeError:
                            continue
                    if isinstance(arguments, dict):
                        return str(arguments.get("final_response", arguments.get("response", "")))
        if isinstance(action, str) and action.strip():
            return action
    return ""


def evaluate_doctor_results(results, tasks, print_examples=0):
    if len(results) != len(tasks):
        raise ValueError("Result/task count mismatch")
    rows = []
    for result, task in zip(results, tasks):
        answer = final_answer(result)
        boxed = re.search(r"\\box(?:ed)?\{(.+?)\}", answer)
        prediction = boxed.group(1).strip() if boxed else answer.strip()
        info = result.steps[-1].info if result.steps else {}
        scored = info.get("is_correct")
        gold = str(task.get("ground_truth", ""))
        normalize = lambda text: " ".join(re.sub(r"[^a-z0-9 ]", " ", text.lower()).split())
        pred_n, gold_n = normalize(prediction), normalize(gold)
        calls = [call for step in result.steps if isinstance(step.action, list) for call in step.action]
        rows.append({
            "case_id": task["case_id"], "repeat_id": task.get("_repeat_id", 0),
            "prediction": prediction, "ground_truth": gold,
            "is_correct": scored, "reward": float(result.reward),
            "ask_patient_calls": sum(c.get("function", {}).get("name") == "ask_patient" for c in calls),
            "lenient_match": bool(pred_n and gold_n and (gold_n in pred_n or pred_n in gold_n)),
            "steps": len(result.steps),
        })
    groups = {}
    for row in rows:
        groups.setdefault(row["case_id"], []).append(row)
    correct = sum(r["is_correct"] is True for r in rows)
    scored = sum(r["is_correct"] is not None for r in rows)
    summary = {
        "total_runs": len(rows), "cases": len(groups), "scored_runs": scored,
        "unscored_runs": len(rows) - scored, "correct_runs": correct,
        "accuracy_all_runs": correct / len(rows) if rows else 0.0,
        "accuracy_scored_runs": correct / scored if scored else None,
        "empirical_pass_at_k": sum(any(r["is_correct"] is True for r in group) for group in groups.values()) / len(groups) if groups else 0.0,
        "mean_reward": sum(r["reward"] for r in rows) / len(rows) if rows else 0.0,
        "lenient_match_runs": sum(r["lenient_match"] for r in rows),
    }
    print(json.dumps(summary, indent=2))
    for row in rows[:print_examples]:
        print(json.dumps(row, ensure_ascii=False))
    return {"summary": summary, "rows": rows}
