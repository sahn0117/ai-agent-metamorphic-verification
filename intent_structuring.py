# -*- coding: utf-8 -*-
"""
intent_structuring.py
=====================
第一階段主程式：Agent 生成程式碼前的需求意圖結構化與規格固定。

版本 v7：補強介面契約與未定義資訊檢查，避免將原需求未指定的方法名稱、狀態欄位或預設值寫入凍結規格。

流程：
    原始題目
      → 模型 A 產生可解釋 intent_spec.json
      → 規則式檢查 JSON 格式與追溯欄位
      → 模型 B 獨立比對原題目與 intent_spec.json（含合理推導是否過度擴張）
      → Embedding 向量相似度提供輔助警示
      → 通過後輸出 frozen_acceptance_spec.json

此階段不做：
- 不辨識資料集來源、不判斷研究範圍。
- 不產生程式碼、不呼叫 Agent。
- 不產生蛻變測試、不執行功能測試。

直接執行本檔時，只需在終端機貼入原始題目，系統會自動建立 RUN-000001 類型的執行編號，
保留每次輸入內容與輸出檔案路徑；任務標籤由模型 A 根據題目整理，僅用於追蹤顯示。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

from skills.skill_planner_intent import (
    build_frozen_acceptance_spec,
    decide_pre_agent_validation,
    freeze_acceptance_spec,
    reconcile_semantic_review,
    validate_intent_spec,
    validate_semantic_review,
)


# ============================================================
# 模型與輸出設定：之後只需在此更換本機可用模型
# ============================================================

DEFAULT_STRUCTURING_MODEL = os.environ.get("STRUCTURING_MODEL", "gemma4:31b")
DEFAULT_REVIEW_MODEL = os.environ.get("REVIEW_MODEL", "nemotron-cascade-2:30b")
DEFAULT_EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "bge-m3")

PROJECT_ROOT = Path(__file__).resolve().parent
OUTPUT_ROOT = PROJECT_ROOT / "stage1_outputs"
HISTORY_FILENAME = "run_history.json"

OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
OLLAMA_CHAT_URL = os.environ.get("OLLAMA_CHAT_URL", f"{OLLAMA_BASE_URL}/api/chat")
OLLAMA_EMBED_URL = os.environ.get("OLLAMA_EMBED_URL", f"{OLLAMA_BASE_URL}/api/embed")
OLLAMA_EMBEDDINGS_URL = os.environ.get("OLLAMA_EMBEDDINGS_URL", f"{OLLAMA_BASE_URL}/api/embeddings")
NUM_CTX = 12000

# 此門檻目前為輔助警示設定，不是實證後的最終門檻。
EMBEDDING_WARNING_THRESHOLD = 0.55


# ============================================================
# 基礎工具
# ============================================================

def save_json(data: Any, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def safe_text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def sanitize_name(name: str) -> str:
    safe = re.sub(r"[^\w\-]+", "_", safe_text(name), flags=re.UNICODE).strip("_")
    return safe or "Task"


def sanitize_requirement_text(text: str) -> str:
    """保留原題內容，只移除會讓 subprocess / Ollama 壞掉的控制字元。"""
    text = safe_text(text)
    text = text.replace("\x00", "")
    text = re.sub(r"[\x01-\x08\x0b\x0c\x0e-\x1f]", "", text)
    return text.strip()


def extract_json_object(text: str) -> dict[str, Any]:
    text = safe_text(text)
    if "</think>" in text:
        text = text.split("</think>", 1)[-1].strip()
    if "```json" in text:
        text = text.split("```json", 1)[1].split("```", 1)[0].strip()
    elif "```" in text:
        text = text.split("```", 1)[1].split("```", 1)[0].strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("LLM 輸出中找不到 JSON object")
    parsed = json.loads(text[start:end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("LLM 輸出的 JSON 必須為 object")
    return parsed


def cosine_similarity(vec_a: list[float], vec_b: list[float]) -> float:
    if not vec_a or not vec_b or len(vec_a) != len(vec_b):
        return 0.0
    dot = sum(a * b for a, b in zip(vec_a, vec_b))
    norm_a = math.sqrt(sum(a * a for a in vec_a))
    norm_b = math.sqrt(sum(b * b for b in vec_b))
    return dot / (norm_a * norm_b) if norm_a and norm_b else 0.0


def _load_history(output_root: Path) -> dict[str, Any]:
    path = output_root / HISTORY_FILENAME
    if not path.exists():
        return {"last_sequence": 0, "runs": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError
        data.setdefault("last_sequence", 0)
        data.setdefault("runs", [])
        return data
    except Exception:
        raise ValueError(f"執行歷史檔案損壞，請檢查：{path}")


def _start_run(output_root: Path, raw_requirement: str) -> tuple[str, Path]:
    """建立僅依執行順序命名的資料夾；不要求使用者額外輸入任務名稱。"""
    output_root.mkdir(parents=True, exist_ok=True)
    history = _load_history(output_root)
    sequence = int(history.get("last_sequence", 0)) + 1
    run_id = f"RUN-{sequence:06d}"
    run_dir = output_root / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    record = {
        "run_id": run_id,
        "raw_requirement": raw_requirement,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "status": "running",
        "output_directory": str(run_dir),
    }
    history["last_sequence"] = sequence
    history["runs"].append(record)
    save_json(history, output_root / HISTORY_FILENAME)
    save_json(record, run_dir / "run_manifest.json")
    return run_id, run_dir

def _finish_run(output_root: Path, run_dir: Path, run_id: str, updates: dict[str, Any]) -> None:
    manifest_path = run_dir / "run_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.update(updates)
    manifest["finished_at"] = datetime.now().isoformat(timespec="seconds")
    save_json(manifest, manifest_path)

    history = _load_history(output_root)
    for record in history.get("runs", []):
        if record.get("run_id") == run_id:
            record.update(updates)
            record["finished_at"] = manifest["finished_at"]
            break
    save_json(history, output_root / HISTORY_FILENAME)


# ============================================================
# Ollama 呼叫
# ============================================================

def call_ollama_chat_json(prompt: str, model: str, system_message: str, timeout: int = 300) -> dict[str, Any]:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_message},
            {"role": "user", "content": prompt},
        ],
        "stream": False,
        "format": "json",
        "options": {"temperature": 0, "num_ctx": NUM_CTX},
    }
    request = urllib.request.Request(
        OLLAMA_CHAT_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        result = json.loads(response.read().decode("utf-8", errors="ignore"))
    return extract_json_object(result.get("message", {}).get("content", ""))


def call_ollama_embedding(text: str, model: str, timeout: int = 120) -> list[float]:
    if not safe_text(text):
        raise ValueError("Embedding 輸入不得為空")
    new_payload = {"model": model, "input": text, "truncate": True}
    try:
        request = urllib.request.Request(
            OLLAMA_EMBED_URL,
            data=json.dumps(new_payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result = json.loads(response.read().decode("utf-8", errors="ignore"))
        vectors = result.get("embeddings", [])
        if vectors:
            return vectors[0]
    except Exception:
        pass

    old_payload = {"model": model, "prompt": text}
    request = urllib.request.Request(
        OLLAMA_EMBEDDINGS_URL,
        data=json.dumps(old_payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        result = json.loads(response.read().decode("utf-8", errors="ignore"))
    vector = result.get("embedding", [])
    if not vector:
        raise ValueError("Ollama embedding 回傳中沒有向量")
    return vector


# ============================================================
# 模型 A：意圖結構化
# ============================================================

def build_intent_prompt(raw_requirement: str) -> str:
    return f"""你是模型 A，負責將程式需求轉為可解釋、可追溯的驗收規格 JSON。

