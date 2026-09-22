# -*- coding: utf-8 -*-
"""
behavior_extraction.py
======================

Stage 3：程式碼 → AST 結構證據 → 行為中介表示
v5 IO-generalized：支援兩種任務型態：
- class/function 狀態任務：Counter / BankAccount / ShoppingCart / Inventory 類型
- original_io_problem：CPE/UVA/LeetCode 等原始題目，統一以 solve(input_text: str) -> str 作為可驗證介面

本階段只做「可觀察結構證據」抽取，不做最終功能驗證。
"""
from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple


PROJECT_ROOT = Path(__file__).resolve().parent
STAGE1_ROOT = PROJECT_ROOT / "stage1_outputs"
WORKSPACE_ROOT = PROJECT_ROOT / "workspace"


def read_text_default(prompt: str, default: str = "") -> str:
    if default:
        raw = input(f"{prompt}（Enter 使用 {default}）：\n> ").strip()
        return raw or default
    return input(f"{prompt}：\n> ").strip()


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def json_to_text(obj: Any) -> str:
    try:
        return json.dumps(obj, ensure_ascii=False)
    except Exception:
        return str(obj)


def latest_gen_id(run_id: str) -> str:
    base = WORKSPACE_ROOT / "code_generation" / run_id
    gens = sorted([p.name for p in base.glob("GEN-*") if p.is_dir()])
    return gens[-1] if gens else "GEN-000001"


def find_raw_requirement(frozen_spec: Dict[str, Any]) -> str:
    candidates = [
        frozen_spec.get("source_requirement", {}).get("raw_requirement"),
        frozen_spec.get("raw_requirement"),
        frozen_spec.get("user_prompt_original"),
        frozen_spec.get("requirement"),
    ]
    for c in candidates:
        if isinstance(c, str) and c.strip():
            return c.strip()
    return json_to_text(frozen_spec)


def extract_class_name(text: str) -> Optional[str]:
    m = re.search(r"([A-Za-z_][A-Za-z0-9_]*)\s*類別", text)
    if m:
        return m.group(1)
    m = re.search(r"class\s+([A-Za-z_][A-Za-z0-9_]*)", text)
    return m.group(1) if m else None


def extract_public_methods(text: str) -> List[str]:
    methods = []
    m = re.search(r"公開方法[^為]*為\s*([^。；;\n]+)", text)
    target = m.group(1) if m else text
    for name in re.findall(r"([A-Za-z_][A-Za-z0-9_]*)\s*\(", target):
        if name not in methods and name not in {"if", "for", "while", "sum", "max", "min", "print"}:
            methods.append(name)
    return methods


def detect_task_type(text: str, methods: List[str]) -> str:
    blob = (text + " " + " ".join(methods)).lower()
    collection_words = ["shoppingcart", "inventory", "item", "商品", "庫存", "購物車", "price", "quantity", "總金額", "總庫存", "total"]
    numeric_words = ["counter", "bankaccount", "balance", "value", "amount", "目前數值", "餘額", "increment", "decrement", "deposit", "withdraw"]
    if any(w.lower() in blob for w in collection_words):
        return "collection_aggregate"
    if any(w.lower() in blob for w in numeric_words):
        return "numeric_state"
    return "deterministic_class_state"


def add_expected(units: List[Dict[str, Any]], source_id: str, op: str, hint: str, desc: str, details: Optional[Dict[str, Any]] = None) -> None:
    units.append({
        "expected_unit_id": source_id,
        "source_id": source_id,
        "expected_operation_type": op,
        "capability_hint": hint,
        "description": desc,
        "details": details or {},
    })


def detect_original_io_problem(text: str, methods: List[str]) -> bool:
    """判斷是否為原始 Online Judge / LeetCode 題目模式。

    這裡不是改題意，只是判斷 requirement 是否要求使用 solve(input_text: str) -> str
    來包裝 stdin/stdout 題目。CPE/UVA 與 LeetCode 都可以用這個模式進入後續驗證。
    """
    blob = (text + " " + " ".join(methods)).lower()
    signals = [
        "solve(input_text", "solve (input_text", "solve 函式", "solve函式",
        "stdin", "stdout", "standard input", "standard output",
        "input_text", "原始題目", "online judge", "uva", "cpe", "leetcode",
        "標準輸入", "標準輸出", "輸入格式", "輸出格式",
    ]
    return any(s.lower() in blob for s in signals)


