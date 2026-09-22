# -*- coding: utf-8 -*-
"""
repair_generation.py
====================

正式流程用：Stage 7 缺陷回饋修復生成模組。

用途：
1. 讀取 Stage 6 產生的 repair_prompt.md / repair_request.json
2. 複製原本 GEN-xxxxxx 成新的 GEN-xxxxxx
3. 呼叫本地 Ollama 修復代理人，修正 generated_solution.py
4. 保留 repair lineage，方便追蹤：舊 GEN -> REPAIR -> 新 GEN

定位：
- 這支是之後會持續用的正式閉環模組。
- 它不負責重跑 Stage 3/4/5，只負責產生修復後的新 GEN。

預設模型：
- gemma4:31b
- 可用 --model 修改

使用範例：
    python repair_generation.py

或：
    python repair_generation.py --run-id RUN-000007 --source-gen-id GEN-000001 --repair-id REPAIR-000001

只建立新 GEN，不呼叫模型：
    python repair_generation.py --prepare-only
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import shutil
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional


DEFAULT_MODEL = os.environ.get("REPAIR_MODEL", "gemma4:31b")
DEFAULT_OLLAMA_CHAT_URL = os.environ.get("OLLAMA_CHAT_URL", "http://localhost:11434/api/chat")


# ============================================================
# 基礎工具
# ============================================================

def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def read_json(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def parse_numbered_id(name: str, prefix: str) -> Optional[int]:
    """GEN-000001 -> 1"""
    if not name.startswith(prefix):
        return None
    tail = name[len(prefix):]
    if not tail.isdigit():
        return None
    return int(tail)


def latest_numbered_dir(parent: Path, prefix: str) -> Optional[str]:
    if not parent.exists():
        return None

    candidates = []
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


def next_numbered_id(parent: Path, prefix: str) -> str:
    parent.mkdir(parents=True, exist_ok=True)

    max_n = 0
    for p in parent.iterdir():
        if not p.is_dir():
            continue
        n = parse_numbered_id(p.name, prefix)
        if n is not None:
            max_n = max(max_n, n)

    return f"{prefix}{max_n + 1:06d}"


def ask_with_default(label: str, default: Optional[str]) -> str:
    if default:
        value = input(f"{label}（Enter 使用 {default}）：\n> ").strip()
        return value or default

    value = input(f"{label}：\n> ").strip()
    if not value:
        raise SystemExit(f"未提供必要欄位：{label}")
    return value


# ============================================================
# 模型輸出處理
# ============================================================

def extract_python_code(model_output: str, target_class: Optional[str] = None) -> str:
    """
    從模型輸出擷取 Python 程式碼。

    優先順序：
    1. ```python ... ``` fenced block
    2. 若指定 target_class，優先找含 class TargetClass 的 block
    3. 沒有 fenced block 時，直接使用全文
    """
    fenced_blocks = re.findall(
        r"```(?:python|py)?\s*(.*?)```",
        model_output,
        flags=re.IGNORECASE | re.DOTALL,
    )

    if fenced_blocks:
        if target_class:
            for block in fenced_blocks:
                if f"class {target_class}" in block:
                    return block.strip()
        return max(fenced_blocks, key=len).strip()

    return model_output.strip()


def _top_level_function_names(code: str) -> set[str]:
    """回傳模組頂層函式名稱；用來確認修復前後的公開入口沒有消失。"""
    tree = ast.parse(code, filename="generated_solution.py")
    return {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _top_level_class_names(code: str) -> set[str]:
    """回傳模組頂層類別名稱。"""
    tree = ast.parse(code, filename="generated_solution.py")
    return {node.name for node in tree.body if isinstance(node, ast.ClassDef)}


def _has_meaningful_module_content(tree: ast.Module) -> bool:
    """排除空檔、只有註解，或只有模組說明字串的修復候選。"""
    for node in tree.body:
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            continue
        return True
    return False


def validate_python_code(
    code: str,
    target_class: Optional[str],
    original_code: str = "",
) -> None:
    """
    修復候選防呆：
    1. 不可為空白、只有註解或只有說明字串。
    2. 必須是合法 Python。
    3. 若 repair_request 指定 class_name，修復後仍需保留該 class。
    4. 若來源程式具有頂層 solve()，修復後必須保留 solve()。

    驗證失敗時，呼叫端只保存 failed candidate，不會覆寫從來源 GEN
    複製過來的 generated_solution.py。
    """
    if not code or not code.strip():
        raise ValueError(
            "修復代理人回傳空白程式碼。為避免以空檔覆寫正常程式，本次修復候選已拒絕。"
        )

    tree = ast.parse(code, filename="generated_solution.py")
    if not _has_meaningful_module_content(tree):
        raise ValueError(
            "修復候選沒有可執行的程式內容（可能只有註解或說明字串），本次修復候選已拒絕。"
        )

    repaired_classes = {
        node.name for node in tree.body if isinstance(node, ast.ClassDef)
    }
    if target_class and target_class not in repaired_classes:
        raise ValueError(
            f"修復後程式碼找不到目標類別：class {target_class}。"
            "為避免破壞公開介面，本次不覆寫 generated_solution.py。"
        )

    if original_code and original_code.strip():
        original_functions = _top_level_function_names(original_code)
        repaired_functions = {
            node.name
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        if "solve" in original_functions and "solve" not in repaired_functions:
            raise ValueError(
                "來源程式具有頂層 solve(input_text) 入口，但修復候選遺失 solve()。"
                "為避免破壞 CPE／IO 介面，本次不覆寫 generated_solution.py。"
            )


def call_ollama_repair(
    *,
    model: str,
    ollama_chat_url: str,
    repair_prompt: str,
    original_code: str,
    target_file: str,
    target_class: Optional[str],
) -> str:
    """呼叫本地 Ollama chat API。"""
    system_prompt = (
        "你是程式碼修復代理人。你的任務是根據驗證框架提供的缺陷回饋修復程式碼。\n"
        "你必須遵守：\n"
        "1. 不修改 frozen acceptance spec。\n"
        "2. 不改公開 class / method 名稱。\n"
        "3. 不移除原本已通過的有效行為。\n"
        "4. 只輸出完整修復後的 Python 程式碼。\n"
        "5. 不要輸出解釋文字，不要輸出 markdown 說明。\n"
    )
    if "solve" in _top_level_function_names(original_code):
        system_prompt += (
            "6. 原始程式具有頂層 solve(input_text)；修復後必須保留 solve(input_text)，"
            "不可改名、刪除或只回傳空檔。\n"
        )

    user_prompt = f"""