規則：
1. 只能整理原需求明確提到的功能與限制，不可自行擴充需求。
2. 所有會交給後續 Agent 作為驗收依據的項目，都必須標示 basis_type：
   - explicit：該項目的完整敘述（包含條件、行為結果與狀態效果）都能在原始需求中直接找到依據。
   - logically_derived：項目中包含為了把明示需求轉為可觀察行為而做的合理推導；此推導不得改變需求目的、介面或允許輸入範圍。
3. core_objectives 與 constraints 只能使用 basis_type="explicit"；原文未明示的限制不得放入這兩區。
4. behavior_spec 與 invariants 可使用 explicit 或 logically_derived，但分類必須看「整條項目」而不是只看觸發條件：
   - 若條件來自原文，但 observable_behavior 加入了原文未直接寫出的狀態效果（例如「拒絕後狀態不變」），整條 behavior 必須標示為 logically_derived，或拆成明示約束與推導行為兩項。
   - 不得因為某行為在一般實作上很合理，就將未明講的狀態效果標示為 explicit。
4-A. 功能行為覆蓋是必要條件：
   - 先辨識原始需求中的每一個獨立功能動作，例如新增、刪除、修改、查詢、計算、存款、提款、排序或轉換。
   - 每一個功能動作都必須在 behavior_spec 中至少有一條可觀察的成功行為；不可只出現在 core_objectives 中而沒有 behavior_spec。
   - 若原文僅寫「支援某功能」，而沒有逐字描述成功後的狀態變化或回傳結果，則為了驗證該功能而補出的直接效果必須標為 logically_derived，不可標為 explicit。
   - 例如：原文只說「支援存入、提領與查詢餘額」，則「成功存入後餘額增加」、「成功提領後餘額減少」與「查詢時回傳目前餘額」均可列為可驗證行為，但均應標為 logically_derived；除非原文已明確寫出這些結果。
