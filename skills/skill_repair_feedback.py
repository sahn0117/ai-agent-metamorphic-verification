# -*- coding: utf-8 -*-
"""
skill_repair_feedback.py
========================
Stage 6：缺陷回饋與修復請求產生技能

此 skill 將 Stage 5 驗證結果轉成可操作修復回饋。
設計原則：
1. 不直接修程式，避免驗證器與修復器責任混在一起。
2. 只根據硬證據產生 defect candidate，不用 LLM 當最終裁判。
3. 產出 machine-readable JSON 與 human-readable Markdown。
4. 保留 RUN / GEN / MR / evidence 對應關係，方便論文追蹤。
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# -----------------------------------------------------------------------------
# Basic IO helpers
# -----------------------------------------------------------------------------

def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _read_json(path: Path, required: bool = True, default: Optional[dict] = None) -> dict:
    if not path.exists():
        if required:
            raise FileNotFoundError(f"Required file not found: {path}")
        return default if default is not None else {}
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _safe_get(d: dict, path: List[Any], default: Any = None) -> Any:
    cur = d
    for key in path:
        if isinstance(cur, dict) and key in cur:
            cur = cur[key]
        else:
            return default
    return cur


def _rel_or_str(path: Path, project_root: Path) -> str:
    try:
        return str(path.relative_to(project_root)).replace("\\", "/")
    except Exception:
        return str(path)


# -----------------------------------------------------------------------------
# Locate current run / generation
# -----------------------------------------------------------------------------

def find_latest_generation_id(project_root: Path, run_id: str) -> str:
    base = project_root / "workspace" / "code_generation" / run_id
    if not base.exists():
        raise FileNotFoundError(f"找不到 code_generation RUN 資料夾: {base}")
    gens = sorted([p.name for p in base.iterdir() if p.is_dir() and p.name.startswith("GEN-")])
    if not gens:
        raise FileNotFoundError(f"找不到任何 GEN-* 資料夾: {base}")
    return gens[-1]


def prompt_for_run_and_generation(project_root: Path) -> Tuple[str, str]:
    run_id = input("請輸入 Stage 1 RUN-ID（例如 RUN-000007）：\n> ").strip()
    if not run_id:
        raise ValueError("RUN-ID 不可為空")
    gen_id = input("請輸入 Generation ID（Enter 使用最新 GEN）：\n> ").strip()
    if not gen_id:
        gen_id = find_latest_generation_id(project_root, run_id)
        print(f"使用最新 GEN：{gen_id}")
    return run_id, gen_id


def _next_repair_id(output_root: Path) -> str:
    output_root.mkdir(parents=True, exist_ok=True)
    existing = []
    for p in output_root.iterdir():
        if p.is_dir() and re.match(r"^REPAIR-\d{6}$", p.name):
            existing.append(int(p.name.split("-")[1]))
    idx = max(existing, default=0) + 1
    return f"REPAIR-{idx:06d}"


# -----------------------------------------------------------------------------
# Failure extraction and classification
# -----------------------------------------------------------------------------

def _get_runtime_results(mr_execution_report: dict) -> List[dict]:
    if isinstance(mr_execution_report.get("runtime_results"), list):
        return mr_execution_report["runtime_results"]
    if isinstance(mr_execution_report.get("results"), list):
        return mr_execution_report["results"]
    return []


def _is_formal_repair_failure(result: dict) -> bool:
    """Return True only for formal MR failures that should enter Stage 6 repair.

    Basic/sample sanity probes are useful warnings, but they are not treated as
    hard defect evidence for repair generation.  This keeps CPE/UVA sample-output
    formatting or PDF/OCR noise from triggering misleading Stage 7 repairs.
    """
    if not isinstance(result, dict):
        return False
    name = str(result.get("name") or "")
    status = str(result.get("status") or "").lower()
    if status == "skipped":
        return False
    lowered = name.lower()
    if name.startswith("BASIC_IO_") or name.startswith("PROBE_"):
        return False
    if "sample_consistency" in lowered or "sample consistency" in lowered:
        return False
    if name in {"MR_IO_SAMPLE_CONSISTENCY", "MR-IO-001_sample_consistency"}:
        return False
    return result.get("passed") is False or status in {"failed", "error"}


def _failed_results(mr_execution_report: dict) -> List[dict]:
    return [r for r in _get_runtime_results(mr_execution_report) if _is_formal_repair_failure(r)]


def _hash_signature(parts: List[str]) -> str:
    raw = "::".join(str(p) for p in parts if p is not None)
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:12]


def _find_line_hints(source: str, keywords: List[str]) -> List[dict]:
    hints = []
    lines = source.splitlines()
    for idx, line in enumerate(lines, start=1):
        stripped = line.strip()
        if any(k in stripped for k in keywords):
            hints.append({"line": idx, "code": stripped})
    return hints


def _detect_nan_state_corruption(failed_result: dict) -> bool:
    text = json.dumps(failed_result, ensure_ascii=False).lower()
    return "nan" in text and ("changed/corrupted state" in text or "after_deposit" in text or "after_withdraw" in text)


def _extract_failed_invalid_cases(failed_result: dict) -> List[dict]:
    cases = _safe_get(failed_result, ["details", "cases"], [])
    if not isinstance(cases, list):
        return []
    failed_cases = []
    for c in cases:
        if not isinstance(c, dict):
            continue
        dep_unchanged = c.get("deposit_state_unchanged", True)
        wd_unchanged = c.get("withdraw_state_unchanged", True)
        if dep_unchanged is False or wd_unchanged is False:
            failed_cases.append(c)
    return failed_cases


def classify_failed_result(failed_result: dict, generated_solution_source: str) -> dict:
    """Convert one failed runtime result into a defect candidate."""
    name = failed_result.get("name", "<unknown_test>")
    details = failed_result.get("details", {}) or {}
    failures = details.get("failures", []) if isinstance(details, dict) else []
    error = failed_result.get("error")

    if _detect_nan_state_corruption(failed_result):
        line_hints = _find_line_hints(
            generated_solution_source,
            ["def deposit", "def withdraw", "if amount <= 0", "if amount >", "self._balance +=", "self._balance -="],
        )
        signature = _hash_signature(["non_finite_numeric_input_not_rejected", name, "nan_state_corruption"])
        return {
            "defect_id": f"DEF-{signature}",
            "source_test": name,
            "defect_type": "non_finite_numeric_input_not_rejected",
            "severity": "critical",
            "confidence": "high",
            "title": "NaN invalid numeric input is not rejected and corrupts account state",
            "what_went_wrong": (
                "The generated code rejects amount <= 0 and over-limit withdrawal, "
                "but it does not reject non-finite numeric values such as NaN. "
                "As a result, deposit(float('nan')) and withdraw(float('nan')) can change the balance to NaN."
            ),
            "why_it_is_hidden": (
                "Normal positive inputs pass, Stage 4 static alignment passes, and simple probes pass. "
                "The defect only appears when an invalid non-finite value is inserted into the operation sequence."
            ),
            "evidence": {
                "failed_test": name,
                "failures": failures,
                "failed_cases": _extract_failed_invalid_cases(failed_result),
                "raw_runtime_result": failed_result,
            },
            "target_file": "generated_solution.py",
            "target_line_hints": line_hints,
            "suggested_fix": (
                "Validate transaction amounts before state update. The amount should be an int or float, "
                "should not be bool, should be finite using math.isfinite(amount), and should be greater than 0. "
                "Apply the same validation to both deposit and withdraw before modifying self._balance."
            ),
            "suggested_patch_strategy": [
                "import math",
                "add a helper such as _validate_positive_finite_amount(amount)",
                "reject bool, non-numeric, NaN, Infinity, zero, and negative values with ValueError",
                "call the helper at the beginning of deposit and withdraw",
                "keep the insufficient-funds check after amount validation in withdraw",
            ],
            "regression_focus": [
                "deposit(float('nan')) must raise ValueError and leave balance unchanged",
                "withdraw(float('nan')) must raise ValueError and leave balance unchanged",
                "deposit(float('inf')) and withdraw(float('inf')) should not corrupt state",
                "existing valid deposit / withdraw / query behavior must remain unchanged",
            ],
        }

    signature = _hash_signature(["mr_failure", name, json.dumps(failures, ensure_ascii=False), str(error)])
    return {
        "defect_id": f"DEF-{signature}",
        "source_test": name,
        "defect_type": "metamorphic_relation_violation",
        "severity": "high",
        "confidence": "medium",
        "title": f"Metamorphic relation failed: {name}",
        "what_went_wrong": "A validated MR did not hold during Stage 5 execution.",
        "why_it_is_hidden": "The failure was detected only after comparing source and follow-up executions.",
        "evidence": {
            "failed_test": name,
            "failures": failures,
            "error": error,
            "raw_runtime_result": failed_result,
        },
        "target_file": "generated_solution.py",
        "target_line_hints": [],
        "suggested_fix": "Inspect the failed MR source/follow-up relation and modify the generated solution so the relation holds without breaking frozen acceptance behavior.",
        "suggested_patch_strategy": [
            "read the failed MR details",
            "identify the method involved in the failed source/follow-up relation",
            "fix the logic while preserving public method names from implementation_manifest",
            "rerun Stage 3, Stage 4, and Stage 5 after repair",
        ],
        "regression_focus": [
            f"The failed MR should pass after repair: {name}",
            "Previously passing MRs should remain passing",
        ],
    }


# -----------------------------------------------------------------------------
# Build output documents
# -----------------------------------------------------------------------------

def _load_optional_text(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _extract_method_names(implementation_manifest: dict) -> dict:
    method_names = implementation_manifest.get("method_names", {})
    if isinstance(method_names, dict):
        return method_names
    return {}


def _extract_class_name(implementation_manifest: dict, generated_source: str) -> str:
    for key in ("class_name", "main_class", "target_class"):
        value = implementation_manifest.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    m = re.search(r"^class\s+([A-Za-z_][A-Za-z0-9_]*)", generated_source, flags=re.MULTILINE)
    return m.group(1) if m else "<unknown_class>"


def _build_defect_report(
    *,
    repair_id: str,
    run_id: str,
    gen_id: str,
    frozen_spec: dict,
    implementation_manifest: dict,
    verification_summary: dict,
    mr_execution_report: dict,
    mr_coverage_report: dict,
    defect_candidates: List[dict],
    project_root: Path,
) -> dict:
    verdict = "Defect" if defect_candidates else "Pass"
    rubric_id = frozen_spec.get("rubric_id") or frozen_spec.get("spec_id") or run_id

    hard_evidence = {
        "stage5_verification": {
            "verification_status": verification_summary.get("verification_status"),
            "final_decision_for_delivery": verification_summary.get("final_decision_for_delivery"),
            "runtime_summary": mr_execution_report.get("runtime_summary") or mr_execution_report.get("summary"),
            "failed_tests": [d["source_test"] for d in defect_candidates],
        },
        "mr_coverage": {
            "coverage_status": mr_coverage_report.get("coverage_status"),
            "coverage_summary": mr_coverage_report.get("summary"),
            "residual_risks": mr_coverage_report.get("residual_risks", []),
        },
    }

    mr_violations = []
    for d in defect_candidates:
        mr_violations.append({
            "mr_id_or_test_name": d.get("source_test"),
            "defect_type": d.get("defect_type"),
            "inconsistency_description": d.get("what_went_wrong"),
            "evidence": d.get("evidence"),
            "confidence": d.get("confidence"),
        })

    actionable_hints = []
    for d in defect_candidates:
        line_hint = ""
        hints = d.get("target_line_hints") or []
        if hints:
            line_hint = ", ".join(f"line {h.get('line')}: {h.get('code')}" for h in hints[:8])
        actionable_hints.append({
            "priority": d.get("severity", "high"),
            "target_file": d.get("target_file", "generated_solution.py"),
            "target_line_hint": line_hint,
            "what_went_wrong": d.get("what_went_wrong"),
            "how_to_fix": d.get("suggested_fix"),
        })

    bug_trace = []
    for d in defect_candidates:
        bug_trace.append({
            "signature": d.get("defect_id"),
            "defect_type": d.get("defect_type"),
            "occurrence_count": 1,
            "first_seen_stage": "Stage 5 MR testing",
            "latest_seen_stage": "Stage 6 repair feedback",
        })

    return {
        "schema_version": "stage6_defect_report_v1",
        "report_id": f"defect_report_{repair_id.lower()}",
        "repair_id": repair_id,
        "generated_at": _now(),
        "verdict": verdict,
        "source_run_id": run_id,
        "generation_id": gen_id,
        "rubric_id": rubric_id,
        "task_name": frozen_spec.get("task_name") or implementation_manifest.get("task_name") or "",
        "iteration_count": 1,
        "hard_evidence": hard_evidence,
        "mr_violations": mr_violations,
        "defect_candidates": defect_candidates,
        "actionable_hints_for_developer": actionable_hints,
        "bug_signature_trace": bug_trace,
        "not_performed": [
            "automatic_code_modification",
            "llm_judging_as_final_oracle",
            "full_semantic_equivalence_proof",
        ],
    }


def _build_repair_request(
    *,
    repair_id: str,
    run_id: str,
    gen_id: str,
    class_name: str,
    method_names: dict,
    defect_candidates: List[dict],
    verification_summary: dict,
    mr_coverage_report: dict,
) -> dict:
    return {
        "schema_version": "stage6_repair_request_v1",
        "repair_id": repair_id,
        "source_run_id": run_id,
        "generation_id": gen_id,
        "created_at": _now(),
        "repair_status": "repair_required" if defect_candidates else "no_repair_required",
        "target_artifact": {
            "file": "generated_solution.py",
            "class_name": class_name,
            "method_names": method_names,
        },
        "must_preserve": [
            "Do not change the frozen acceptance spec.",
            "Preserve the public class and method names recorded in implementation_manifest whenever possible.",
            "Do not remove valid deposit, withdraw, or query behavior that already passed Stage 4 and Stage 5 MRs.",
            "Keep invalid operations from mutating internal state.",
        ],
        "defects_to_fix": [
            {
                "defect_id": d.get("defect_id"),
                "defect_type": d.get("defect_type"),
                "severity": d.get("severity"),
                "source_test": d.get("source_test"),
                "title": d.get("title"),
                "what_went_wrong": d.get("what_went_wrong"),
                "suggested_fix": d.get("suggested_fix"),
                "suggested_patch_strategy": d.get("suggested_patch_strategy", []),
                "regression_focus": d.get("regression_focus", []),
            }
            for d in defect_candidates
        ],
        "acceptance_after_repair": [
            "Rerun Stage 3 behavior_extraction.py for the repaired GEN.",
            "Rerun Stage 4 behavior_alignment.py and confirm no mismatch or unknown expected units.",
            "Rerun Stage 5 verification_framework.py and confirm MR-005 and all previously passing MRs pass within the current MR coverage.",
            "The final decision should become verification_passed_for_current_scope only if no defect candidate is found within current MR coverage.",
        ],
        "verification_context": {
            "previous_verification_status": verification_summary.get("verification_status"),
            "previous_final_decision_for_delivery": verification_summary.get("final_decision_for_delivery"),
            "coverage_status": mr_coverage_report.get("coverage_status"),
            "scope_note": verification_summary.get("scope_note"),
            "interpretation_rule": verification_summary.get("interpretation_rule"),
        },
    }


def _make_repair_prompt(
    *,
    repair_id: str,
    run_id: str,
    gen_id: str,
    class_name: str,
    method_names: dict,
    defect_candidates: List[dict],
    generated_solution_source: str,
    verification_summary: dict,
    mr_coverage_report: dict,
) -> str:
    lines = []
    lines.append(f"# Repair Prompt：{repair_id}")
    lines.append("")
    lines.append(f"- RUN-ID: `{run_id}`")
    lines.append(f"- GEN-ID: `{gen_id}`")
    lines.append(f"- Target file: `generated_solution.py`")
    lines.append(f"- Target class: `{class_name}`")
    if method_names:
        lines.append(f"- Method mapping: `{json.dumps(method_names, ensure_ascii=False)}`")
    lines.append("")
    lines.append("## 任務")
    lines.append("請根據下列硬性驗證證據修復 `generated_solution.py`。不要修改 frozen spec，也不要改掉既有公開介面名稱。")
    lines.append("")
    lines.append("## 前一輪驗證結論")
    lines.append(f"- Verification status: `{verification_summary.get('verification_status')}`")
    lines.append(f"- Delivery decision: `{verification_summary.get('final_decision_for_delivery')}`")
    cov = mr_coverage_report.get("summary", {})
    if cov:
        lines.append(f"- MR coverage: `{cov.get('expected_units_covered_by_mr')}` / `{cov.get('expected_unit_count')}` expected units covered")
        lines.append(f"- Passed MR: `{cov.get('passed_mr_count')}`, Failed MR: `{cov.get('failed_mr_count')}`")
    lines.append("")
    lines.append("## 缺陷候選")
    if not defect_candidates:
        lines.append("目前沒有 defect candidate，不需要修復。")
    for idx, d in enumerate(defect_candidates, start=1):
        lines.append(f"### {idx}. {d.get('title')}")
        lines.append(f"- Defect ID: `{d.get('defect_id')}`")
        lines.append(f"- Source test: `{d.get('source_test')}`")
        lines.append(f"- Severity: `{d.get('severity')}`")
        lines.append(f"- Confidence: `{d.get('confidence')}`")
        lines.append(f"- What went wrong: {d.get('what_went_wrong')}")
        lines.append(f"- Why hidden: {d.get('why_it_is_hidden')}")
        lines.append("- Evidence failures:")
        failures = _safe_get(d, ["evidence", "failures"], [])
        if failures:
            for f in failures:
                lines.append(f"  - {f}")
        else:
            lines.append("  - See defect_report.json for raw runtime evidence.")
        lines.append("- Suggested fix:")
        lines.append(f"  - {d.get('suggested_fix')}")
        if d.get("suggested_patch_strategy"):
            lines.append("- Suggested patch strategy:")
            for s in d["suggested_patch_strategy"]:
                lines.append(f"  - {s}")
        if d.get("regression_focus"):
            lines.append("- Regression focus after repair:")
            for s in d["regression_focus"]:
                lines.append(f"  - {s}")
        if d.get("target_line_hints"):
            lines.append("- Related source line hints:")
            for h in d["target_line_hints"][:12]:
                lines.append(f"  - line {h.get('line')}: `{h.get('code')}`")
        lines.append("")

    lines.append("## 修復要求")
    lines.append("1. 修復缺陷時必須保留已通過的主要行為：有效存款、有效提款、查詢餘額、超額提款拒絕。")
    lines.append("2. 對數值金額進行明確驗證：必須是 int 或 float、不可為 bool、必須是有限數值、且必須大於 0。")
    lines.append("3. `deposit` 與 `withdraw` 都必須在改變 `_balance` 之前完成金額驗證。")
    lines.append("4. `withdraw` 的金額驗證應早於餘額不足檢查。")
    lines.append("5. 修復後重新執行 Stage 3、Stage 4、Stage 5，確認目前 MR 覆蓋範圍內不再出現 defect candidate。")
    lines.append("")
    lines.append("## 參考修復方向")
    lines.append("以下是方向提示，不一定要逐字照抄，但語意必須等價：")
    lines.append("")
    lines.append("```python")
    lines.append("import math")
    lines.append("")
    lines.append("def _validate_amount(amount):")
    lines.append("    if isinstance(amount, bool) or not isinstance(amount, (int, float)):")
    lines.append("        raise ValueError('Amount must be a finite positive number')")
    lines.append("    if not math.isfinite(amount) or amount <= 0:")
    lines.append("        raise ValueError('Amount must be a finite positive number')")
    lines.append("```")
    lines.append("")
    lines.append("## 原始程式碼片段")
    lines.append("```python")
    snippet = generated_solution_source.strip()
    lines.append(snippet[:6000] + ("\n# ... truncated ..." if len(snippet) > 6000 else ""))
    lines.append("```")
    return "\n".join(lines).rstrip() + "\n"


def _make_human_review(*args, **kwargs) -> str:
    # Stage 6 human review generator, compatible with Stage 5 v4.
    # Accepts both list[dict] and list[str] for residual_risks.
    import json

    def safe_json(obj) -> str:
        try:
            return json.dumps(obj, ensure_ascii=False, indent=2, default=str)
        except Exception:
            return str(obj)

    def pick_arg(index, default=None):
        try:
            return args[index]
        except Exception:
            return default

    def first_value(names, default=None):
        for name in names:
            if name in kwargs:
                return kwargs.get(name)
        return default

    def as_list(value):
        if value is None:
            return []
        if isinstance(value, list):
            return value
        return [value]

    def format_item(prefix: str, item, index: int) -> str:
        if isinstance(item, dict):
            item_id = (
                item.get(prefix + "_id")
                or item.get("risk_id")
                or item.get("defect_id")
                or item.get("hint_id")
                or item.get("id")
                or f"{prefix}_{index:03d}"
            )
            text = (
                item.get("summary")
                or item.get("description")
                or item.get("risk")
                or item.get("message")
                or item.get("reason")
                or item.get("failure_reason")
                or item.get("how_to_fix")
                or item.get("suggested_fix")
                or safe_json(item)
            )
            return f"- `{item_id}`：{text}"
        return f"- `{prefix}_{index:03d}`：{item}"

    verification_summary = first_value(["verification_summary"], pick_arg(0, {}))
    defect_report = first_value(["defect_report"], pick_arg(1, {}))
    repair_request = first_value(["repair_request"], pick_arg(2, {}))

    run_id = first_value(["run_id", "source_run_id"])
    gen_id = first_value(["gen_id", "generation_id"])
    repair_id = first_value(["repair_id"])

    for obj in (verification_summary, defect_report, repair_request):
        if isinstance(obj, dict):
            run_id = run_id or obj.get("source_run_id") or obj.get("run_id")
            gen_id = gen_id or obj.get("generation_id") or obj.get("gen_id")
            repair_id = repair_id or obj.get("repair_id")

    defect_candidates = []
    if isinstance(defect_report, dict):
        defect_candidates = (
            defect_report.get("defect_candidates")
            or defect_report.get("defects")
            or defect_report.get("failed_cases")
            or defect_report.get("mr_failed_cases")
            or []
        )
    defect_candidates = first_value(["defect_candidates", "defects", "failed_cases"], defect_candidates)

    actionable_hints = []
    if isinstance(defect_report, dict):
        actionable_hints = (
            defect_report.get("actionable_hints")
            or defect_report.get("repair_hints")
            or defect_report.get("fix_hints")
            or defect_report.get("hints")
            or []
        )
    actionable_hints = first_value(["actionable_hints", "repair_hints", "fix_hints", "hints"], actionable_hints)

    residual_risks = []
    if isinstance(verification_summary, dict):
        residual_risks = verification_summary.get("residual_risks") or verification_summary.get("risks") or []
    if isinstance(defect_report, dict):
        residual_risks = defect_report.get("residual_risks") or defect_report.get("risks") or residual_risks
    residual_risks = first_value(["residual_risks", "risks", "remaining_risks"], residual_risks)

    lines = []
    lines.append("# Stage 6 缺陷回饋人工審查")
    lines.append("")
    lines.append("本文件由 Stage 6 自動產生，用於檢視 MR 失敗證據、缺陷候選與修復方向。")
    lines.append("")
    lines.append("## 基本資訊")
    if run_id:
        lines.append(f"- RUN-ID：`{run_id}`")
    if gen_id:
        lines.append(f"- GEN-ID：`{gen_id}`")
    if repair_id:
        lines.append(f"- Repair ID：`{repair_id}`")
    lines.append("")

    lines.append("## 缺陷候選")
    items = as_list(defect_candidates)
    if items:
        for i, item in enumerate(items, 1):
            lines.append(format_item("defect", item, i))
    else:
        lines.append("- 無明確缺陷候選。")
    lines.append("")

    lines.append("## 可操作修復提示")
    items = as_list(actionable_hints)
    if items:
        for i, item in enumerate(items, 1):
            lines.append(format_item("hint", item, i))
    else:
        lines.append("- 無額外修復提示。")
    lines.append("")

    lines.append("## 殘餘風險")
    items = as_list(residual_risks)
    if items:
        for i, item in enumerate(items, 1):
            lines.append(format_item("residual_risk", item, i))
    else:
        lines.append("- 無殘餘風險資料。")
    lines.append("")

    lines.append("## 輸入摘要")
    summary = {
        "verification_summary": verification_summary,
        "defect_report": defect_report,
        "repair_request": repair_request,
    }
    preview = safe_json(summary)
    if len(preview) > 4000:
        preview = preview[:4000] + "\\n...（後略）"
    lines.append("```json")
    lines.append(preview)
    lines.append("```")
    lines.append("")

    return "\\n".join(lines) + "\\n"


def create_repair_feedback(project_root: Path, run_id: str, gen_id: str) -> dict:
    project_root = Path(project_root).resolve()

    stage1_dir = project_root / "stage1_outputs" / run_id
    code_dir = project_root / "workspace" / "code_generation" / run_id / gen_id
    verification_dir = project_root / "workspace" / "verification" / run_id / gen_id
    repair_root = project_root / "workspace" / "repair_feedback" / run_id / gen_id
    repair_id = _next_repair_id(repair_root)
    out_dir = repair_root / repair_id

    # Inputs
    frozen_spec = _read_json(stage1_dir / "frozen_acceptance_spec.json", required=False, default={})
    generated_solution_path = code_dir / "generated_solution.py"
    generated_solution_source = _read_text(generated_solution_path)
    implementation_manifest = _read_json(code_dir / "implementation_manifest.json", required=False, default={})
    verification_summary = _read_json(verification_dir / "verification_summary.json")
    mr_execution_report = _read_json(verification_dir / "mr_execution_report.json")
    mr_coverage_report = _read_json(verification_dir / "mr_coverage_report.json", required=False, default={})
    mr_human_review_text = _load_optional_text(verification_dir / "mr_human_review.md")

    input_snapshot = {
        "source_run_id": run_id,
        "generation_id": gen_id,
        "repair_id": repair_id,
        "created_at": _now(),
        "input_files": {
            "frozen_acceptance_spec": str(stage1_dir / "frozen_acceptance_spec.json"),
            "generated_solution": str(generated_solution_path),
            "implementation_manifest": str(code_dir / "implementation_manifest.json"),
            "verification_summary": str(verification_dir / "verification_summary.json"),
            "mr_execution_report": str(verification_dir / "mr_execution_report.json"),
            "mr_coverage_report": str(verification_dir / "mr_coverage_report.json"),
            "mr_human_review": str(verification_dir / "mr_human_review.md"),
        },
        "verification_status": verification_summary.get("verification_status"),
        "final_decision_for_delivery": verification_summary.get("final_decision_for_delivery"),
    }

    failed = _failed_results(mr_execution_report)
    defect_candidates = [classify_failed_result(r, generated_solution_source) for r in failed]

    class_name = _extract_class_name(implementation_manifest, generated_solution_source)
    method_names = _extract_method_names(implementation_manifest)

    defect_report = _build_defect_report(
        repair_id=repair_id,
        run_id=run_id,
        gen_id=gen_id,
        frozen_spec=frozen_spec,
        implementation_manifest=implementation_manifest,
        verification_summary=verification_summary,
        mr_execution_report=mr_execution_report,
        mr_coverage_report=mr_coverage_report,
        defect_candidates=defect_candidates,
        project_root=project_root,
    )

    repair_request = _build_repair_request(
        repair_id=repair_id,
        run_id=run_id,
        gen_id=gen_id,
        class_name=class_name,
        method_names=method_names,
        defect_candidates=defect_candidates,
        verification_summary=verification_summary,
        mr_coverage_report=mr_coverage_report,
    )

    repair_prompt = _make_repair_prompt(
        repair_id=repair_id,
        run_id=run_id,
        gen_id=gen_id,
        class_name=class_name,
        method_names=method_names,
        defect_candidates=defect_candidates,
        generated_solution_source=generated_solution_source,
        verification_summary=verification_summary,
        mr_coverage_report=mr_coverage_report,
    )

    human_review = _make_human_review(
        repair_id=repair_id,
        run_id=run_id,
        gen_id=gen_id,
        defect_report=defect_report,
        repair_request=repair_request,
        verification_summary=verification_summary,
        mr_execution_report=mr_execution_report,
        mr_coverage_report=mr_coverage_report,
    )

    status = "repair_required" if defect_candidates else "no_repair_required"
    summary = {
        "source_run_id": run_id,
        "generation_id": gen_id,
        "repair_id": repair_id,
        "created_at": _now(),
        "status": status,
        "defect_candidate_count": len(defect_candidates),
        "actionable_hint_count": len(defect_report.get("actionable_hints_for_developer", [])),
        "verification_status": verification_summary.get("verification_status"),
        "previous_final_decision_for_delivery": verification_summary.get("final_decision_for_delivery"),
        "next_step": (
            "send repair_prompt.md to the code generation agent, then rerun Stage 3/4/5"
            if defect_candidates else
            "no repair needed for current Stage 5 evidence"
        ),
        "scope_note": "Repair feedback is based on current Stage 5 MR evidence and does not modify code automatically.",
    }

    # Write outputs
    output_files = {
        "repair_feedback_input_snapshot": out_dir / "repair_feedback_input_snapshot.json",
        "defect_report": out_dir / "defect_report.json",
        "repair_request": out_dir / "repair_request.json",
        "repair_prompt": out_dir / "repair_prompt.md",
        "repair_feedback_summary": out_dir / "repair_feedback_summary.json",
        "repair_feedback_human_review": out_dir / "repair_feedback_human_review.md",
    }
    _write_json(output_files["repair_feedback_input_snapshot"], input_snapshot)
    _write_json(output_files["defect_report"], defect_report)
    _write_json(output_files["repair_request"], repair_request)
    _write_text(output_files["repair_prompt"], repair_prompt)
    _write_json(output_files["repair_feedback_summary"], summary)
    _write_text(output_files["repair_feedback_human_review"], human_review)

    return {
        **summary,
        "output_files": {k: str(v) for k, v in output_files.items()},
    }

# =============================================================================
# pipeline hardening v1: precise Stage 5 failure -> Stage 6 repair feedback
# Added by install_pipeline_hardening_v1_no_backup.py
# =============================================================================

def _hardening_v1_read_json(path):
    import json
    from pathlib import Path
    p = Path(path)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        return {"_read_error": str(e), "_path": str(p)}


def _hardening_v1_read_text(path, limit=8000):
    from pathlib import Path
    p = Path(path)
    if not p.exists():
        return ""
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        text = f"[read_error] {e}"
    return text if len(text) <= limit else text[:limit] + "\n...（後略）"


def _hardening_v1_write_json(path, data):
    import json
    from pathlib import Path
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def _hardening_v1_write_text(path, text):
    from pathlib import Path
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(str(text), encoding="utf-8")


def _hardening_v1_to_text(obj, limit=2000):
    import json
    if obj is None:
        return ""
    if isinstance(obj, str):
        text = obj
    else:
        try:
            text = json.dumps(obj, ensure_ascii=False, indent=2, default=str)
        except Exception:
            text = str(obj)
    return text if len(text) <= limit else text[:limit] + "\n...（後略）"


def _hardening_v1_is_fail_value(value):
    if value is False:
        return True
    if isinstance(value, str):
        v = value.lower()
        return any(token in v for token in [
            "fail", "failed", "failure", "error", "defect",
            "not_ready", "not ready", "not_pass", "not pass",
            "assert", "exception", "traceback",
        ])
    if isinstance(value, (int, float)):
        return value > 0
    if isinstance(value, list):
        return len(value) > 0
    return False


def _hardening_v1_pick_failure_text(record):
    if isinstance(record, dict):
        for key in [
            "failure_reason", "reason", "message", "error",
            "stderr", "stdout", "traceback", "assertion",
            "description", "expected", "actual",
        ]:
            value = record.get(key)
            if value:
                return _hardening_v1_to_text(value, 1200)
        return _hardening_v1_to_text(record, 1200)
    return _hardening_v1_to_text(record, 1200)


def _hardening_v1_failed_record(record):
    if not isinstance(record, dict):
        return _hardening_v1_is_fail_value(record)

    lower = {str(k).lower(): k for k in record.keys()}

    for key in ["passed", "ok", "success"]:
        if key in lower and record.get(lower[key]) is False:
            return True

    for key in ["status", "result", "outcome", "test_status", "verification_status"]:
        if key in lower and _hardening_v1_is_fail_value(record.get(lower[key])):
            return True

    for key in ["failed", "failure", "failed_count", "failed_tests", "failed_mr_count"]:
        if key in lower and _hardening_v1_is_fail_value(record.get(lower[key])):
            return True

    return False


def _hardening_v1_walk_failures(obj, source_name, path="root", out=None):
    if out is None:
        out = []

    if isinstance(obj, dict):
        if _hardening_v1_failed_record(obj):
            has_case_signal = any(k in obj for k in [
                "test_id", "case_id", "mr_id", "name", "title",
                "failure_reason", "assertion", "expected", "actual",
                "stderr", "traceback", "status", "passed",
            ])
            if has_case_signal:
                out.append({
                    "source": source_name,
                    "path": path,
                    "summary": _hardening_v1_pick_failure_text(obj),
                    "evidence": _hardening_v1_to_text(obj, 2500),
                })

        for key in ["failed_cases", "failed_tests", "failures", "failed_mrs", "errors"]:
            value = obj.get(key)
            if isinstance(value, list):
                for i, item in enumerate(value, 1):
                    out.append({
                        "source": source_name,
                        "path": f"{path}.{key}[{i-1}]",
                        "summary": _hardening_v1_pick_failure_text(item),
                        "evidence": _hardening_v1_to_text(item, 2500),
                    })

        for k, v in obj.items():
            _hardening_v1_walk_failures(v, source_name, f"{path}.{k}", out)

    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            _hardening_v1_walk_failures(item, source_name, f"{path}[{i}]", out)

    return out


def _hardening_v1_dedupe(records, limit=3):
    seen = set()
    output = []
    for r in records:
        key = (r.get("source"), r.get("path"), r.get("summary"))
        if key in seen:
            continue
        seen.add(key)
        output.append(r)
        if len(output) >= limit:
            break
    return output


def _hardening_v1_make_repair_prompt(run_id, gen_id, repair_id, verification_summary, defect_report, extra_context):
    lines = []
    lines.append("# Stage 7 修復指令")
    lines.append("")
    lines.append(f"- RUN-ID：`{run_id}`")
    lines.append(f"- Source GEN-ID：`{gen_id}`")
    lines.append(f"- Repair ID：`{repair_id}`")
    lines.append("")
    lines.append("## 修復目標")
    lines.append("")
    lines.append("請只修改 generated_solution.py，不得修改 frozen spec，不得改變公開方法名稱與參數。")
    lines.append("修復後必須符合原始需求，並通過 Stage 5 的 Basic Probe / MR 測試。")
    lines.append("")
    lines.append("## Stage 5 驗證狀態")
    lines.append("")
    if isinstance(verification_summary, dict):
        for key in [
            "verification_status", "final_decision_for_delivery",
            "coverage_status", "total_tests", "passed_tests",
            "failed_tests",
            "mr_covered_expected_units", "mr_total_expected_units",
        ]:
            if key in verification_summary:
                lines.append(f"- {key}：`{verification_summary.get(key)}`")
    lines.append("")
    lines.append("## 需要修復的失敗證據")
    lines.append("")
    defects = defect_report.get("defect_candidates", [])
    if defects:
        for d in defects:
            lines.append(f"### {d.get('defect_id')}: {d.get('summary')}")
            lines.append("")
            lines.append(f"- 來源：`{d.get('source')}`")
            lines.append(f"- JSON 路徑：`{d.get('path')}`")
            lines.append("")
            lines.append("```text")
            lines.append(str(d.get("evidence", "")))
            lines.append("```")
            lines.append("")
    else:
        lines.append("- Stage 5 回報失敗，但沒有抽到細節。請根據下方摘要檢查。")
        lines.append("")

    lines.append("## 修復原則")
    lines.append("")
    lines.append("1. 針對失敗 MR 的 expected / actual 差異修正邏輯。")
    lines.append("2. 非法操作可以 raise，但狀態不可改變。")
    lines.append("3. 數值狀態任務要保留上下界限制。")
    lines.append("4. 集合/字典任務要避免非法操作污染內部資料。")
    lines.append("5. 不要為了通過測試硬編特定值。")
    lines.append("")
    lines.append("## 其他驗證摘要")
    lines.append("")
    lines.append("```text")
    lines.append(extra_context)
    lines.append("```")
    lines.append("")
    return "\n".join(lines) + "\n"


def _hardening_v1_make_human_review(run_id, gen_id, repair_id, defect_report, repair_request):
    lines = []
    lines.append("# Stage 6 缺陷回饋人工審查")
    lines.append("")
    lines.append(f"- RUN-ID：`{run_id}`")
    lines.append(f"- GEN-ID：`{gen_id}`")
    lines.append(f"- Repair ID：`{repair_id}`")
    lines.append(f"- 狀態：`{repair_request.get('status')}`")
    lines.append("")
    lines.append("## 缺陷候選")
    for d in defect_report.get("defect_candidates", []):
        lines.append(f"- `{d.get('defect_id')}`：{d.get('summary')}")
    if not defect_report.get("defect_candidates"):
        lines.append("- 無缺陷候選。")
    lines.append("")
    lines.append("## 修復提示")
    for h in repair_request.get("actionable_repair_hints", []):
        lines.append(f"- {h}")
    if not repair_request.get("actionable_repair_hints"):
        lines.append("- 無。")
    lines.append("")
    return "\n".join(lines) + "\n"


def create_repair_feedback(project_root, run_id, gen_id):
    from pathlib import Path
    import datetime

    root = Path(project_root)
    verification_dir = root / "workspace" / "verification" / run_id / gen_id
    stage1_dir = root / "stage1_outputs" / run_id

    files = {
        "verification_summary": verification_dir / "verification_summary.json",
        "mr_execution_report": verification_dir / "mr_execution_report.json",
        "docker_execution_result": verification_dir / "docker_execution_result.json",
        "test_runtime_report": verification_dir / "test_runtime_report.json",
        "mr_coverage_report": verification_dir / "mr_coverage_report.json",
        "mr_plan_validated": verification_dir / "mr_plan_validated.json",
        "mr_human_review": verification_dir / "mr_human_review.md",
        "frozen_spec": stage1_dir / "frozen_acceptance_spec.json",
    }

    data = {
        "verification_summary": _hardening_v1_read_json(files["verification_summary"]) or {},
        "mr_execution_report": _hardening_v1_read_json(files["mr_execution_report"]) or {},
        "docker_execution_result": _hardening_v1_read_json(files["docker_execution_result"]) or {},
        "test_runtime_report": _hardening_v1_read_json(files["test_runtime_report"]) or {},
        "mr_coverage_report": _hardening_v1_read_json(files["mr_coverage_report"]) or {},
        "mr_plan_validated": _hardening_v1_read_json(files["mr_plan_validated"]) or {},
        "frozen_spec": _hardening_v1_read_json(files["frozen_spec"]) or {},
        "mr_human_review_text": _hardening_v1_read_text(files["mr_human_review"], limit=5000),
    }

    summary = data["verification_summary"]
    verification_status = str(summary.get("verification_status", "")) if isinstance(summary, dict) else ""
    final_decision = str(summary.get("final_decision_for_delivery", "")) if isinstance(summary, dict) else ""
    failed_tests = 0
    if isinstance(summary, dict):
        for key in ["failed_tests", "failed_mr_count", "failed_count"]:
            v = summary.get(key)
            if isinstance(v, int):
                failed_tests = max(failed_tests, v)

    stage5_failed = (
        "failed" in verification_status.lower()
        or "defect" in verification_status.lower()
        or "not_ready" in final_decision.lower()
        or "not ready" in final_decision.lower()
        or failed_tests > 0
    )

    raw_failures = []
    for source_name in [
        "mr_execution_report",
        "docker_execution_result",
        "test_runtime_report",
        "mr_coverage_report",
        "verification_summary",
    ]:
        raw_failures.extend(_hardening_v1_walk_failures(data.get(source_name), source_name=source_name))

    failures = _hardening_v1_dedupe(raw_failures, limit=3)

    if stage5_failed and not failures:
        combined = "\n\n".join([
            "[verification_summary]",
            _hardening_v1_to_text(data["verification_summary"], 2500),
            "[mr_execution_report]",
            _hardening_v1_to_text(data["mr_execution_report"], 2500),
            "[docker_execution_result]",
            _hardening_v1_to_text(data["docker_execution_result"], 2500),
            "[test_runtime_report]",
            _hardening_v1_to_text(data["test_runtime_report"], 2500),
            "[mr_human_review]",
            data["mr_human_review_text"],
        ])
        failures = [{
            "source": "stage5_reports",
            "path": "combined",
            "summary": "Stage 5 reported MR/test failure; see combined evidence.",
            "evidence": combined[:5000] + ("\n...（後略）" if len(combined) > 5000 else ""),
        }]

    defects = []
    for i, f in enumerate(failures, 1):
        defects.append({
            "defect_id": f"DEFECT-{i:06d}",
            "source": f.get("source"),
            "path": f.get("path"),
            "summary": f.get("summary") or "Stage 5 MR/test failure",
            "failure_reason": f.get("summary") or "Stage 5 MR/test failure",
            "evidence": f.get("evidence"),
            "severity": "high",
            "repair_hint": "根據失敗 MR/test 證據修正 generated_solution.py，保留公開介面與 frozen spec，並確保非法操作不改變狀態。",
        })

    status = "repair_required" if defects else "no_repair_required"
    next_step = (
        "send repair_prompt.md to the code generation agent, then rerun Stage 3/4/5"
        if defects else
        "no repair needed for current Stage 5 evidence"
    )

    base_dir = root / "workspace" / "repair_feedback" / run_id / gen_id
    base_dir.mkdir(parents=True, exist_ok=True)
    nums = []
    for p in base_dir.glob("REPAIR-*"):
        if p.is_dir():
            try:
                nums.append(int(p.name.split("-")[-1]))
            except Exception:
                pass
    repair_id = f"REPAIR-{(max(nums) + 1) if nums else 1:06d}"
    out_dir = base_dir / repair_id
    out_dir.mkdir(parents=True, exist_ok=True)

    now = datetime.datetime.now().isoformat(timespec="seconds")

    input_snapshot = {
        "schema_version": "stage6_input_hardening_v1",
        "source_run_id": run_id,
        "generation_id": gen_id,
        "created_at": now,
        "input_files": {k: str(v) for k, v in files.items()},
    }

    defect_report = {
        "schema_version": "stage6_defect_report_hardening_v1",
        "source_run_id": run_id,
        "generation_id": gen_id,
        "repair_id": repair_id,
        "created_at": now,
        "status": status,
        "verification_status": verification_status,
        "final_decision_for_delivery": final_decision,
        "defect_candidates": defects,
        "defect_candidate_count": len(defects),
    }

    hints = [d["repair_hint"] for d in defects if d.get("repair_hint")]
    repair_request = {
        "schema_version": "stage6_repair_request_hardening_v1",
        "source_run_id": run_id,
        "generation_id": gen_id,
        "repair_id": repair_id,
        "created_at": now,
        "status": status,
        "target_file": str(root / "workspace" / "code_generation" / run_id / gen_id / "generated_solution.py"),
        "defect_candidate_count": len(defects),
        "actionable_repair_hints_count": len(hints),
        "actionable_repair_hints": hints,
        "next_step": next_step,
    }

    extra_context = "\n\n".join([
        "[frozen_spec]",
        _hardening_v1_to_text(data["frozen_spec"], 2000),
        "[mr_human_review]",
        data["mr_human_review_text"],
    ])

    repair_prompt = _hardening_v1_make_repair_prompt(
        run_id=run_id,
        gen_id=gen_id,
        repair_id=repair_id,
        verification_summary=summary,
        defect_report=defect_report,
        extra_context=extra_context,
    )

    human_review = _hardening_v1_make_human_review(
        run_id=run_id,
        gen_id=gen_id,
        repair_id=repair_id,
        defect_report=defect_report,
        repair_request=repair_request,
    )

    repair_feedback_summary = {
        "schema_version": "stage6_summary_hardening_v1",
        "source_run_id": run_id,
        "generation_id": gen_id,
        "repair_id": repair_id,
        "created_at": now,
        "status": status,
        "defect_candidate_count": len(defects),
        "actionable_repair_hints_count": len(hints),
        "next_step": next_step,
        "output_dir": str(out_dir),
    }

    output_files = {
        "repair_feedback_input_snapshot": out_dir / "repair_feedback_input_snapshot.json",
        "defect_report": out_dir / "defect_report.json",
        "repair_request": out_dir / "repair_request.json",
        "repair_prompt": out_dir / "repair_prompt.md",
        "repair_feedback_summary": out_dir / "repair_feedback_summary.json",
        "repair_feedback_human_review": out_dir / "repair_feedback_human_review.md",
    }

    _hardening_v1_write_json(output_files["repair_feedback_input_snapshot"], input_snapshot)
    _hardening_v1_write_json(output_files["defect_report"], defect_report)
    _hardening_v1_write_json(output_files["repair_request"], repair_request)
    _hardening_v1_write_text(output_files["repair_prompt"], repair_prompt)
    _hardening_v1_write_json(output_files["repair_feedback_summary"], repair_feedback_summary)
    _hardening_v1_write_text(output_files["repair_feedback_human_review"], human_review)

    output_files_str = {k: str(v) for k, v in output_files.items()}

    return {
        **repair_feedback_summary,
        "ok": True,
        "run_id": run_id,
        "gen_id": gen_id,
        "source_run_id": run_id,
        "generation_id": gen_id,
        "repair_id": repair_id,
        "status": status,
        "defect_count": len(defects),
        "defect_candidate_count": len(defects),
        "actionable_hint_count": len(hints),
        "actionable_repair_hints_count": len(hints),
        "next_step": next_step,
        "paths": output_files_str,
        "output_files": output_files_str,
        "repair_feedback_input_snapshot": output_files_str["repair_feedback_input_snapshot"],
        "defect_report": output_files_str["defect_report"],
        "repair_request": output_files_str["repair_request"],
        "repair_prompt": output_files_str["repair_prompt"],
        "repair_feedback_summary": output_files_str["repair_feedback_summary"],
        "repair_feedback_human_review": output_files_str["repair_feedback_human_review"],
    }
