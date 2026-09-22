# -*- coding: utf-8 -*-
"""
main_pipeline.py
================

論文實作主控程式：串接 Stage 3 / 4 / 5 / 6 / 7 的驗證與修復閉環。

這支主程式的定位：
- 不是取代原本各 Stage 腳本
- 不修改 frozen spec
- 不直接修改 generated_solution.py
- 只負責呼叫既有腳本、讀取 JSON 結果、必要時啟動 repair feedback 與 repair generation

目前支援流程：

模式一：只驗證目前 GEN
    python main_pipeline.py --run-id RUN-000007 --gen-id GEN-000002 --mode verify

模式二：驗證一次，若 Stage 5 失敗就修復一次
    python main_pipeline.py --run-id RUN-000007 --gen-id GEN-000001 --mode repair-once

模式三：驗證與修復閉環，最多修 N 輪
    python main_pipeline.py --run-id RUN-000007 --gen-id GEN-000001 --mode repair-loop --max-repairs 3

如果不輸入 gen-id，會使用該 RUN 底下最新 GEN：
    python main_pipeline.py --run-id RUN-000007 --mode verify

建議目前先從：
    python main_pipeline.py --run-id RUN-000007 --gen-id GEN-000001 --mode repair-once

開始測。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# ============================================================
# Windows UTF-8 防爆設定
# ============================================================

def configure_console_encoding() -> None:
    """
    避免 Windows cmd / PowerShell 使用 cp950 時，
    因為子腳本輸出 ✅、❌、⚠️ 等符號造成 UnicodeEncodeError。
    """
    if sys.platform != "win32":
        return

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def build_child_env() -> Dict[str, str]:
    """
    給 subprocess 用的環境變數。
    """
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
# ID 與檔案工具
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


def ask_with_default(label: str, default: Optional[str]) -> str:
    if default:
        value = input(f"{label}（Enter 使用 {default}）：\n> ").strip()
        return value or default

    value = input(f"{label}：\n> ").strip()
    if not value:
        raise SystemExit(f"未提供必要欄位：{label}")

    return value


# ============================================================
# 專案輸出路徑
# ============================================================

def get_codegen_root(project_root: Path) -> Path:
    return project_root / "workspace" / "code_generation"


def get_verification_dir(project_root: Path, run_id: str, gen_id: str) -> Path:
    return project_root / "workspace" / "verification" / run_id / gen_id


def get_repair_feedback_dir(project_root: Path, run_id: str, gen_id: str) -> Path:
    return project_root / "workspace" / "repair_feedback" / run_id / gen_id


def get_main_run_dir(project_root: Path, run_id: str) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return project_root / "workspace" / "main_runs" / run_id / timestamp


# ============================================================
# subprocess 呼叫工具
# ============================================================

def run_python_script(
    *,
    project_root: Path,
    script_name: str,
    input_text: Optional[str],
    args: Optional[List[str]],
    child_env: Dict[str, str],
    log_dir: Path,
    stop_on_fail: bool = False,
) -> Tuple[int, str]:
    """
    執行一個 Python 腳本。

    - input_text：給互動式腳本使用
    - args：給 argparse 腳本使用
    """
    script_path = project_root / script_name

    if not script_path.exists():
        output = f"找不到腳本：{script_path}"
        safe_print(f"[ERROR] {output}")
        return 999, output

    cmd = [sys.executable, str(script_path)]
    if args:
        cmd.extend(args)

    safe_print()
    safe_print("=" * 72)
    safe_print(f"執行：python {script_name}" + (f" {' '.join(args)}" if args else ""))
    safe_print("=" * 72)

    try:
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
    except Exception as e:
        output = f"執行 {script_name} 時發生例外：{type(e).__name__}: {e}"
        safe_print(f"[ERROR] {output}")
        return 998, output

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

        if stop_on_fail:
            raise SystemExit(completed.returncode)

    return completed.returncode, output


# ============================================================
# Stage 3 / 4 / 5
# ============================================================

def run_stage3_4_5(
    *,
    project_root: Path,
    run_id: str,
    gen_id: str,
    mr_model: str,
    child_env: Dict[str, str],
    log_dir: Path,
    stop_on_fail: bool = False,
) -> List[Dict[str, Any]]:
    """
    重跑：
    - Stage 3 behavior_extraction.py
    - Stage 4 behavior_alignment.py
    - Stage 5 verification_framework.py
    """
    records: List[Dict[str, Any]] = []

    jobs = [
        ("behavior_extraction.py", f"{run_id}\n{gen_id}\n"),
        ("behavior_alignment.py", f"{run_id}\n{gen_id}\n"),
        ("verification_framework.py", f"{run_id}\n{gen_id}\n{mr_model}\n"),
    ]

    for script_name, input_text in jobs:
        returncode, _ = run_python_script(
            project_root=project_root,
            script_name=script_name,
            input_text=input_text,
            args=None,
            child_env=child_env,
            log_dir=log_dir,
            stop_on_fail=stop_on_fail,
        )
        records.append({"script": script_name, "returncode": returncode})

    return records


def read_verification_summary(project_root: Path, run_id: str, gen_id: str) -> Optional[Dict[str, Any]]:
    path = get_verification_dir(project_root, run_id, gen_id) / "verification_summary.json"
    return read_json_if_exists(path)


def is_verification_passed(summary: Optional[Dict[str, Any]]) -> bool:
    if not summary:
        return False

    return summary.get("final_decision_for_delivery") == "verification_passed_for_current_scope"


def is_repairable_failure(summary: Optional[Dict[str, Any]]) -> bool:
    """
    判斷目前失敗是否適合進入 Stage 6/7 修復。

    可以修復的情況：
    - Stage 5 已經真的跑出 MR/runtime failure
    - coverage_summary.failed_mr_count > 0

    不應修復的情況：
    - blocked_by_stage4_alignment
    - 沒有任何 failed MR
    - 沒有 defect candidate evidence
    """
    if not summary:
        return False

    verification_status = summary.get("verification_status")
    if verification_status in {
        "mr_failed_defect_candidate",
        "runtime_failed_defect_candidate",
        "defect_candidate_detected",
    }:
        return True

    coverage_summary = summary.get("coverage_summary", {})
    if isinstance(coverage_summary, dict):
        failed_mr_count = coverage_summary.get("failed_mr_count", 0)
        try:
            if int(failed_mr_count) > 0:
                return True
        except Exception:
            pass

    return False


def explain_non_repairable_failure(summary: Optional[Dict[str, Any]]) -> str:
    if not summary:
        return "missing_verification_summary"

    verification_status = summary.get("verification_status")
    if verification_status == "blocked_by_stage4_alignment":
        return "blocked_by_stage4_alignment_not_a_repair_feedback_case"

    coverage_summary = summary.get("coverage_summary", {})
    if isinstance(coverage_summary, dict):
        failed_mr_count = coverage_summary.get("failed_mr_count", 0)
        accepted_mr_count = coverage_summary.get("accepted_mr_count", 0)
        if failed_mr_count == 0 and accepted_mr_count == 0:
            return "no_accepted_or_failed_mr_evidence_for_repair"
        if failed_mr_count == 0:
            return "no_failed_mr_evidence_for_repair"

    return "not_classified_as_repairable_failure"


def print_verification_summary(summary: Optional[Dict[str, Any]], run_id: str, gen_id: str) -> None:
    safe_print()
    safe_print("=" * 72)
    safe_print("目前驗證結果")
    safe_print("=" * 72)

    if not summary:
        safe_print(f"[WARN] 找不到 verification_summary.json：RUN={run_id}, GEN={gen_id}")
        return

    coverage_summary = summary.get("coverage_summary", {})

    safe_print(f"RUN-ID: {run_id}")
    safe_print(f"GEN-ID: {gen_id}")
    safe_print(f"verification_status: {summary.get('verification_status')}")
    safe_print(f"final_decision_for_delivery: {summary.get('final_decision_for_delivery')}")
    safe_print(f"coverage_status: {summary.get('coverage_status')}")

    if isinstance(coverage_summary, dict):
        safe_print(f"accepted_mr_count: {coverage_summary.get('accepted_mr_count')}")
        safe_print(f"executed_mr_count: {coverage_summary.get('executed_mr_count')}")
        safe_print(f"passed_mr_count: {coverage_summary.get('passed_mr_count')}")
        safe_print(f"failed_mr_count: {coverage_summary.get('failed_mr_count')}")
        safe_print(
            "expected units covered: "
            f"{coverage_summary.get('expected_units_covered_by_mr')} / {coverage_summary.get('expected_unit_count')}"
        )

    if is_verification_passed(summary):
        safe_print("[PASS] 目前 GEN 在 Stage 5 現有 MR 覆蓋範圍內通過。")
    else:
        safe_print("[NOT PASS] 目前 GEN 尚未通過 Stage 5 現有驗證範圍。")


# ============================================================
# Stage 6 / 7 修復閉環
# ============================================================

def run_stage6_repair_feedback(
    *,
    project_root: Path,
    run_id: str,
    gen_id: str,
    child_env: Dict[str, str],
    log_dir: Path,
    stop_on_fail: bool = False,
) -> Optional[str]:
    """
    執行 repair_feedback.py，並回傳最新 REPAIR-ID。
    """
    before_latest = latest_numbered_dir(get_repair_feedback_dir(project_root, run_id, gen_id), "REPAIR-")

    returncode, _ = run_python_script(
        project_root=project_root,
        script_name="repair_feedback.py",
        input_text=f"{run_id}\n{gen_id}\n",
        args=None,
        child_env=child_env,
        log_dir=log_dir,
        stop_on_fail=stop_on_fail,
    )

    if returncode != 0:
        safe_print("[WARN] repair_feedback.py 沒有正常結束。")
        return None

    after_latest = latest_numbered_dir(get_repair_feedback_dir(project_root, run_id, gen_id), "REPAIR-")

    if not after_latest:
        safe_print("[WARN] 找不到 repair feedback 輸出 REPAIR-ID。")
        return None

    summary_path = get_repair_feedback_dir(project_root, run_id, gen_id) / after_latest / "repair_feedback_summary.json"
    repair_summary = read_json_if_exists(summary_path)
    repair_status = repair_summary.get("repair_status") if isinstance(repair_summary, dict) else None

    if repair_status and repair_status != "repair_required":
        safe_print(f"[STOP] repair_feedback_summary 顯示不需要修復：{repair_status}")
        safe_print(f"[INFO] summary: {summary_path}")
        return None

    if before_latest == after_latest:
        safe_print(f"[INFO] 最新 REPAIR-ID 未改變：{after_latest}")

    safe_print(f"[INFO] 使用 REPAIR-ID：{after_latest}")
    return after_latest


def run_stage7_repair_generation(
    *,
    project_root: Path,
    run_id: str,
    source_gen_id: str,
    repair_id: str,
    repair_model: str,
    child_env: Dict[str, str],
    log_dir: Path,
    stop_on_fail: bool = False,
) -> Optional[str]:
    """
    執行 repair_generation.py，並回傳新 GEN-ID。
    """
    codegen_run_dir = get_codegen_root(project_root) / run_id
    before_latest = latest_numbered_dir(codegen_run_dir, "GEN-")

    args = [
        "--run-id", run_id,
        "--source-gen-id", source_gen_id,
        "--repair-id", repair_id,
    ]

    if repair_model:
        args.extend(["--model", repair_model])

    returncode, _ = run_python_script(
        project_root=project_root,
        script_name="repair_generation.py",
        input_text=None,
        args=args,
        child_env=child_env,
        log_dir=log_dir,
        stop_on_fail=stop_on_fail,
    )

    if returncode != 0:
        safe_print("[WARN] repair_generation.py 沒有正常結束。")
        return None

    after_latest = latest_numbered_dir(codegen_run_dir, "GEN-")

    if not after_latest:
        safe_print("[WARN] 找不到 code_generation 輸出的新 GEN。")
        return None

    if after_latest == before_latest:
        safe_print(f"[WARN] 最新 GEN-ID 沒有變化：{after_latest}")
    else:
        safe_print(f"[INFO] 新 GEN-ID：{after_latest}")

    return after_latest


# ============================================================
# 主流程模式
# ============================================================

def run_verify_mode(
    *,
    project_root: Path,
    run_id: str,
    gen_id: str,
    mr_model: str,
    child_env: Dict[str, str],
    log_dir: Path,
    stop_on_fail: bool,
) -> Dict[str, Any]:
    records = run_stage3_4_5(
        project_root=project_root,
        run_id=run_id,
        gen_id=gen_id,
        mr_model=mr_model,
        child_env=child_env,
        log_dir=log_dir,
        stop_on_fail=stop_on_fail,
    )

    summary = read_verification_summary(project_root, run_id, gen_id)
    print_verification_summary(summary, run_id, gen_id)

    return {
        "mode": "verify",
        "run_id": run_id,
        "final_gen_id": gen_id,
        "stage_records": records,
        "verification_passed": is_verification_passed(summary),
        "verification_summary": summary,
    }


def run_repair_once_mode(
    *,
    project_root: Path,
    run_id: str,
    gen_id: str,
    mr_model: str,
    repair_model: str,
    child_env: Dict[str, str],
    log_dir: Path,
    stop_on_fail: bool,
) -> Dict[str, Any]:
    safe_print()
    safe_print("=" * 72)
    safe_print("Repair-once 模式：先驗證，若失敗則修復一次")
    safe_print("=" * 72)

    first_records = run_stage3_4_5(
        project_root=project_root,
        run_id=run_id,
        gen_id=gen_id,
        mr_model=mr_model,
        child_env=child_env,
        log_dir=log_dir,
        stop_on_fail=stop_on_fail,
    )

    first_summary = read_verification_summary(project_root, run_id, gen_id)
    print_verification_summary(first_summary, run_id, gen_id)

    if is_verification_passed(first_summary):
        safe_print("[DONE] 原始 GEN 已通過，不需要修復。")
        return {
            "mode": "repair-once",
            "run_id": run_id,
            "source_gen_id": gen_id,
            "final_gen_id": gen_id,
            "repair_performed": False,
            "stage_records": first_records,
            "verification_passed": True,
            "verification_summary": first_summary,
        }

    if not is_repairable_failure(first_summary):
        stop_reason = explain_non_repairable_failure(first_summary)
        safe_print()
        safe_print(f"[STOP] Stage 5 未通過，但沒有可修復的 MR/runtime defect evidence：{stop_reason}")
        safe_print("[NOTE] 這通常代表 Stage 4 對齊不足、目前模板不支援，或沒有 failed MR；不應進入 repair_generation。")
        return {
            "mode": "repair-once",
            "run_id": run_id,
            "source_gen_id": gen_id,
            "final_gen_id": gen_id,
            "repair_performed": False,
            "verification_passed": False,
            "stop_reason": stop_reason,
            "verification_summary": first_summary,
        }

    safe_print()
    safe_print("[ACTION] Stage 5 有可修復失敗證據，啟動 Stage 6 repair_feedback.py")

    repair_id = run_stage6_repair_feedback(
        project_root=project_root,
        run_id=run_id,
        gen_id=gen_id,
        child_env=child_env,
        log_dir=log_dir,
        stop_on_fail=stop_on_fail,
    )

    if not repair_id:
        return {
            "mode": "repair-once",
            "run_id": run_id,
            "source_gen_id": gen_id,
            "final_gen_id": gen_id,
            "repair_performed": False,
            "error": "repair_feedback_failed",
            "verification_passed": False,
        }

    safe_print()
    safe_print("[ACTION] 啟動 Stage 7 repair_generation.py")

    new_gen_id = run_stage7_repair_generation(
        project_root=project_root,
        run_id=run_id,
        source_gen_id=gen_id,
        repair_id=repair_id,
        repair_model=repair_model,
        child_env=child_env,
        log_dir=log_dir,
        stop_on_fail=stop_on_fail,
    )

    if not new_gen_id:
        return {
            "mode": "repair-once",
            "run_id": run_id,
            "source_gen_id": gen_id,
            "final_gen_id": gen_id,
            "repair_performed": True,
            "repair_id": repair_id,
            "error": "repair_generation_failed",
            "verification_passed": False,
        }

    safe_print()
    safe_print("[ACTION] 修復後重跑 Stage 3 / 4 / 5")

    second_records = run_stage3_4_5(
        project_root=project_root,
        run_id=run_id,
        gen_id=new_gen_id,
        mr_model=mr_model,
        child_env=child_env,
        log_dir=log_dir,
        stop_on_fail=stop_on_fail,
    )

    second_summary = read_verification_summary(project_root, run_id, new_gen_id)
    print_verification_summary(second_summary, run_id, new_gen_id)

    return {
        "mode": "repair-once",
        "run_id": run_id,
        "source_gen_id": gen_id,
        "repair_id": repair_id,
        "final_gen_id": new_gen_id,
        "repair_performed": True,
        "first_stage_records": first_records,
        "second_stage_records": second_records,
        "verification_passed": is_verification_passed(second_summary),
        "verification_summary": second_summary,
    }


def run_repair_loop_mode(
    *,
    project_root: Path,
    run_id: str,
    gen_id: str,
    mr_model: str,
    repair_model: str,
    max_repairs: int,
    child_env: Dict[str, str],
    log_dir: Path,
    stop_on_fail: bool,
) -> Dict[str, Any]:
    safe_print()
    safe_print("=" * 72)
    safe_print(f"Repair-loop 模式：最多修復 {max_repairs} 輪")
    safe_print("=" * 72)

    current_gen_id = gen_id
    history: List[Dict[str, Any]] = []

    for round_index in range(max_repairs + 1):
        safe_print()
        safe_print("=" * 72)
        safe_print(f"閉環第 {round_index + 1} 次驗證：GEN={current_gen_id}")
        safe_print("=" * 72)

        records = run_stage3_4_5(
            project_root=project_root,
            run_id=run_id,
            gen_id=current_gen_id,
            mr_model=mr_model,
            child_env=child_env,
            log_dir=log_dir / f"loop_{round_index + 1}_{current_gen_id}",
            stop_on_fail=stop_on_fail,
        )

        summary = read_verification_summary(project_root, run_id, current_gen_id)
        print_verification_summary(summary, run_id, current_gen_id)

        history.append({
            "round": round_index + 1,
            "gen_id": current_gen_id,
            "stage_records": records,
            "verification_passed": is_verification_passed(summary),
            "verification_summary": summary,
        })

        if is_verification_passed(summary):
            safe_print("[DONE] 閉環完成：目前 GEN 已通過。")
            return {
                "mode": "repair-loop",
                "run_id": run_id,
                "initial_gen_id": gen_id,
                "final_gen_id": current_gen_id,
                "verification_passed": True,
                "history": history,
            }

        if round_index >= max_repairs:
            safe_print("[STOP] 已達最大修復輪數，停止。")
            return {
                "mode": "repair-loop",
                "run_id": run_id,
                "initial_gen_id": gen_id,
                "final_gen_id": current_gen_id,
                "verification_passed": False,
                "history": history,
                "stop_reason": "max_repairs_reached",
            }

        if not is_repairable_failure(summary):
            stop_reason = explain_non_repairable_failure(summary)
            safe_print()
            safe_print(f"[STOP] GEN={current_gen_id} 未通過，但沒有可修復的 MR/runtime defect evidence：{stop_reason}")
            safe_print("[NOTE] 這通常代表 Stage 4 對齊不足、目前模板不支援，或沒有 failed MR；不應進入 repair_generation。")
            return {
                "mode": "repair-loop",
                "run_id": run_id,
                "initial_gen_id": gen_id,
                "final_gen_id": current_gen_id,
                "verification_passed": False,
                "history": history,
                "stop_reason": stop_reason,
            }

        safe_print()
        safe_print(f"[ACTION] GEN={current_gen_id} 有可修復失敗證據，產生 Stage 6 repair feedback")

        repair_id = run_stage6_repair_feedback(
            project_root=project_root,
            run_id=run_id,
            gen_id=current_gen_id,
            child_env=child_env,
            log_dir=log_dir / f"loop_{round_index + 1}_{current_gen_id}",
            stop_on_fail=stop_on_fail,
        )

        if not repair_id:
            safe_print("[STOP] repair_feedback 失敗，停止閉環。")
            return {
                "mode": "repair-loop",
                "run_id": run_id,
                "initial_gen_id": gen_id,
                "final_gen_id": current_gen_id,
                "verification_passed": False,
                "history": history,
                "stop_reason": "repair_feedback_failed",
            }

        safe_print()
        safe_print(f"[ACTION] 使用 {repair_id} 產生下一個 GEN")

        next_gen_id = run_stage7_repair_generation(
            project_root=project_root,
            run_id=run_id,
            source_gen_id=current_gen_id,
            repair_id=repair_id,
            repair_model=repair_model,
            child_env=child_env,
            log_dir=log_dir / f"loop_{round_index + 1}_{current_gen_id}",
            stop_on_fail=stop_on_fail,
        )

        if not next_gen_id:
            safe_print("[STOP] repair_generation 失敗，停止閉環。")
            return {
                "mode": "repair-loop",
                "run_id": run_id,
                "initial_gen_id": gen_id,
                "final_gen_id": current_gen_id,
                "verification_passed": False,
                "history": history,
                "stop_reason": "repair_generation_failed",
            }

        current_gen_id = next_gen_id

    return {
        "mode": "repair-loop",
        "run_id": run_id,
        "initial_gen_id": gen_id,
        "final_gen_id": current_gen_id,
        "verification_passed": False,
        "history": history,
        "stop_reason": "unexpected_loop_exit",
    }


# ============================================================
# 主程式
# ============================================================

def main() -> int:
    configure_console_encoding()

    parser = argparse.ArgumentParser(
        description="主控程式：串接 Stage 3/4/5/6/7 驗證與修復閉環。"
    )
    parser.add_argument(
        "--run-id",
        default=None,
        help="例如 RUN-000007；不填則互動輸入。",
    )
    parser.add_argument(
        "--gen-id",
        default=None,
        help="例如 GEN-000001；不填則使用該 RUN 最新 GEN。",
    )
    parser.add_argument(
        "--mode",
        choices=["verify", "repair-once", "repair-loop"],
        default="verify",
        help="verify=只驗證；repair-once=失敗修一次；repair-loop=最多修多輪。",
    )
    parser.add_argument(
        "--mr-model",
        default="",
        help=(
            "Stage 5 MR 候選產生模型。"
            "空字串代表直接按 Enter 使用 verification_framework.py 預設值。"
            "若要不用 LLM，填 none。"
        ),
    )
    parser.add_argument(
        "--repair-model",
        default="",
        help="repair_generation.py 使用的修復模型；不填則使用 repair_generation.py 預設值。",
    )
    parser.add_argument(
        "--max-repairs",
        type=int,
        default=2,
        help="repair-loop 模式最多修復幾輪。",
    )
    parser.add_argument(
        "--stop-on-fail",
        action="store_true",
        help="任一子腳本 returncode 非 0 時立即停止。",
    )

    args = parser.parse_args()

    project_root = Path(__file__).resolve().parent
    child_env = build_child_env()

    safe_print("=" * 72)
    safe_print("論文驗證框架主控程式")
    safe_print("串接 Stage 3 / 4 / 5 / 6 / 7")
    safe_print("=" * 72)

    codegen_root = get_codegen_root(project_root)

    default_run = latest_numbered_dir(codegen_root, "RUN-")
    run_id = args.run_id or ask_with_default("請輸入 RUN-ID", default_run)

    run_codegen_dir = codegen_root / run_id
    default_gen = latest_numbered_dir(run_codegen_dir, "GEN-")
    gen_id = args.gen_id or ask_with_default("請輸入 GEN-ID", default_gen)

    log_dir = get_main_run_dir(project_root, run_id)
    log_dir.mkdir(parents=True, exist_ok=True)

    safe_print()
    safe_print(f"RUN-ID: {run_id}")
    safe_print(f"Start GEN-ID: {gen_id}")
    safe_print(f"Mode: {args.mode}")
    safe_print(f"Main log dir: {log_dir}")

    if args.mode == "verify":
        result = run_verify_mode(
            project_root=project_root,
            run_id=run_id,
            gen_id=gen_id,
            mr_model=args.mr_model,
            child_env=child_env,
            log_dir=log_dir,
            stop_on_fail=args.stop_on_fail,
        )
    elif args.mode == "repair-once":
        result = run_repair_once_mode(
            project_root=project_root,
            run_id=run_id,
            gen_id=gen_id,
            mr_model=args.mr_model,
            repair_model=args.repair_model,
            child_env=child_env,
            log_dir=log_dir,
            stop_on_fail=args.stop_on_fail,
        )
    elif args.mode == "repair-loop":
        result = run_repair_loop_mode(
            project_root=project_root,
            run_id=run_id,
            gen_id=gen_id,
            mr_model=args.mr_model,
            repair_model=args.repair_model,
            max_repairs=args.max_repairs,
            child_env=child_env,
            log_dir=log_dir,
            stop_on_fail=args.stop_on_fail,
        )
    else:
        raise ValueError(f"未知模式：{args.mode}")

    result_path = log_dir / "main_pipeline_result.json"
    write_json(result_path, result)

    safe_print()
    safe_print("=" * 72)
    safe_print("主控流程完成")
    safe_print("=" * 72)
    safe_print(f"Final GEN-ID: {result.get('final_gen_id')}")
    safe_print(f"Verification passed: {result.get('verification_passed')}")
    safe_print(f"Result JSON: {result_path}")

    return 0 if result.get("verification_passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