def build_expected_units(frozen_spec: Dict[str, Any]) -> Tuple[str, List[Dict[str, Any]]]:
    text = find_raw_requirement(frozen_spec)
    methods = extract_public_methods(text)

    # v5：原始 CPE/UVA/LeetCode 題目優先走 IO 模式，不再硬要求 class/add/remove/query。
    if detect_original_io_problem(text, methods):
        units: List[Dict[str, Any]] = []
        add_expected(
            units,
            "API-001",
            "solve_function",
            "solve",
            "需提供 top-level solve(input_text: str) -> str，將原題標準輸入字串轉為標準輸出字串。",
            {"required_functions": ["solve"], "signature": "solve(input_text: str) -> str"},
        )
        add_expected(
            units,
            "IO-001",
            "io_solver_behavior",
            "io_behavior",
            "solve 必須依原始題意處理輸入格式、輸出格式與多組測資，不得改寫成 unrelated template。",
            {"mode": "original_io_problem"},
        )
        add_expected(
            units,
            "CON-001",
            "local_deterministic_execution",
            "deterministic_local",
            "程式必須 self-contained、deterministic、local run；不得使用外部 API、網路、資料庫、檔案系統或隨機性。",
            {"forbidden": ["external_api", "network", "database", "filesystem", "random"]},
        )
        return "original_io_problem", units

    class_name = extract_class_name(text)
    task_type = detect_task_type(text, methods)

    units: List[Dict[str, Any]] = []

    if class_name or methods:
        add_expected(
            units,
            "OBJ-001",
            "public_api",
            "api",
            f"需實作 {class_name or '指定'} 類別，並提供公開方法：{', '.join(methods) if methods else '(未明確列出)'}。",
            {"class_name": class_name, "required_methods": methods},
        )

    for method in methods:
        ml = method.lower()
        if any(k in ml for k in ["increment", "deposit", "add"]):
            if task_type == "collection_aggregate":
                op, hint = "collection_add", "add"
                desc = f"{method} 成功執行後，應加入或累加集合中的資料。"
            else:
                op, hint = "state_increase", "increase"
                desc = f"{method} 成功執行後，應增加目前數值狀態。"
            add_expected(units, f"BHV-{len(units):03d}", op, hint, desc, {"method_name": method})
        elif any(k in ml for k in ["decrement", "withdraw", "remove"]):
            if task_type == "collection_aggregate":
                op, hint = "collection_remove", "remove"
                desc = f"{method} 成功執行後，應移除或扣減集合中的資料。"
            else:
                op, hint = "state_decrease", "decrease"
                desc = f"{method} 成功執行後，應減少目前數值狀態。"
            add_expected(units, f"BHV-{len(units):03d}", op, hint, desc, {"method_name": method})
        elif any(k in ml for k in ["get_value", "value", "balance"]):
            add_expected(units, f"BHV-{len(units):03d}", "return_state", "query", f"{method} 應回傳目前狀態值。", {"method_name": method})
        elif any(k in ml for k in ["get_total", "total", "sum"]):
            add_expected(units, f"BHV-{len(units):03d}", "aggregate_total", "query", f"{method} 應回傳目前集合的加總結果。", {"method_name": method})

    if any(k in text for k in ["正數", "大於 0", "> 0", "非法數量", "非法操作"]):
        add_expected(
            units,
            f"CON-{len(units):03d}",
            "guard_reject",
            "invalid_guard",
            "非法或非正數輸入不得被接受；必要時可 raise，但不可造成錯誤狀態。",
            {"keywords": ["positive", "invalid"]},
        )

    if any(k in text for k in ["低於 0", "低於0", "超過目前庫存", "不存在", "不足", "不可讓"]):
        add_expected(
            units,
            f"CON-{len(units):03d}",
            "boundary_guard",
            "boundary_guard",
            "邊界違規操作不得被接受，例如低於 0、超過庫存或移除不存在資料。",
            {"keywords": ["non_negative", "not_found", "over_remove"]},
        )

    if any(k in text for k in ["不可改變", "不得改變", "維持不變", "狀態不得改變"]):
        add_expected(
            units,
            f"CON-{len(units):03d}",
            "state_unchanged_on_invalid",
            "invalid_no_state_change",
            "非法操作執行後，系統狀態必須維持不變。",
            {"keywords": ["state_unchanged"]},
        )

    return task_type, units