4-B. 不可用一個過於概括的 core_objective 取代行為覆蓋；若原需求包含多個功能，behavior_spec 應逐一留下可供後續程式行為比對的證據點。
4-C. 介面契約與狀態欄位不可自行命名：
   - interface_contract 與 state_model 也是後續 Agent 可能依循的規格內容，不得憑一般程式設計慣例自行固定。
   - 只有當原始需求明確提供介面名稱時，functions_or_methods 才能填入具體 name；例如原文直接寫出 deposit(amount) 或 deposit 方法。
   - 若原文只說「支援存款、提款與查詢餘額」而沒有方法名稱，functions_or_methods 必須輸出 []，不可自行產生 deposit、withdraw、query_balance 等命名。
   - 只有當原始需求明確提供狀態欄位名稱時，state_model.state_fields 才能填入具體欄位；不可把「目前餘額」自行翻譯成 balance 作為固定欄位名。
   - 回傳型別、參數名稱、初始值與錯誤處理方式同理：未明示時不可固定，應放入 uncertainties。
5. basis_type="logically_derived" 的推導邊界必須嚴格遵守：
   - 只可將某一項明示功能或限制，轉成該功能直接可觀察、可驗證的結果。
   - 不可把原本只針對某個操作的限制，擴張成「任何操作後」、「所有狀態」、「始終」、「永遠不得」等全域規則，除非原始需求明確支持該範圍。
   - 若推導成立需要依賴原始需求未指定的前提，例如初始值、允許的初始狀態、資料型別、方法名稱或錯誤處理策略，則不得放入 behavior_spec 或 invariants 作為驗收項目；應放入 assumptions 或 uncertainties。
   - 若沒有不依賴額外假設的安全 invariant，invariants 必須輸出 []，不得為了填滿欄位而建立全域規則。
6. assumptions 與 uncertainties 僅用來透明揭露原題目未定義之處，不是硬性驗收條件；不得使用 OBJ/CON/BHV/INV 編號，也不得寫成 Agent 必須遵守的要求。
   - 對於原需求沒有提供答案的實作選項，優先放入 uncertainties，並描述為「未指定／不納入硬性驗收」。
   - assumptions 不得替原需求選定預設方案，不可寫成「初始餘額預設為 0」、「資料型別採用 float」、「錯誤處理使用例外」等敘述。
7. requirement_basis 必須清楚指出該規格如何來自原始需求；若 basis_type="logically_derived"，必須說明推導理由與其適用範圍。
8. verification_focus 必須描述後續觀察程式行為時要確認什麼。
9. 不產生程式碼、不產生測試碼、不產生蛻變關係。
10. task_meta.task_name 僅作為後續紀錄使用：若原需求明確出現類別或函式名稱，請沿用；否則提供中性的短標籤，不得將標籤視為新增功能需求。
11. 只輸出 JSON object。

JSON 格式：
{{
  "task_meta": {{
    "task_name": "",
    "target_language": "python",
    "expected_files": []
  }},
  "core_objectives": [
    {{
      "id": "OBJ-001",
      "basis_type": "explicit",
      "description": "",
      "requirement_basis": "",
      "verification_focus": ""
    }}
  ],
  "constraints": [
    {{
      "id": "CON-001",
      "basis_type": "explicit",
      "rule": "",
      "requirement_basis": "",
      "verification_focus": ""
    }}
  ],
  "interface_contract": {{
    "entity_name": "",
    "entity_type": "class",
    "functions_or_methods": [
      {{
        "name": "",
        "purpose": "",
        "parameters": [],
        "returns": "",
        "requirement_basis": ""
      }}
    ]
  }},
  "state_model": {{"state_fields": []}},
  "behavior_spec": [
    {{
      "id": "BHV-001",
      "related_objective_ids": ["OBJ-001"],
      "basis_type": "explicit",
      "description": "",
      "observable_behavior": "",
      "requirement_basis": "",
      "verification_focus": ""
    }}
  ],
  "invariants": [
    {{
      "id": "INV-001",
      "basis_type": "explicit",
      "description": "",
      "requirement_basis": "",
      "verification_focus": ""
    }}
  ],
  "assumptions": [],
  "uncertainties": []
}}

若原需求沒有明確限制，constraints 請輸出 []；若沒有明確不變條件，invariants 請輸出 []。
若原需求未指定函式或方法名稱，functions_or_methods 必須輸出 []，不可憑空建立 method name；請在 uncertainties 說明「具體方法名稱未指定」。
若原需求未指定狀態欄位名稱，state_fields 必須輸出 []，不可將功能語意自行翻譯成固定程式欄位。
若初始值、資料型別或錯誤處理方式未指定，請只在 uncertainties 以「未指定」描述，不得在 assumptions 中替需求選擇預設方案。