以下是 Stage 6 repair_prompt.md 的內容，代表驗證框架根據 Stage 5 MR 失敗證據產生的修復要求：

===== REPAIR PROMPT START =====
{repair_prompt}
===== REPAIR PROMPT END =====

以下是目前要修復的原始檔案：{target_file}
Target class: {target_class}

===== ORIGINAL CODE START =====
```python
{original_code}
```
===== ORIGINAL CODE END =====

請輸出完整修復後的 {target_file}。
只輸出 Python 程式碼。
""".strip()

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "stream": False,
        "options": {
            "temperature": 0.1,
            "num_ctx": 32768,
        },
    }

    request = urllib.request.Request(
        ollama_chat_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as e:
        raise RuntimeError(
            "無法連線到 Ollama。請確認 Ollama 已啟動，"
            f"且模型 {model} 已安裝。原始錯誤：{e}"
        ) from e

    data = json.loads(raw)

    try:
        return data["message"]["content"]
    except KeyError as e:
        raise RuntimeError(f"Ollama 回傳格式不符合預期：{data}") from e


# ============================================================
# manifest / lineage
# ============================================================

def update_manifest_if_possible(gen_dir: Path, new_gen_id: str, source_gen_id: str, repair_id: str) -> None:
    """
    若 implementation_manifest.json 內有 generation_id，就順手更新。
    若沒有，就只補 repair metadata，不強迫改既有格式。
    """
    manifest_path = gen_dir / "implementation_manifest.json"
    if not manifest_path.exists():
        return

    try:
        manifest = read_json(manifest_path)
    except Exception:
        return

    for key in ("generation_id", "gen_id"):
        if key in manifest:
            manifest[key] = new_gen_id

    manifest["repaired_from_generation_id"] = source_gen_id
    manifest["repair_id"] = repair_id
    manifest["repaired_at"] = datetime.now().isoformat()

    write_json(manifest_path, manifest)


# ============================================================
# main
# ============================================================

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Stage 7：根據 Stage 6 repair feedback 呼叫修復代理人，產生下一輪 GEN。"
    )
    parser.add_argument("--run-id", default=None, help="例如 RUN-000007")
    parser.add_argument("--source-gen-id", default=None, help="例如 GEN-000001")
    parser.add_argument("--repair-id", default=None, help="例如 REPAIR-000001")
    parser.add_argument("--new-gen-id", default=None, help="指定新 GEN；不指定則自動建立下一號")
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"Ollama 修復模型，預設 {DEFAULT_MODEL}")
    parser.add_argument("--ollama-chat-url", default=DEFAULT_OLLAMA_CHAT_URL)
    parser.add_argument("--prepare-only", action="store_true", help="只建立新 GEN 與 repair_agent_input.md，不呼叫 Ollama")
    parser.add_argument("--overwrite", action="store_true", help="允許覆蓋已存在的新 GEN 目錄")
    args = parser.parse_args()

    project_root = Path(__file__).resolve().parent
    repair_root = project_root / "workspace" / "repair_feedback"
    codegen_root = project_root / "workspace" / "code_generation"

    print("=" * 72)
    print("Stage 7：缺陷回饋 → 修復代理人 → 新 GEN 產生")
    print("本階段不修改 frozen spec，也不覆蓋原始 GEN。")
    print("=" * 72)

    default_run = latest_numbered_dir(repair_root, "RUN-")
    run_id = args.run_id or ask_with_default("請輸入 RUN-ID", default_run)

    run_repair_dir = repair_root / run_id
    default_source_gen = latest_numbered_dir(run_repair_dir, "GEN-")
    source_gen_id = args.source_gen_id or ask_with_default("請輸入來源 GEN-ID", default_source_gen)

    source_repair_dir = run_repair_dir / source_gen_id
    default_repair_id = latest_numbered_dir(source_repair_dir, "REPAIR-")
    repair_id = args.repair_id or ask_with_default("請輸入 REPAIR-ID", default_repair_id)

    repair_dir = repair_root / run_id / source_gen_id / repair_id
    repair_prompt_path = repair_dir / "repair_prompt.md"
    repair_request_path = repair_dir / "repair_request.json"

    if not repair_prompt_path.exists():
        print(f"❌ 找不到 repair_prompt.md：{repair_prompt_path}")
        return 1
    if not repair_request_path.exists():
        print(f"❌ 找不到 repair_request.json：{repair_request_path}")
        return 1

    repair_prompt = read_text(repair_prompt_path)
    repair_request = read_json(repair_request_path)

    target_artifact = repair_request.get("target_artifact", {})
    target_file = target_artifact.get("file", "generated_solution.py")
    target_class = target_artifact.get("class_name")

    source_gen_dir = codegen_root / run_id / source_gen_id
    source_target_path = source_gen_dir / target_file

    if not source_gen_dir.exists():
        print(f"❌ 找不到來源 GEN 目錄：{source_gen_dir}")
        return 1
    if not source_target_path.exists():
        print(f"❌ 找不到目標檔案：{source_target_path}")
        return 1

    run_codegen_dir = codegen_root / run_id
    new_gen_id = args.new_gen_id or next_numbered_id(run_codegen_dir, "GEN-")
    new_gen_dir = run_codegen_dir / new_gen_id
    new_target_path = new_gen_dir / target_file

    if new_gen_dir.exists():
        if args.overwrite:
            shutil.rmtree(new_gen_dir)
        else:
            print(f"❌ 新 GEN 目錄已存在：{new_gen_dir}")
            print("若確定要覆蓋，請加上 --overwrite")
            return 1

    print(f"[1/5] 複製來源 GEN：{source_gen_id} → {new_gen_id}")
    shutil.copytree(source_gen_dir, new_gen_dir)

    original_code = read_text(source_target_path)

    lineage = {
        "schema_version": "repair_lineage_v1",
        "run_id": run_id,
        "source_generation_id": source_gen_id,
        "new_generation_id": new_gen_id,
        "repair_id": repair_id,
        "target_file": target_file,
        "target_class": target_class,
        "repair_prompt_path": str(repair_prompt_path),
        "repair_request_path": str(repair_request_path),
        "created_at": datetime.now().isoformat(),
        "model": args.model,
    }
    write_json(new_gen_dir / "repair_lineage.json", lineage)

    repair_agent_input = f"""# Repair Agent Input