def unparse(node: ast.AST) -> str:
    try:
        return ast.unparse(node)
    except Exception:
        return type(node).__name__


def is_self_attr(node: ast.AST) -> Optional[str]:
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "self":
        return node.attr
    return None


def contains_raise(stmts: List[ast.stmt]) -> bool:
    for s in stmts:
        if isinstance(s, ast.Raise):
            return True
        for child in ast.iter_child_nodes(s):
            if isinstance(child, ast.Raise):
                return True
    return False


def add_observed(units: List[Dict[str, Any]], op: str, method: str, evidence: str, lineno: int = 0, details: Optional[Dict[str, Any]] = None) -> None:
    units.append({
        "observed_unit_id": f"OBS-{len(units)+1:03d}",
        "method_name": method,
        "capability": op,
        "operation_type": op,
        "evidence": evidence,
        "lineno": lineno,
        "details": details or {},
    })


def method_has_return_state(node: ast.FunctionDef) -> Optional[str]:
    for sub in ast.walk(node):
        if isinstance(sub, ast.Return) and sub.value is not None:
            v = sub.value
            if isinstance(v, ast.Attribute) and is_self_attr(v):
                return unparse(v)
            if isinstance(v, ast.Name):
                return unparse(v)
    return None


def method_has_aggregate_return(node: ast.FunctionDef) -> Optional[str]:
    for sub in ast.walk(node):
        if isinstance(sub, ast.Return) and sub.value is not None:
            txt = unparse(sub.value)
            if "sum(" in txt or "*" in txt or "total" in txt.lower():
                return txt
    return None


def expr_mentions_self_collection(expr: ast.AST) -> bool:
    txt = unparse(expr)
    return "self." in txt and ("[" in txt or ".get(" in txt or ".items(" in txt or ".values(" in txt)


def extract_method_units(method: ast.FunctionDef, units: List[Dict[str, Any]]) -> None:
    method_name = method.name

    for sub in ast.walk(method):
        if isinstance(sub, ast.If) and contains_raise(sub.body):
            condition = unparse(sub.test)
            op = "boundary_guard" if any(k in condition for k in ["< 0", "<= 0", ">", "-", "not in", "in "]) else "guard_reject"
            add_observed(units, op, method_name, f"if {condition}: raise", getattr(sub, "lineno", 0), {"condition": condition})
            if op != "guard_reject":
                add_observed(units, "guard_reject", method_name, f"if {condition}: raise", getattr(sub, "lineno", 0), {"condition": condition})

        if isinstance(sub, ast.AugAssign):
            attr = is_self_attr(sub.target)
            target_txt = unparse(sub.target)
            value_txt = unparse(sub.value)
            if attr:
                if isinstance(sub.op, ast.Add):
                    add_observed(units, "state_increase", method_name, f"{target_txt} += {value_txt}", getattr(sub, "lineno", 0), {"state_attr": attr})
                elif isinstance(sub.op, ast.Sub):
                    add_observed(units, "state_decrease", method_name, f"{target_txt} -= {value_txt}", getattr(sub, "lineno", 0), {"state_attr": attr})
            if "total" in target_txt.lower():
                add_observed(units, "aggregate_total", method_name, f"{target_txt} += {value_txt}", getattr(sub, "lineno", 0))

        if isinstance(sub, ast.Assign):
            targets = sub.targets
            val_txt = unparse(sub.value)

            for t in targets:
                target_txt = unparse(t)
                attr = is_self_attr(t)

                if attr:
                    if isinstance(sub.value, ast.BinOp):
                        left_txt = unparse(sub.value.left)
                        right_txt = unparse(sub.value.right)
                        if "self." in left_txt:
                            if isinstance(sub.value.op, ast.Add):
                                add_observed(units, "state_increase", method_name, f"{target_txt} = {left_txt} + {right_txt}", getattr(sub, "lineno", 0), {"state_attr": attr})
                            elif isinstance(sub.value.op, ast.Sub):
                                add_observed(units, "state_decrease", method_name, f"{target_txt} = {left_txt} - {right_txt}", getattr(sub, "lineno", 0), {"state_attr": attr})
                    if isinstance(sub.value, ast.Call):
                        func_txt = unparse(sub.value.func)
                        if func_txt in {"max", "min"} and "self." in val_txt:
                            if "-" in val_txt:
                                add_observed(units, "state_decrease", method_name, f"{target_txt} = {val_txt}", getattr(sub, "lineno", 0), {"state_attr": attr})
                            add_observed(units, "boundary_guard", method_name, f"{target_txt} = {val_txt}", getattr(sub, "lineno", 0), {"state_attr": attr})

                if isinstance(t, ast.Subscript) and "self." in target_txt:
                    op = "collection_add" if any(k in method_name.lower() for k in ["add", "append", "insert"]) else "collection_update"
                    add_observed(units, op, method_name, f"{target_txt} = {val_txt}", getattr(sub, "lineno", 0))
                elif expr_mentions_self_collection(t) or expr_mentions_self_collection(sub.value):
                    op = "collection_add" if any(k in method_name.lower() for k in ["add", "append", "insert"]) else "collection_update"
                    add_observed(units, op, method_name, f"{target_txt} = {val_txt}", getattr(sub, "lineno", 0))

        if isinstance(sub, ast.Delete):
            for t in sub.targets:
                target_txt = unparse(t)
                if "self." in target_txt:
                    add_observed(units, "collection_remove", method_name, f"del {target_txt}", getattr(sub, "lineno", 0))

        if isinstance(sub, ast.Call):
            call_txt = unparse(sub)
            if any(x in call_txt for x in [".pop(", ".remove(", ".discard("]) and "self." in call_txt:
                add_observed(units, "collection_remove", method_name, call_txt, getattr(sub, "lineno", 0))

    ret_state = method_has_return_state(method)
    if ret_state:
        add_observed(units, "return_state", method_name, f"return {ret_state}", getattr(method, "lineno", 0))

    ret_agg = method_has_aggregate_return(method)
    if ret_agg:
        add_observed(units, "aggregate_total", method_name, f"return {ret_agg}", getattr(method, "lineno", 0))


