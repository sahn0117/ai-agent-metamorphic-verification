# -*- coding: utf-8 -*-
"""
behavior_alignment.py
=====================

Stage 4：Expected vs Observed 行為一致性比對
v5 IO-generalized：支援 class/function 與 original_io_problem 靜態證據對齊。
若沒有明確 mismatch，且已具備足夠結構證據，會允許 Stage 5 動態 MR 繼續驗證。
"""
from __future__ import annotations

import json
from pathlib import Path
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple


PROJECT_ROOT = Path(__file__).resolve().parent
WORKSPACE_ROOT = PROJECT_ROOT / "workspace"


def read_text_default(prompt: str, default: str = "") -> str:
    if default:
        raw = input(f"{prompt}（Enter 使用 {default}）：\n> ").strip()
        return raw or default
    return input(f"{prompt}：\n> ").strip()


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def latest_gen_id(run_id: str) -> str:
    base = WORKSPACE_ROOT / "behavior_extraction" / run_id
    gens = sorted([p.name for p in base.glob("GEN-*") if p.is_dir()])
    return gens[-1] if gens else "GEN-000001"


COMPATIBLE = {
    "public_api": {"public_api", "solve_function", "algorithm_function"},
    "solve_function": {"solve_function", "io_solver_behavior"},
    "io_solver_behavior": {"io_solver_behavior", "solve_function"},
    "algorithm_function": {"algorithm_function", "public_api"},
    "state_increase": {"state_increase"},
    "state_decrease": {"state_decrease"},
    "return_state": {"return_state", "aggregate_total"},
    "aggregate_total": {"aggregate_total", "return_state"},
    "collection_add": {"collection_add", "collection_update"},
    "collection_remove": {"collection_remove", "collection_update", "boundary_guard", "guard_reject"},
    "collection_update": {"collection_update", "collection_add", "collection_remove"},
    "guard_reject": {"guard_reject", "boundary_guard"},
    "boundary_guard": {"boundary_guard", "guard_reject"},
    "state_unchanged_on_invalid": {"guard_reject", "boundary_guard"},
}


def method_hint_from_desc(desc: str) -> Optional[str]:
    for token in ["increment", "decrement", "get_value", "get_total", "add_item", "remove_item", "deposit", "withdraw"]:
        if token in desc:
            return token
    return None


def score_match(exp: Dict[str, Any], obs: Dict[str, Any]) -> Tuple[int, str]:
    exp_type = exp.get("expected_operation_type")
    obs_type = obs.get("operation_type")
    desc = exp.get("description", "")
    score = 0
    reasons = []

    if obs_type in COMPATIBLE.get(exp_type, {exp_type}):
        score += 10
        reasons.append("operation_type compatible")
    else:
        return 0, "operation_type incompatible"

    if exp_type == "public_api":
        details = exp.get("details", {})
        req_class = details.get("class_name")
        req_methods = details.get("required_methods", []) or []
        obs_details = obs.get("details", {})
        obs_class = obs_details.get("class_name")
        public_methods = obs_details.get("public_methods", []) or []

        if req_class and obs_class == req_class:
            score += 4
            reasons.append("class_name matched")
        missing = [m for m in req_methods if m not in public_methods]
        if not missing:
            score += 8
            reasons.append("all public methods present")
        else:
            return 0, f"missing public methods: {missing}"

    if exp_type == "solve_function":
        details = exp.get("details", {})
        required_functions = details.get("required_functions", []) or ["solve"]
        obs_details = obs.get("details", {})
        fn = obs_details.get("function_name") or obs.get("method_name")
        if fn in required_functions:
            score += 12
            reasons.append("required solve function present")
        else:
            return 0, f"missing required solve function: {required_functions}"

    if exp_type in {"io_solver_behavior", "local_deterministic_execution"} and obs_type in {"io_solver_behavior", "solve_function", "algorithm_function"}:
        score += 8
        reasons.append("io solver structural evidence present")

    method_hint = method_hint_from_desc(desc)
    obs_method = obs.get("method_name", "")
    if method_hint and method_hint == obs_method:
        score += 5
        reasons.append("method matched")
    elif method_hint and method_hint in obs.get("evidence", ""):
        score += 3
        reasons.append("method mentioned in evidence")

    if exp_type in {"guard_reject", "boundary_guard", "state_unchanged_on_invalid"} and obs_type in {"guard_reject", "boundary_guard"}:
        score += 3
        reasons.append("guard evidence")

    if exp_type == "collection_add" and "add" in obs_method.lower():
        score += 4
    if exp_type == "collection_remove" and "remove" in obs_method.lower():
        score += 4

    return score, "; ".join(reasons)