分類與過度推導示例（僅示範判斷原則，不代表本次需求內容）：
- 原需求 A：「出貨數量不得超過目前庫存；庫存不足時應拒絕出貨，且庫存不得改變。」
  - 可標為 explicit：「庫存不足時拒絕出貨，且庫存維持不變。」因為條件與狀態效果均由原文明示。
  - 可標為 logically_derived：「成功出貨後庫存應依出貨數量減少。」因為這是出貨功能可驗證的直接效果。
  - 不可接受：invariants 中記錄「庫存在任何操作後均不得為負數」；此敘述擴張成全域限制，且需要額外假設初始庫存本來不可為負。此時 invariants 應輸出 []。
- 原需求 B：「調整商品數量時，數量必須大於 0。」
  - constraints 可標為 explicit：「數量必須大於 0。」
  - 若 behavior_spec 寫成「輸入數量 <= 0 時拒絕更新，且既有數量維持不變」，該項必須標為 logically_derived；因為「拒絕後狀態不變」並非原文直接明示，而是為了可驗證而做的合理推導。
- 原需求 C：「支援存入金額、提領金額及查詢目前餘額。」
  - 三個功能都必須有 behavior_spec，不可漏掉查詢行為。
  - 「成功存入後餘額增加」、「成功提領後餘額減少」、「查詢時回傳目前餘額」皆是讓功能可觀察的直接結果；若原文未明示結果，三項皆標為 logically_derived。
  - 若原文未寫方法名稱，interface_contract.functions_or_methods 必須為 []；不可自行固定為 deposit、withdraw 或 query_balance。
  - 若原文未寫初始餘額，uncertainties 可記錄「初始餘額未指定」，但 assumptions 不可寫「初始餘額預設為 0」。

原始需求：
{raw_requirement}
"""


def generate_intent_spec(raw_requirement: str, model: str) -> dict[str, Any]:
    return call_ollama_chat_json(
        build_intent_prompt(raw_requirement),
        model=model,
        system_message="你是嚴格依據原始需求產生 JSON 驗收規格的需求分析模型。",
    )


# ============================================================
# 模型 B：獨立語意一致性審查
# ============================================================

def build_review_prompt(raw_requirement: str, intent_spec: dict[str, Any]) -> str:
    return f"""你是模型 B，負責獨立審查模型 A 產生的驗收規格是否忠於原始需求。

你不可以重新設計規格，也不可以因為模型 A 已寫出某項功能就預設它合理。

審查邊界：
1. 會影響後續 Agent 與驗收的區塊包含 core_objectives、constraints、interface_contract、state_model、behavior_spec 與 invariants。
   interface_contract 或 state_model 若自行固定原文未指定的名稱，也屬於規格偏移，必須攔截。
2. assumptions 與 uncertainties 只是透明揭露的未定義事項，不是 Agent 必須達成的驗收門檻。
   因此，不得只因 assumptions 或 uncertainties 中出現原需求未明講內容，就把它列入 unsupported_hard_requirements 或判定 review。
3. 若某個未授權假設同時被提升寫入硬性驗收區塊，才可列為 unsupported_hard_requirements；此時必須指出實際的硬性項目 ID，例如 CON-002、BHV-003 或 INV-001，不可只指向 assumptions。
4. basis_type="explicit" 的項目必須以「整個項目內容」逐項直接追溯到原始需求，而不是只有觸發條件可追溯：
   - 對 behavior_spec，請同時檢查 description、observable_behavior 與 verification_focus 所描述的狀態效果是否都由原文明示。
   - 若原文只說某輸入必須符合條件，但 explicit 行為額外加入「拒絕後狀態不變」、「不改變既有資料」或其他未明講效果，這是 basis_type 分類錯誤。請將具體 ID 與理由列入 traceability_issues，verdict 必須為 review，要求改為 logically_derived 或拆分規格後再審。
   - 若原文只說「支援」某功能，而 explicit behavior 進一步寫出成功後狀態改變或回傳值，例如存入後餘額增加、提領後餘額減少、查詢後回傳當前狀態，也屬分類錯誤；除非原文已直接明示該效果。
4-A. 你必須進行「功能行為覆蓋檢查」：
   - 先從原始需求列出每一個獨立功能動作，例如新增、刪除、修改、查詢、計算、存款、提款、排序或轉換。
   - 再確認每個功能動作在 behavior_spec 中至少有一條可觀察的成功行為對應。
   - 若某個需求功能只出現在 core_objectives 或 constraints，而 behavior_spec 沒有可觀察結果，必須列入 missing_requirements 並將 verdict 設為 review。
   - review_reason 必須說明你已檢查功能覆蓋，並列出已覆蓋或缺漏的功能。