def extract_top_level_functions(tree: ast.Module) -> List[ast.FunctionDef]:
    return [n for n in tree.body if isinstance(n, ast.FunctionDef) and not n.name.startswith("_")]


def extract_observed_units(source_code: str, expected_units: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    tree = ast.parse(source_code)
    observed: List[Dict[str, Any]] = []
    raw_ast: List[Dict[str, Any]] = []
    unsupported: List[Dict[str, Any]] = []

    expected_public = next((u for u in expected_units if u["expected_operation_type"] in {"public_api", "solve_function"}), None)
    required_methods = []
    required_functions = []
    required_class = None
    if expected_public:
        details = expected_public.get("details", {})
        required_methods = details.get("required_methods", []) or []
        required_functions = details.get("required_functions", []) or []
        required_class = details.get("class_name")

    # v5：支援 top-level solve(input_text: str) -> str。
    for fn in extract_top_level_functions(tree):
        arg_names = [a.arg for a in fn.args.args]
        raw_ast.append({"type": "function", "name": fn.name, "args": arg_names, "lineno": fn.lineno})
        if fn.name == "solve":
            add_observed(
                observed,
                "solve_function",
                "solve",
                f"def solve({', '.join(arg_names)})",
                fn.lineno,
                {"function_name": fn.name, "args": arg_names, "required_functions": required_functions},
            )
            returns = [unparse(n.value) for n in ast.walk(fn) if isinstance(n, ast.Return) and n.value is not None]
            add_observed(
                observed,
                "io_solver_behavior",
                "solve",
                "top-level solve function contains return statements" if returns else "top-level solve function found",
                fn.lineno,
                {"return_expr_samples": returns[:5]},
            )

    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            public_methods = [n.name for n in node.body if isinstance(n, ast.FunctionDef) and not n.name.startswith("_")]
            raw_ast.append({"type": "class", "name": node.name, "public_methods": public_methods, "lineno": node.lineno})
            add_observed(
                observed,
                "public_api",
                "<class>",
                f"class {node.name} public_methods={public_methods}",
                node.lineno,
                {"class_name": node.name, "public_methods": public_methods, "required_class": required_class, "required_methods": required_methods},
            )
            # LeetCode 類題若生成 class Solution，也先留下 algorithm_function 證據。
            if node.name == "Solution" and public_methods:
                add_observed(
                    observed,
                    "algorithm_function",
                    "Solution",
                    f"class Solution public_methods={public_methods}",
                    node.lineno,
                    {"class_name": node.name, "public_methods": public_methods},
                )
            for item in node.body:
                if isinstance(item, ast.FunctionDef):
                    raw_ast.append({"type": "method", "class": node.name, "name": item.name, "lineno": item.lineno})
                    extract_method_units(item, observed)

    seen = set()
    deduped = []
    for u in observed:
        key = (u.get("method_name"), u.get("operation_type"), u.get("evidence"))
        if key in seen:
            continue
        seen.add(key)
        u["observed_unit_id"] = f"OBS-{len(deduped)+1:03d}"
        deduped.append(u)

    return deduped, raw_ast, unsupported


def main() -> int:
    print("=" * 70)
    print("Stage 3：程式碼 → AST 結構證據 → 行為中介表示")
    print("v5：class/function + original IO problem 支援")
    print("=" * 70)

    run_id = read_text_default("請輸入 Stage 1 RUN-ID（例如 RUN-000001）")
    if not run_id:
        print("[ERROR] RUN-ID 不可為空")
        return 1
    gen_id = read_text_default("請輸入 Generation ID", latest_gen_id(run_id))

    frozen_path = STAGE1_ROOT / run_id / "frozen_acceptance_spec.json"
    gen_path = WORKSPACE_ROOT / "code_generation" / run_id / gen_id / "generated_solution.py"
    manifest_path = WORKSPACE_ROOT / "code_generation" / run_id / gen_id / "implementation_manifest.json"
    out_dir = WORKSPACE_ROOT / "behavior_extraction" / run_id / gen_id
    out_dir.mkdir(parents=True, exist_ok=True)

    if not frozen_path.exists():
        print(f"\n[stderr]\n找不到 frozen spec：{frozen_path}")
        return 1
    if not gen_path.exists():
        print(f"\n[stderr]\n找不到 generated_solution.py：{gen_path}")
        return 1

    print("[1/3] 讀取 frozen spec、generated_solution.py、implementation_manifest.json")
    frozen = read_json(frozen_path)
    source = gen_path.read_text(encoding="utf-8")

    print("[2/3] 使用 Python AST 抽取 class / method / guard / raise / return / state update / collection operation")
    task_type, expected_units = build_expected_units(frozen)
    observed_units, raw_ast, unsupported = extract_observed_units(source, expected_units)

    print("[3/3] 輸出 generated_behavior_spec.json 與 expected_behavior_units.json")
    input_snapshot = {
        "source_run_id": run_id,
        "generation_id": gen_id,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "input_files": {
            "frozen_acceptance_spec": str(frozen_path),
            "generated_solution": str(gen_path),
            "implementation_manifest": str(manifest_path),
        },
    }

    generated_behavior_spec = {
        "schema_version": "stage3_generated_behavior_spec_v5_io_generalized",
        "source_run_id": run_id,
        "generation_id": gen_id,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "task_type": task_type,
        "observed_behavior_units": observed_units,
        "unsupported_patterns": unsupported,
    }

    report = {
        "schema_version": "stage3_behavior_extraction_report_v5_io_generalized",
        "source_run_id": run_id,
        "generation_id": gen_id,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "status": "extraction_completed",
        "task_type": task_type,
        "confidence": "high" if observed_units else "low",
        "observed_count": len(observed_units),
        "expected_count": len(expected_units),
        "unsupported_patterns": len(unsupported),
    }

    write_json(out_dir / "behavior_extraction_input.json", input_snapshot)
    write_json(out_dir / "raw_ast_evidence.json", raw_ast)
    write_json(out_dir / "generated_behavior_spec.json", generated_behavior_spec)
    write_json(out_dir / "expected_behavior_units.json", {
        "schema_version": "stage3_expected_behavior_units_v5_io_generalized",
        "source_run_id": run_id,
        "generation_id": gen_id,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "task_type": task_type,
        "expected_behavior_units": expected_units,
    })
    write_json(out_dir / "behavior_extraction_report.json", report)

    print("\n✅ 行為抽取完成")
    print(f"RUN-ID: {run_id}")
    print(f"GEN-ID: {gen_id}")
    print(f"任務型態: {task_type}")
    print(f"抽取信心: {report['confidence']}")
    print(f"觀察行為單位數: {len(observed_units)}")
    print(f"預期行為單位數: {len(expected_units)}")
    print(f"unsupported patterns: {len(unsupported)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
