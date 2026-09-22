# -*- coding: utf-8 -*-
"""
main.py
=======

完整流程入口：只輸入自然語言需求，系統自動跑完整驗證與修復閉環。

定位：
- 這支是「全流程入口」
- 不取代 Stage 1 / Stage 2 / main_pipeline.py
- 只負責呼叫既有腳本、抓 RUN-ID / GEN-ID、再交給 main_pipeline.py

目前流程：
1. Stage 1：intent_structuring.py
   NLP 需求 → RUN-ID / frozen_acceptance_spec.json

2. Stage 2：code_generation_agent.py
   RUN-ID → GEN-000001 / generated_solution.py / implementation_manifest.json

3. Stage 3～7：main_pipeline.py
   GEN-000001 → 行為抽取 → 行為對齊 → MR 驗證
   若失敗 → repair_feedback.py → repair_generation.py → 新 GEN → 重驗

建議用法：

互動輸入需求：
    python main.py

直接帶需求：
    python main.py --requirement "設計一個 ShoppingCart 類別，支援加入商品、移除商品、查詢總金額。非法操作不可改變狀態。"

最多自動修 3 輪：
    python main.py --mode repair-loop --max-repairs 3 --requirement "..."

只跑到驗證，不自動修：
    python main.py --mode verify --requirement "..."

注意：
- 預設 mode=repair-once
- 若你剛整理過資料夾，請先確認 main_pipeline.py、intent_structuring.py、code_generation_agent.py 都在專案根目錄。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# ============================================================
# Windows UTF-8 防爆設定
# ============================================================

def configure_console_encoding() -> None:
    if sys.platform != "win32":
        return

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def build_child_env() -> Dict[str, str]:
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    return env


def safe_print(text: Any = "") -> None:
    try:
        print(text)
    except UnicodeEncodeError:
        print(str(text).encode("utf-8", errors="replace").decode("utf-8", errors="replace"))


# ============================================================
# 基本工具
# ============================================================

def parse_numbered_id(name: str, prefix: str) -> Optional[int]:
    if not name.startswith(prefix):
        return None
    tail = name[len(prefix):]
    if not tail.isdigit():
        return None
    return int(tail)


def latest_numbered_dir(parent: Path, prefix: str) -> Optional[str]:
    if not parent.exists():
        return None

    candidates: List[Tuple[int, str]] = []

    for p in parent.iterdir():
        if not p.is_dir():
            continue
        n = parse_numbered_id(p.name, prefix)
        if n is not None:
            candidates.append((n, p.name))

    if not candidates:
        return None

    candidates.sort()
    return candidates[-1][1]


def list_numbered_dirs(parent: Path, prefix: str) -> List[str]:
    if not parent.exists():
        return []

    candidates: List[Tuple[int, str]] = []
    for p in parent.iterdir():
        if not p.is_dir():
            continue
        n = parse_numbered_id(p.name, prefix)
        if n is not None:
            candidates.append((n, p.name))

    candidates.sort()
    return [name for _, name in candidates]


def newest_created_id(before: List[str], after: List[str]) -> Optional[str]:
    before_set = set(before)
    created = [x for x in after if x not in before_set]
    if created:
        return created[-1]
    return None


def read_json_if_exists(path: Path) -> Optional[Dict[str, Any]]:
    if not path.exists():
        return None

    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def write_json(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def read_text_file(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def extract_last_id_from_output(output: str, prefix: str) -> Optional[str]:
    """
    從 stdout/stderr 嘗試抓 RUN-000xxx 或 GEN-000xxx。
    """
    pattern = re.compile(rf"{re.escape(prefix)}\d{{6}}")
    matches = pattern.findall(output or "")
    if not matches:
        return None
    return matches[-1]


def ensure_script_exists(project_root: Path, script_name: str) -> None:
    path = project_root / script_name
    if not path.exists():
        raise FileNotFoundError(f"找不到必要腳本：{path}")


# ============================================================
# 路徑
# ============================================================

def get_stage1_root(project_root: Path) -> Path:
    return project_root / "stage1_outputs"


def get_codegen_root(project_root: Path) -> Path:
    return project_root / "workspace" / "code_generation"


def get_full_run_dir(project_root: Path) -> Path:
    return project_root / "workspace" / "full_runs" / now_stamp()


def get_latest_main_pipeline_result(project_root: Path, run_id: str) -> Optional[Path]:
    root = project_root / "workspace" / "main_runs" / run_id
    if not root.exists():
        return None

    candidates: List[Path] = []
    for p in root.rglob("main_pipeline_result.json"):
        if p.is_file():
            candidates.append(p)

    if not candidates:
        return None

    candidates.sort(key=lambda p: p.stat().st_mtime)
    return candidates[-1]



# ============================================================
# RUN 輸出快照整理：workspace/run/<RUN-ID>
# ============================================================

def copy_path_if_exists(src: Path, dst: Path) -> Optional[Path]:
    """Copy a runtime artifact into the organized RUN snapshot without removing the live source."""
    if not src.exists():
        return None

    final_dst = dst
    idx = 1
    while final_dst.exists():
        final_dst = Path(f"{dst}_{idx:02d}")
        idx += 1

    final_dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        shutil.copytree(src, final_dst)
    else:
        shutil.copy2(src, final_dst)
    return final_dst


def copy_file_if_exists(src: Path, dst: Path) -> bool:
    if not src.exists() or not src.is_file():
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return True


def remove_empty_dir(path: Path) -> None:
    if not path.exists() or not path.is_dir():
        return
    try:
        if not any(path.iterdir()):
            path.rmdir()
    except Exception:
        pass


def organize_run_output_to_workspace_run(
    *,
    project_root: Path,
    run_id: str,
    full_run_dir: Path,
    final_gen_id: Optional[str],
) -> Optional[Path]:
    if not run_id:
        return None

    workspace_root = project_root / "workspace"
    target_root = workspace_root / "run" / run_id

    if target_root.exists():
        backup = workspace_root / "run" / f"{run_id}_old_{now_stamp()}"
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(target_root), str(backup))

    target_root.mkdir(parents=True, exist_ok=True)

    manifest: Dict[str, Any] = {
        "created_at": datetime.now().isoformat(),
        "run_id": run_id,
        "final_gen_id": final_gen_id,
        "target_root": str(target_root),
        "copied": [],
        "missing": [],
        "notes": [
            "Stage scripts keep their normal runtime paths so main_pipeline.py can rerun an existing RUN/GEN.",
            "main.py also copies a consolidated snapshot into workspace/run/<RUN-ID> after the full pipeline finishes.",
            "stage1_outputs/run_history.json is intentionally kept for RUN numbering.",
        ],
    }

    copy_plan = [
        (project_root / "stage1_outputs" / run_id, target_root / "01_stage1", "Stage 1 frozen spec and intent outputs"),
        (workspace_root / "code_generation" / run_id, target_root / "02_code_generation", "Stage 2 code generation outputs"),
        (workspace_root / "behavior_extraction" / run_id, target_root / "03_behavior_extraction", "Stage 3 behavior extraction outputs"),
        (workspace_root / "behavior_alignment" / run_id, target_root / "04_behavior_alignment", "Stage 4 behavior alignment outputs"),
        (workspace_root / "verification" / run_id, target_root / "05_verification", "Stage 5 verification outputs"),
        (workspace_root / "repair_feedback" / run_id, target_root / "06_repair_feedback", "Stage 6 repair feedback outputs"),
        (workspace_root / "main_runs" / run_id, target_root / "07_main_runs", "main pipeline logs and result"),
        (full_run_dir, target_root / "08_full_runs" / full_run_dir.name, "main.py full run folder"),
    ]

    for src, dst, desc in copy_plan:
        copied_to = copy_path_if_exists(src, dst)
        record = {
            "src": str(src),
            "dst": str(dst if copied_to is None else copied_to),
            "description": desc,
        }
        if copied_to is None:
            manifest["missing"].append(record)
        else:
            manifest["copied"].append(record)

    if final_gen_id:
        final_dir = target_root / "99_final"
        final_sources = [
            (target_root / "02_code_generation" / final_gen_id / "generated_solution.py", final_dir / "generated_solution.py"),
            (target_root / "02_code_generation" / final_gen_id / "implementation_manifest.json", final_dir / "implementation_manifest.json"),
            (target_root / "05_verification" / final_gen_id / "verification_summary.json", final_dir / "verification_summary.json"),
            (target_root / "05_verification" / final_gen_id / "mr_execution_report.json", final_dir / "mr_execution_report.json"),
            (target_root / "05_verification" / final_gen_id / "mr_coverage_report.json", final_dir / "mr_coverage_report.json"),
            (target_root / "05_verification" / final_gen_id / "mr_human_review.md", final_dir / "mr_human_review.md"),
            (target_root / "06_repair_feedback" / final_gen_id / "repair_feedback.json", final_dir / "repair_feedback.json"),
            (target_root / "06_repair_feedback" / final_gen_id / "repair_feedback.md", final_dir / "repair_feedback.md"),
        ]

        copied = []
        for src, dst in final_sources:
            if copy_file_if_exists(src, dst):
                copied.append({"src": str(src), "dst": str(dst)})

        manifest["final_artifacts"] = copied

    write_json(target_root / "run_manifest.json", manifest)

    for p in [
        workspace_root / "behavior_alignment",
        workspace_root / "behavior_extraction",
        workspace_root / "code_generation",
        workspace_root / "full_runs",
        workspace_root / "main_runs",
        workspace_root / "repair_feedback",
        workspace_root / "verification",
    ]:
        remove_empty_dir(p)

    return target_root


# ============================================================
# 子程序執行
# ============================================================

def run_script(
    *,
    project_root: Path,
    script_name: str,
    input_text: Optional[str],
    args: Optional[List[str]],
    child_env: Dict[str, str],
    log_dir: Path,
    title: str,
) -> Tuple[int, str, Path]:
    ensure_script_exists(project_root, script_name)

    cmd = [sys.executable, str(project_root / script_name)]
    if args:
        cmd.extend(args)

    safe_print()
    safe_print("=" * 72)
    safe_print(title)
    safe_print("=" * 72)
    safe_print(" ".join(["python", script_name] + (args or [])))

    completed = subprocess.run(
        cmd,
        input=input_text,
        cwd=str(project_root),
        env=child_env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    output_parts: List[str] = []
    if completed.stdout:
        output_parts.append(completed.stdout)
    if completed.stderr:
        output_parts.append("\n[stderr]\n" + completed.stderr)

    output = "\n".join(output_parts)

    if output.strip():
        safe_print(output)

    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{script_name.replace('.py', '')}_{datetime.now().strftime('%H%M%S')}.log"
    log_path.write_text(output, encoding="utf-8")

    if completed.returncode != 0:
        safe_print(f"[WARN] {script_name} returncode={completed.returncode}")
        safe_print(f"[LOG] {log_path}")

    return completed.returncode, output, log_path


# ============================================================
# Stage 1：NLP → RUN-ID
# ============================================================

def run_stage1_intent_structuring(
    *,
    project_root: Path,
    requirement: str,
    child_env: Dict[str, str],
    log_dir: Path,
    extra_blank_lines: int = 3,
) -> Tuple[Optional[str], Dict[str, Any]]:
    stage1_root = get_stage1_root(project_root)
    before_runs = list_numbered_dirs(stage1_root, "RUN-")

    # v8.6.2-safe input hardening:
    # 不再用固定 END sentinel 傳原題，避免 CPE/LeetCode 題目本身含 END/STOP 時被截斷。
    # 這裡只改「傳輸方式」，不改題意、不改 Stage 1 判斷邏輯。
    stage1_requirement_path = log_dir / "stage1_requirement_input.txt"
    stage1_requirement_path.parent.mkdir(parents=True, exist_ok=True)
    clean_requirement = requirement.replace("\x00", "")
    stage1_requirement_path.write_text(clean_requirement, encoding="utf-8", errors="replace")

    returncode, output, log_path = run_script(
        project_root=project_root,
        script_name="intent_structuring.py",
        input_text=None,
        args=["--requirement-file", str(stage1_requirement_path)],
        child_env=child_env,
        log_dir=log_dir,
        title="Stage 1：自然語言需求 → 意圖結構化 / frozen spec",
    )

    after_runs = list_numbered_dirs(stage1_root, "RUN-")

    run_id_from_output = extract_last_id_from_output(output, "RUN-")
    run_id_from_dir = newest_created_id(before_runs, after_runs)

    # Stage 1 若失敗，原則上不可用舊 RUN-ID 充數。
    # 但 original_io_problem 的 LLM JSON 失敗很常見；若本次確實建立了新 RUN 目錄，
    # 後面會用完整原題建立 fallback frozen spec，不會使用空規格。
    candidate_run_id = run_id_from_output or run_id_from_dir
    if returncode != 0 and _stage1_scope_safe_fallback_is_original_io_problem(clean_requirement):
        run_id = candidate_run_id
    else:
        run_id = None if returncode != 0 else candidate_run_id

    result = {
        "script": "intent_structuring.py",
        "returncode": returncode,
        "log_path": str(log_path),
        "run_id_from_output": run_id_from_output,
        "run_id_from_dir": run_id_from_dir,
        "selected_run_id": run_id,
        "stage1_requirement_path": str(stage1_requirement_path),
    }

    if returncode != 0 and not run_id:
        safe_print("[ERROR] Stage 1 執行失敗，且無法安全建立 original_io fallback；不選用任何 RUN-ID。")
        return None, result
    elif returncode != 0 and run_id:
        safe_print("[WARN] Stage 1 執行失敗，但偵測到本次 original_io_problem RUN-ID，將交由 fallback frozen spec 處理。")

    if not run_id:
        safe_print("[ERROR] 無法判定 Stage 1 產生的 RUN-ID。")
        return None, result

    frozen_spec = stage1_root / run_id / "frozen_acceptance_spec.json"
    if not frozen_spec.exists():
        safe_print(f"[WARN] 找不到 frozen_acceptance_spec.json：{frozen_spec}")
    else:
        safe_print(f"[INFO] Stage 1 RUN-ID: {run_id}")
        safe_print(f"[INFO] frozen spec: {frozen_spec}")

    return run_id, result


# ============================================================
# Stage 2：RUN-ID → GEN-ID
# ============================================================

def run_stage2_code_generation(
    *,
    project_root: Path,
    run_id: str,
    generation_model: str,
    child_env: Dict[str, str],
    log_dir: Path,
    extra_blank_lines: int = 3,
) -> Tuple[Optional[str], Dict[str, Any]]:
    codegen_run_dir = get_codegen_root(project_root) / run_id
    before_gens = list_numbered_dirs(codegen_run_dir, "GEN-")

    # 常見互動：
    # 1. 輸入 RUN-ID
    # 2. 輸入模型或 Enter 使用預設
    # 後面多送幾個 Enter 避免卡住。
    model_line = generation_model.strip()
    input_text = f"{run_id}\n{model_line}\n" + ("\n" * max(0, extra_blank_lines))

    returncode, output, log_path = run_script(
        project_root=project_root,
        script_name="code_generation_agent.py",
        input_text=input_text,
        args=None,
        child_env=child_env,
        log_dir=log_dir,
        title="Stage 2：根據 frozen spec 產生程式碼",
    )

    after_gens = list_numbered_dirs(codegen_run_dir, "GEN-")

    gen_id_from_output = extract_last_id_from_output(output, "GEN-")
    gen_id_from_dir = newest_created_id(before_gens, after_gens)

    gen_id = gen_id_from_output or gen_id_from_dir

    result = {
        "script": "code_generation_agent.py",
        "returncode": returncode,
        "log_path": str(log_path),
        "gen_id_from_output": gen_id_from_output,
        "gen_id_from_dir": gen_id_from_dir,
        "selected_gen_id": gen_id,
    }

    if not gen_id:
        safe_print("[ERROR] 無法判定 Stage 2 產生的 GEN-ID。")
        return None, result

    generated_solution = codegen_run_dir / gen_id / "generated_solution.py"
    implementation_manifest = codegen_run_dir / gen_id / "implementation_manifest.json"

    safe_print(f"[INFO] Stage 2 GEN-ID: {gen_id}")
    safe_print(f"[INFO] generated_solution: {generated_solution}")
    safe_print(f"[INFO] implementation_manifest: {implementation_manifest}")

    if not generated_solution.exists():
        safe_print(f"[WARN] 找不到 generated_solution.py：{generated_solution}")

    if not implementation_manifest.exists():
        safe_print(f"[WARN] 找不到 implementation_manifest.json：{implementation_manifest}")

    return gen_id, result


# ============================================================
# Stage 3～7：交給 main_pipeline.py
# ============================================================

def run_main_pipeline(
    *,
    project_root: Path,
    run_id: str,
    gen_id: str,
    mode: str,
    max_repairs: int,
    mr_model: str,
    repair_model: str,
    child_env: Dict[str, str],
    log_dir: Path,
) -> Tuple[int, Dict[str, Any]]:
    args = [
        "--run-id", run_id,
        "--gen-id", gen_id,
        "--mode", mode,
    ]

    if mode == "repair-loop":
        args.extend(["--max-repairs", str(max_repairs)])

    if mr_model:
        args.extend(["--mr-model", mr_model])

    if repair_model:
        args.extend(["--repair-model", repair_model])

    returncode, output, log_path = run_script(
        project_root=project_root,
        script_name="main_pipeline.py",
        input_text=None,
        args=args,
        child_env=child_env,
        log_dir=log_dir,
        title="Stage 3～7：驗證與修復閉環",
    )

    result_path = get_latest_main_pipeline_result(project_root, run_id)
    pipeline_result = read_json_if_exists(result_path) if result_path else None

    result = {
        "script": "main_pipeline.py",
        "returncode": returncode,
        "log_path": str(log_path),
        "main_pipeline_result_path": str(result_path) if result_path else None,
        "main_pipeline_result": pipeline_result,
    }

    return returncode, result


# ============================================================
# 主程式
# ============================================================


# =============================================================================
# Stage 1 scope-safe fallback freeze v4
# Added by fix_stage1_scope_safe_false_reject_v1_no_backup.py
# =============================================================================

def _stage1_scope_safe_fallback_read_json(path):
    import json
    from pathlib import Path
    p = Path(path)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _stage1_scope_safe_fallback_text_from(obj):
    if obj is None:
        return ""
    if isinstance(obj, str):
        return obj
    if isinstance(obj, dict):
        preferred_keys = [
            "raw_requirement", "requirement", "normalized_requirement",
            "source_requirement", "user_requirement", "task_description",
            "description", "text",
        ]
        for key in preferred_keys:
            value = obj.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
            if isinstance(value, dict):
                nested = _stage1_scope_safe_fallback_text_from(value)
                if nested:
                    return nested
        for value in obj.values():
            nested = _stage1_scope_safe_fallback_text_from(value)
            if nested:
                return nested
    if isinstance(obj, list):
        for value in obj:
            nested = _stage1_scope_safe_fallback_text_from(value)
            if nested:
                return nested
    return ""


def _stage1_scope_safe_fallback_task_name(text):
    import re
    patterns = [
        r"設計一個\s*([A-Za-z_][A-Za-z0-9_]*)\s*類別",
        r"設計\s*([A-Za-z_][A-Za-z0-9_]*)\s*類別",
        r"class\s+([A-Za-z_][A-Za-z0-9_]*)",
    ]
    for pat in patterns:
        m = re.search(pat, text)
        if m:
            return m.group(1)
    return "ScopeSafeClassTask"




def _stage1_scope_safe_fallback_read_requirement_file(stage1_result):
    """v8.6.2-safe original IO：Stage 1 失敗/false reject 時，從 main.py 暫存 requirement file 還原完整原題。"""
    from pathlib import Path
    if not isinstance(stage1_result, dict):
        return ""
    path = stage1_result.get("stage1_requirement_path")
    if not path:
        return ""
    try:
        p = Path(path)
        if p.exists():
            return p.read_text(encoding="utf-8", errors="replace").replace("\x00", "").strip()
    except Exception:
        return ""
    return ""


def _stage1_scope_safe_fallback_title_from_original_io(text):
    import re
    for pat in [
        r"題名[:：]\s*([^\n\r]+)",
        r"Title[:：]\s*([^\n\r]+)",
    ]:
        m = re.search(pat, text or "", flags=re.IGNORECASE)
        if m:
            return m.group(1).strip()
    m = re.search(r"^\s*(\d{3,5})\s+([^\n\r]{3,120})", text or "", flags=re.MULTILINE)
    if m:
        return m.group(2).strip()
    return "Original IO Problem"


def _stage1_scope_safe_fallback_is_original_io_problem(text):
    """
    只辨識「原始題目 + solve(input_text)->str 包裝」。
    這不是題意轉換，只是讓原始 stdin/stdout 題目可進入本地驗證介面。
    """
    if not isinstance(text, str) or not text.strip():
        return False
    low = text.lower()
    has_original_source = (
        "cpe/uva 原始程式題目" in text
        or "uva" in low
        or "online judge" in low
        or "leetcode" in low
        or "sample input" in low
        or "sample output" in low
    )
    has_io_sections = (
        ("input" in low and "output" in low)
        or ("標準輸入" in text and "標準輸出" in text)
        or ("sample input" in low and "sample output" in low)
    )
    has_solve_contract = (
        "solve(input_text" in low
        or "def solve" in low
        or ("input_text" in low and "stdout" in low)
    )
    blocked_interactive = any(x in low for x in ["interactive problem", "互動式題目", "interactor"])
    return bool(has_original_source and has_io_sections and has_solve_contract and not blocked_interactive)


def _stage1_scope_safe_fallback_build_original_io_acceptance(raw_requirement, task_name):
    """v8.6.2-safe original IO：建立最小、可追溯、保留原題的 frozen spec。"""
    return {
        "task_meta": {
            "task_name": task_name or "Original IO Problem",
            "target_language": "python",
            "expected_files": ["generated_solution.py"],
            "task_mode": "original_io_problem",
        },
        "core_objectives": [
            {
                "id": "OBJ-001",
                "basis_type": "explicit",
                "description": "依照原始題目敘述實作標準輸入至標準輸出的解題邏輯，且不得改寫題意。",
                "requirement_basis": "原始 CPE/UVA/Online Judge 題目 statement 與補充的 solve(input_text)->str 介面要求。",
                "verification_focus": "檢查程式是否保留原題輸入格式、輸出格式與多組測資語意。",
            }
        ],
        "constraints": [
            {
                "id": "CON-001",
                "basis_type": "explicit",
                "rule": "程式必須 self-contained、deterministic、local run；不得使用外部 API、網路、資料庫、檔案系統或隨機性。",
                "requirement_basis": "介面規則 3-5。",
                "verification_focus": "驗證 generated_solution.py 是否可在本地沙盒中執行，且不依賴外部資源。",
            }
        ],
        "interface_contract": {
            "entity_name": "solve",
            "entity_type": "function",
            "functions_or_methods": [
                {
                    "name": "solve",
                    "purpose": "接收原題完整 stdin 文字並回傳完整 stdout 文字。",
                    "parameters": [{"name": "input_text", "type": "str"}],
                    "returns": "str",
                    "requirement_basis": "介面規則：def solve(input_text: str) -> str。",
                }
            ],
        },
        "state_model": {
            "state_fields": [],
            "initialization": "stateless_io_solver_or_internal_local_state_only",
            "requirement_basis": "原始題目以 stdin/stdout 為任務介面，不固定內部狀態欄位。",
        },
        "behavior_spec": [
            {
                "id": "BHV-001",
                "related_objective_ids": ["OBJ-001"],
                "basis_type": "explicit",
                "description": "solve(input_text) 應解析原題完整輸入，並回傳符合原題 Output 規格的完整輸出字串。",
                "observable_behavior": "對任一合法 input_text，函式應回傳 str，且不透過 input()/print() 作為唯一核心輸入輸出。",
                "requirement_basis": "原始題目 Input/Output 段落與 solve 介面規則。",
                "verification_focus": "以 IO 型蛻變測試檢查 source/follow-up input 的輸出關係是否一致。",
            }
        ],
        "invariants": [
            {
                "id": "INV-001",
                "basis_type": "explicit",
                "description": "相同 input_text 重複執行應產生相同 output_text。",
                "requirement_basis": "deterministic、local run 介面規則。",
                "verification_focus": "IO determinism MR。",
            }
        ],
        "assumptions": [
            {
                "id": "ASM-001",
                "description": "此 fallback 僅在 Stage 1 未能穩定凍結規格時，保留原始題目作為共享語意錨點；最終是否通過仍由 Stage 3/4/5 驗證判定。",
                "basis_type": "logically_derived",
            }
        ],
        "uncertainties": [],
    }


def _stage1_scope_safe_fallback_is_in_scope(text):
    """
    v8.6.2-safe：
    - class/function 任務維持原本 scope-safe 檢查
    - original_io_problem 題目允許 solve(input_text)->str 包裝進入後續驗證
    """
    if not text or not isinstance(text, str):
        return False, "empty_requirement"

    # 原始 CPE/UVA/LeetCode 題目是正式任務載體。
    # 「介面規則」「不得使用外部 API」是我們加的限制，不是 out-of-scope 需求。
    if _stage1_scope_safe_fallback_is_original_io_problem(text):
        return True, "scope_safe_original_io_problem"

    t = text.lower()

    forbidden = [
        "gui", "介面", "視窗", "按鈕", "網頁", "web", "api", "http", "https",
        "資料庫", "database", "sql", "sqlite", "mysql", "postgres",
        "檔案", "file", "csv", "excel", "json檔", "讀檔", "寫檔",
        "網路", "network", "socket", "爬蟲", "scrape",
        "隨機", "random", "時間", "日期", "thread", "async", "多執行緒",
        "外部服務", "登入", "oauth", "token",
    ]
    for bad in forbidden:
        if bad in t:
            return False, f"out_of_scope_forbidden_keyword:{bad}"

    has_class = ("類別" in text) or ("class" in t)
    has_public_methods = ("公開方法" in text) or ("方法固定" in text) or ("public method" in t)

    numeric_signals = [
        "amount", "balance", "counter", "score", "quota", "capacity",
        "餘額", "金額", "數值", "分數", "庫存", "點數", "額度", "能量",
        "預約", "進度", "溫度", "目前", "初始",
        "增加", "減少", "低於", "超過", "上限", "下限", "正數", "正整數",
    ]
    collection_signals = [
        "集合", "字典", "商品", "品項", "購物車", "庫存", "分類",
        "學生", "任務", "票數", "折價券", "館藏", "資源",
        "新增", "移除", "總和", "平均", "數量",
    ]

    has_state_or_collection = any(x.lower() in t for x in numeric_signals) or any(x.lower() in t for x in collection_signals)

    if not has_class:
        return False, "missing_class_signal"
    if not has_public_methods:
        return False, "missing_public_methods_signal"
    if not has_state_or_collection:
        return False, "missing_numeric_or_collection_signal"

    return True, "scope_safe_class_task"


def _stage1_scope_safe_fallback_freeze(run_id, stage1_result=None, project_root=None):
    """
    Stage 1 若沒有 frozen spec，但題目明確屬於 scope-safe class task，
    則建立 fallback frozen_acceptance_spec.json。

    注意：
    - 只處理 false reject，不處理 out-of-scope。
    - 不會讓題目直接通過；只是允許它進 Stage 2~5 接受後續驗證。
    """
    import json
    import hashlib
    from pathlib import Path
    from datetime import datetime

    root = Path(project_root).resolve() if project_root is not None else Path(__file__).resolve().parent
    base = root / "stage1_outputs" / str(run_id)
    frozen_path = base / "frozen_acceptance_spec.json"

    if frozen_path.exists():
        return {
            "created": False,
            "allowed": True,
            "reason": "frozen_spec_already_exists",
            "frozen_spec_path": str(frozen_path),
        }

    intent_spec_path = base / "intent_spec.json"
    normalized_path = base / "normalized_requirement.json"
    alignment_path = base / "intent_alignment_report.json"

    intent_spec = _stage1_scope_safe_fallback_read_json(intent_spec_path) or {}
    normalized = _stage1_scope_safe_fallback_read_json(normalized_path) or {}
    alignment = _stage1_scope_safe_fallback_read_json(alignment_path) or {}

    raw_requirement = (
        _stage1_scope_safe_fallback_read_requirement_file(stage1_result)
        or _stage1_scope_safe_fallback_text_from(normalized)
        or _stage1_scope_safe_fallback_text_from(intent_spec)
    )

    allowed, reason = _stage1_scope_safe_fallback_is_in_scope(raw_requirement)
    if not allowed:
        return {
            "created": False,
            "allowed": False,
            "reason": reason,
            "raw_requirement": raw_requirement,
            "frozen_spec_path": str(frozen_path),
        }

    task_name = None
    if isinstance(intent_spec, dict):
        task_name = intent_spec.get("task_name") or intent_spec.get("class_name")
    if _stage1_scope_safe_fallback_is_original_io_problem(raw_requirement):
        task_name = task_name or _stage1_scope_safe_fallback_title_from_original_io(raw_requirement)
        frozen = _stage1_scope_safe_fallback_build_original_io_acceptance(raw_requirement, task_name)
    else:
        task_name = task_name or _stage1_scope_safe_fallback_task_name(raw_requirement)
        # 盡量沿用 intent_spec 原內容，避免改壞 Stage 2 / Stage 3 讀取習慣。
        frozen = dict(intent_spec) if isinstance(intent_spec, dict) else {}

    frozen["spec_version"] = frozen.get("spec_version") or "1.1"
    frozen["frozen_at"] = datetime.now().isoformat(timespec="seconds")
    frozen["spec_status"] = "frozen_pre_agent_acceptance_spec"
    frozen["run_id"] = str(run_id)
    frozen["task_name"] = task_name
    frozen["source_requirement"] = {
        "raw_requirement": raw_requirement,
        "requirement_sha256": hashlib.sha256(raw_requirement.encode("utf-8")).hexdigest(),
    }


    # stage1_fallback_codegen_ready_fields_v1
    # 這些欄位只代表「允許進入後續驗證」，不是代表程式已通過。
    frozen["decision"] = "pass"
    frozen["ready_for_agent"] = True
    frozen["agent_ready"] = True
    frozen["frozen_spec"] = "frozen_created"
    frozen["frozen_created"] = True
    frozen["pre_agent_acceptance_passed"] = True
    frozen["pre_agent_acceptance"] = {
        "decision": "pass",
        "ready_for_agent": True,
        "frozen_spec": "frozen_created",
        "source": "scope_safe_fallback",
        "note": "Allowed to enter code generation because requirement matched scope-safe thesis boundary. Final pass still depends on Stage 3/4/5.",
    }
    frozen["intent_alignment_decision"] = "pass_scope_safe_fallback"
    frozen["fallback_allowed_for_code_generation"] = True

    frozen["stage1_fallback_freeze"] = {
        "enabled": True,
        "reason": "stage1_false_reject_scope_safe_override",
        "scope_reason": reason,
        "original_stage1_result": stage1_result,
        "alignment_report": alignment,
        "note": "Stage 1 did not create frozen spec, but requirement matched the thesis scope-safe deterministic class-task boundary. This fallback only allows downstream verification; it does not mark the implementation as passed.",
    }

    base.mkdir(parents=True, exist_ok=True)
    frozen_path.write_text(json.dumps(frozen, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    return {
        "created": True,
        "allowed": True,
        "reason": "fallback_frozen_spec_created",
        "scope_reason": reason,
        "task_name": task_name,
        "frozen_spec_path": str(frozen_path),
    }

def main() -> int:
    configure_console_encoding()

    parser = argparse.ArgumentParser(
        description="完整流程入口：NLP 需求 → Stage 1/2 → Stage 3~7 驗證修復閉環。"
    )
    parser.add_argument(
        "--requirement",
        default=None,
        help="自然語言需求。若不提供，會互動輸入。",
    )
    parser.add_argument(
        "--requirement-file",
        default=None,
        help="從文字檔讀取自然語言需求。",
    )
    parser.add_argument(
        "--mode",
        choices=["verify", "repair-once", "repair-loop"],
        default="repair-once",
        help="交給 main_pipeline.py 的模式。預設 repair-once。",
    )
    parser.add_argument(
        "--max-repairs",
        type=int,
        default=2,
        help="repair-loop 模式最多修復幾輪。",
    )
    parser.add_argument(
        "--generation-model",
        default="",
        help="Stage 2 程式生成模型；空字串代表使用 code_generation_agent.py 預設。",
    )
    parser.add_argument(
        "--mr-model",
        default="",
        help="Stage 5 MR 候選產生模型；空字串代表使用 verification_framework.py 預設；none 代表不用 LLM。",
    )
    parser.add_argument(
        "--repair-model",
        default="",
        help="Stage 7 修復模型；空字串代表使用 repair_generation.py 預設。",
    )
    parser.add_argument(
        "--stage1-extra-blank-lines",
        type=int,
        default=3,
        help="餵給 Stage 1 的額外 Enter 數，避免互動提示卡住。",
    )
    parser.add_argument(
        "--stage2-extra-blank-lines",
        type=int,
        default=3,
        help="餵給 Stage 2 的額外 Enter 數，避免互動提示卡住。",
    )

    args = parser.parse_args()

    project_root = Path(__file__).resolve().parent
    child_env = build_child_env()
    full_run_dir = get_full_run_dir(project_root)
    full_run_dir.mkdir(parents=True, exist_ok=True)

    safe_print("=" * 72)
    safe_print("完整論文驗證流程入口")
    safe_print("NLP → Stage 1 → Stage 2 → Stage 3/4/5/6/7")
    safe_print("=" * 72)
    safe_print(f"Project root: {project_root}")
    safe_print(f"Full run dir: {full_run_dir}")
    safe_print(f"Mode: {args.mode}")

    # 讀需求
    if args.requirement_file:
        requirement_file = Path(args.requirement_file)
        if not requirement_file.is_absolute():
            requirement_file = project_root / requirement_file
        requirement = read_text_file(requirement_file)
    elif args.requirement:
        requirement = args.requirement
    else:
        safe_print()
        safe_print("請輸入自然語言需求，輸入完成後按 Enter：")
        requirement = input("> ").strip()

    if not requirement.strip():
        safe_print("[ERROR] 需求不可為空。")
        return 1

    requirement_path = full_run_dir / "input_requirement.txt"
    requirement_path.write_text(requirement, encoding="utf-8")

    safe_print()
    safe_print("=" * 72)
    safe_print("輸入需求")
    safe_print("=" * 72)
    safe_print(requirement)

    full_result: Dict[str, Any] = {
        "created_at": datetime.now().isoformat(),
        "project_root": str(project_root),
        "full_run_dir": str(full_run_dir),
        "input_requirement_path": str(requirement_path),
        "input_requirement": requirement,
        "mode": args.mode,
        "stage1": None,
        "stage2": None,
        "main_pipeline": None,
        "selected_run_id": None,
        "selected_initial_gen_id": None,
        "final_gen_id": None,
        "verification_passed": False,
    }

    # Stage 1
    run_id, stage1_result = run_stage1_intent_structuring(
        project_root=project_root,
        requirement=requirement,
        child_env=child_env,
        log_dir=full_run_dir,
        extra_blank_lines=args.stage1_extra_blank_lines,
    )

    if not run_id:
        safe_print("[STOP] Stage 1 失敗或無法取得 RUN-ID；不進入 Stage 2，避免使用空規格。")
        full_result["stage1"] = stage1_result
        full_result["stop_reason"] = "stage1_failed_or_no_run_id"
        result_path = full_run_dir / "full_pipeline_result.json"
        write_json(result_path, full_result)
        safe_print(f"Result JSON: {result_path}")
        return 1

    # stage1_guard_v4_scope_safe_fallback
    frozen_spec_path = project_root / 'stage1_outputs' / run_id / 'frozen_acceptance_spec.json'
    if not frozen_spec_path.exists():
        fallback = _stage1_scope_safe_fallback_freeze(run_id, stage1_result, project_root=project_root)
        if fallback.get('created'):
            frozen_spec_path = Path(fallback['frozen_spec_path'])
            safe_print('[INFO] Stage 1 未產生 frozen spec，但題目符合 scope-safe 研究範圍，已建立 fallback frozen spec。')
            safe_print(f"[INFO] fallback frozen spec: {frozen_spec_path}")
        else:
            safe_print(f'[STOP] Stage 1 未產生 frozen spec，且不符合 fallback 條件，停止流程：{frozen_spec_path}')
            safe_print(f"[STOP] fallback reason: {fallback.get('reason')}")
            full_result = {
                'ok': False,
                'run_id': run_id,
                'initial_gen_id': None,
                'final_gen_id': None,
                'verification_passed': False,
                'stop_reason': 'stage1_not_ready_no_frozen_spec',
                'frozen_spec_path': str(frozen_spec_path),
                'fallback': fallback,
                'stage1_result': stage1_result,
            }
            try:
                write_json(full_run_dir / 'full_pipeline_result.json', full_result)
                safe_print(f"Result JSON: {full_run_dir / 'full_pipeline_result.json'}")
            except Exception as e:
                safe_print(f'[WARN] Stage 1 stop result 寫入失敗：{type(e).__name__}: {e}')
            return 1

    # stage1_outer_guard_hardening_v2
    frozen_spec_guard_path = project_root / 'stage1_outputs' / run_id / 'frozen_acceptance_spec.json'
    if not frozen_spec_guard_path.exists():
        safe_print(f'[WARN] 找不到 frozen_acceptance_spec.json：{frozen_spec_guard_path}')
        safe_print('[STOP] Stage 1 未產生 frozen spec，停止流程，避免 Stage 2 用空規格繼續執行。')
        full_result = {
            'ok': False,
            'run_id': run_id,
            'initial_gen_id': None,
            'final_gen_id': None,
            'verification_passed': False,
            'stop_reason': 'stage1_not_ready_no_frozen_spec',
            'frozen_spec_path': str(frozen_spec_guard_path),
        }
        write_json(full_run_dir / 'full_pipeline_result.json', full_result)
        safe_print(f"Result JSON: {full_run_dir / 'full_pipeline_result.json'}")
        return 1
    full_result["stage1"] = stage1_result
    full_result["selected_run_id"] = run_id

    if not run_id:
        safe_print("[STOP] Stage 1 失敗或無法取得 RUN-ID。")
        result_path = full_run_dir / "full_pipeline_result.json"
        write_json(result_path, full_result)
        safe_print(f"Result JSON: {result_path}")
        return 1

    # Stage 2
    gen_id, stage2_result = run_stage2_code_generation(
        project_root=project_root,
        run_id=run_id,
        generation_model=args.generation_model,
        child_env=child_env,
        log_dir=full_run_dir,
        extra_blank_lines=args.stage2_extra_blank_lines,
    )
    full_result["stage2"] = stage2_result
    full_result["selected_initial_gen_id"] = gen_id

    if not gen_id or stage2_result.get("returncode") != 0:
        safe_print("[STOP] Stage 2 失敗或無法取得 GEN-ID。")
        result_path = full_run_dir / "full_pipeline_result.json"
        write_json(result_path, full_result)
        safe_print(f"Result JSON: {result_path}")
        return 1

    # Stage 3~7
    _, pipeline_result = run_main_pipeline(
        project_root=project_root,
        run_id=run_id,
        gen_id=gen_id,
        mode=args.mode,
        max_repairs=args.max_repairs,
        mr_model=args.mr_model,
        repair_model=args.repair_model,
        child_env=child_env,
        log_dir=full_run_dir,
    )

    full_result["main_pipeline"] = pipeline_result

    main_pipeline_result = pipeline_result.get("main_pipeline_result") or {}
    full_result["final_gen_id"] = main_pipeline_result.get("final_gen_id", gen_id)
    full_result["verification_passed"] = bool(main_pipeline_result.get("verification_passed", False))

    result_path = full_run_dir / "full_pipeline_result.json"
    write_json(result_path, full_result)

    safe_print()
    safe_print("=" * 72)
    safe_print("完整流程結束")
    safe_print("=" * 72)
    safe_print(f"RUN-ID: {run_id}")
    safe_print(f"Initial GEN-ID: {gen_id}")
    safe_print(f"Final GEN-ID: {full_result.get('final_gen_id')}")
    safe_print(f"Verification passed: {full_result.get('verification_passed')}")
    safe_print(f"Full result JSON: {result_path}")



    # main_py_internal_workspace_run_layout
    # 將本次 RUN 輸出複製成集中快照到 workspace/run/<RUN-ID>，保留原路徑供重跑。
    try:
        organized_dir = organize_run_output_to_workspace_run(
            project_root=project_root,
            run_id=run_id,
            full_run_dir=full_run_dir,
            final_gen_id=full_result.get("final_gen_id"),
        )
        if organized_dir:
            safe_print()
            safe_print("=" * 72)
            safe_print("RUN 輸出已集中整理")
            safe_print("=" * 72)
            safe_print(f"Run folder: {organized_dir}")
            safe_print(f"Final artifacts: {organized_dir / '99_final'}")
    except Exception as e:
        safe_print(f"[WARN] RUN 輸出集中整理失敗：{type(e).__name__}: {e}")

    return 0 if full_result.get("verification_passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
