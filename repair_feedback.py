# -*- coding: utf-8 -*-
"""
repair_feedback.py
==================
Stage 6：缺陷回饋與修復請求產生器

用途：
    讀取 Stage 5 驗證框架輸出的 MR 失敗結果，整理成：
    - defect_report.json
    - repair_request.json
    - repair_prompt.md
    - repair_feedback_summary.json
    - repair_feedback_human_review.md

定位：
    本階段不直接修改程式碼，也不直接重新執行 Agent。
    它只負責把驗證失敗證據轉成可追蹤、可操作的修復回饋。

執行方式：
    python repair_feedback.py

或：
    python repair_feedback.py --run-id RUN-000007 --gen-id GEN-000001
"""
from __future__ import annotations

import argparse
from pathlib import Path

from skills.skill_repair_feedback import (
    create_repair_feedback,
    find_latest_generation_id,
    prompt_for_run_and_generation,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Stage 6 repair feedback generator")
    parser.add_argument("--run-id", default="", help="Stage 1 RUN-ID, e.g. RUN-000007")
    parser.add_argument("--gen-id", default="", help="Generation ID, e.g. GEN-000001")
    parser.add_argument("--project-root", default="", help="Project root. Default: folder containing this script")
    args = parser.parse_args()

    project_root = Path(args.project_root).resolve() if args.project_root else Path(__file__).resolve().parent

    print("=" * 70)
    print("Stage 6：缺陷回饋與修復請求產生")
    print("本階段讀取 Stage 5 的 MR 失敗證據，產生 defect report 與 repair request。")
    print("注意：本階段不直接修改 generated_solution.py，也不自動修復。")
    print("=" * 70)
    print()

    run_id = args.run_id.strip()
    gen_id = args.gen_id.strip()

    if not run_id:
        run_id, gen_id = prompt_for_run_and_generation(project_root)
    elif not gen_id:
        gen_id = find_latest_generation_id(project_root, run_id)
        print(f"使用最新 GEN：{gen_id}")

    print()
    print("[1/4] 讀取 Stage 5 verification outputs")
    print("[2/4] 擷取 MR 失敗證據與缺陷候選")
    print("[3/4] 產生 defect_report.json、repair_request.json、repair_prompt.md")
    print("[4/4] 產生人工審查文件與摘要")
    print()

    result = create_repair_feedback(project_root=project_root, run_id=run_id, gen_id=gen_id)

    print("✅ Stage 6 缺陷回饋產生完成")
    print(f"RUN-ID: {run_id}")
    print(f"GEN-ID: {gen_id}")
    print(f"Repair ID: {result['repair_id']}")
    print(f"狀態: {result['status']}")
    print(f"缺陷候選數: {result['defect_candidate_count']}")
    print(f"可操作修復提示數: {result['actionable_hint_count']}")
    print(f"下一步建議: {result['next_step']}")
    print()
    print("輸出檔案：")
    output_files = result.get("output_files") or result.get("paths") or {}
    if output_files:
        for key, path in output_files.items():
            print(f"- {key}: {path}")
    else:
        print("(no output_files/paths returned)")

    print()
    print("注意：repair_prompt.md 是給下一輪 AI Agent / Developer 使用的修復指令。")
    print("建議先人工查看 defect_report.json 與 repair_feedback_human_review.md，再丟回 Agent。")


if __name__ == "__main__":
    main()