5. basis_type="logically_derived" 僅允許出現在 behavior_spec 或 invariants；若它只是讓明示需求轉為直接可觀察且不改變需求目的或適用範圍的行為，不視為新增需求。
6. 你必須逐項審查所有 basis_type="logically_derived" 的 behavior_spec 與 invariants，確認是否存在「過度推導」。下列任一情形都必須列為 unsupported_hard_requirements，並指出項目 ID：
   - 把僅針對特定操作的限制，擴張成任何操作、所有狀態、始終或永久成立的全域規則。
   - 規格成立需要依賴原需求未指定的初始值、允許的初始狀態、資料型別、方法名稱、例外類型或錯誤處理策略。
   - 規格會讓後續驗證拒絕一個仍可能符合原始題目的實作。
7. 審查 invariants 時要特別嚴格：若 invariant 使用「任何操作後」、「所有情況」、「始終」、「永遠不得」等範圍表述，原始需求必須直接支持同樣範圍；否則應判定為過度推導。
8. 你必須檢查 interface_contract 與 state_model 的未授權具體化：
   - 若原始需求未直接指定函式／方法名稱，functions_or_methods 不得出現自行翻譯或慣用命名（例如原文只寫支援某操作，卻固定成 add_item、deposit、query_balance）。
   - 若原始需求未直接指定狀態欄位名稱，state_model.state_fields 不得自行固定程式欄位名稱。
   - 若出現以上問題，請在 traceability_issues 中列出具體欄位路徑，verdict 必須為 review。
9. 你必須檢查 assumptions 與 uncertainties 是否對同一未定義事項自相矛盾，或 assumptions 是否替原需求選擇預設方案：
   - 例如 uncertainties 表示初始值未指定，但 assumptions 寫成「初始值預設為 0」，此規格會誘導 Agent 採用未授權方案，必須在 traceability_issues 指出並判定 review。
   - 透明揭露應使用「未指定／不納入硬性驗收」，而非替需求決定預設值。
10. task_meta.task_name 是追蹤標籤；只要沒有被當成功能約束，不視為新增需求。

請逐項檢查：
- missing_requirements：原需求有說，但硬性驗收規格漏掉的功能或限制。
- distorted_requirements：硬性驗收規格將原需求解讀錯誤的內容。
- unsupported_hard_requirements：原需求未要求、也非合理推導，卻被硬性驗收規格寫成必做或必通過條件的內容。每項須指出規格項目 ID。
- traceability_issues：硬性驗收項目的 requirement_basis 或 basis_type 無法對應原始需求的情況；包含「項目被標成 explicit，但其中一部分狀態效果其實只能合理推導」的錯誤分類。

分類與過度推導審查示例（僅示範判斷原則，不代表本次需求內容）：
- 原需求只限制「出貨不得超過庫存」且「庫存不足時拒絕出貨且庫存不變」。
  - 若 intent_spec 有 INV-001：「庫存在任何操作後均不得為負數」，你必須回報：
    "INV-001: 此 invariant 將出貨限制擴張為所有操作後的全域限制，且依賴原需求未指定的初始庫存前提。"
  - 若 intent_spec 只有「成功出貨後依出貨數量扣減庫存」與「庫存不足出貨不得改變庫存」，則可視為可驗證的直接行為，不因其為 logically_derived 而否決。
- 原需求只說「調整商品數量時，數量必須大於 0」。
  - 若 BHV-001 被標為 explicit，內容卻寫「數量 <= 0 時拒絕更新且既有數量不變」，你必須在 traceability_issues 回報：
    "BHV-001: 原需求只明示輸入限制；狀態維持不變屬合理推導，整項不可標為 explicit，應改為 logically_derived 或拆分。"
- 原需求只說「支援存入、提領與查詢目前餘額」。
  - 若 success behavior 將「存入後餘額增加」或「提領後餘額減少」標為 explicit，必須在 traceability_issues 回報分類錯誤。
  - 若 behavior_spec 沒有任何「查詢目前餘額」對應的可觀察行為，必須在 missing_requirements 回報該功能缺漏。
  - 若 interface_contract 另行固定方法名稱為 deposit、withdraw、query_balance，而原文沒有提供這些名稱，必須在 traceability_issues 回報未授權介面具體化。
  - 若 assumptions 寫「初始餘額預設為 0」，但原文沒有指定初始餘額，必須在 traceability_issues 回報未授權預設值。

判決規則：
- 若任一阻擋類別有內容，verdict 必須為 review。
- 只有四個阻擋類別皆為空時，verdict 才可為 pass。
- review_reason 無論 verdict 為 pass 或 review 都必須具體填寫，不得為空。
- 若存在 basis_type="logically_derived" 項目，review_reason 必須明確說明你已檢查其是否過度擴張或依賴未指定前提。

只輸出 JSON object，格式如下：
{{
  "verdict": "pass",
  "missing_requirements": [],
  "distorted_requirements": [],
  "unsupported_hard_requirements": [],
  "traceability_issues": [],
  "review_reason": "必須填寫具體審查理由，不可留空",
  "confidence": 0.0
}}

原始需求：
{raw_requirement}