- RUN-ID: `{run_id}`
- Source GEN-ID: `{source_gen_id}`
- New GEN-ID: `{new_gen_id}`
- Repair ID: `{repair_id}`
- Target file: `{target_file}`
- Target class: `{target_class}`
- Model: `{args.model}`

## Repair Prompt

{repair_prompt}

## Original Code

```python
{original_code}
```
"""
    write_text(new_gen_dir / "repair_agent_input.md", repair_agent_input)

    if args.prepare_only:
        summary = {
            "schema_version": "repair_generation_summary_v1",
            "status": "prepared_only",
            "run_id": run_id,
            "source_generation_id": source_gen_id,
            "new_generation_id": new_gen_id,
            "repair_id": repair_id,
            "target_file": target_file,
            "created_at": datetime.now().isoformat(),
            "next_step": "Manually repair generated_solution.py or rerun without --prepare-only.",
        }
        write_json(new_gen_dir / "repair_generation_summary.json", summary)
        print("✅ 已建立新 GEN，但尚未呼叫修復代理人。")
        print(f"新 GEN 目錄：{new_gen_dir}")
        return 0

    print(f"[2/5] 呼叫修復代理人：Ollama model = {args.model}")
    try:
        model_output = call_ollama_repair(
            model=args.model,
            ollama_chat_url=args.ollama_chat_url,
            repair_prompt=repair_prompt,
            original_code=original_code,
            target_file=target_file,
            target_class=target_class,
        )
    except Exception as e:
        summary = {
            "schema_version": "repair_generation_summary_v1",
            "status": "agent_call_failed",
            "error": str(e),
            "run_id": run_id,
            "source_generation_id": source_gen_id,
            "new_generation_id": new_gen_id,
            "repair_id": repair_id,
            "created_at": datetime.now().isoformat(),
        }
        write_json(new_gen_dir / "repair_generation_summary.json", summary)
        print(f"❌ 修復代理人呼叫失敗：{e}")
        print(f"已保留新 GEN 目錄供人工檢查：{new_gen_dir}")
        return 2

    write_text(new_gen_dir / "repair_agent_response.md", model_output)

    print("[3/5] 擷取修復後 Python 程式碼")
    repaired_code = extract_python_code(model_output, target_class=target_class)

    print("[4/5] 檢查修復後程式碼語法與公開介面")
    try:
        validate_python_code(
            repaired_code,
            target_class=target_class,
            original_code=original_code,
        )
    except Exception as e:
        failed_candidate_path = new_gen_dir / f"{target_file}.repair_candidate_failed.py"
        write_text(failed_candidate_path, repaired_code)

        summary = {
            "schema_version": "repair_generation_summary_v1",
            "status": "repair_candidate_rejected",
            "error": str(e),
            "run_id": run_id,
            "source_generation_id": source_gen_id,
            "new_generation_id": new_gen_id,
            "repair_id": repair_id,
            "target_file": target_file,
            "failed_candidate_path": str(failed_candidate_path),
            "created_at": datetime.now().isoformat(),
        }
        write_json(new_gen_dir / "repair_generation_summary.json", summary)

        print("❌ 修復候選未通過基本檢查，因此沒有覆寫 generated_solution.py")
        print(f"候選檔已保存：{failed_candidate_path}")
        print(f"錯誤：{e}")
        return 3

    print(f"[5/5] 寫入修復後 {target_file}")
    write_text(new_target_path, repaired_code)
    update_manifest_if_possible(new_gen_dir, new_gen_id, source_gen_id, repair_id)

    summary = {
        "schema_version": "repair_generation_summary_v1",
        "status": "repair_generated",
        "run_id": run_id,
        "source_generation_id": source_gen_id,
        "new_generation_id": new_gen_id,
        "repair_id": repair_id,
        "target_file": target_file,
        "target_class": target_class,
        "model": args.model,
        "created_at": datetime.now().isoformat(),
        "next_step": f"python dev_rerun_stage345.py --run-id {run_id} --gen-id {new_gen_id}",
    }
    write_json(new_gen_dir / "repair_generation_summary.json", summary)

    print()
    print("✅ 修復版 GEN 產生完成")
    print(f"RUN-ID: {run_id}")
    print(f"Source GEN-ID: {source_gen_id}")
    print(f"New GEN-ID: {new_gen_id}")
    print(f"Repair ID: {repair_id}")
    print(f"Target file: {new_target_path}")
    print()
    print("下一步建議執行：")
    print(f"python dev_rerun_stage345.py --run-id {run_id} --gen-id {new_gen_id}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
