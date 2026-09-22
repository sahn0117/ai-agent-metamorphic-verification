# -*- coding: utf-8 -*-
"""
skill_verification_framework.py
===============================

Stage 5：LLM-assisted Verification Profile + Rule-validated MR Execution.

v8.6.0: IO Oracle Policy + Semantic IO MR Framework.

這版重點：
- Sample / testdata 僅作 Basic IO Probe，失敗不再主導正式 MR verdict。
- IO MR 分成 generic_mr 與 semantic_mr，正式 MR 不依賴 ground-truth expected output。
- 依原始題意自動啟用低風險語意型 IO MR registry，避免一直針對單題補洞。

這版重點：
- LLM 可以產生 verification_profile 候選，負責舉一反三。
- 規則式 validator 會檢查 class/method/task_family。
- Docker 執行結果才是最後證據，LLM 不當 final judge。
- profile 不支援 / method role 不完整時，不再誤判為 generated code defect。
"""

from __future__ import annotations

import ast
import inspect
import json
import math
import os
import re
import shutil
import subprocess
import textwrap
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


PROJECT_ROOT = Path(__file__).resolve().parents[1]
STAGE1_ROOT = PROJECT_ROOT / "stage1_outputs"
WORKSPACE_ROOT = PROJECT_ROOT / "workspace"
CODEGEN_ROOT = WORKSPACE_ROOT / "code_generation"
ALIGNMENT_ROOT = WORKSPACE_ROOT / "behavior_alignment"
EXTRACTION_ROOT = WORKSPACE_ROOT / "behavior_extraction"
VERIFICATION_ROOT = WORKSPACE_ROOT / "verification"

DEFAULT_DOCKER_IMAGE = "python:3.11-slim"
DEFAULT_DOCKER_TIMEOUT_SECONDS = 60
OLLAMA_CHAT_URL = os.environ.get("OLLAMA_CHAT_URL", "http://localhost:11434/api/chat")


def now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def safe_id(value: str, prefix: str) -> str:
    value = (value or "").strip().upper()
    if not re.fullmatch(rf"{prefix}-\d{{6}}", value):
        raise ValueError(f"Invalid {prefix} id: {value!r}. Expected format like {prefix}-000007")
    return value


def safe_gen_id(value: str) -> str:
    value = (value or "").strip().upper()
    if not re.fullmatch(r"GEN-\d{6}", value):
        raise ValueError(f"Invalid GEN id: {value!r}. Expected format like GEN-000001")
    return value


def resolve_under(base: Path, *parts: str) -> Path:
    base = base.resolve()
    path = base.joinpath(*parts).resolve()
    if path != base and base not in path.parents:
        raise ValueError(f"Path escapes allowed base: {path}")
    return path


def latest_gen_id_for_run(run_id: str) -> str:
    run_dir = CODEGEN_ROOT / run_id
    if not run_dir.exists():
        raise FileNotFoundError(f"Cannot find code generation directory: {run_dir}")
    gens = sorted([p.name for p in run_dir.iterdir() if p.is_dir() and re.fullmatch(r"GEN-\d{6}", p.name)])
    if not gens:
        raise FileNotFoundError(f"No GEN-* folder found under {run_dir}")
    return gens[-1]


@dataclass
class VerificationInputs:
    run_id: str
    gen_id: str
    frozen_spec_path: Path
    generated_solution_path: Path
    implementation_manifest_path: Path
    behavior_alignment_report_path: Path
    generated_behavior_spec_path: Path
    expected_behavior_units_path: Path
    frozen_spec: Dict[str, Any]
    generated_solution_code: str
    implementation_manifest: Dict[str, Any]
    behavior_alignment_report: Dict[str, Any]
    generated_behavior_spec: Dict[str, Any]
    expected_behavior_units: Dict[str, Any]


def load_verification_inputs(run_id: str, gen_id: str) -> VerificationInputs:
    run_id = safe_id(run_id, "RUN")
    gen_id = safe_gen_id(gen_id)

    frozen_spec_path = resolve_under(STAGE1_ROOT, run_id, "frozen_acceptance_spec.json")
    generated_solution_path = resolve_under(CODEGEN_ROOT, run_id, gen_id, "generated_solution.py")
    implementation_manifest_path = resolve_under(CODEGEN_ROOT, run_id, gen_id, "implementation_manifest.json")
    behavior_alignment_report_path = resolve_under(ALIGNMENT_ROOT, run_id, gen_id, "behavior_alignment_report.json")
    generated_behavior_spec_path = resolve_under(EXTRACTION_ROOT, run_id, gen_id, "generated_behavior_spec.json")
    expected_behavior_units_path = resolve_under(EXTRACTION_ROOT, run_id, gen_id, "expected_behavior_units.json")

    required = [
        frozen_spec_path,
        generated_solution_path,
        implementation_manifest_path,
        behavior_alignment_report_path,
        generated_behavior_spec_path,
        expected_behavior_units_path,
    ]
    missing = [str(p) for p in required if not p.exists()]
    if missing:
        raise FileNotFoundError("Missing required Stage 5 inputs:\n" + "\n".join(missing))

    return VerificationInputs(
        run_id=run_id,
        gen_id=gen_id,
        frozen_spec_path=frozen_spec_path,
        generated_solution_path=generated_solution_path,
        implementation_manifest_path=implementation_manifest_path,
        behavior_alignment_report_path=behavior_alignment_report_path,
        generated_behavior_spec_path=generated_behavior_spec_path,
        expected_behavior_units_path=expected_behavior_units_path,
        frozen_spec=read_json(frozen_spec_path),
        generated_solution_code=generated_solution_path.read_text(encoding="utf-8"),
        implementation_manifest=read_json(implementation_manifest_path),
        behavior_alignment_report=read_json(behavior_alignment_report_path),
        generated_behavior_spec=read_json(generated_behavior_spec_path),
        expected_behavior_units=read_json(expected_behavior_units_path),
    )


def check_stage4_ready(report: Dict[str, Any]) -> Tuple[bool, List[str]]:
    reasons: List[str] = []
    summary = report.get("summary", {}) or {}
    status = str(report.get("alignment_status", ""))
    mismatch_count = int(summary.get("mismatch_count", 0) or 0)
    if mismatch_count != 0:
        reasons.append(f"mismatch_count is {mismatch_count}")
    if "blocked" in status or "failed" in status:
        reasons.append(f"alignment_status is blocking-like: {status}")
    return (not reasons), reasons


def get_acceptance_spec(frozen_spec: Dict[str, Any]) -> Dict[str, Any]:
    return frozen_spec.get("acceptance_spec", frozen_spec)


def _text_blob(*items: Any) -> str:
    return json.dumps(items, ensure_ascii=False, default=str).lower()


def get_task_name(frozen_spec: Dict[str, Any], fallback: str = "GeneratedTask") -> str:
    acc = get_acceptance_spec(frozen_spec)
    meta = acc.get("task_meta", {}) or {}
    if meta.get("task_name"):
        return str(meta["task_name"])
    interface = acc.get("interface_contract", {}) or {}
    if interface.get("entity_name"):
        return str(interface["entity_name"])
    return fallback


def extract_classes_and_methods(code: str, *, public_only: bool = True) -> Dict[str, List[str]]:
    """Extract class methods from generated code.

    public_only=True intentionally excludes any method beginning with '_' so helper
    functions such as _compute_collatz_length are not reported as public API and
    cannot be selected as method_roles by the verifier.
    """
    result: Dict[str, List[str]] = {}
    try:
        tree = ast.parse(code)
    except Exception:
        return result
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            methods = []
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if item.name.startswith("__"):
                        continue
                    if public_only and item.name.startswith("_"):
                        continue
                    methods.append(item.name)
            result[node.name] = methods
    return result


def extract_top_level_functions(code: str, *, public_only: bool = True) -> List[str]:
    try:
        tree = ast.parse(code)
    except Exception:
        return []
    names: List[str] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if public_only and node.name.startswith("_"):
                continue
            names.append(node.name)
    return names


def find_raw_requirement_in_frozen(frozen_spec: Dict[str, Any]) -> str:
    candidates = [
        frozen_spec.get("source_requirement", {}).get("raw_requirement"),
        frozen_spec.get("raw_requirement"),
        frozen_spec.get("requirement"),
        frozen_spec.get("user_prompt_original"),
    ]
    for c in candidates:
        if isinstance(c, str) and c.strip():
            return c
    return json.dumps(frozen_spec, ensure_ascii=False, default=str)


def is_original_io_problem(inputs: VerificationInputs) -> bool:
    text = _text_blob(inputs.frozen_spec, inputs.implementation_manifest, inputs.generated_behavior_spec, inputs.behavior_alignment_report)
    top = extract_top_level_functions(inputs.generated_solution_code)
    task_type = str(inputs.generated_behavior_spec.get("task_type") or inputs.expected_behavior_units.get("task_type") or "")
    if task_type == "original_io_problem":
        return True
    if "solve" in top and any(s in text for s in ["solve(input_text", "input_text", "stdin", "stdout", "標準輸入", "標準輸出", "cpe", "uva", "leetcode", "online judge"]):
        return True
    return False


def pick_class_name(inputs: VerificationInputs, classes: Dict[str, List[str]]) -> str:
    task_name = get_task_name(inputs.frozen_spec, fallback="")
    if task_name and task_name in classes:
        return task_name
    manifest_text = _text_blob(inputs.implementation_manifest)
    for cls in classes:
        if cls.lower() in manifest_text:
            return cls
    if task_name:
        for cls in classes:
            if cls.lower() == task_name.lower():
                return cls
    if classes:
        return sorted(classes.keys())[0]
    return task_name or "GeneratedTask"


def extract_manifest_methods(manifest: Dict[str, Any]) -> List[str]:
    methods: List[str] = []
    blob = manifest.get("implementation_decisions", manifest)
    if isinstance(blob, dict):
        method_names = blob.get("method_names", {})
        if isinstance(method_names, dict):
            for v in method_names.values():
                if isinstance(v, str):
                    methods.append(v)
        public_interface = blob.get("public_interface", [])
        if isinstance(public_interface, list):
            for item in public_interface:
                if isinstance(item, str):
                    m = re.match(r"([A-Za-z_]\w*)", item.strip())
                    if m:
                        methods.append(m.group(1))
    return list(dict.fromkeys(methods))


SUPPORTED_TASK_FAMILIES = {
    "numeric_state",
    "numeric_collection",
    "collection_aggregate",
    "sorting_sequence",
    "string_collection",
    "string_counting",
    "matrix_grid",
    "rule_classification",
    "finite_state_machine",
    "original_io_problem",
}

FAMILY_REQUIRED_ROLES = {
    "numeric_state": ["increase", "decrease", "query"],
    "numeric_collection": ["add", "remove", "query"],
    "collection_aggregate": ["add_item", "remove_item", "query_total"],
    "sorting_sequence": ["add", "remove", "query_sorted"],
    "string_collection": ["add_text", "remove_text", "query"],
    "string_counting": ["add_text", "query"],
    "matrix_grid": ["set_grid"],
    "rule_classification": ["classify"],
    "finite_state_machine": ["transition", "query_state"],
    "original_io_problem": ["solve"],
}

MR_TEMPLATES_BY_FAMILY = {
    "numeric_state": ["MR_QUERY_IDEMPOTENCE", "MR_ADD_REMOVE_RESTORATION", "MR_OPERATION_ORDER_INVARIANCE", "MR_INVALID_OPERATION_STATE_UNCHANGED"],
    "numeric_collection": ["MR_QUERY_IDEMPOTENCE", "MR_ADD_IDEMPOTENCE", "MR_ADD_REMOVE_RESTORATION", "MR_OPERATION_ORDER_INVARIANCE", "MR_QUERY_EMPTY_STATE", "MR_INVALID_OPERATION_STATE_UNCHANGED"],
    "collection_aggregate": ["MR_QUERY_IDEMPOTENCE", "MR_ADD_REMOVE_RESTORATION", "MR_AGGREGATE_MONOTONICITY", "MR_INVALID_OPERATION_STATE_UNCHANGED"],
    "sorting_sequence": ["MR_SORT_ORDER_INVARIANCE", "MR_SORT_QUERY_IDEMPOTENCE", "MR_SORT_REMOVE_EFFECT", "MR_INVALID_OPERATION_STATE_UNCHANGED"],
    "string_collection": ["MR_QUERY_IDEMPOTENCE", "MR_TEXT_ADD_REMOVE_RESTORATION", "MR_TEXT_ORDER_INVARIANCE", "MR_INVALID_OPERATION_STATE_UNCHANGED"],
    "string_counting": ["MR_QUERY_IDEMPOTENCE", "MR_TEXT_ORDER_INVARIANCE", "MR_TEXT_DUPLICATION_SCALING_OR_CHANGE", "MR_INVALID_OPERATION_STATE_UNCHANGED"],
    "matrix_grid": ["MR_GRID_QUERY_IDEMPOTENCE", "MR_GRID_CELL_COUNT_CONSISTENCY", "MR_INVALID_GRID_REJECTED"],
    "rule_classification": ["MR_CLASSIFICATION_IDEMPOTENCE"],
    "finite_state_machine": ["MR_QUERY_IDEMPOTENCE", "MR_INVALID_TRANSITION_STATE_UNCHANGED"],
    "original_io_problem": [
        "MR_IO_DETERMINISM",
        "MR_IO_CRLF_NORMALIZATION",
        "MR_IO_T_CASE_DUPLICATION",
        "MR_IO_SEMANTIC_INTERVAL_ORDER_INVARIANCE",
        "MR_IO_SEMANTIC_MATRIX_ZERO_PADDING",
        "MR_IO_SEMANTIC_SKYLINE_DOMINATED_BUILDING",
    ],
}


def _find_method(methods: List[str], include: List[str], exclude: Optional[List[str]] = None) -> Optional[str]:
    exclude = exclude or []
    lowered = [(m, m.lower()) for m in methods]
    for m, ml in lowered:
        if any(ml == x for x in include):
            return m
    for m, ml in lowered:
        if any(x in ml for x in include) and not any(x in ml for x in exclude):
            return m
    return None


