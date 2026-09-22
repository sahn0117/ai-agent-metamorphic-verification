# -*- coding: utf-8 -*-
"""
skill_planner_intent.py
=======================
第一階段專用工具：驗收規格 JSON 的規則式檢查與凍結。

職責：
1. 檢查模型 A 產出的 intent_spec.json 是否可供後續驗證使用。
2. 檢查模型 B 的獨立語意審查結果格式。
3. 綜合規則檢查、模型 B 審查與向量輔助訊號，決定是否可交給 Agent。
4. 通過後建立 frozen_acceptance_spec.json，固定後續驗收基準。

不負責：
- 不呼叫 LLM。
- 不挑選資料集或判斷研究範圍。
- 不產生程式碼、測試或蛻變關係。
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any


SPEC_VERSION = "1.1"
_OBJECTIVE_ID = re.compile(r"^OBJ-\d{3}$")
_CONSTRAINT_ID = re.compile(r"^CON-\d{3}$")
_BEHAVIOR_ID = re.compile(r"^BHV-\d{3}$")
_INVARIANT_ID = re.compile(r"^INV-\d{3}$")

# 驗收規格中的來源分類：硬性條件只接受明示需求；可觀察行為與不變量可含合理推導。
_EXPLICIT_ONLY = {"explicit"}
_OBSERVABLE_BASIS_TYPES = {"explicit", "logically_derived"}
_ASSUMPTION_SECTION_PREFIX = re.compile(r"^\s*(?:assumptions?|uncertainties?)\s*(?:[.\[(:：#-]|$)", re.IGNORECASE)
_HARD_SECTION_REFERENCE = re.compile(r"(?:core_objectives|constraints|interface_contract|state_model|behavior_spec|invariants)", re.IGNORECASE)
_NON_BINDING_NAME_MARKERS = {"", "unspecified", "未指定", "unknown", "not_specified", "not specified", "n/a", "na"}
_ASSUMPTION_CHOICE_PATTERN = re.compile(r"(?:預設為|固定為|設定為|採用|資料型別為|型別為)")
_UNDEFINED_DISCLOSURE_PATTERN = re.compile(r"(?:未指定|未明示|未定義|不納入硬性驗收|不作為硬性驗收)")


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _required_text(item: dict[str, Any], key: str, label: str, errors: list[str]) -> None:
    if not _text(item.get(key)):
        errors.append(f"{label}.{key} 不可為空")


def stable_sha256(data: Any) -> str:
    """針對 JSON 資料建立可重現雜湊，供後續識別凍結規格版本。"""
    raw = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _is_concrete_unstated_name(name: str, raw_requirement: str) -> bool:
    """只有原文逐字提供的介面／欄位名稱，才可成為固定契約名稱。"""
    normalized_name = _text(name)
    if normalized_name.lower() in _NON_BINDING_NAME_MARKERS:
        return False
    if not _text(raw_requirement):
        return False
    return normalized_name.casefold() not in raw_requirement.casefold()


def validate_intent_spec(intent_spec: dict[str, Any], raw_requirement: str = "") -> list[str]:
    """
    檢查模型 A 產出的可解釋驗收規格。

    此處檢查格式、可追溯性與可被規則攔截的未授權具體化；
    其餘需求語意是否被誤解，仍由模型 B 另行審查。
    """
    errors: list[str] = []
    if not isinstance(intent_spec, dict):
        return ["intent_spec 必須是 JSON object"]

    required_top_level = (
        "task_meta", "core_objectives", "constraints", "interface_contract",
        "state_model", "behavior_spec", "invariants", "assumptions", "uncertainties",
    )
    for key in required_top_level:
        if key not in intent_spec:
            errors.append(f"缺少頂層欄位: {key}")

    task_meta = intent_spec.get("task_meta", {})
    if not isinstance(task_meta, dict):
        errors.append("task_meta 必須是 object")
    else:
        _required_text(task_meta, "task_name", "task_meta", errors)
        if _text(task_meta.get("target_language")).lower() != "python":
            errors.append("task_meta.target_language 目前必須為 python")
        if not isinstance(task_meta.get("expected_files", []), list):
            errors.append("task_meta.expected_files 必須是 array")

    objectives = intent_spec.get("core_objectives", [])
    if not isinstance(objectives, list) or not objectives:
        errors.append("core_objectives 必須是至少一項的 array")
        objectives = []
    objective_ids: set[str] = set()
    for index, item in enumerate(objectives):
        label = f"core_objectives[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{label} 必須是 object")
            continue
        obj_id = _text(item.get("id"))
        if not _OBJECTIVE_ID.fullmatch(obj_id):
            errors.append(f"{label}.id 必須符合 OBJ-001 格式")
        elif obj_id in objective_ids:
            errors.append(f"{label}.id 重複: {obj_id}")
        else:
            objective_ids.add(obj_id)
        if _text(item.get("basis_type")) not in _EXPLICIT_ONLY:
            errors.append(f"{label}.basis_type 必須為 explicit；核心目標不可由未明示假設提升而來")
        for key in ("description", "requirement_basis", "verification_focus"):
            _required_text(item, key, label, errors)

    constraints = intent_spec.get("constraints", [])
    if not isinstance(constraints, list):
        errors.append("constraints 必須是 array；原需求沒有明確限制時使用 []")
        constraints = []
    constraint_ids: set[str] = set()
    for index, item in enumerate(constraints):
        label = f"constraints[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{label} 必須是 object")
            continue
        con_id = _text(item.get("id"))
        if not _CONSTRAINT_ID.fullmatch(con_id):
            errors.append(f"{label}.id 必須符合 CON-001 格式")
        elif con_id in constraint_ids:
            errors.append(f"{label}.id 重複: {con_id}")
        else:
            constraint_ids.add(con_id)
        if _text(item.get("basis_type")) not in _EXPLICIT_ONLY:
            errors.append(f"{label}.basis_type 必須為 explicit；原題目未明示的內容不可成為硬性 constraint")
        for key in ("rule", "requirement_basis", "verification_focus"):
            _required_text(item, key, label, errors)

    interface_contract = intent_spec.get("interface_contract", {})
    if not isinstance(interface_contract, dict):
        errors.append("interface_contract 必須是 object")
    else:
        if _text(interface_contract.get("entity_type")) not in {
            "class", "function", "module", "unspecified"
        }:
            errors.append("interface_contract.entity_type 必須為 class/function/module/unspecified")
        methods = interface_contract.get("functions_or_methods", [])
        if not isinstance(methods, list):
            errors.append("interface_contract.functions_or_methods 必須是 array")
        else:
            for index, method in enumerate(methods):
                label = f"interface_contract.functions_or_methods[{index}]"
                if not isinstance(method, dict):
                    errors.append(f"{label} 必須是 object")
                    continue
                for key in ("name", "purpose", "requirement_basis"):
                    _required_text(method, key, label, errors)
                if not isinstance(method.get("parameters", []), list):
                    errors.append(f"{label}.parameters 必須是 array")
                method_name = _text(method.get("name"))
                if _is_concrete_unstated_name(method_name, raw_requirement):
                    errors.append(
                        f"{label}.name='{method_name}' 未在原始需求中明確指定；"
                        "不可將自行命名的方法寫入固定介面契約，請改為 functions_or_methods=[] 並在 uncertainties 揭露。"
                    )

    state_model = intent_spec.get("state_model", {})
    if not isinstance(state_model, dict):
        errors.append("state_model 必須是 object")
    elif not isinstance(state_model.get("state_fields", []), list):
        errors.append("state_model.state_fields 必須是 array")
    else:
        for index, state_field in enumerate(state_model.get("state_fields", [])):
            field_name = _text(state_field.get("name") if isinstance(state_field, dict) else state_field)
            if _is_concrete_unstated_name(field_name, raw_requirement):
                errors.append(
                    f"state_model.state_fields[{index}]='{field_name}' 未在原始需求中明確指定；"
                    "不可自行固定程式狀態欄位名稱。"
                )

    behaviors = intent_spec.get("behavior_spec", [])
    if not isinstance(behaviors, list) or not behaviors:
        errors.append("behavior_spec 必須是至少一項的 array")
        behaviors = []
    behavior_ids: set[str] = set()
    for index, item in enumerate(behaviors):
        label = f"behavior_spec[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{label} 必須是 object")
            continue
        bhv_id = _text(item.get("id"))
        if not _BEHAVIOR_ID.fullmatch(bhv_id):
            errors.append(f"{label}.id 必須符合 BHV-001 格式")
        elif bhv_id in behavior_ids:
            errors.append(f"{label}.id 重複: {bhv_id}")
        else:
            behavior_ids.add(bhv_id)
        if _text(item.get("basis_type")) not in _OBSERVABLE_BASIS_TYPES:
            errors.append(f"{label}.basis_type 必須為 explicit 或 logically_derived")
        for key in ("description", "observable_behavior", "requirement_basis", "verification_focus"):
            _required_text(item, key, label, errors)
        related_ids = _list(item.get("related_objective_ids", []))
        if not related_ids:
            errors.append(f"{label}.related_objective_ids 不可為空")
        for obj_id in related_ids:
            if _text(obj_id) not in objective_ids:
                errors.append(f"{label} 引用不存在的核心目標: {_text(obj_id)}")

    invariants = intent_spec.get("invariants", [])
    if not isinstance(invariants, list):
        errors.append("invariants 必須是 array；沒有明確不變條件時使用 []")
        invariants = []
    invariant_ids: set[str] = set()
    for index, item in enumerate(invariants):
        label = f"invariants[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{label} 必須是 object")
            continue
        inv_id = _text(item.get("id"))
        if not _INVARIANT_ID.fullmatch(inv_id):
            errors.append(f"{label}.id 必須符合 INV-001 格式")
        elif inv_id in invariant_ids:
            errors.append(f"{label}.id 重複: {inv_id}")
        else:
            invariant_ids.add(inv_id)
        if _text(item.get("basis_type")) not in _OBSERVABLE_BASIS_TYPES:
            errors.append(f"{label}.basis_type 必須為 explicit 或 logically_derived")
        for key in ("description", "requirement_basis", "verification_focus"):
            _required_text(item, key, label, errors)

    for key in ("assumptions", "uncertainties"):
        if not isinstance(intent_spec.get(key, []), list):
            errors.append(f"{key} 必須是 array")

    assumptions = intent_spec.get("assumptions", [])
    if isinstance(assumptions, list):
        for index, item in enumerate(assumptions):
            assumption_text = _text(item)
            if (_ASSUMPTION_CHOICE_PATTERN.search(assumption_text)
                    and not _UNDEFINED_DISCLOSURE_PATTERN.search(assumption_text)):
                errors.append(
                    f"assumptions[{index}] 不可替原始需求選定預設方案或實作選項：'{assumption_text}'；"
                    "請改寫為 uncertainties 中的未指定事項。"
                )

    return errors


def validate_semantic_review(review: dict[str, Any]) -> list[str]:
    """檢查模型 B 的審查 JSON 是否具備可供判決使用的格式。"""
    errors: list[str] = []
    if not isinstance(review, dict):
        return ["semantic_review 必須是 JSON object"]
    if review.get("verdict") not in {"pass", "review"}:
        errors.append("semantic_review.verdict 必須為 pass 或 review")
    for key in (
        "missing_requirements", "distorted_requirements",
        "unsupported_hard_requirements", "traceability_issues",
    ):
        if not isinstance(review.get(key, []), list):
            errors.append(f"semantic_review.{key} 必須是 array")
    if not _text(review.get("review_reason")):
        errors.append("semantic_review.review_reason 不可為空")
    try:
        confidence = float(review.get("confidence"))
        if not 0.0 <= confidence <= 1.0:
            errors.append("semantic_review.confidence 必須介於 0 到 1")
    except (TypeError, ValueError):
        errors.append("semantic_review.confidence 必須是數字")
    return errors



def reconcile_semantic_review(review: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """
    對模型 B 已完成的語意審查做規則式校正。

    assumptions / uncertainties 在本框架中是「透明揭露」，不是驗收門檻。
    因此模型 B 若只因這兩區存在未明示資訊就列為 unsupported_hard_requirements，
    該項會被降為 non_blocking_assumption_observations；若同時指向硬性驗收區塊，
    則仍保留為阻擋項目，交由人工或後續修正规格處理。
    """
    normalized = dict(review)
    raw_unsupported = [_text(item) for item in _list(review.get("unsupported_hard_requirements", [])) if _text(item)]
    blocking_unsupported: list[str] = []
    non_blocking_observations: list[str] = []

    for issue in raw_unsupported:
        is_assumption_only = bool(_ASSUMPTION_SECTION_PREFIX.search(issue)) and not bool(_HARD_SECTION_REFERENCE.search(issue))
        if is_assumption_only:
            non_blocking_observations.append(issue)
        else:
            blocking_unsupported.append(issue)

    normalized["raw_verdict"] = review.get("verdict")
    normalized["raw_unsupported_hard_requirements"] = raw_unsupported
    normalized["unsupported_hard_requirements"] = blocking_unsupported
    normalized["non_blocking_assumption_observations"] = non_blocking_observations

    blocking_count = sum(
        len(_list(normalized.get(key, [])))
        for key in (
            "missing_requirements", "distorted_requirements",
            "unsupported_hard_requirements", "traceability_issues",
        )
    )
    normalized["verdict"] = "review" if blocking_count else "pass"

    applied = bool(non_blocking_observations) or normalized["verdict"] != review.get("verdict")
    reconciliation = {
        "applied": applied,
        "policy": "assumptions_and_uncertainties_are_non_binding_unless_promoted_to_acceptance_items",
        "downgraded_to_non_blocking_observations": non_blocking_observations,
        "remaining_blocking_unsupported_items": blocking_unsupported,
        "raw_verdict": review.get("verdict"),
        "reconciled_verdict": normalized["verdict"],
    }
    if applied:
        normalized["review_reason"] = (
            _text(review.get("review_reason"))
            + " [規則式校正：僅位於 assumptions/uncertainties 的揭露內容不作為阻擋條件；"
            "若該內容日後被提升為硬性驗收項目，仍須重新審查。]"
        ).strip()
    return normalized, reconciliation


def decide_pre_agent_validation(
    intent_spec_errors: list[str],
    semantic_review_errors: list[str],
    semantic_review: dict[str, Any],
    embedding_similarity: float | None,
    embedding_warning_threshold: float,
) -> dict[str, Any]:
    """整合前置驗收結果；向量相似度只產生警示，不單獨否決規格。"""
    if intent_spec_errors:
        decision = "review_intent_spec_structure"
        ready = False
    elif semantic_review_errors:
        decision = "review_invalid_semantic_review"
        ready = False
    else:
        reported_issues = sum(
            len(_list(semantic_review.get(key, [])))
            for key in (
                "missing_requirements", "distorted_requirements",
                "unsupported_hard_requirements", "traceability_issues",
            )
        )
        if semantic_review.get("verdict") != "pass" or reported_issues > 0:
            decision = "review_semantic_mismatch"
            ready = False
        elif embedding_similarity is None:
            decision = "pass_with_embedding_unavailable"
            ready = True
        elif embedding_similarity < embedding_warning_threshold:
            decision = "pass_with_embedding_warning"
            ready = True
        else:
            decision = "pass"
            ready = True

    return {
        "decision": decision,
        "ready_for_agent": ready,
        "intent_spec_structure_passed": not intent_spec_errors,
        "semantic_review_passed": (
            not semantic_review_errors
            and semantic_review.get("verdict") == "pass"
            and ready
        ),
        "embedding_role": "auxiliary_warning_only",
        "embedding_warning_threshold": embedding_warning_threshold,
    }


def build_frozen_acceptance_spec(
    normalized_requirement: dict[str, Any],
    intent_spec: dict[str, Any],
    validation_summary: dict[str, Any],
    model_configuration: dict[str, Any],
) -> dict[str, Any]:
    """建立後續 Agent 與行為比對模組使用的固定驗收規格。"""
    raw_requirement = _text(normalized_requirement.get("raw_requirement"))
    acceptance_spec = {
        key: intent_spec.get(key)
        for key in (
            "task_meta", "core_objectives", "constraints", "interface_contract",
            "state_model", "behavior_spec", "invariants", "assumptions", "uncertainties",
        )
    }
    return {
        "spec_version": SPEC_VERSION,
        "frozen_at": datetime.now().isoformat(timespec="seconds"),
        "spec_status": "frozen_pre_agent_acceptance_spec",
        "run_id": normalized_requirement.get("run_id", ""),
        "task_name": normalized_requirement.get("task_name", ""),
        "source_requirement": {
            "raw_requirement": raw_requirement,
            "requirement_sha256": hashlib.sha256(raw_requirement.encode("utf-8")).hexdigest(),
        },
        "acceptance_spec": acceptance_spec,
        "acceptance_spec_sha256": stable_sha256(acceptance_spec),
        "pre_agent_validation": validation_summary,
        "model_configuration": model_configuration,
        "usage_rule": (
            "此規格於 Agent 產生程式碼前固定；後續行為比對與測試不得依 Agent 產出反向修改本規格。"
        ),
    }


def freeze_acceptance_spec(
    output_path: str | Path,
    frozen_spec: dict[str, Any],
    allow_overwrite: bool = False,
) -> dict[str, Any]:
    """寫入凍結規格。既有檔案預設不可覆寫。"""
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not allow_overwrite:
        return {
            "created": False,
            "status": "already_frozen_kept",
            "path": str(path),
            "message": "凍結規格已存在，未覆寫。",
        }
    path.write_text(json.dumps(frozen_spec, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "created": True,
        "status": "frozen_created",
        "path": str(path),
        "message": "frozen_acceptance_spec.json 已建立。",
    }