def align(expected_units: List[Dict[str, Any]], observed_units: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    results = []
    used_obs = set()

    for exp in expected_units:
        best = None
        best_score = 0
        best_reason = ""

        for obs in observed_units:
            score, reason = score_match(exp, obs)
            if score > best_score:
                best = obs
                best_score = score
                best_reason = reason

        if best and best_score >= 10:
            used_obs.add(best.get("observed_unit_id"))
            results.append({
                "expected_unit_id": exp.get("expected_unit_id"),
                "source_id": exp.get("source_id"),
                "expected_operation_type": exp.get("expected_operation_type"),
                "capability_hint": exp.get("capability_hint"),
                "description": exp.get("description"),
                "decision": "matched",
                "matched_observed_unit_id": best.get("observed_unit_id"),
                "matched_operation_type": best.get("operation_type"),
                "matched_method_name": best.get("method_name"),
                "evidence": best.get("evidence"),
                "score": best_score,
                "reason": best_reason,
            })
        else:
            results.append({
                "expected_unit_id": exp.get("expected_unit_id"),
                "source_id": exp.get("source_id"),
                "expected_operation_type": exp.get("expected_operation_type"),
                "capability_hint": exp.get("capability_hint"),
                "description": exp.get("description"),
                "decision": "unknown",
                "matched_observed_unit_id": None,
                "reason": "no compatible observed AST evidence found",
                "score": 0,
            })

    extra = [obs for obs in observed_units if obs.get("observed_unit_id") not in used_obs]
    return results, extra


def decide_stage4_ready(task_type: str, results: List[Dict[str, Any]], observed_units: List[Dict[str, Any]]) -> Tuple[bool, str, List[str]]:
    expected_count = len(results)
    matched = sum(1 for r in results if r["decision"] == "matched")
    unknown = sum(1 for r in results if r["decision"] == "unknown")
    mismatch = sum(1 for r in results if r["decision"] == "mismatch")

    blocking = []
    if mismatch:
        blocking.append("mismatch_expected_units_present")
        return False, "static_alignment_failed_mismatch", blocking

    if matched == 0:
        blocking.append("no_matched_expected_units")
        return False, "static_alignment_unknown_needs_further_validation", blocking

    public_results = [r for r in results if r.get("expected_operation_type") == "public_api"]
    if public_results and public_results[0]["decision"] != "matched":
        blocking.append("public_api_not_matched")
        return False, "static_alignment_unknown_needs_further_validation", blocking

    if unknown == 0:
        return True, "static_alignment_passed_semantics_not_fully_verified", []

    ratio = matched / max(expected_count, 1)
    if task_type in {"numeric_state", "collection_aggregate", "deterministic_class_state", "original_io_problem"} and ratio >= 0.50:
        return True, "static_alignment_partial_but_dynamic_validation_allowed", ["partial_static_unknown_delegated_to_stage5"]

    blocking.append("unknown_expected_units_present")
    return False, "static_alignment_unknown_needs_further_validation", blocking


def main() -> int:
    print("=" * 70)
    print("Stage 4：Expected vs Observed 行為一致性比對")
    print("v5：class/function + original IO 靜態對齊 + 動態驗證放行")
    print("=" * 70)

    run_id = read_text_default("請輸入 Stage 1 RUN-ID（例如 RUN-000001）")
    if not run_id:
        print("[ERROR] RUN-ID 不可為空")
        return 1
    gen_id = read_text_default("請輸入 Generation ID", latest_gen_id(run_id))

    in_dir = WORKSPACE_ROOT / "behavior_extraction" / run_id / gen_id
    expected_path = in_dir / "expected_behavior_units.json"
    observed_path = in_dir / "generated_behavior_spec.json"
    out_dir = WORKSPACE_ROOT / "behavior_alignment" / run_id / gen_id
    out_dir.mkdir(parents=True, exist_ok=True)

    if not expected_path.exists():
        print(f"\n[stderr]\n找不到 expected_behavior_units.json：{expected_path}")
        return 1
    if not observed_path.exists():
        print(f"\n[stderr]\n找不到 generated_behavior_spec.json：{observed_path}")
        return 1

    print("[1/3] 讀取 expected_behavior_units.json 與 generated_behavior_spec.json")
    expected_doc = read_json(expected_path)
    observed_doc = read_json(observed_path)

    expected_units = expected_doc.get("expected_behavior_units", [])
    observed_units = observed_doc.get("observed_behavior_units", [])
    task_type = observed_doc.get("task_type") or expected_doc.get("task_type") or "unknown"

    print("[2/3] 對齊 expected operation type 與 observed AST evidence")
    results, extra = align(expected_units, observed_units)
    stage4_ready, status, blocking = decide_stage4_ready(task_type, results, observed_units)

    matched = sum(1 for r in results if r["decision"] == "matched")
    unknown = sum(1 for r in results if r["decision"] == "unknown")
    mismatch = sum(1 for r in results if r["decision"] == "mismatch")

    print("[3/3] 輸出 behavior_alignment_report.json")
    snapshot = {
        "source_run_id": run_id,
        "generation_id": gen_id,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "input_files": {
            "expected_behavior_units": str(expected_path),
            "generated_behavior_spec": str(observed_path),
        },
    }

    report = {
        "schema_version": "stage4_behavior_alignment_report_v5_io_generalized",
        "source_run_id": run_id,
        "generation_id": gen_id,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "status": status,
        "stage4_ready": stage4_ready,
        "stage4_blocking_reasons": blocking,
        "task_type": task_type,
        "summary": {
            "expected_count": len(expected_units),
            "matched": matched,
            "unknown": unknown,
            "mismatch": mismatch,
            "matched_count": matched,
            "unknown_count": unknown,
            "mismatch_count": mismatch,
            "extra_observed": len(extra),
        },
        "alignment_results": results,
        "extra_observed_units": extra,
        "interpretation_rule": (
            "Stage 4 is structural only. If no mismatch exists and enough static evidence is present, "
            "remaining uncertainty is delegated to Stage 5 dynamic MR tests."
        ),
    }

    write_json(out_dir / "alignment_input_snapshot.json", snapshot)
    write_json(out_dir / "behavior_alignment_report.json", report)

    print("\n✅ 行為一致性比對完成")
    print(f"RUN-ID: {run_id}")
    print(f"GEN-ID: {gen_id}")
    print(f"狀態: {status}")
    print(f"任務型態: {task_type}")
    print(f"expected 行為數: {len(expected_units)}")
    print(f"matched: {matched}")
    print(f"unknown: {unknown}")
    print(f"mismatch: {mismatch}")
    print(f"extra observed: {len(extra)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