模型 A 產出的 intent_spec.json：
{json.dumps(intent_spec, ensure_ascii=False, indent=2)}
"""


def review_intent_spec(raw_requirement: str, intent_spec: dict[str, Any], model: str) -> dict[str, Any]:
    return call_ollama_chat_json(
        build_review_prompt(raw_requirement, intent_spec),
        model=model,
        system_message="你是獨立語意審查模型，負責找出需求功能覆蓋遺漏、來源分類錯誤、不當新增與 logically_derived 過度推導。",
    )


def repair_semantic_review_format(
    raw_requirement: str,
    intent_spec: dict[str, Any],
    previous_review: dict[str, Any],
    format_errors: list[str],
    model: str,
) -> dict[str, Any]:
    """
    僅在模型 B 回傳格式不完整時，要求同一個審查模型補齊格式一次。
    這不是重新產生驗收規格，也不是讓模型 A 自我審查。
    """
    prompt = f"""你先前已完成一份語意審查，但回傳 JSON 的格式不完整。
請保留你的實質判斷，僅補齊或修正必要欄位，重新輸出完整 JSON object。

格式錯誤：
{json.dumps(format_errors, ensure_ascii=False, indent=2)}

必要規則：
1. verdict 只能是 "pass" 或 "review"。
2. missing_requirements、distorted_requirements、unsupported_hard_requirements、traceability_issues 都必須為 array。
3. review_reason 絕對不可為空；即使 verdict="pass"，也必須逐項說明主要需求已如何被保留。
4. confidence 必須是 0 到 1 的數字。
5. assumptions 與 uncertainties 只是透明揭露，若未被提升進 core_objectives、constraints、behavior_spec 或 invariants，不得單獨列入 unsupported_hard_requirements。
6. 若 intent_spec 中存在 logically_derived 項目，你仍必須確認該項目沒有擴張成全域限制、也沒有依賴原需求未指定的前提；若有此問題，必須保留為阻擋項目並指出其 ID。
7. 若 intent_spec 中有 basis_type="explicit" 的 behavior_spec 混入原文未直接寫出的狀態效果，包含由「支援功能」推導出的成功狀態變更或回傳值，你必須在 traceability_issues 中保留該分類問題，verdict 必須為 review；不得因為該效果看似合理就放行。
8. 若原始需求中的任一獨立功能缺少 behavior_spec 可觀察結果，你必須在 missing_requirements 中保留該覆蓋缺漏，verdict 必須為 review。
9. 若 interface_contract 或 state_model 固定了原文未指定的方法名稱、欄位名稱、回傳型別、參數名稱或初始值，你必須在 traceability_issues 中保留此問題，verdict 必須為 review。
10. 若 assumptions 替原需求選擇了未指定的預設方案（例如初始值預設為 0），即使它名為 assumption，也必須在 traceability_issues 中保留此誘導性規格問題，verdict 必須為 review。
11. 不得在此步驟修改 intent_spec，也不得新增原需求沒有的判定標準。

原始需求：
{raw_requirement}

intent_spec.json：
{json.dumps(intent_spec, ensure_ascii=False, indent=2)}

你先前的審查結果：
{json.dumps(previous_review, ensure_ascii=False, indent=2)}