def infer_family_from_methods_and_text(methods: List[str], text: str) -> str:
    method_text = " ".join(methods).lower()
    blob = (method_text + "\n" + text.lower())

    # v8.2 guardrail: explicit text APIs take precedence over generic words
    # such as "sort" or "result" that may appear in problem statements or LLM notes.
    # If a class exposes add_text/remove_text/get_result and no get_sorted-like method,
    # it should not be classified as sorting_sequence.
    has_text_api = any(x in method_text for x in ["add_text", "remove_text", "add_string", "remove_string", "add_word", "remove_word", "get_counts"])
    has_sorted_query = any(x in method_text for x in ["get_sorted", "sorted", "query_sorted"])
    if has_text_api and not has_sorted_query:
        if any(x in blob for x in ["count", "frequency", "統計", "次數", "頻率", "字元頻率", "character frequency"]):
            return "string_counting"
        return "string_collection"

    if any(x in method_text for x in ["set_grid", "grid", "matrix"]) or any(x in blob for x in ["矩陣", "網格", "二維", "matrix", "grid"]):
        return "matrix_grid"
    if "get_sorted" in method_text or "sorted" in method_text or "query_sorted" in method_text or "排序" in blob:
        return "sorting_sequence"
    if any(x in method_text for x in ["add_item", "remove_item", "get_total_quantity", "get_total"]):
        return "collection_aggregate"
    if any(x in method_text for x in ["add_value", "remove_value", "add_number", "remove_number"]):
        return "numeric_collection"
    if any(x in method_text for x in ["deposit", "withdraw", "increment", "decrement", "increase", "decrease", "charge", "use", "restock", "sell"]):
        return "numeric_state"
    if any(x in method_text for x in ["classify", "evaluate", "is_", "check", "judge"]):
        return "rule_classification"
    if any(x in method_text for x in ["transition", "next", "approve", "cancel", "start", "finish"]) and any(x in blob for x in ["state", "status", "狀態"]):
        return "finite_state_machine"
    return "numeric_collection"


def infer_empty_query_expected(family: str, text: str) -> Any:
    """Infer the expected observable result on an empty state from the frozen requirement.

    This is intentionally conservative. It exists to avoid hard-coding None for
    every numeric collection. The value is later embedded in verification_profile
    and consumed by generated_mr_tests.py.
    """
    t = (text or "").lower()
    if family == "sorting_sequence":
        return []
    if family in {"string_collection", "string_counting"}:
        return {}
    if family == "collection_aggregate":
        return 0
    if family == "numeric_collection":
        if "回傳 none" in t or "return none" in t or "none" in t:
            return None
        if "回傳 0" in t or "return 0" in t or "總和" in t or "sum" in t:
            return 0
        return None
    return None



