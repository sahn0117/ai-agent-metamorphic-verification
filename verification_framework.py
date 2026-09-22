# -*- coding: utf-8 -*-
"""
verification_framework.py
=========================

Stage 5 entrypoint：Verification Profile + Rule-validated MR execution.

v8.5 新增：original_io_problem / solve(input_text: str) -> str 的 IO 型 MR。
核心邏輯在 skills/skill_verification_framework.py。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any


def configure_console_encoding() -> None:
    if sys.platform != "win32":
        return
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def safe_print(value: Any = "") -> None:
    try:
        print(value)
    except UnicodeEncodeError:
        print(str(value).encode("utf-8", errors="replace").decode("utf-8", errors="replace"))


def prompt_line(message: str, default: str = "") -> str:
    if default:
        safe_print(f"{message}（Enter 使用 {default}）：")
    else:
        safe_print(f"{message}：")
    try:
        value = input("> ").strip()
    except EOFError:
        value = ""
    return value or default


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Stage 5 verification framework")
    parser.add_argument("--run-id", default="", help="Stage 1 RUN-ID, e.g. RUN-000001")
    parser.add_argument("--gen-id", default="", help="GEN-ID, e.g. GEN-000001. Empty means latest GEN.")
    parser.add_argument("--mr-model", default="", help="LLM model for verification_profile. Use none to disable LLM profile generation.")
    parser.add_argument("--docker-image", default="python:3.11-slim", help="Docker image for sandbox execution")
    return parser.parse_args()


def main() -> int:
    configure_console_encoding()
    try:
        from skills.skill_verification_framework import latest_gen_id_for_run, run_verification_framework, read_json
    except Exception as exc:
        safe_print("[ERROR] 無法匯入 skills.skill_verification_framework")
        safe_print(f"原因：{type(exc).__name__}: {exc}")
        return 2

    args = parse_args()
    safe_print("=" * 70)
    safe_print("Stage 5：驗證框架 v8.5（Class MR + Original IO MR）")
    safe_print("支援：class/function 任務，以及 solve(input_text: str) -> str 原始題目 IO 任務")
    safe_print("IO MR：sample consistency / determinism / CRLF normalization / low-risk T-case duplication")
    safe_print("=" * 70)

    run_id = args.run_id.strip() or prompt_line("請輸入 Stage 1 RUN-ID（例如 RUN-000001）")
    if not run_id:
        safe_print("[ERROR] RUN-ID 不可為空")
        return 2

    try:
        default_gen = latest_gen_id_for_run(run_id)
    except Exception:
        default_gen = ""
    gen_id = args.gen_id.strip() or prompt_line("請輸入 Generation ID（Enter 使用最新 GEN）", default_gen)
    if not gen_id:
        safe_print("[ERROR] GEN-ID 不可為空，而且無法自動取得最新 GEN")
        return 2

    mr_model = args.mr_model.strip() or prompt_line("Verification profile 產生模型（輸入 none 則不用 LLM）", "gemma4:31b")
    use_llm = bool(mr_model and mr_model.lower() != "none")

    try:
        outputs = run_verification_framework(run_id=run_id, gen_id=gen_id, model=mr_model, use_llm=use_llm, docker_image=args.docker_image)
    except Exception as exc:
        safe_print("[ERROR] Stage 5 執行失敗")
        safe_print(f"原因：{type(exc).__name__}: {exc}")
        return 1

    summary_path = outputs.get("verification_summary")
    profile_path = outputs.get("verification_profile")
    execution_path = outputs.get("mr_execution_report")
    coverage_path = outputs.get("mr_coverage_report")
    summary = read_json(summary_path) if summary_path and Path(summary_path).exists() else {}
    execution = read_json(execution_path) if execution_path and Path(execution_path).exists() else {}
    coverage = read_json(coverage_path) if coverage_path and Path(coverage_path).exists() else {}
    profile = read_json(profile_path) if profile_path and Path(profile_path).exists() else {}
    runtime_summary = execution.get("runtime_summary", {}) or {}
    coverage_summary = coverage.get("summary", {}) or {}

    safe_print("")
    safe_print("✅ Stage 5 驗證框架執行完成")
    safe_print(f"RUN-ID: {run_id}")
    safe_print(f"GEN-ID: {gen_id}")
    safe_print(f"task_family: {summary.get('task_family') or profile.get('task_family')}")
    safe_print(f"profile_support_status: {summary.get('profile_support_status') or profile.get('support_status')}")
    safe_print(f"verification_status: {summary.get('verification_status')}")
    safe_print(f"failure_type: {summary.get('failure_type')}")
    safe_print(f"實際執行 MR: {runtime_summary.get('total', 0)}")
    safe_print(f"通過: {runtime_summary.get('passed', 0)}")
    safe_print(f"失敗: {runtime_summary.get('failed', 0)}")
    safe_print(f"accepted_mr_count: {coverage_summary.get('accepted_mr_count')}")
    safe_print(f"executed_mr_count: {coverage_summary.get('executed_mr_count')}")
    safe_print(f"passed_mr_count: {coverage_summary.get('passed_mr_count')}")
    safe_print(f"failed_mr_count: {coverage_summary.get('failed_mr_count')}")
    safe_print(f"交付判定: {summary.get('final_decision_for_delivery')}")
    safe_print("")
    safe_print("輸出檔案：")
    for name, path in outputs.items():
        safe_print(f"- {name}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