請只輸出修正後的完整審查 JSON object。
"""
    return call_ollama_chat_json(
        prompt,
        model=model,
        system_message="你是獨立語意審查模型，現在僅補正你自己的審查 JSON 格式與理由欄位。",
    )


# ============================================================
# Embedding 輔助檢查
# ============================================================

def compute_embedding_similarity(
    raw_requirement: str,
    intent_spec: dict[str, Any],
    model: str,
) -> tuple[float | None, str]:
    semantic_spec = {
        "core_objectives": intent_spec.get("core_objectives", []),
        "constraints": intent_spec.get("constraints", []),
        "behavior_spec": intent_spec.get("behavior_spec", []),
        "invariants": intent_spec.get("invariants", []),
    }
    spec_text = json.dumps(semantic_spec, ensure_ascii=False)
    try:
        original_vector = call_ollama_embedding(raw_requirement, model)
        spec_vector = call_ollama_embedding(spec_text, model)
        return cosine_similarity(original_vector, spec_vector), ""
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"


# ============================================================
# 核心流程：供目前手動執行與後續測試程式共用
# ============================================================

def run_intent_structuring(
    raw_requirement: str,
    structuring_model: str = DEFAULT_STRUCTURING_MODEL,
    review_model: str = DEFAULT_REVIEW_MODEL,
    embedding_model: str = DEFAULT_EMBEDDING_MODEL,
    output_root: Path = OUTPUT_ROOT,
) -> dict[str, Any]:
    raw_requirement = safe_text(raw_requirement)
    if not raw_requirement:
        raise ValueError("原始需求不可為空")
    if structuring_model.strip() == review_model.strip():
        raise ValueError("模型 A 與模型 B 不可使用相同模型，避免自我審查偏差。")

    run_id, run_dir = _start_run(output_root, raw_requirement)
    normalized_requirement = {
        "run_id": run_id,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "target_language": "python",
        "raw_requirement": raw_requirement,
        "requirement_sha256": hashlib.sha256(raw_requirement.encode("utf-8")).hexdigest(),
        "note": (
            "此檔只保存原始需求與執行識別資訊；不要求人工提供任務名稱，"
            "不進行資料來源或研究範圍判斷。"
        ),
    }
    save_json(normalized_requirement, run_dir / "normalized_requirement.json")

    try:
        print(f"\n[{run_id}] [1/5] 模型 A 產生可解釋 intent_spec.json")
        intent_spec = generate_intent_spec(raw_requirement, structuring_model)
        save_json(intent_spec, run_dir / "intent_spec.json")
        task_label = safe_text(intent_spec.get("task_meta", {}).get("task_name")) or "Task"

        print(f"[{run_id}] [2/5] 規則式檢查 JSON 結構與需求追溯欄位")
        intent_errors = validate_intent_spec(intent_spec, raw_requirement=raw_requirement)

        print(f"[{run_id}] [3/5] 模型 B 獨立審查原題目與 intent_spec.json")
        initial_semantic_review = review_intent_spec(raw_requirement, intent_spec, review_model)
        initial_review_errors = validate_semantic_review(initial_semantic_review)
        semantic_review_attempts = [
            {
                "attempt": 1,
                "purpose": "independent_semantic_review",
                "review": initial_semantic_review,
                "format_errors": initial_review_errors,
            }
        ]

        semantic_review = initial_semantic_review
        semantic_review_errors = initial_review_errors
        format_repair_used = False

        if semantic_review_errors:
            format_repair_used = True
            print(f"[{run_id}]       模型 B 格式不完整，要求補正審查 JSON 一次")
            semantic_review = repair_semantic_review_format(
                raw_requirement=raw_requirement,
                intent_spec=intent_spec,
                previous_review=initial_semantic_review,
                format_errors=semantic_review_errors,
                model=review_model,
            )
            semantic_review_errors = validate_semantic_review(semantic_review)
            semantic_review_attempts.append(
                {
                    "attempt": 2,
                    "purpose": "semantic_review_format_repair",
                    "review": semantic_review,
                    "format_errors": semantic_review_errors,
                }
            )

        raw_semantic_review = semantic_review
        review_reconciliation = {
            "applied": False,
            "policy": "not_applied_due_to_invalid_semantic_review_format" if semantic_review_errors else "no_reconciliation_needed",
            "downgraded_to_non_blocking_observations": [],
            "remaining_blocking_unsupported_items": [],
        }
        if not semantic_review_errors:
            semantic_review, review_reconciliation = reconcile_semantic_review(raw_semantic_review)
            semantic_review_errors = validate_semantic_review(semantic_review)
            if review_reconciliation.get("applied"):
                print(f"[{run_id}]       規則式校正：assumptions/uncertainties 之非阻擋揭露不作為否決理由")

        print(f"[{run_id}] [4/5] Embedding 向量相似度輔助警示")
        similarity, embedding_error = compute_embedding_similarity(raw_requirement, intent_spec, embedding_model)
        validation_summary = decide_pre_agent_validation(
            intent_spec_errors=intent_errors,
            semantic_review_errors=semantic_review_errors,
            semantic_review=semantic_review,
            embedding_similarity=similarity,
            embedding_warning_threshold=EMBEDDING_WARNING_THRESHOLD,
        )

        report = {
            "run_id": run_id,
            "task_label": task_label,
            "task_label_note": "由模型 A 根據原始題目整理，僅供紀錄追蹤，不構成功能需求。",
            "models": {
                "intent_structuring_model": structuring_model,
                "semantic_review_model": review_model,
                "embedding_model": embedding_model,
            },
            "intent_spec_structure_check": {
                "passed": not intent_errors,
                "errors": intent_errors,
            },
            "semantic_review_raw": raw_semantic_review,
            "semantic_review": semantic_review,
            "semantic_review_rule_reconciliation": review_reconciliation,
            "semantic_review_attempts": semantic_review_attempts,
            "semantic_review_format_repair_used": format_repair_used,
            "semantic_review_format_check": {
                "passed": not semantic_review_errors,
                "errors": semantic_review_errors,
            },
            "embedding_similarity": {
                "value": similarity,
                "error": embedding_error,
                "role": "auxiliary_warning_only",
                "warning_threshold": EMBEDDING_WARNING_THRESHOLD,
            },
            "pre_agent_validation": validation_summary,
        }
        save_json(report, run_dir / "intent_alignment_report.json")

        print(f"[{run_id}] [5/5] 前置驗收通過時凍結驗收規格")
        frozen_result: dict[str, Any]
        if validation_summary["ready_for_agent"]:
            normalized_for_freeze = dict(normalized_requirement)
            normalized_for_freeze["task_name"] = task_label
            normalized_for_freeze["task_name_source"] = "model_a_tracking_label_only"
            frozen_spec = build_frozen_acceptance_spec(
                normalized_requirement=normalized_for_freeze,
                intent_spec=intent_spec,
                validation_summary=validation_summary,
                model_configuration=report["models"],
            )
            frozen_result = freeze_acceptance_spec(run_dir / "frozen_acceptance_spec.json", frozen_spec)
        else:
            frozen_result = {
                "created": False,
                "status": "not_frozen_due_to_review",
                "path": "",
                "message": "前置驗收未通過，因此未建立凍結規格。",
            }
        report["frozen_spec"] = frozen_result
        save_json(report, run_dir / "intent_alignment_report.json")

        _finish_run(
            output_root, run_dir, run_id,
            {
                "status": "completed",
                "task_label": task_label,
                "decision": validation_summary["decision"],
                "ready_for_agent": validation_summary["ready_for_agent"],
                "outputs": {
                    "normalized_requirement": str(run_dir / "normalized_requirement.json"),
                    "intent_spec": str(run_dir / "intent_spec.json"),
                    "intent_alignment_report": str(run_dir / "intent_alignment_report.json"),
                    "frozen_acceptance_spec": frozen_result.get("path", ""),
                },
            },
        )
        return {
            "run_id": run_id,
            "task_label": task_label,
            "run_directory": str(run_dir),
            "report": report,
            "frozen_spec": frozen_result,
        }
    except Exception as exc:
        _finish_run(
            output_root, run_dir, run_id,
            {"status": "error", "error": f"{type(exc).__name__}: {exc}"},
        )
        raise


# ============================================================
# 終端機執行入口
# ============================================================

def read_multiline_requirement() -> str:
    print("請貼上需求內容；輸入完成後，請在新的一行單獨輸入 END：")
    lines: list[str] = []
    while True:
        line = input()
        if line.strip() == "END":
            break
        lines.append(line)
    return "\n".join(lines).strip()


def main() -> None:
    parser = argparse.ArgumentParser(description="Intent Structuring：前置驗收規格建立")
    parser.add_argument("--requirement-file", default="", help="從文字檔讀取完整需求；避免原題內含 END 時被截斷。")
    parser.add_argument("--structuring-model", default=DEFAULT_STRUCTURING_MODEL)
    parser.add_argument("--review-model", default=DEFAULT_REVIEW_MODEL)
    parser.add_argument("--embedding-model", default=DEFAULT_EMBEDDING_MODEL)
    args = parser.parse_args()

    print("========== Intent Structuring：前置驗收規格建立 ==========")
    print("本流程只處理：題目 → 可解釋 JSON → 獨立語意審查 → 凍結規格")
    print("每次執行會自動產生 RUN 編號並保存原始輸入內容。\n")

    if args.requirement_file:
        raw_requirement = Path(args.requirement_file).read_text(encoding="utf-8", errors="replace")
        raw_requirement = sanitize_requirement_text(raw_requirement)
        structuring_model = args.structuring_model
        review_model = args.review_model
        embedding_model = args.embedding_model
        print(f"[INFO] 使用 requirement file: {args.requirement_file}")
    else:
        raw_requirement = sanitize_requirement_text(read_multiline_requirement())
        structuring_model = input(f"模型 A／意圖結構化模型（Enter 使用 {DEFAULT_STRUCTURING_MODEL}）：").strip() or DEFAULT_STRUCTURING_MODEL
        review_model = input(f"模型 B／獨立審查模型（Enter 使用 {DEFAULT_REVIEW_MODEL}）：").strip() or DEFAULT_REVIEW_MODEL
        embedding_model = input(f"Embedding 模型（Enter 使用 {DEFAULT_EMBEDDING_MODEL}）：").strip() or DEFAULT_EMBEDDING_MODEL

    result = run_intent_structuring(
        raw_requirement=raw_requirement,
        structuring_model=structuring_model,
        review_model=review_model,
        embedding_model=embedding_model,
    )
    report = result["report"]
    decision = report["pre_agent_validation"]

    print("\n========== 本次執行完成 ==========")
    print(f"Run ID:          {result['run_id']}")
    print(f"Task Label:      {result['task_label']}（模型整理，僅供追蹤）")
    print(f"Output Folder:   {result['run_directory']}")
    print(f"Decision:        {decision['decision']}")
    print(f"Ready for Agent: {decision['ready_for_agent']}")
    print(f"Frozen Spec:     {result['frozen_spec'].get('status')}")
    print(f"History:         {OUTPUT_ROOT / HISTORY_FILENAME}")


if __name__ == "__main__":
    main()