def normalize_empty_query_expected_from_requirement(family: str, text: str, state_model: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize empty-state query oracle using frozen requirement text.

    v8.4 stability fix:
    Some LLM-produced profiles set empty_query_expected=None even when the
    frozen requirement explicitly says an empty numeric collection should return 0.
    This function treats the frozen requirement and conservative family defaults
    as stronger evidence than the LLM state_model for this single oracle field.
    """
    sm = dict(state_model) if isinstance(state_model, dict) else {}
    t = (text or "").lower()

    mentions_empty = any(k in t for k in ["目前沒有", "沒有任何", "空集合", "empty", "no value", "no values", "no text", "no strings"])
    explicit_zero = mentions_empty and bool(re.search(r"(?:回傳|return(?:s)?)\s*0\b", t))
    explicit_none = mentions_empty and bool(re.search(r"(?:回傳|return(?:s)?)\s*none\b", t))
    explicit_empty_list = mentions_empty and ("回傳 []" in t or "return []" in t or "empty list" in t)
    explicit_empty_dict = mentions_empty and ("回傳空 dict" in t or "回傳 {}" in t or "return {}" in t or "empty dict" in t or "empty dictionary" in t)

    inferred = infer_empty_query_expected(family, text)
    query_semantics = sm.get("query_semantics") or infer_query_semantics(family, text)

    if explicit_zero:
        sm["empty_query_expected"] = 0
    elif explicit_none:
        sm["empty_query_expected"] = None
    elif explicit_empty_list:
        sm["empty_query_expected"] = []
    elif explicit_empty_dict:
        sm["empty_query_expected"] = {}
    elif family == "sorting_sequence":
        sm["empty_query_expected"] = []
    elif family in {"string_collection", "string_counting"}:
        sm["empty_query_expected"] = {}
    elif family == "collection_aggregate":
        sm["empty_query_expected"] = 0
    elif family == "numeric_collection" and query_semantics == "sum":
        sm["empty_query_expected"] = 0
    elif "empty_query_expected" not in sm:
        sm["empty_query_expected"] = inferred

    sm["query_semantics"] = query_semantics
    sm["empty_query_oracle_source"] = "requirement_normalized_v8_4"
    return sm

def infer_query_semantics(family: str, text: str) -> str:
    t = (text or "").lower()
    if family == "numeric_collection":
        if "collatz" in t or "3n+1" in t or "3n + 1" in t:
            return "collatz_max_cycle_length"
        if "總和" in t or "sum" in t:
            return "sum"
        return "numeric_collection_query"
    if family == "string_counting":
        return "character_frequency"
    if family == "sorting_sequence":
        return "ascending_sorted_view"
    return "unknown"


def infer_state_model(family: str, text: str) -> Dict[str, Any]:
    empty_expected = infer_empty_query_expected(family, text)
    if family == "numeric_state":
        return {"state_kind": "numeric_scalar", "element_type": "number", "empty_query_expected": empty_expected, "query_semantics": infer_query_semantics(family, text)}
    if family in {"numeric_collection", "sorting_sequence"}:
        return {"state_kind": "collection", "element_type": "int", "allows_duplicates": False if "不重複" in text else "unknown", "empty_query_expected": empty_expected, "query_semantics": infer_query_semantics(family, text)}
    if family == "collection_aggregate":
        return {"state_kind": "mapping_or_collection", "element_type": "item_quantity", "empty_query_expected": empty_expected, "query_semantics": infer_query_semantics(family, text)}
    if family in {"string_collection", "string_counting"}:
        return {"state_kind": "text_collection", "element_type": "str", "empty_query_expected": empty_expected, "query_semantics": infer_query_semantics(family, text)}
    if family == "matrix_grid":
        return {"state_kind": "matrix_grid", "element_type": "any", "empty_query_expected": empty_expected, "query_semantics": infer_query_semantics(family, text)}
    if family == "rule_classification":
        return {"state_kind": "stateless_or_rule_based", "query_semantics": infer_query_semantics(family, text)}
    if family == "finite_state_machine":
        return {"state_kind": "finite_state", "query_semantics": infer_query_semantics(family, text)}
    return {"state_kind": "unknown", "empty_query_expected": empty_expected, "query_semantics": infer_query_semantics(family, text)}


def default_invariants_for_family(family: str) -> List[str]:
    if family == "original_io_problem":
        return [
            "solve(input_text) should be deterministic for the same input",
            "line-ending style should not change input semantics when the original problem treats newlines structurally",
            "sample expected output, when available, is only a Basic IO Probe; a cleaned sample input may be used only as a source seed for generic MR execution",
        ]
    base = ["query methods should not mutate state", "invalid operations should not corrupt state"]
    if family == "sorting_sequence":
        return base + ["query_sorted output should be in ascending order", "insertion order should not affect sorted view"]
    if family in {"numeric_state", "numeric_collection", "collection_aggregate"}:
        return base + ["add followed by equivalent remove should restore previous observable state"]
    if family in {"string_collection", "string_counting"}:
        return base + ["adding text in different order should produce equivalent observable result"]
    if family == "matrix_grid":
        return base + ["cell count query should match the stored grid"]
    return base


def default_mr_candidates_for_family(family: str) -> List[Dict[str, Any]]:
    """
    Build default MR candidates.

    v8.6.1 hotfix:
    For original_io_problem, semantic IO MRs must NOT be included by default.
    They are only allowed when infer_semantic_io_mr_candidates() finds strong
    statement-level evidence for that specific relation.

    This prevents the v8.6 failure mode where Collatz / matrix / skyline MRs
    were executed on unrelated CPE problems, creating false failures.
    """
    templates = list(MR_TEMPLATES_BY_FAMILY.get(family, []))
    if family == "original_io_problem":
        templates = [t for t in templates if not str(t).startswith("MR_IO_SEMANTIC_")]

    return [
        {"mr_id": f"MR-{i + 1:03d}", "mr_template": name, "metamorphic": True, "risk_target": name.lower()}
        for i, name in enumerate(templates)
    ]




def infer_semantic_io_mr_candidates(inputs: VerificationInputs) -> List[Dict[str, Any]]:
    """Infer low-risk semantic IO MR templates from the original statement.

    This is a registry, not a per-failure hotfix.  Each candidate is enabled only
    when the statement contains enough semantic evidence for a source/follow-up
    relation that does not require ground-truth output.
    """
    raw = _strip_appended_interface_block(
        _clean_control_chars(find_raw_requirement_in_frozen(inputs.frozen_spec))
    )
    low = raw.lower()
    candidates: List[Dict[str, Any]] = []

    def add(template: str, title: str, evidence: List[str]) -> None:
        candidates.append({
            "mr_id": f"MR-SEM-{len(candidates) + 1:03d}",
            "mr_template": template,
            "metamorphic": True,
            "risk_target": "semantic_io_relation",
            "mr_strength": "semantic",
            "title": title,
            "evidence_terms": evidence,
        })

    # Family: interval maximum over unordered endpoints, e.g. The 3n + 1 problem.
    # v8.6.2: UVA PDFs often say "between and including both i and j"
    # rather than the exact phrase "between i and j".  Use a conservative
    # conjunction: Collatz/cycle-length evidence + endpoint/range evidence.
    has_collatz_core = any(term in low for term in ["3n + 1", "3n+1", "collatz", "cycle length", "cycle-length"])
    has_interval_core = any(term in low for term in [
        "two numbers i and j",
        "integers i and j",
        "pairs of integers i and j",
        "pair of integers i and j",
        "between and including",
        "maximum cycle length over all numbers between",
        "range",
    ])
    if has_collatz_core and has_interval_core:
        add(
            "MR_IO_SEMANTIC_INTERVAL_ORDER_INVARIANCE",
            "Interval endpoint order should not change the computed range maximum.",
            ["3n+1/collatz/cycle length", "range endpoints"],
        )

    # Family: maximum sub-rectangle / maximum sum matrix problems.
    # v8.6.2: use the problem semantics rather than the word "matrix" only;
    # UVA 108 says "2-dimensional array" and "maximal sub-rectangle".
    has_max_subrect_core = any(term in low for term in [
        "maximum sum",
        "maximal sub-rectangle",
        "maximum sub-rectangle",
        "sub-rectangle with the largest sum",
    ])
    has_2d_array_core = any(term in low for term in [
        "2-dimensional array",
        "two dimensional array",
        "two-dimensional array",
        "square two dimensional array",
        "n2 integers",
        "row-major order",
        "matrix",
        "n x n",
    ])
    if has_max_subrect_core and has_2d_array_core:
        add(
            "MR_IO_SEMANTIC_MATRIX_ZERO_PADDING",
            "Adding an all-zero outer border should not change the maximum sub-rectangle sum.",
            ["maximum sub-rectangle sum", "2-dimensional array/matrix"],
        )

    # Family: skyline / building silhouette.
    if "skyline" in low and ("building" in low or "buildings" in low):
        add(
            "MR_IO_SEMANTIC_SKYLINE_DOMINATED_BUILDING",
            "Adding a lower building fully contained under an existing building should not change the skyline.",
            ["skyline", "building"],
        )

    return candidates

def heuristic_profile(inputs: VerificationInputs) -> Dict[str, Any]:
    classes = extract_classes_and_methods(inputs.generated_solution_code)
    class_name = pick_class_name(inputs, classes)
    # Public methods must come from AST evidence only.
    # Do not mix implementation_manifest descriptions into public_methods,
    # otherwise natural-language phrases may be mistaken as callable methods.
    methods = list(classes.get(class_name, []))
    text = _text_blob(inputs.frozen_spec, inputs.implementation_manifest, inputs.generated_behavior_spec, inputs.behavior_alignment_report)

    if is_original_io_problem(inputs):
        top_functions = extract_top_level_functions(inputs.generated_solution_code)
        return {
            "profile_version": "3.0",
            "created_at": now_iso(),
            "generation_mode": "heuristic_original_io_profile_v8_6_2",
            "llm_used": False,
            "task_family": "original_io_problem",
            "support_status": "candidate",
            "class_name": "",
            "public_methods": top_functions,
            "top_level_functions": top_functions,
            "method_roles": {"solve": "solve" if "solve" in top_functions else ""},
            "state_model": {"interface": "solve(input_text: str) -> str"},
            "invariants": default_invariants_for_family("original_io_problem"),
            "mr_candidates": default_mr_candidates_for_family("original_io_problem") + infer_semantic_io_mr_candidates(inputs),
            "warnings": [],
        }

    family = infer_family_from_methods_and_text(methods, text)
    roles: Dict[str, str] = {}

    if family in {"numeric_state", "numeric_collection"}:
        roles["increase" if family == "numeric_state" else "add"] = _find_method(methods, ["deposit", "increment", "increase", "charge", "restock", "add_value", "add_number", "add"], ["remove"]) or ""
        roles["decrease" if family == "numeric_state" else "remove"] = _find_method(methods, ["withdraw", "decrement", "decrease", "use", "sell", "remove_value", "remove_number", "remove"], ["add"]) or ""
        roles["query"] = _find_method(methods, ["get_result", "get_value", "get_balance", "get_total", "get_score", "get_stock", "get_energy", "get_used", "get_remaining", "query", "get"], []) or ""
    elif family == "collection_aggregate":
        roles["add_item"] = _find_method(methods, ["add_item", "add", "insert", "append"], ["remove"]) or ""
        roles["remove_item"] = _find_method(methods, ["remove_item", "remove", "delete", "pop"], ["add"]) or ""
        roles["query_total"] = _find_method(methods, ["get_total_quantity", "get_total_value", "get_total", "total", "count", "get"], []) or ""
    elif family == "sorting_sequence":
        roles["add"] = _find_method(methods, ["add_number", "add_value", "add", "insert", "append", "push"], ["remove"]) or ""
        roles["remove"] = _find_method(methods, ["remove_number", "remove_value", "remove", "delete", "pop"], ["add"]) or ""
        roles["query_sorted"] = _find_method(methods, ["get_sorted", "sorted", "sort", "query", "get"], []) or ""
    elif family == "string_collection":
        roles["add_text"] = _find_method(methods, ["add_text", "add_string", "add_word", "add"], ["remove"]) or ""
        roles["remove_text"] = _find_method(methods, ["remove_text", "remove_string", "remove_word", "remove"], ["add"]) or ""
        roles["query"] = _find_method(methods, ["get_result", "get_counts", "get_count", "get_total_count", "result", "get"], []) or ""
    elif family == "string_counting":
        roles["add_text"] = _find_method(methods, ["add_text", "add_string", "add_word", "add"], ["remove"]) or ""
        roles["remove_text"] = _find_method(methods, ["remove_text", "remove_string", "remove_word", "remove"], ["add"]) or ""
        roles["query"] = _find_method(methods, ["get_result", "get_counts", "get_count", "get_total_count", "result", "get"], []) or ""
    elif family == "matrix_grid":
        roles["set_grid"] = _find_method(methods, ["set_grid", "set_matrix", "load_grid", "set"], []) or ""
        roles["query_transform"] = _find_method(methods, ["get_transformed_grid", "transform", "rotate", "get_grid", "get"], ["count"]) or ""
        roles["query_count"] = _find_method(methods, ["get_cell_count", "count_cell", "count"], []) or ""
    elif family == "rule_classification":
        roles["classify"] = _find_method(methods, ["classify", "evaluate", "judge", "check", "is_"], []) or ""
    elif family == "finite_state_machine":
        roles["transition"] = _find_method(methods, ["transition", "next", "start", "finish", "approve", "cancel", "move"], ["get"]) or ""
        roles["query_state"] = _find_method(methods, ["get_state", "get_status", "status", "state", "get"], []) or ""

    return {
        "profile_version": "2.1",
        "created_at": now_iso(),
        "generation_mode": "heuristic_profile_fallback_v2",
        "llm_used": False,
        "task_family": family,
        "support_status": "candidate",
        "class_name": class_name,
        "public_methods": methods,
        "method_roles": roles,
        "state_model": infer_state_model(family, text),
        "invariants": default_invariants_for_family(family),
        "mr_candidates": default_mr_candidates_for_family(family),
        "warnings": [],
    }


def extract_json_object(text: str) -> Any:
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?", "", text).strip()
        text = re.sub(r"```$", "", text).strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        return json.loads(text[start:end + 1])
    raise ValueError("Cannot extract JSON object")


def call_ollama_profile(inputs: VerificationInputs, model: str, heuristic: Dict[str, Any], timeout: int = 180) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    classes = extract_classes_and_methods(inputs.generated_solution_code)
    compact = {
        "frozen_spec": get_acceptance_spec(inputs.frozen_spec),
        "implementation_manifest": inputs.implementation_manifest,
        "classes_and_methods": classes,
        "stage4_summary": inputs.behavior_alignment_report.get("summary", {}),
        "generated_behavior_units": inputs.generated_behavior_spec.get("behavior_units", [])[:30],
        "heuristic_profile": heuristic,
    }
    prompt = textwrap.dedent(f"""
    你是驗證策略產生器，不是最終裁判。
    請根據 frozen spec、implementation manifest、AST 行為摘要，產生 verification_profile JSON。

    task_family 必須為其中之一：
    numeric_state, numeric_collection, collection_aggregate, sorting_sequence,
    string_collection, string_counting, matrix_grid, rule_classification, finite_state_machine,
    original_io_problem

    若 task_family=original_io_problem，method_roles 必須為 {{"solve": "solve"}}，且 generated_solution.py 必須有 top-level solve(input_text: str) -> str。
    method_roles 裡的 method name 必須存在於 public methods 或 top-level functions。
    不要新增 frozen spec 沒有的能力。
    mr_candidates 的 mr_template 必須使用系統已實作的模板名稱；不要自創新名稱。
    你可以優先沿用 heuristic_profile.mr_candidates 中的 mr_template。
    若需要舉一反三，只能在下列模板中選擇：
    {json.dumps(MR_TEMPLATES_BY_FAMILY, ensure_ascii=False)}

    請只輸出 JSON：
    {{
      "task_family": "...",
      "support_status": "candidate",
      "class_name": "...",
      "method_roles": {{"abstract_role": "actual_method_name"}},
      "state_model": {{}},
      "invariants": ["..."],
      "mr_candidates": [{{"mr_id": "MR-001", "mr_template": "...", "metamorphic": true, "risk_target": "..."}}],
      "warnings": []
    }}

    INPUT:
    {json.dumps(compact, ensure_ascii=False, default=str)[:60000]}
    """).strip()
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": "你只輸出 JSON，不輸出 markdown。"}, {"role": "user", "content": prompt}],
        "stream": False,
        "format": "json",
        "options": {"temperature": 0.0, "num_ctx": 20000},
    }
    try:
        req = urllib.request.Request(OLLAMA_CHAT_URL, data=json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        result = json.loads(raw)
        content = result.get("message", {}).get("content", "")
        parsed = extract_json_object(content)
        if not isinstance(parsed, dict):
            return None, "LLM profile output is not JSON object"
        parsed["generation_mode"] = "llm_profile_candidate_with_rule_validation_v2"
        parsed["llm_used"] = True
        parsed["llm_model"] = model
        parsed["llm_raw_content"] = content
        return parsed, None
    except Exception as e:
        return None, f"LLM profile generation failed: {type(e).__name__}: {e}"


def validate_profile(inputs: VerificationInputs, profile: Dict[str, Any]) -> Dict[str, Any]:
    classes = extract_classes_and_methods(inputs.generated_solution_code)
    top_functions = extract_top_level_functions(inputs.generated_solution_code)
    family = str(profile.get("task_family") or "")
    roles = profile.get("method_roles", {}) or {}
    if not isinstance(roles, dict):
        roles = {}
        profile["method_roles"] = roles
    errors: List[str] = []
    warnings: List[str] = list(profile.get("warnings", []) or [])

    if family == "original_io_problem":
        # Preferred contract is top-level solve(input_text: str) -> str.
        # Robust verifier fallback also accepts a zero-argument class instance with
        # a solve(input_text) method, because local LLM code generation sometimes
        # wraps the solver in a class even when the requirement asks for solve().
        class_solve_methods = {
            class_name: methods
            for class_name, methods in classes.items()
            if "solve" in methods
        }
        has_top_level_solve = "solve" in top_functions
        has_class_solve = bool(class_solve_methods)

        profile["class_name"] = ""
        profile["top_level_functions"] = top_functions
        profile["public_methods"] = top_functions + [
            f"{class_name}.solve" for class_name in sorted(class_solve_methods)
        ]

        if not has_top_level_solve and not has_class_solve:
            errors.append("solve(input_text: str) function not found in generated_solution.py")
        if roles.get("solve") not in {"solve", None, ""}:
            errors.append(f"method_roles.solve must point to solve, got: {roles.get('solve')}")
        roles["solve"] = "solve" if (has_top_level_solve or has_class_solve) else roles.get("solve", "")

        if has_class_solve and not has_top_level_solve:
            warnings.append("solve is implemented as a class method; verifier will instantiate the class as an IO-solver compatibility fallback")

        if family not in SUPPORTED_TASK_FAMILIES:
            errors.append(f"unsupported task_family value: {family!r}")
        if not MR_TEMPLATES_BY_FAMILY.get(family):
            errors.append(f"no MR templates available for task_family={family}")

        profile["state_model"] = profile.get("state_model") or {"interface": "solve(input_text: str) -> str"}
        profile["support_status"] = "supported" if not errors else "profile_validation_failed"
        profile["validation"] = {
            "validated_at": now_iso(),
            "validator": "rule_based_profile_validator_v8_5_5_original_io",
            "top_level_functions": top_functions,
            "class_methods": class_solve_methods,
            "errors": errors,
            "warnings": warnings,
        }
        profile.setdefault("mr_candidates", default_mr_candidates_for_family(family))
        return profile

    class_name = str(profile.get("class_name") or "")
    if class_name not in classes and classes:
        class_name = pick_class_name(inputs, classes)
        profile["class_name"] = class_name
    methods = classes.get(class_name, [])

    if not class_name or class_name not in classes:
        errors.append(f"class not found in generated_solution.py: {class_name!r}")
    if family not in SUPPORTED_TASK_FAMILIES:
        errors.append(f"unsupported task_family value: {family!r}")
    for role, method in list(roles.items()):
        if method in (None, ""):
            continue
        if str(method) not in methods:
            errors.append(f"method_roles.{role} points to missing method: {method}")
    missing_roles = [role for role in FAMILY_REQUIRED_ROLES.get(family, []) if not roles.get(role)]
    if missing_roles:
        errors.append(f"missing required method roles for {family}: {missing_roles}")
    if not MR_TEMPLATES_BY_FAMILY.get(family):
        errors.append(f"no MR templates available for task_family={family}")

    text_for_oracle = _text_blob(inputs.frozen_spec, inputs.implementation_manifest, inputs.generated_behavior_spec, inputs.behavior_alignment_report)
    profile["state_model"] = normalize_empty_query_expected_from_requirement(
        family, text_for_oracle, profile.get("state_model", {})
    )

    profile["support_status"] = "supported" if not errors else "profile_validation_failed"
    profile["validation"] = {"validated_at": now_iso(), "validator": "rule_based_profile_validator_v8_5_empty_oracle_and_io", "class_methods": methods, "top_level_functions": top_functions, "errors": errors, "warnings": warnings}
    profile["public_methods"] = methods
    profile.setdefault("mr_candidates", default_mr_candidates_for_family(family))
    return profile


def build_verification_profile(inputs: VerificationInputs, model: str, use_llm: bool) -> Dict[str, Any]:
    heuristic = heuristic_profile(inputs)
    if use_llm and model and model.lower() != "none":
        llm_profile, warning = call_ollama_profile(inputs, model, heuristic)
        if llm_profile:
            for key in ["class_name", "task_family", "method_roles", "state_model", "invariants", "mr_candidates"]:
                if not llm_profile.get(key):
                    llm_profile[key] = heuristic.get(key)

            # v8.6.2 original-IO stability rule:
            # The LLM may suggest semantic MR names, but executable semantic IO MRs
            # must come from the statement-evidence registry so that evidence_terms
            # and conservative gates are preserved.  This prevents a free-form LLM
            # candidate from overriding or duplicating the registry candidate, and
            # also lets known-safe registry MRs actually run when the statement
            # contains strong evidence.
            if heuristic.get("task_family") == "original_io_problem":
                llm_profile["task_family"] = "original_io_problem"
                llm_profile["class_name"] = heuristic.get("class_name", "")
                llm_profile["method_roles"] = heuristic.get("method_roles", {"solve": "solve"})
                llm_profile["state_model"] = heuristic.get("state_model", {"interface": "solve(input_text: str) -> str"})
                llm_profile["invariants"] = heuristic.get("invariants", [])
                llm_profile["mr_candidates"] = heuristic.get("mr_candidates", [])
                llm_profile.setdefault("warnings", []).append(
                    "original_io_problem uses registry-gated MR candidates; free-form LLM MR candidates are not executed directly."
                )

            profile = validate_profile(inputs, llm_profile)
            # v8.2 semantic guardrail: when AST-public methods clearly indicate a
            # different task family than the LLM selected, prefer the heuristic
            # profile. This prevents cases such as add_text/remove_text/get_result
            # being misclassified as sorting_sequence.
            if profile.get("support_status") == "supported":
                if profile.get("task_family") != heuristic.get("task_family") and heuristic.get("task_family") != "original_io_problem":
                    method_text = " ".join(str(m) for m in heuristic.get("public_methods", [])).lower()
                    strong_text_api = ("add_text" in method_text or "remove_text" in method_text) and "get_sorted" not in method_text
                    strong_sort_api = "get_sorted" in method_text
                    strong_numeric_api = ("add_value" in method_text or "remove_value" in method_text or "add_number" in method_text or "remove_number" in method_text) and "get_sorted" not in method_text
                    if strong_text_api or strong_sort_api or strong_numeric_api:
                        heuristic.setdefault("warnings", []).append(
                            "LLM profile task_family conflicted with AST public-method guardrail; fallback to heuristic profile."
                        )
                        heuristic.setdefault("warnings", []).append(
                            f"llm_family={profile.get('task_family')}, heuristic_family={heuristic.get('task_family')}"
                        )
                        return validate_profile(inputs, heuristic)
                return profile
            heuristic.setdefault("warnings", []).append("LLM profile rejected by rule validator; fallback to heuristic profile.")
            heuristic.setdefault("warnings", []).append(str(profile.get("validation", {}).get("errors")))
        else:
            heuristic.setdefault("warnings", []).append(warning or "LLM profile unavailable; fallback to heuristic.")
    return validate_profile(inputs, heuristic)


def build_basic_probe_plan(inputs: VerificationInputs, profile: Dict[str, Any]) -> Dict[str, Any]:
    probes = []
    if profile.get("support_status") == "supported":
        if profile.get("task_family") == "original_io_problem":
            probes.append({"probe_id": "PROBE-001", "description": "Import generated_solution.solve and run sample/basic IO checks.", "task_family": profile.get("task_family"), "method_roles": profile.get("method_roles")})
        else:
            probes.append({"probe_id": "PROBE-001", "description": f"Instantiate {profile.get('class_name')} and call basic public methods.", "task_family": profile.get("task_family"), "method_roles": profile.get("method_roles")})
    return {"source_run_id": inputs.run_id, "generation_id": inputs.gen_id, "created_at": now_iso(), "probe_generation_method": "verification_profile_based_probe_v8_2", "task_family": profile.get("task_family"), "support_status": profile.get("support_status"), "probes": probes, "notes": ["LLM may generate profile candidates; rule validation and Docker execution provide hard evidence."]}


def build_mr_plan(inputs: VerificationInputs, profile: Dict[str, Any]) -> Dict[str, Any]:
    family = profile.get("task_family")
    candidates = profile.get("mr_candidates") or default_mr_candidates_for_family(str(family))
    return {"source_run_id": inputs.run_id, "generation_id": inputs.gen_id, "created_at": now_iso(), "task_name": profile.get("class_name") or get_task_name(inputs.frozen_spec), "generation_mode": profile.get("generation_mode"), "llm_used": bool(profile.get("llm_used")), "llm_model": profile.get("llm_model"), "task_family": family, "method_roles": profile.get("method_roles"), "mr_candidates": candidates}


EXECUTABLE_TEST_BY_TEMPLATE = {
    "MR_QUERY_IDEMPOTENCE": "test_query_idempotence",
    "MR_SORT_QUERY_IDEMPOTENCE": "test_query_idempotence",
    "MR_GRID_QUERY_IDEMPOTENCE": "test_query_idempotence",
    "MR_CLASSIFICATION_IDEMPOTENCE": "test_query_idempotence",
    "MR_QUERY_EMPTY_STATE": "test_empty_query_behavior",

    "MR_ADD_IDEMPOTENCE": "test_add_idempotence",
    "MR_ADD_REMOVE_RESTORATION": "test_add_remove_restoration",
    "MR_SORT_REMOVE_EFFECT": "test_add_remove_restoration",
    "MR_TEXT_ADD_REMOVE_RESTORATION": "test_add_remove_restoration",

    "MR_OPERATION_ORDER_INVARIANCE": "test_order_invariance",
    "MR_SORT_ORDER_INVARIANCE": "test_order_invariance",
    "MR_TEXT_ORDER_INVARIANCE": "test_order_invariance",
    "MR_TEXT_DUPLICATION_SCALING_OR_CHANGE": "test_text_duplication_scaling",

    "MR_INVALID_OPERATION_STATE_UNCHANGED": "test_invalid_operation_state_unchanged",
    "MR_INVALID_GRID_REJECTED": "test_invalid_operation_state_unchanged",
    "MR_INVALID_TRANSITION_STATE_UNCHANGED": "test_invalid_operation_state_unchanged",

    "MR_GRID_CELL_COUNT_CONSISTENCY": "test_grid_cell_count",

    "MR_IO_DETERMINISM": "test_io_determinism",
    "MR_IO_CRLF_NORMALIZATION": "test_io_crlf_normalization",
    "MR_IO_T_CASE_DUPLICATION": "test_io_t_case_duplication",
    "MR_IO_SEMANTIC_INTERVAL_ORDER_INVARIANCE": "test_io_semantic_interval_order_invariance",
    "MR_IO_SEMANTIC_MATRIX_ZERO_PADDING": "test_io_semantic_matrix_zero_padding",
    "MR_IO_SEMANTIC_SKYLINE_DOMINATED_BUILDING": "test_io_semantic_skyline_dominated_building",
}


MR_TEMPLATE_ALIASES = {
    # Common LLM paraphrases normalized into the executable MR vocabulary.
    "MR_ADD_REMOVE_CANCELLATION": "MR_ADD_REMOVE_RESTORATION",
    "MR_REMOVE_ADD_CANCELLATION": "MR_ADD_REMOVE_RESTORATION",
    "MR_INVALID_INPUT_STATE_UNCHANGED": "MR_INVALID_OPERATION_STATE_UNCHANGED",
    "MR_INVALID_REMOVE_STATE_UNCHANGED": "MR_INVALID_OPERATION_STATE_UNCHANGED",
    "MR_EMPTY_QUERY": "MR_QUERY_EMPTY_STATE",
    "MR_EMPTY_STATE_QUERY": "MR_QUERY_EMPTY_STATE",
    "MR_QUERY_ON_EMPTY_STATE": "MR_QUERY_EMPTY_STATE",
    "MR_IO_SAMPLE": "MR_IO_SAMPLE_CONSISTENCY",
    "MR_SAMPLE_IO": "MR_IO_SAMPLE_CONSISTENCY",
    "MR_DETERMINISTIC_IO": "MR_IO_DETERMINISM",
    "MR_LINE_ENDING_NORMALIZATION": "MR_IO_CRLF_NORMALIZATION",
}

def _canonical_mr_template(value: Any) -> str:
    template = str(value or "").strip().upper().replace("-", "_").replace(" ", "_")
    return MR_TEMPLATE_ALIASES.get(template, template)


def _accepted_test_names(validated_plan: Dict[str, Any]) -> List[str]:
    names: List[str] = []
    for mr in validated_plan.get("accepted_mrs", []) or []:
        template = _canonical_mr_template(mr.get("mr_template") or mr.get("template") or mr.get("name"))
        fn = EXECUTABLE_TEST_BY_TEMPLATE.get(template)
        if fn and fn not in names:
            names.append(fn)
    return names


def validate_mr_plan(inputs: VerificationInputs, profile: Dict[str, Any], draft_plan: Dict[str, Any]) -> Dict[str, Any]:
    """Validate LLM/heuristic MR candidates before execution.

    v3 policy:
    - Only MR templates supported by the current task family are accepted.
    - Only templates with an implemented executable test are accepted.
    - Non-applicable MR candidates are rejected at plan-validation time, not
      emitted into generated_mr_tests.py and not shown as runtime skipped.
    """
    candidates = draft_plan.get("mr_candidates", []) or []
    family = str(profile.get("task_family") or "")
    family_templates = {_canonical_mr_template(x) for x in MR_TEMPLATES_BY_FAMILY.get(family, [])}

    if profile.get("support_status") != "supported":
        return {
            "source_run_id": inputs.run_id,
            "generation_id": inputs.gen_id,
            "created_at": now_iso(),
            "validation_method": "profile_gated_mr_validation_v8_6_2",
            "accepted_count": 0,
            "rejected_count": len(candidates),
            "accepted_mrs": [],
            "rejected_mrs": [
                {**mr, "validation_status": "rejected", "rejection_reasons": profile.get("validation", {}).get("errors", [])}
                for mr in candidates
            ],
            "profile_support_status": profile.get("support_status"),
            "profile_validation_errors": profile.get("validation", {}).get("errors", []),
            "presentation_note": "Rejected candidates are audit evidence, not executed tests.",
        }

    accepted: List[Dict[str, Any]] = []
    rejected: List[Dict[str, Any]] = []
    seen_templates: set[str] = set()

    for i, mr in enumerate(candidates, start=1):
        item = dict(mr)
        item.setdefault("mr_id", f"MR-{i:03d}")
        item.setdefault("metamorphic", True)
        template = _canonical_mr_template(item.get("mr_template") or item.get("template") or item.get("name"))
        item["mr_template"] = template
        reasons: List[str] = []

        if not template:
            reasons.append("missing mr_template")
        if template not in family_templates:
            reasons.append(f"MR template {template!r} is not listed for task_family={family!r}")
        if template not in EXECUTABLE_TEST_BY_TEMPLATE:
            reasons.append(f"MR template {template!r} has no implemented executable checker")
        if template in seen_templates:
            reasons.append(f"duplicate MR template {template!r}")

        if family == "original_io_problem" and template.startswith("MR_IO_SEMANTIC_"):
            # v8.6.1: semantic IO MRs must come from registry evidence, not from
            # the default family template list or a free-form LLM suggestion.
            evidence_terms = item.get("evidence_terms")
            if item.get("mr_strength") != "semantic" or not isinstance(evidence_terms, list) or not evidence_terms:
                reasons.append("semantic IO MR requires statement-level registry evidence")

        if reasons:
            item["validation_status"] = "rejected"
            item["rejection_reasons"] = reasons
            rejected.append(item)
            continue

        seen_templates.add(template)
        item["validation_status"] = "accepted"
        item["executable_test"] = EXECUTABLE_TEST_BY_TEMPLATE[template]
        item["validation_reasons"] = [
            "profile support_status is supported",
            "required public method roles exist",
            "MR template belongs to the current task family",
            "MR template has an implemented executable checker",
        ]
        accepted.append(item)

    return {
        "source_run_id": inputs.run_id,
        "generation_id": inputs.gen_id,
        "created_at": now_iso(),
        "validation_method": "profile_gated_mr_validation_v8_6_2",
        "accepted_count": len(accepted),
        "rejected_count": len(rejected),
        "accepted_mrs": accepted,
        "rejected_mrs": rejected,
        "profile_support_status": profile.get("support_status"),
        "presentation_note": "Only accepted MR candidates are emitted into generated_mr_tests.py. Rejected candidates are kept for audit, not counted as skipped runtime tests.",
    }


def _clean_control_chars(text: str) -> str:
    if not isinstance(text, str):
        text = str(text or "")
    # Windows subprocess cannot accept embedded NUL; PDF extraction may contain it.
    text = text.replace("\x00", "")
    return "".join(ch for ch in text if ch in "\n\r\t" or ord(ch) >= 32)


def _strip_appended_interface_block(text: str) -> str:
    """
    Original CPE/OJ statements are followed by our local interface wrapper.
    The wrapper is not part of the sample output oracle.  Keep the original
    statement only when extracting Sample Input/Output.
    """
    if not isinstance(text, str):
        text = str(text or "")
    stop_markers = [
        "\n請依照上面的原始題目需求",
        "\n請依照上面的題目需求",
        "\n介面規則：",
        "\ndef solve(input_text: str)",
        "\n    def solve(input_text: str)",
    ]
    cut = len(text)
    for marker in stop_markers:
        pos = text.find(marker)
        if pos >= 0:
            cut = min(cut, pos)
    return text[:cut]


def _trim_sample_output_tail(text: str) -> str:
    """
    PDF/OCR statements sometimes append footer or local wrapper text after
    Sample Output.  Trim only very explicit non-oracle markers.
    """
    if not isinstance(text, str):
        text = str(text or "")
    tail_markers = [
        "\n請依照上面的原始題目需求",
        "\n請依照上面的題目需求",
        "\n介面規則：",
        "\nModified from UVA",
        "\nUniversidad de Valladolid",
        "\nUVa Online Judge",
    ]
    cut = len(text)
    for marker in tail_markers:
        pos = text.find(marker)
        if pos >= 0:
            cut = min(cut, pos)
    return text[:cut].strip("\n")

def _sanitize_sample_input_seed(text: str) -> Dict[str, Any]:
    """
    Clean Sample Input before using it as a *formal MR seed*.

    This is intentionally different from using sample expected output as a
    Basic IO Probe.  Formal IO MRs should not be triggered by PDF/OCR footer
    text such as "Universidad de Valladolid OJ ... 2/2" accidentally captured
    after the sample block.  If the seed remains suspicious after cleaning, it
    is marked low-confidence and formal MRs will skip it.
    """
    original = _clean_control_chars(text or "")
    normalized = original.replace("\r\n", "\n").replace("\r", "\n")
    notes: List[str] = []

    # Cut local wrapper first.  This wrapper is not part of the original input.
    wrapper_cut_markers = [
        "\n請依照上面的原始題目需求",
        "\n請依照上面的題目需求",
        "\n介面規則：",
        "\ndef solve(input_text: str)",
    ]
    cut = len(normalized)
    for marker in wrapper_cut_markers:
        pos = normalized.find(marker)
        if pos >= 0:
            cut = min(cut, pos)
            notes.append("local_wrapper_removed")
    normalized = normalized[:cut]

    # Cut common UVA PDF footer / page marker lines.  We cut from the first
    # detected footer line to the end, rather than deleting individual tokens,
    # because footer capture usually means everything after it is non-input.
    footer_patterns = [
        r"^\s*Universidad\s+de\s+Valladolid\s+OJ\s*:",
        r"^\s*Universidad\s+de\s+Valladolid",
        r"^\s*UVa\s+Online\s+Judge",
        r"^\s*UVA\s+Online\s+Judge",
        r"^\s*Online\s+Judge\s*$",
        r"^\s*\d+\s*/\s*\d+\s*$",
        r"^\s*.*\bOJ\s*:\s*\d+.*\b\d+\s*/\s*\d+\s*$",
    ]
    lines = normalized.split("\n")
    keep: List[str] = []
    footer_seen = False
    for line in lines:
        if any(re.search(pat, line, flags=re.I) for pat in footer_patterns):
            footer_seen = True
            notes.append("pdf_footer_removed")
            break
        keep.append(line)
    cleaned = "\n".join(keep).strip("\n")

    remaining_suspicious = any(
        re.search(pat, cleaned, flags=re.I | re.M)
        for pat in footer_patterns
    )
    if remaining_suspicious:
        notes.append("suspicious_footer_still_present")

    confidence = "low" if remaining_suspicious else "high"
    if footer_seen and cleaned.strip():
        # Footer was removed successfully; the remaining seed is usually better
        # than the raw sample and can still be used by formal MRs.
        confidence = "high"
    if not cleaned.strip() and original.strip():
        confidence = "low"
        notes.append("seed_empty_after_sanitization")

    return {
        "input": cleaned + ("\n" if cleaned and not cleaned.endswith("\n") else ""),
        "confidence": confidence,
        "notes": sorted(set(notes)),
    }


def _is_placeholder_sample_output(text: str) -> bool:
    low = str(text or "").lower()
    return "<number>" in low or "<output>" in low or "..." in low


def _extract_sample_tests_from_statement(text: str, max_samples: int = 3) -> List[Dict[str, str]]:
    text = _clean_control_chars(text or "")
    text = _strip_appended_interface_block(text)

    # Normalize common OCR / PDF variants.
    normalized = text.replace("Sample Input", "\nSample Input\n").replace("Sample Output", "\nSample Output\n")
    normalized = normalized.replace("Sample input", "\nSample Input\n").replace("Sample output", "\nSample Output\n")
    tests: List[Dict[str, str]] = []

    # Capture Sample Input ... Sample Output ... blocks.  Stop before another
    # sample block, an explanation section, a problem footer, or our wrapper.
    pat = re.compile(
        r"Sample\s+Input\s*\n(?P<input>.*?)(?:\n\s*Sample\s+Output\s*\n)(?P<output>.*?)(?=\n\s*(?:Sample\s+Input|Explanation|Notes?|Problem\s+\d+|Input\s*$|Output\s*$|請依照上面的|介面規則：|Modified\s+from\s+UVA|Universidad\s+de\s+Valladolid)|\Z)",
        re.I | re.S,
    )
    for m in pat.finditer(normalized):
        sin_raw = m.group("input").strip("\n")
        seed = _sanitize_sample_input_seed(sin_raw)
        sin = seed["input"]
        sout = _trim_sample_output_tail(m.group("output")).strip()
        if sin and sout and not _is_placeholder_sample_output(sout):
            tests.append({
                "input": sin,
                "expected_output": sout,
                "seed_confidence": seed["confidence"],
                "seed_sanitizer_notes": seed["notes"],
            })
        if len(tests) >= max_samples:
            break

    # Some original OJ problems have no input and only a concrete Sample Output.
    # Use it only when the statement explicitly says there is no input and the
    # output is concrete, not a placeholder like <number>.
    if not tests and re.search(r"there\s+is\s+no\s+input|no\s+input\s+to\s+this\s+program", normalized, re.I):
        m = re.search(r"Sample\s+Output\s*\n(?P<output>.*?)(?=\n\s*(?:請依照上面的|介面規則：|Modified\s+from\s+UVA|Universidad\s+de\s+Valladolid)|\Z)", normalized, re.I | re.S)
        if m:
            sout = _trim_sample_output_tail(m.group("output")).strip()
            if sout and not _is_placeholder_sample_output(sout):
                tests.append({"input": "", "expected_output": sout, "seed_confidence": "low", "seed_sanitizer_notes": ["no_input_problem_not_useful_as_formal_seed"]})

    return tests


def get_sample_tests(inputs: VerificationInputs) -> List[Dict[str, str]]:
    raw = find_raw_requirement_in_frozen(inputs.frozen_spec)
    return _extract_sample_tests_from_statement(raw, max_samples=3)


def statement_allows_t_case_duplication(inputs: VerificationInputs) -> bool:
    """
    Conservative guard for MR_IO_T_CASE_DUPLICATION.

    Minimal v8.6.3-safe change:
    - Keep the existing T-case MR implementation.
    - Only tighten the *precondition* for enabling it.
    - If output has case/pair/dataset numbering or special inter-case separators,
      skip this MR instead of treating the relation as hard defect evidence.

    Rationale:
    Duplicating the source test set and expecting output == source_output repeated
    is unsafe when the problem prints labels such as "Case #1", "Pair #1",
    "Scenario #1", or requires blank lines between cases.  In those problems,
    the correct follow-up output may include renumbered labels or separator rules
    that are not captured by this generic MR.
    """
    raw = _strip_appended_interface_block(
        _clean_control_chars(find_raw_requirement_in_frozen(inputs.frozen_spec))
    )
    low = raw.lower()

    # Hard negative gates first: these make the generic duplication oracle unsafe.
    # They do not mean the generated program is correct; they only mean this
    # specific MR is not a reliable oracle for the task.
    unsafe_output_patterns = [
        # Explicit case / pair / dataset / scenario numbering in output.
        r"\bcase\s*#\s*\d*",
        r"\bcase\s+\d+\s*:",
        r"\bcase\s+number\b",
        r"\bcases?\s+are\s+numbered\b",
        r"\bnumbered\s+(?:sequentially|starting)\b",
        r"\bpair\s*#\s*\d*",
        r"\bscenario\s*#\s*\d*",
        r"\bproblem\s*#\s*\d*",
        r"\bdataset\s*#\s*\d*",
        r"\bdata\s+set\s*#\s*\d*",
        r"\bset\s*#\s*\d*",
        r"\btrial\s*#\s*\d*",
        r"\btest\s+case\s*#\s*\d*",
        r"\bprint.*?(?:case|pair|dataset|data\s+set|scenario|problem|set).*?(?:number|#)",
        r"\boutput.*?(?:case|pair|dataset|data\s+set|scenario|problem|set).*?(?:number|#)",

        # Generic duplication is also fragile when the statement has explicit
        # blank-line rules between outputs/cases.
        r"blank\s+line\s+between\s+(?:datasets|data\s+sets|test\s+cases|cases|outputs?)",
        r"blank\s+line\s+after\s+(?:each|every)\s+(?:dataset|data\s+set|test\s+case|case)",
        r"separat(?:e|ed|es|ing).*?(?:datasets|data\s+sets|test\s+cases|cases|outputs?).*?blank\s+line",
        r"(?:datasets|data\s+sets|test\s+cases|cases|outputs?).*?separat(?:e|ed|es|ing).*?blank\s+line",
        r"exactly\s+one\s+blank\s+line\s+between",

        # Multiple independent datasets not introduced by a T/count line.
        r"input\s+is\s+a\s+sequence\s+of",
        r"input\s+will\s+consist\s+of\s+a\s+series",
        r"each\s+test\s+begins\s+with",
        r"each\s+case\s+begins\s+with",
        r"the\s+last\s+test\s+will\s+be\s+followed\s+by",
    ]
    if any(re.search(p, low, re.I | re.S) for p in unsafe_output_patterns):
        return False

    # Positive gates: the statement must clearly say the first line / beginning
    # of input gives a test-case count.
    positive_patterns = [
        r"first\s+line\s+(?:of\s+input\s+)?(?:contains|has|consists\s+of).*?(?:number\s+of\s+)?(?:test\s+cases|cases)",
        r"first\s+line.*?integer\s+(?:t|n).*?(?:test\s+cases|cases)",
        r"number\s+of\s+test\s+cases",
        r"there\s+are\s+\w+\s+test\s+cases",
        r"input\s+begins\s+with.*?(?:test\s+cases|cases)",
        r"第一行.*?(測資|案例|case).*?(數量|個數|組數)",
        r"測資.*?(組數|數量|個數)",
    ]
    if not any(re.search(p, low, re.I | re.S) for p in positive_patterns):
        return False

    # Extra negative gates after positives to avoid integer-first-line false
    # positives where the first integer is a size, dimension, coordinate bound,
    # number of buildings, etc.
    negative_patterns = [
        r"first\s+line.*?(?:matrix|dimension|size|n\s*x\s*n|grid|upper-right|coordinate|number\s+of\s+buildings)",
        r"there\s+is\s+no\s+input",
        r"no\s+input\s+to\s+this\s+program",
    ]
    if any(re.search(p, low, re.I | re.S) for p in negative_patterns):
        return False

    return True


# -----------------------------------------------------------------------------
# Class / method MR runner
# -----------------------------------------------------------------------------

TEST_RUNNER_TEMPLATE = r'''
import importlib
import inspect
import json
import math
import traceback
from pathlib import Path

CLASS_NAME = "__CLASS_NAME__"
PROFILE = __PROFILE_JSON__
ACTIVE_TESTS = __ACTIVE_TESTS_JSON__


def _record(name, passed, details=None, error=None, skipped=False):
    status = "skipped" if skipped else ("passed" if passed else "failed")
    return {"name": name, "passed": bool(passed) and not skipped, "status": status, "details": details or {}, "error": error}


def _load_class():
    mod = importlib.import_module("generated_solution")
    cls = getattr(mod, CLASS_NAME)
    return cls


def _required_positional_count(callable_obj):
    try:
        sig = inspect.signature(callable_obj)
        count = 0
        for p in sig.parameters.values():
            if p.default is inspect._empty and p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD):
                count += 1
        return count
    except Exception:
        return 0


def _new_instance():
    cls = _load_class()
    init = cls.__init__
    required = max(0, _required_positional_count(init) - 1)  # remove self
    candidates = [
        [],
        [10] * required,
        [100] * required,
        [10, 100][:required],
        ["default"] * required,
    ]
    last_error = None
    for args in candidates:
        if len(args) != required:
            continue
        try:
            return cls(*args)
        except Exception as e:
            last_error = e
    try:
        return cls()
    except Exception as e:
        raise RuntimeError(f"cannot instantiate {CLASS_NAME}: {last_error or e}")


def _call(obj, method_name, *args):
    if not method_name:
        raise AttributeError("missing method role")
    fn = getattr(obj, method_name)
    return fn(*args)


def _try_call(obj, method_name, candidates):
    last = None
    for args in candidates:
        try:
            return True, _call(obj, method_name, *args), None, args
        except Exception:
            last = traceback.format_exc()
    return False, None, last, None


def _jsonable(value):
    if isinstance(value, float):
        if math.isnan(value):
            return "NaN"
        if math.isinf(value):
            return "Infinity" if value > 0 else "-Infinity"
    if isinstance(value, tuple):
        return [_jsonable(x) for x in value]
    if isinstance(value, list):
        return [_jsonable(x) for x in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    try:
        json.dumps(value, ensure_ascii=False, default=str)
        return value
    except Exception:
        return repr(value)


def _equal(a, b):
    return json.dumps(_jsonable(a), ensure_ascii=False, sort_keys=True, default=str) == json.dumps(_jsonable(b), ensure_ascii=False, sort_keys=True, default=str)


def _snapshot(obj):
    roles = PROFILE.get("method_roles", {}) or {}
    query_candidates = []
    for key in ["query", "query_total", "query_sorted", "query_state", "query_count", "query_transform", "classify"]:
        if roles.get(key):
            query_candidates.append(roles.get(key))
    query_candidates += ["get_value", "get_balance", "get_result", "get_total", "get_total_quantity", "get_state", "get_status", "query", "count"]
    seen = set()
    for name in query_candidates:
        if not name or name in seen or not hasattr(obj, name):
            continue
        seen.add(name)
        try:
            value = getattr(obj, name)()
            return {"query_method": name, "value": _jsonable(value)}
        except TypeError:
            continue
        except Exception as e:
            return {"query_method": name, "error": repr(e)}
    state = {}
    for k, v in sorted(getattr(obj, "__dict__", {}).items()):
        if not callable(v):
            state[k] = _jsonable(v)
    return {"query_method": "__dict__", "value": state}


def _mutation_roles():
    roles = PROFILE.get("method_roles", {}) or {}
    add_roles = ["increase", "add", "add_item", "add_text", "set_grid", "transition"]
    remove_roles = ["decrease", "remove", "remove_item", "remove_text"]
    add = next((roles.get(r) for r in add_roles if roles.get(r)), "")
    remove = next((roles.get(r) for r in remove_roles if roles.get(r)), "")
    return add, remove


def _valid_args_for(method_name):
    low = (method_name or "").lower()
    if any(x in low for x in ["text", "word", "string"]):
        return [("alpha",), ("beta",)]
    if any(x in low for x in ["item", "cart", "quantity"]):
        return [("apple", 2), ("apple",), (2,)]
    if any(x in low for x in ["grid", "matrix"]):
        return [([[1, 2], [3, 4]],), ([[1]],)]
    if any(x in low for x in ["classify", "check", "judge"]):
        return [(1,), ("A",)]
    return [(5,), (2,), (1,)]


def _invalid_args_for(method_name):
    low = (method_name or "").lower()
    if any(x in low for x in ["text", "word", "string"]):
        return [("",), (None,), (123,)]
    if any(x in low for x in ["item", "cart", "quantity"]):
        return [("apple", 0), ("apple", -1), ("__missing__", 1), (None,), ("apple", None)]
    if any(x in low for x in ["grid", "matrix"]):
        return [([],), ([[1], [1, 2]],), (None,)]
    return [(0,), (-1,), (float("nan"),), (float("inf"),), (True,), ("bad",), (None,)]


def _apply_valid(obj, method_name):
    ok, value, err, args = _try_call(obj, method_name, _valid_args_for(method_name))
    return ok, value, err, args


def test_query_idempotence():
    name = "test_query_idempotence"
    try:
        obj = _new_instance()
        add, _ = _mutation_roles()
        if add:
            _apply_valid(obj, add)
        before = _snapshot(obj)
        after = _snapshot(obj)
        return _record(name, _equal(before, after), {"before": before, "after": after})
    except Exception:
        return _record(name, False, error=traceback.format_exc())


def test_empty_query_behavior():
    name = "test_empty_query_behavior"
    try:
        expected_marker = (PROFILE.get("state_model") or {}).get("empty_query_expected", "__NO_EXPECTATION__")
        if expected_marker == "__NO_EXPECTATION__":
            return _record(name, False, {"skipped": "no empty_query_expected in profile"}, skipped=True)
        obj = _new_instance()
        snap = _snapshot(obj)
        observed = snap.get("value")
        return _record(name, _equal(observed, expected_marker), {"observed": observed, "expected": expected_marker, "snapshot": snap})
    except Exception:
        return _record(name, False, error=traceback.format_exc())


def test_add_idempotence():
    name = "test_add_idempotence"
    try:
        obj = _new_instance()
        add, _ = _mutation_roles()
        if not add:
            return _record(name, False, {"skipped": "no add/increase role"}, skipped=True)
        ok1, _, err1, args1 = _apply_valid(obj, add)
        s1 = _snapshot(obj)
        ok2, _, err2, args2 = _try_call(obj, add, [args1] if args1 else _valid_args_for(add))
        s2 = _snapshot(obj)
        passed = ok1 and ok2 and "error" not in s2
        return _record(name, passed, {"first_ok": ok1, "second_ok": ok2, "first_snapshot": s1, "second_snapshot": s2, "errors": [err1, err2]})
    except Exception:
        return _record(name, False, error=traceback.format_exc())


def test_add_remove_restoration():
    name = "test_add_remove_restoration"
    try:
        obj = _new_instance()
        add, remove = _mutation_roles()
        if not add or not remove:
            return _record(name, False, {"skipped": "missing add/remove roles", "add": add, "remove": remove}, skipped=True)
        before = _snapshot(obj)
        ok_add, _, err_add, args_add = _apply_valid(obj, add)
        remove_candidates = [args_add] if args_add else []
        remove_candidates += _valid_args_for(remove)
        ok_remove, _, err_remove, args_remove = _try_call(obj, remove, remove_candidates)
        after = _snapshot(obj)
        passed = ok_add and ok_remove and _equal(before, after)
        return _record(name, passed, {"before": before, "after": after, "add_args": _jsonable(args_add), "remove_args": _jsonable(args_remove), "errors": [err_add, err_remove]})
    except Exception:
        return _record(name, False, error=traceback.format_exc())


def test_order_invariance():
    name = "test_order_invariance"
    try:
        add, _ = _mutation_roles()
        if not add:
            return _record(name, False, {"skipped": "missing add/increase role"}, skipped=True)
        obj1 = _new_instance()
        obj2 = _new_instance()
        args = _valid_args_for(add)[:2]
        if len(args) < 2:
            args = [(1,), (2,)]
        ok11, _, e11, _ = _try_call(obj1, add, [args[0]])
        ok12, _, e12, _ = _try_call(obj1, add, [args[1]])
        ok21, _, e21, _ = _try_call(obj2, add, [args[1]])
        ok22, _, e22, _ = _try_call(obj2, add, [args[0]])
        s1 = _snapshot(obj1)
        s2 = _snapshot(obj2)
        passed = all([ok11, ok12, ok21, ok22]) and _equal(s1, s2)
        return _record(name, passed, {"source_snapshot": s1, "followup_snapshot": s2, "errors": [e11, e12, e21, e22]})
    except Exception:
        return _record(name, False, error=traceback.format_exc())


def test_invalid_operation_state_unchanged():
    name = "test_invalid_operation_state_unchanged"
    try:
        add, remove = _mutation_roles()
        methods = [m for m in [add, remove] if m]
        if not methods:
            return _record(name, False, {"skipped": "no mutation roles"}, skipped=True)
        failures = []
        cases = []
        for method in methods:
            for args in _invalid_args_for(method):
                obj = _new_instance()
                if method == remove and add:
                    _apply_valid(obj, add)
                before = _snapshot(obj)
                raised = False
                try:
                    _call(obj, method, *args)
                except Exception:
                    raised = True
                after = _snapshot(obj)
                unchanged = _equal(before, after)
                case = {"method": method, "args": _jsonable(args), "raised": raised, "state_unchanged": unchanged, "before": before, "after": after}
                cases.append(case)
                if not unchanged:
                    failures.append(case)
        return _record(name, not failures, {"relation": "invalid operations should not change observable state", "cases": cases, "failures": failures})
    except Exception:
        return _record(name, False, error=traceback.format_exc())


def test_text_duplication_scaling():
    return _record("test_text_duplication_scaling", False, {"skipped": "generic text scaling oracle is intentionally disabled"}, skipped=True)


def test_grid_cell_count():
    return _record("test_grid_cell_count", False, {"skipped": "generic grid count oracle is intentionally disabled"}, skipped=True)


TESTS = {
    "test_query_idempotence": test_query_idempotence,
    "test_empty_query_behavior": test_empty_query_behavior,
    "test_add_idempotence": test_add_idempotence,
    "test_add_remove_restoration": test_add_remove_restoration,
    "test_order_invariance": test_order_invariance,
    "test_invalid_operation_state_unchanged": test_invalid_operation_state_unchanged,
    "test_text_duplication_scaling": test_text_duplication_scaling,
    "test_grid_cell_count": test_grid_cell_count,
}


def main():
    results = []
    for name in ACTIVE_TESTS:
        fn = TESTS.get(name)
        if fn is None:
            results.append(_record(name, False, error="unknown test name"))
            continue
        try:
            results.append(fn())
        except Exception:
            results.append(_record(name, False, error=traceback.format_exc()))

    runtime = {
        "total": len(results),
        "executed": sum(1 for r in results if r.get("status") != "skipped"),
        "passed": sum(1 for r in results if r.get("status") == "passed"),
        "failed": sum(1 for r in results if r.get("status") == "failed"),
        "skipped": sum(1 for r in results if r.get("status") == "skipped"),
    }
    report = {
        "runtime_results": results,
        "runtime_summary": runtime,
        "oracle_policy": {
            "formal_mr": "class-state MRs compare observable state before/after source/follow-up operations",
            "skipped_policy": "unsupported generic checks are skipped, not counted as failures",
        },
    }
    Path("test_runtime_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if runtime["failed"] > 0:
        raise SystemExit(1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''



IO_TEST_RUNNER_TEMPLATE = r'''
import importlib
import inspect
import json
import re
import traceback
from pathlib import Path

ACTIVE_TESTS = __ACTIVE_TESTS_JSON__
SAMPLE_TESTS = __SAMPLE_TESTS_JSON__
ALLOW_T_CASE_DUPLICATION = __ALLOW_T_CASE_DUPLICATION__
RAW_STATEMENT = __RAW_STATEMENT_JSON__


def _record(name, passed, details=None, error=None, skipped=False):
    status = "skipped" if skipped else ("passed" if passed else "failed")
    return {"name": name, "passed": bool(passed) and not skipped, "status": status, "details": details or {}, "error": error}


def _candidate_accepts_input(fn):
    try:
        sig = inspect.signature(fn)
        params = list(sig.parameters.values())
        required = [
            p for p in params
            if p.default is inspect._empty
            and p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)
        ]
        return len(required) <= 1
    except Exception:
        return True


def _load_solve():
    mod = importlib.import_module("generated_solution")

    # Preferred contract: top-level solve(input_text: str) -> str
    fn = getattr(mod, "solve", None)
    if callable(fn) and _candidate_accepts_input(fn):
        return fn

    # Robust fallback: generated code sometimes wraps solve as a method in a class.
    # This does NOT change the required public contract; it only prevents the
    # verifier from rejecting otherwise usable IO solvers too early.
    for name, obj in vars(mod).items():
        if isinstance(obj, type):
            method = getattr(obj, "solve", None)
            if callable(method):
                try:
                    instance = obj()
                    bound = getattr(instance, "solve", None)
                    if callable(bound) and _candidate_accepts_input(bound):
                        return bound
                except Exception:
                    continue

    raise AttributeError("generated_solution.py must define solve(input_text: str) -> str as a top-level function or a zero-arg class instance method")


def _normalize_output(value):
    if value is None:
        value = ""
    if not isinstance(value, str):
        value = str(value)
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in value.split("\n")]
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines)


def _run_solve(input_text):
    solve = _load_solve()
    out = solve(input_text)
    if not isinstance(out, str):
        out = str(out)
    return out


def _sanitize_formal_seed_input(input_text):
    """Runtime-side safety net for formal MR seeds.

    Host-side extraction already sanitizes sample input.  This duplicate guard
    prevents stale case files or older builders from feeding PDF footer text into
    formal MRs and accidentally turning footer noise into a repair target.
    """
    if input_text is None:
        return "", ["empty_input"], False
    text = str(input_text).replace("\r\n", "\n").replace("\r", "\n")
    notes = []
    footer_patterns = [
        r"^\s*Universidad\s+de\s+Valladolid\s+OJ\s*:",
        r"^\s*Universidad\s+de\s+Valladolid",
        r"^\s*UVa\s+Online\s+Judge",
        r"^\s*UVA\s+Online\s+Judge",
        r"^\s*Online\s+Judge\s*$",
        r"^\s*\d+\s*/\s*\d+\s*$",
        r"^\s*.*\bOJ\s*:\s*\d+.*\b\d+\s*/\s*\d+\s*$",
    ]
    keep = []
    for line in text.split("\n"):
        if any(re.search(pat, line, flags=re.I) for pat in footer_patterns):
            notes.append("runtime_pdf_footer_removed")
            break
        keep.append(line)
    clean = "\n".join(keep).strip("\n")
    suspicious = any(re.search(pat, clean, flags=re.I | re.M) for pat in footer_patterns)
    if suspicious:
        notes.append("runtime_suspicious_footer_still_present")
    ok = bool(clean.strip()) and not suspicious
    return clean + ("\n" if clean and not clean.endswith("\n") else ""), sorted(set(notes)), ok


def _formal_seed_samples():
    """Return sample inputs that are safe enough to seed formal IO MRs.

    Basic probes may still use extracted samples as warnings, but formal MRs
    should not be executed on low-confidence sample input because that can create
    false repair pressure.  This is the oracle-boundary rule for original IO
    problems: sample expected outputs are optional sanity-probe oracles; cleaned
    sample inputs may seed a generic source/follow-up relation, whose formal
    evidence still comes only from the relation between runtime executions.
    """
    usable = []
    skipped = []
    for idx, sample in enumerate(SAMPLE_TESTS, start=1):
        if sample.get("seed_confidence") == "low":
            skipped.append({"sample_index": idx, "reason": "low_seed_confidence", "notes": sample.get("seed_sanitizer_notes", [])})
            continue
        clean, notes, ok = _sanitize_formal_seed_input(sample.get("input", ""))
        if not ok:
            skipped.append({"sample_index": idx, "reason": "runtime_seed_sanitization_failed", "notes": notes})
            continue
        item = dict(sample)
        item["input"] = clean
        item["formal_seed_notes"] = sorted(set((item.get("seed_sanitizer_notes") or []) + notes))
        item["_sample_index"] = idx
        usable.append(item)
    return usable, skipped


# -----------------------------------------------------------------------------
# Basic IO Probe: uses sample expected output only as a sanity check.
# It is intentionally NOT a metamorphic relation and is NOT counted in MR summary.
# -----------------------------------------------------------------------------

def basic_probe_io_sample_consistency():
    if not SAMPLE_TESTS:
        return _record("BASIC_IO_SAMPLE_CONSISTENCY", False, {"skipped": "no sample input/output found in statement"}, skipped=True)
    failures = []
    details = []
    for idx, sample in enumerate(SAMPLE_TESTS, start=1):
        inp = sample.get("input", "")
        exp = sample.get("expected_output", "")
        try:
            obs = _run_solve(inp)
            ok = _normalize_output(obs) == _normalize_output(exp)
            details.append({"sample_index": idx, "passed": ok, "observed": _normalize_output(obs), "expected": _normalize_output(exp)})
            if not ok:
                failures.append(idx)
        except Exception:
            details.append({"sample_index": idx, "passed": False, "error": traceback.format_exc()})
            failures.append(idx)
    return _record("BASIC_IO_SAMPLE_CONSISTENCY", not failures, {"samples": details, "failed_samples": failures, "policy": "basic_probe_not_formal_mr"})


# -----------------------------------------------------------------------------
# Formal IO MRs: these do not compare against ground-truth expected output.
# They only compare source/follow-up executions of the generated solve().
# -----------------------------------------------------------------------------

def test_io_determinism():
    samples, skipped_seeds = _formal_seed_samples()
    if not samples:
        return _record("MR_IO_DETERMINISM", False, {"skipped": "no reliable formal seed input available", "skipped_seeds": skipped_seeds}, skipped=True)
    failures = []
    details = []
    for sample in samples:
        idx = sample.get("_sample_index")
        inp = sample.get("input", "")
        try:
            out1 = _run_solve(inp)
            out2 = _run_solve(inp)
            ok = _normalize_output(out1) == _normalize_output(out2)
            details.append({"sample_index": idx, "passed": ok, "first": _normalize_output(out1), "second": _normalize_output(out2)})
            if not ok:
                failures.append(idx)
        except Exception:
            details.append({"sample_index": idx, "passed": False, "error": traceback.format_exc()})
            failures.append(idx)
    return _record("MR_IO_DETERMINISM", not failures, {"relation": "same input should produce same output", "oracle_type": "metamorphic_self_relation", "cases": details})


def test_io_crlf_normalization():
    samples, skipped_seeds = _formal_seed_samples()
    if not samples:
        return _record("MR_IO_CRLF_NORMALIZATION", False, {"skipped": "no reliable formal seed input available", "skipped_seeds": skipped_seeds}, skipped=True)
    failures = []
    details = []
    for sample in samples:
        idx = sample.get("_sample_index")
        inp = sample.get("input", "")
        if "\n" not in inp:
            continue
        try:
            out_lf = _run_solve(inp.replace("\r\n", "\n"))
            out_crlf = _run_solve(inp.replace("\r\n", "\n").replace("\n", "\r\n"))
            ok = _normalize_output(out_lf) == _normalize_output(out_crlf)
            details.append({"sample_index": idx, "passed": ok, "lf": _normalize_output(out_lf), "crlf": _normalize_output(out_crlf)})
            if not ok:
                failures.append(idx)
        except Exception:
            details.append({"sample_index": idx, "passed": False, "error": traceback.format_exc()})
            failures.append(idx)
    if not details:
        return _record("MR_IO_CRLF_NORMALIZATION", False, {"skipped": "reliable formal seed has no line breaks", "skipped_seeds": skipped_seeds}, skipped=True)
    return _record("MR_IO_CRLF_NORMALIZATION", not failures, {"relation": "LF and CRLF line endings should be semantically equivalent", "oracle_type": "metamorphic_self_relation", "cases": details})


def _statement_lower():
    return str(RAW_STATEMENT or "").lower()


def _t_case_output_has_numbering_risk():
    """
    Some problems print case/dataset numbers or require special case separators.
    Duplicating T changes numbering/separators, so source_output + source_output
    is not a safe generic oracle.  Be conservative and skip.
    """
    raw = _statement_lower()
    risk_patterns = [
        r"\bcase\s*#",
        r"\bcase\s+\d+\s*:",
        r"case\s+number",
        r"cases?\s+are\s+numbered",
        r"numbered\s+(?:sequentially|starting)",
        r"\bpair\s*#",
        r"\bscenario\s*#",
        r"\bproblem\s*#",
        r"\bdataset\s*#",
        r"\bdata\s+set\s*#",
        r"\bset\s*#",
        r"\btrial\s*#",
        r"\btest\s+case\s*#",
        r"print.*?(?:case|pair|dataset|data\s+set|scenario|problem|set).*?(?:number|#)",
        r"output.*?(?:case|pair|dataset|data\s+set|scenario|problem|set).*?(?:number|#)",
        r"blank\s+line\s+between\s+(?:datasets|data\s+sets|test\s+cases|cases|outputs?)",
        r"blank\s+line\s+after\s+(?:each|every)\s+(?:dataset|data\s+set|test\s+case|case)",
        r"separat(?:e|ed|es|ing).*?(?:datasets|data\s+sets|test\s+cases|cases|outputs?).*?blank\s+line",
        r"(?:datasets|data\s+sets|test\s+cases|cases|outputs?).*?separat(?:e|ed|es|ing).*?blank\s+line",
        r"exactly\s+one\s+blank\s+line\s+between",
    ]
    return any(re.search(p, raw, re.I | re.S) for p in risk_patterns)


def _t_case_output_separator():
    """
    T-case duplication does not use ground-truth output. It expects follow-up
    output to be the source output repeated, but some problems explicitly
    require a blank line between datasets/test cases. In that case the
    separator between the repeated source-output blocks must also be blank-line.
    """
    raw = _statement_lower()
    blank_patterns = [
        r"blank\s+line\s+between\s+(?:datasets|data\s+sets|test\s+cases|cases|outputs?)",
        r"blank\s+line\s+after\s+(?:each|every)\s+(?:dataset|data\s+set|test\s+case|case)",
        r"separat(?:e|ed|es|ing).*?(?:datasets|data\s+sets|test\s+cases|cases|outputs?).*?blank\s+line",
        r"(?:datasets|data\s+sets|test\s+cases|cases|outputs?).*?separat(?:e|ed|es|ing).*?blank\s+line",
        r"Print\s+a\s+blank\s+line\s+between\s+datasets",
    ]
    if any(re.search(p, raw, re.I | re.S) for p in blank_patterns):
        return "\n\n"
    return "\n"


def _try_build_t_duplication(inp):
    if not ALLOW_T_CASE_DUPLICATION:
        return None
    lines = inp.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if not lines or not lines[0].strip().isdigit():
        return None
    t = int(lines[0].strip())
    if t <= 0:
        return None
    body = "\n".join(lines[1:]).strip("\n")
    if not body:
        return None
    new_input = str(t * 2) + "\n" + body + "\n" + body + "\n"
    return new_input


def test_io_t_case_duplication():
    samples, skipped_seeds = _formal_seed_samples()
    if not samples:
        return _record("MR_IO_T_CASE_DUPLICATION", False, {"skipped": "no reliable formal seed input available", "skipped_seeds": skipped_seeds}, skipped=True)
    if _t_case_output_has_numbering_risk():
        return _record(
            "MR_IO_T_CASE_DUPLICATION",
            False,
            {"skipped": "statement suggests case/dataset numbering in output; simple duplication relation is unsafe"},
            skipped=True,
        )
    attempted = []
    failures = []
    sep = _t_case_output_separator()
    for sample in samples:
        idx = sample.get("_sample_index")
        inp = sample.get("input", "")
        follow_input = _try_build_t_duplication(inp)
        if not follow_input:
            continue
        try:
            source_observed = _run_solve(inp)
            follow_observed = _run_solve(follow_input)
            source_norm = _normalize_output(source_observed)
            follow_norm = _normalize_output(follow_observed)
            expected_relation_output = source_norm + sep + source_norm
            ok = follow_norm == expected_relation_output
            attempted.append({
                "sample_index": idx,
                "passed": ok,
                "source_observed": source_norm,
                "follow_observed": follow_norm,
                "expected_relation_output": expected_relation_output,
                "separator_policy": "blank_line_between_datasets" if sep == "\n\n" else "single_newline_default",
            })
            if not ok:
                failures.append(idx)
        except Exception:
            attempted.append({"sample_index": idx, "passed": False, "error": traceback.format_exc()})
            failures.append(idx)
    if not attempted:
        return _record("MR_IO_T_CASE_DUPLICATION", False, {"skipped": "sample does not match low-risk T-case duplication precondition"}, skipped=True)
    return _record(
        "MR_IO_T_CASE_DUPLICATION",
        not failures,
        {
            "relation": "duplicating all T cases and doubling T should duplicate the program's source output with the statement-consistent separator",
            "oracle_type": "metamorphic_self_relation",
            "separator_policy": "blank_line_between_datasets" if sep == "\n\n" else "single_newline_default",
            "cases": attempted,
        },
    )




def _last_int_from_output(output):
    import re
    nums = re.findall(r"-?\d+", _normalize_output(output))
    return int(nums[-1]) if nums else None


def test_io_semantic_interval_order_invariance():
    """Semantic MR for interval/range problems such as 3n+1.

    Source and follow-up do not use ground-truth answers.  The relation checks
    whether reversing interval endpoints preserves the computed range aggregate.
    """
    pairs = [(1, 10), (100, 200), (201, 210)]
    failures = []
    details = []
    for a, b in pairs:
        src = f"{a} {b}\n"
        follow = f"{b} {a}\n"
        try:
            out_src = _run_solve(src)
            out_follow = _run_solve(follow)
            src_value = _last_int_from_output(out_src)
            follow_value = _last_int_from_output(out_follow)
            ok = src_value is not None and src_value == follow_value
            details.append({
                "source_input": src,
                "follow_input": follow,
                "source_output": _normalize_output(out_src),
                "follow_output": _normalize_output(out_follow),
                "source_relation_value": src_value,
                "follow_relation_value": follow_value,
                "passed": ok,
            })
            if not ok:
                failures.append((a, b))
        except Exception:
            details.append({"source_input": src, "follow_input": follow, "passed": False, "error": traceback.format_exc()})
            failures.append((a, b))
    return _record(
        "MR_IO_SEMANTIC_INTERVAL_ORDER_INVARIANCE",
        not failures,
        {"relation": "range aggregate is invariant to endpoint order", "oracle_type": "semantic_metamorphic_relation", "cases": details},
    )


def test_io_semantic_matrix_zero_padding():
    """Semantic MR for maximum sub-rectangle / maximum sum matrix tasks."""
    src = "2\n1 2\n3 4\n"
    follow = "3\n0 0 0\n0 1 2\n0 3 4\n"
    try:
        out_src = _run_solve(src)
        out_follow = _run_solve(follow)
        v_src = _last_int_from_output(out_src)
        v_follow = _last_int_from_output(out_follow)
        ok = v_src is not None and v_src == v_follow
        return _record(
            "MR_IO_SEMANTIC_MATRIX_ZERO_PADDING",
            ok,
            {
                "relation": "adding an all-zero outer border should not change maximum sub-rectangle sum",
                "oracle_type": "semantic_metamorphic_relation",
                "source_input": src,
                "follow_input": follow,
                "source_output": _normalize_output(out_src),
                "follow_output": _normalize_output(out_follow),
                "source_relation_value": v_src,
                "follow_relation_value": v_follow,
            },
        )
    except Exception:
        return _record("MR_IO_SEMANTIC_MATRIX_ZERO_PADDING", False, {"relation": "zero border padding"}, error=traceback.format_exc())


def test_io_semantic_skyline_dominated_building():
    """Semantic MR for skyline tasks: dominated building should not alter skyline."""
    src = "1 11 5\n"
    follow = "1 11 5\n2 3 4\n"
    try:
        out_src = _run_solve(src)
        out_follow = _run_solve(follow)
        ok = _normalize_output(out_src) == _normalize_output(out_follow)
        return _record(
            "MR_IO_SEMANTIC_SKYLINE_DOMINATED_BUILDING",
            ok,
            {
                "relation": "a lower building fully inside an existing building should not change skyline output",
                "oracle_type": "semantic_metamorphic_relation",
                "source_input": src,
                "follow_input": follow,
                "source_output": _normalize_output(out_src),
                "follow_output": _normalize_output(out_follow),
            },
        )
    except Exception:
        return _record("MR_IO_SEMANTIC_SKYLINE_DOMINATED_BUILDING", False, {"relation": "dominated skyline building"}, error=traceback.format_exc())


TESTS = {
    "test_io_determinism": test_io_determinism,
    "test_io_crlf_normalization": test_io_crlf_normalization,
    "test_io_t_case_duplication": test_io_t_case_duplication,
    "test_io_semantic_interval_order_invariance": test_io_semantic_interval_order_invariance,
    "test_io_semantic_matrix_zero_padding": test_io_semantic_matrix_zero_padding,
    "test_io_semantic_skyline_dominated_building": test_io_semantic_skyline_dominated_building,
}


def main():
    results = []
    basic_results = [basic_probe_io_sample_consistency()]

    for name in ACTIVE_TESTS:
        fn = TESTS.get(name)
        if fn is None:
            results.append(_record(name, False, error="unknown test name"))
            continue
        try:
            results.append(fn())
        except Exception:
            results.append(_record(name, False, error=traceback.format_exc()))

    runtime = {
        "total": len(results),
        "executed": sum(1 for r in results if r.get("status") != "skipped"),
        "passed": sum(1 for r in results if r.get("status") == "passed"),
        "failed": sum(1 for r in results if r.get("status") == "failed"),
        "skipped": sum(1 for r in results if r.get("status") == "skipped"),
    }
    basic_summary = {
        "total": len(basic_results),
        "executed": sum(1 for r in basic_results if r.get("status") != "skipped"),
        "passed": sum(1 for r in basic_results if r.get("status") == "passed"),
        "failed": sum(1 for r in basic_results if r.get("status") == "failed"),
        "skipped": sum(1 for r in basic_results if r.get("status") == "skipped"),
    }
    report = {
        "runtime_results": results,
        "runtime_summary": runtime,
        "basic_io_probe_results": basic_results,
        "basic_io_probe_summary": basic_summary,
        "oracle_policy": {
            "basic_io_probe": "sample expected outputs are sanity-probe oracles only; cleaned sample inputs may seed generic MRs, but expected outputs are not counted as formal MR evidence",
            "formal_mr": "formal IO MRs compare source/follow-up outputs without ground-truth expected output",
            "basic_probe_failure_policy": "basic probe failure is reported as sanity warning and does not define formal MR verdict",
            "semantic_mr_policy": "semantic IO MRs are enabled only by statement-level evidence and registry preconditions",
        },
    }
    Path("test_runtime_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    # Basic IO Probe is a sanity warning, not the formal metamorphic verdict.
    # The process fails only when formal MR fails or the runner itself crashes.
    if runtime["failed"] > 0:
        raise SystemExit(1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''


def generate_test_runner(inputs: VerificationInputs, profile: Dict[str, Any], validated_plan: Dict[str, Any]) -> str:
    active_tests = _accepted_test_names(validated_plan)
    if profile.get("task_family") == "original_io_problem":
        sample_tests = get_sample_tests(inputs)
        allow_t_duplication = statement_allows_t_case_duplication(inputs)
        return (
            IO_TEST_RUNNER_TEMPLATE
            .replace("__ACTIVE_TESTS_JSON__", repr(active_tests))
            .replace("__SAMPLE_TESTS_JSON__", repr(sample_tests))
            .replace("__ALLOW_T_CASE_DUPLICATION__", repr(bool(allow_t_duplication)))
            .replace("__RAW_STATEMENT_JSON__", repr(_strip_appended_interface_block(_clean_control_chars(find_raw_requirement_in_frozen(inputs.frozen_spec)))))
        )

    class_name = profile.get("class_name") or get_task_name(inputs.frozen_spec)
    safe_profile = {"task_family": profile.get("task_family"), "class_name": class_name, "method_roles": profile.get("method_roles", {}), "state_model": profile.get("state_model", {})}
    return (
        TEST_RUNNER_TEMPLATE
        .replace("__CLASS_NAME__", str(class_name))
        .replace("__PROFILE_JSON__", repr(safe_profile))
        .replace("__ACTIVE_TESTS_JSON__", repr(active_tests))
    )


def run_tests_in_docker(output_dir: Path, image: str = DEFAULT_DOCKER_IMAGE, timeout_seconds: int = DEFAULT_DOCKER_TIMEOUT_SECONDS) -> Dict[str, Any]:
    volume = f"{str(output_dir.resolve())}:/app:rw"
    cmd = ["docker", "run", "--rm", "--network", "none", "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true", "--memory", "512m", "--cpus", "1", "--pids-limit", "128", "-v", volume, "-w", "/app", image, "python", "generated_mr_tests.py"]
    started_at = now_iso()
    try:
        completed = subprocess.run(cmd, cwd=str(PROJECT_ROOT), capture_output=True, text=True, timeout=timeout_seconds, encoding="utf-8", errors="replace")
        return {"started_at": started_at, "finished_at": now_iso(), "executor": "docker_run_ephemeral", "docker_image": image, "command": cmd, "returncode": completed.returncode, "stdout": completed.stdout, "stderr": completed.stderr, "timeout": False}
    except subprocess.TimeoutExpired as e:
        return {"started_at": started_at, "finished_at": now_iso(), "executor": "docker_run_ephemeral", "docker_image": image, "command": cmd, "returncode": None, "stdout": e.stdout or "", "stderr": e.stderr or "", "timeout": True, "error": f"timeout after {timeout_seconds} seconds"}
    except FileNotFoundError as e:
        return {"started_at": started_at, "finished_at": now_iso(), "executor": "docker_run_ephemeral", "docker_image": image, "command": cmd, "returncode": None, "stdout": "", "stderr": "", "timeout": False, "error": f"docker executable not found: {e}"}


def read_runtime_report(output_dir: Path) -> Optional[Dict[str, Any]]:
    path = output_dir / "test_runtime_report.json"
    if path.exists():
        try:
            return read_json(path)
        except Exception:
            return None
    return None


def build_mr_execution_report(inputs: VerificationInputs, profile: Dict[str, Any], validated_plan: Dict[str, Any], docker_result: Dict[str, Any], runtime_report: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if profile.get("support_status") != "supported":
        status, summary, results = "profile_validation_failed", {"total": 0, "executed": 0, "passed": 0, "failed": 0, "skipped": 0}, []
    elif runtime_report is None:
        status, summary, results = "execution_error", {"total": 0, "executed": 0, "passed": 0, "failed": 0, "skipped": 0}, []
    else:
        # v8.5.6: generated_mr_tests.py writes runtime_summary/runtime_results.
        # Older reports may use summary/results. Support both schemas so Stage 5
        # does not silently treat executed IO MRs as "no executable MR".
        summary = (
            runtime_report.get("runtime_summary")
            or runtime_report.get("summary")
            or {}
        )
        results = (
            runtime_report.get("runtime_results")
            or runtime_report.get("results")
            or []
        )

        failed = int(summary.get("failed", 0) or 0)
        executed = int(summary.get("executed", 0) or 0)
        runtime_status = str(runtime_report.get("status") or "")

        basic_probe_summary = runtime_report.get("basic_io_probe_summary", {}) or {}
        basic_probe_failed = int(basic_probe_summary.get("failed", 0) or 0)

        if failed:
            status = "mr_failed_defect_candidate"
        elif runtime_status in {"insufficient_metamorphic_oracle", "insufficient_oracle"} or executed == 0:
            status = "insufficient_metamorphic_oracle"
        else:
            status = "mr_passed_no_runtime_defect_detected"
    return {"source_run_id": inputs.run_id, "generation_id": inputs.gen_id, "created_at": now_iso(), "status": status, "execution_scope": "verification_profile_plus_rule_validated_mr_v8_6_2_safe5_seed_sanitation", "task_family": profile.get("task_family"), "profile_support_status": profile.get("support_status"), "profile_validation_errors": profile.get("validation", {}).get("errors", []), "accepted_mr_count": validated_plan.get("accepted_count", 0), "rejected_mr_count": validated_plan.get("rejected_count", 0), "runtime_summary": summary, "runtime_results": results, "basic_io_probe_summary": (runtime_report or {}).get("basic_io_probe_summary", {}), "basic_io_probe_results": (runtime_report or {}).get("basic_io_probe_results", []), "basic_io_probe_warning_count": int(((runtime_report or {}).get("basic_io_probe_summary", {}) or {}).get("failed", 0) or 0), "oracle_policy": (runtime_report or {}).get("oracle_policy", {}), "docker_execution": docker_result, "failure_type_policy": {"profile_validation_failed": "framework/profile issue, do not repair generated code", "execution_error": "framework/runtime issue", "basic_io_probe_warning": "sample/basic IO probe failed; sanity evidence only, not counted as MR defect and not repair-eligible by itself", "insufficient_metamorphic_oracle": "no executable MR; do not mark pass and do not repair generated code", "mr_failed_defect_candidate": "formal MR violation; possible generated code defect, eligible for repair"}, "interpretation_rule": "Sample expected outputs are basic-probe evidence only. Cleaned sample inputs may seed generic source/follow-up MRs; formal delivery evidence comes from the rule-validated runtime relation in Docker. LLM is not final judge."}


def build_mr_coverage_report(inputs: VerificationInputs, profile: Dict[str, Any], validated_plan: Dict[str, Any], execution_report: Dict[str, Any]) -> Dict[str, Any]:
    alignment_results = inputs.behavior_alignment_report.get("alignment_results", []) or []
    accepted_mrs = validated_plan.get("accepted_mrs", []) or []
    runtime_results = execution_report.get("runtime_results", []) or []
    failed_mr = sum(1 for r in runtime_results if r.get("status") == "failed" or (not r.get("passed") and r.get("status") != "skipped"))
    passed_mr = sum(1 for r in runtime_results if r.get("status") == "passed" or (r.get("passed") and r.get("status") != "skipped"))
    skipped_mr = sum(1 for r in runtime_results if r.get("status") == "skipped")
    executed_mr = failed_mr + passed_mr
    if profile.get("support_status") != "supported": coverage_status = "profile_validation_failed_no_mr_executed"
    elif failed_mr: coverage_status = "covered_mr_failed_defect_candidate"
    elif executed_mr == 0: coverage_status = "insufficient_metamorphic_oracle"
    else: coverage_status = "covered_scope_no_failed_mr_detected"
    expected_unit_coverage = [{"expected_unit_id": item.get("expected_unit_id"), "behavior_id": item.get("behavior_id"), "expected_operation_type": item.get("expected_operation_type"), "capability_hint": item.get("capability_hint"), "stage4_decision": item.get("decision"), "coverage_status": "targeted_by_family_mr_scope" if accepted_mrs else "not_targeted"} for item in alignment_results]
    return {"source_run_id": inputs.run_id, "generation_id": inputs.gen_id, "created_at": now_iso(), "coverage_method": "verification_profile_mr_scope_report_v8_6_2_safe5", "coverage_status": coverage_status, "task_family": profile.get("task_family"), "summary": {"expected_unit_count": len(alignment_results), "expected_units_covered_by_mr": len(alignment_results) if accepted_mrs and profile.get("support_status") == "supported" else 0, "expected_units_uncovered_by_mr": 0 if accepted_mrs and profile.get("support_status") == "supported" else len(alignment_results), "accepted_mr_count": len(accepted_mrs), "executed_mr_count": executed_mr, "passed_mr_count": passed_mr, "failed_mr_count": failed_mr, "skipped_mr_count": skipped_mr, "operation_types_covered": sorted({str(x.get("expected_operation_type")) for x in alignment_results if x.get("expected_operation_type")}), "risk_targets_covered": sorted({str(x.get("risk_target")) for x in accepted_mrs if x.get("risk_target")})}, "expected_unit_coverage": expected_unit_coverage, "mr_execution_coverage": [{"name": r.get("name"), "runtime_status": r.get("status") or ("passed" if r.get("passed") else "failed"), "details": r.get("details", {}), "error": r.get("error")} for r in runtime_results], "residual_risks": [{"risk_id": "RR-001", "risk": "MR templates are finite and do not prove full semantic equivalence."}, {"risk_id": "RR-002", "risk": "LLM profile generation may be incomplete or over-abstracted."}, {"risk_id": "RR-003", "risk": "For Original IO tasks, sample expected outputs are Basic IO Probe evidence only; cleaned sample inputs may seed generic MRs, while formal MR strength depends on executable source/follow-up relations."}], "interpretation_rule": "Coverage means current task-family MR scope was executed. Basic IO probes are reported separately and are not counted as MR evidence."}


def detect_stage1_fallback_used(frozen_spec: Dict[str, Any]) -> bool:
    fallback = frozen_spec.get("stage1_fallback_freeze", {})
    if isinstance(fallback, dict) and fallback.get("enabled") is True:
        return True
    spec_status = str(frozen_spec.get("spec_status", ""))
    if spec_status and spec_status != "frozen_pre_agent_acceptance_spec":
        return "fallback" in spec_status.lower() or "review" in spec_status.lower()
    return False


def build_verification_summary(inputs: VerificationInputs, profile: Dict[str, Any], stage4_ready: bool, stage4_reasons: List[str], execution_report: Dict[str, Any], coverage_report: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    status = "blocked_by_stage4_alignment" if not stage4_ready else execution_report.get("status")
    decision = "verification_passed_for_current_scope" if status == "mr_passed_no_runtime_defect_detected" else "not_ready_for_delivery"
    coverage_summary = (coverage_report or {}).get("summary", {}) or {}
    stage1_fallback_used = detect_stage1_fallback_used(inputs.frozen_spec)
    experiment_case_tag = "stage1_fallback_case" if stage1_fallback_used else "normal_frozen_spec_case"

    return {
        "source_run_id": inputs.run_id,
        "generation_id": inputs.gen_id,
        "created_at": now_iso(),
        "verification_status": status,
        "failure_type": status,
        "stage4_ready": stage4_ready,
        "stage4_blocking_reasons": stage4_reasons,
        "stage1_fallback_used": stage1_fallback_used,
        "experiment_case_tag": experiment_case_tag,
        "task_family": profile.get("task_family"),
        "profile_support_status": profile.get("support_status"),
        "profile_generation_mode": profile.get("generation_mode"),
        "profile_llm_used": bool(profile.get("llm_used")),
        "profile_validation_errors": profile.get("validation", {}).get("errors", []),
        "final_decision_for_delivery": decision,
        "coverage_status": (coverage_report or {}).get("coverage_status"),
        "coverage_summary": coverage_summary,

        # v8.3: duplicate key MR metrics at top level for CSV/statistics scripts.
        "expected_unit_count": coverage_summary.get("expected_unit_count"),
        "expected_units_covered_by_mr": coverage_summary.get("expected_units_covered_by_mr"),
        "expected_units_uncovered_by_mr": coverage_summary.get("expected_units_uncovered_by_mr"),
        "accepted_mr_count": coverage_summary.get("accepted_mr_count"),
        "executed_mr_count": coverage_summary.get("executed_mr_count"),
        "passed_mr_count": coverage_summary.get("passed_mr_count"),
        "failed_mr_count": coverage_summary.get("failed_mr_count"),
        "skipped_mr_count": coverage_summary.get("skipped_mr_count"),

        "rejected_mr_count": execution_report.get("rejected_mr_count"),
        "runtime_summary": execution_report.get("runtime_summary", {}),
        "basic_io_probe_summary": execution_report.get("basic_io_probe_summary", {}),
        "basic_io_probe_warning_count": execution_report.get("basic_io_probe_warning_count", 0),
        "oracle_policy": execution_report.get("oracle_policy", {}),
        "residual_risk_count": len((coverage_report or {}).get("residual_risks", [])),
        "residual_risk_ids": [r.get("risk_id") for r in (coverage_report or {}).get("residual_risks", [])],
        "repair_eligibility": status == "mr_failed_defect_candidate",
        "scope_note": "This decision covers the current verification profile and MR scope only.",
        "statistics_note": "Top-level MR counts are copied from coverage_summary for downstream experiment aggregation.",
        "interpretation_rule": "LLM is used for candidate abstraction/profile generation only; final evidence is from rule validation and Docker execution."
    }


def build_human_review(inputs: VerificationInputs, profile: Dict[str, Any], basic_probe_plan: Dict[str, Any], draft_plan: Dict[str, Any], validated_plan: Dict[str, Any], execution_report: Dict[str, Any], summary: Dict[str, Any], coverage_report: Optional[Dict[str, Any]] = None) -> str:
    lines: List[str] = []
    lines.append("# Stage 5 Verification Human Review")
    lines.append("")
    lines.append(f"- RUN-ID: `{inputs.run_id}`")
    lines.append(f"- GEN-ID: `{inputs.gen_id}`")
    lines.append(f"- Created at: `{now_iso()}`")
    lines.append("")
    lines.append("## 1. Verification Profile")
    lines.append(f"- Task family: `{profile.get('task_family')}`")
    lines.append(f"- Support status: `{profile.get('support_status')}`")
    lines.append(f"- LLM used: `{profile.get('llm_used')}`")
    lines.append(f"- Generation mode: `{profile.get('generation_mode')}`")
    lines.append(f"- Class name: `{profile.get('class_name')}`")
    lines.append(f"- Method roles: `{json.dumps(profile.get('method_roles'), ensure_ascii=False)}`")
    errors = profile.get("validation", {}).get("errors", [])
    if errors:
        lines.append(f"- Profile validation errors: `{errors}`")
    lines.append("")
    lines.append("## 2. MR Candidate Policy")
    lines.append("- LLM may generate task abstraction and MR candidates.")
    lines.append("- Rule validator checks method existence and supported task family.")
    lines.append("- Docker execution result is the final evidence; LLM is not final judge.")
    lines.append("")
    lines.append("## 3. Execution Result")
    lines.append(f"- Execution status: `{execution_report.get('status')}`")
    lines.append(f"- Runtime summary (formal MR only): `{json.dumps(execution_report.get('runtime_summary'), ensure_ascii=False)}`")
    if execution_report.get("basic_io_probe_summary"):
        lines.append(f"- Basic IO probe summary (not MR evidence): `{json.dumps(execution_report.get('basic_io_probe_summary'), ensure_ascii=False)}`")
        if execution_report.get("basic_io_probe_warning_count", 0):
            lines.append("- Basic probe warning: sample sanity check failed, but this is not counted as formal MR evidence under the oracle-problem policy.")
    failed = [r for r in execution_report.get("runtime_results", []) if not r.get("passed")]
    if failed:
        lines.append("")
        lines.append("### Failed MR / probe results")
        for r in failed:
            lines.append(f"- `{r.get('name')}`")
            lines.append(f"  - details: `{json.dumps(r.get('details'), ensure_ascii=False)}`")
            if r.get("error"):
                lines.append(f"  - error: `{r.get('error')}`")
    lines.append("")
    lines.append("## 4. Final Summary")
    lines.append(f"- Verification status: `{summary.get('verification_status')}`")
    lines.append(f"- Repair eligibility: `{summary.get('repair_eligibility')}`")
    lines.append(f"- Final decision: `{summary.get('final_decision_for_delivery')}`")
    lines.append("")
    return "\n".join(lines)


def run_verification_framework(run_id: str, gen_id: str, model: str = "gemma4:31b", use_llm: bool = True, docker_image: str = DEFAULT_DOCKER_IMAGE) -> Dict[str, Path]:
    inputs = load_verification_inputs(run_id, gen_id)
    output_dir = resolve_under(VERIFICATION_ROOT, inputs.run_id, inputs.gen_id)
    output_dir.mkdir(parents=True, exist_ok=True)

    stage4_ready, stage4_reasons = check_stage4_ready(inputs.behavior_alignment_report)
    write_json(output_dir / "verification_input_snapshot.json", {"source_run_id": inputs.run_id, "generation_id": inputs.gen_id, "created_at": now_iso(), "input_files": {"frozen_acceptance_spec": str(inputs.frozen_spec_path), "generated_solution": str(inputs.generated_solution_path), "implementation_manifest": str(inputs.implementation_manifest_path), "behavior_alignment_report": str(inputs.behavior_alignment_report_path), "generated_behavior_spec": str(inputs.generated_behavior_spec_path), "expected_behavior_units": str(inputs.expected_behavior_units_path)}, "stage4_ready": stage4_ready, "stage4_reasons": stage4_reasons})

    profile = build_verification_profile(inputs, model=model, use_llm=use_llm)
    write_json(output_dir / "verification_profile.json", profile)

    basic_probe_plan = build_basic_probe_plan(inputs, profile)
    write_json(output_dir / "basic_probe_plan.json", basic_probe_plan)

    draft_plan = build_mr_plan(inputs, profile)
    write_json(output_dir / "mr_plan_draft.json", draft_plan)

    validated_plan = validate_mr_plan(inputs, profile, draft_plan)
    write_json(output_dir / "mr_plan_validated.json", validated_plan)

    shutil.copy2(inputs.generated_solution_path, output_dir / "generated_solution.py")
    write_text(output_dir / "generated_mr_tests.py", generate_test_runner(inputs, profile, validated_plan))

    if not stage4_ready:
        docker_result = {"skipped": True, "reason": "Stage 4 alignment has mismatch/blocking reason; MR execution blocked."}
        runtime_report = None
    elif profile.get("support_status") != "supported":
        docker_result = {"skipped": True, "reason": "verification profile is not supported; do not execute MR and do not repair code.", "profile_validation_errors": profile.get("validation", {}).get("errors", [])}
        runtime_report = None
    elif not validated_plan.get("accepted_mrs"):
        docker_result = {"skipped": True, "reason": "No accepted MR candidates to execute."}
        runtime_report = None
    else:
        docker_result = run_tests_in_docker(output_dir, image=docker_image)
        runtime_report = read_runtime_report(output_dir)

    write_json(output_dir / "docker_execution_result.json", docker_result)
    execution_report = build_mr_execution_report(inputs, profile, validated_plan, docker_result, runtime_report)
    write_json(output_dir / "mr_execution_report.json", execution_report)
    coverage_report = build_mr_coverage_report(inputs, profile, validated_plan, execution_report)
    write_json(output_dir / "mr_coverage_report.json", coverage_report)
    summary = build_verification_summary(inputs, profile, stage4_ready, stage4_reasons, execution_report, coverage_report)
    write_json(output_dir / "verification_summary.json", summary)
    human_review = build_human_review(inputs, profile, basic_probe_plan, draft_plan, validated_plan, execution_report, summary, coverage_report)
    write_text(output_dir / "mr_human_review.md", human_review)

    return {"output_dir": output_dir, "verification_input_snapshot": output_dir / "verification_input_snapshot.json", "verification_profile": output_dir / "verification_profile.json", "basic_probe_plan": output_dir / "basic_probe_plan.json", "mr_plan_draft": output_dir / "mr_plan_draft.json", "mr_plan_validated": output_dir / "mr_plan_validated.json", "generated_mr_tests": output_dir / "generated_mr_tests.py", "docker_execution_result": output_dir / "docker_execution_result.json", "mr_execution_report": output_dir / "mr_execution_report.json", "mr_coverage_report": output_dir / "mr_coverage_report.json", "verification_summary": output_dir / "verification_summary.json", "mr_human_review": output_dir / "mr_human_review.md"}
