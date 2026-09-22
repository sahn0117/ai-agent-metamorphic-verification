# -*- coding: utf-8 -*-
"""
code_generation_agent.py
========================
Stage 2 主程式：讀取 Stage 1 RUN-ID，讓生成 Agent 依 frozen spec 產生程式碼。

v2 hardening 重點：
- 將最大工具迴圈步數由 12 提高到 24。
- 若模型已寫出 generated_solution.py、manifest、syntax_check，但忘記 finalize，主控會自動 finalize。
- 若模型已寫出 generated_solution.py 但忘記 manifest / syntax_check，主控會在後段補上最小 manifest、執行語法檢查並 finalize。
- 若模型一直不呼叫工具、只把程式碼貼在文字回覆中，主控會嘗試擷取程式碼並寫入工具。
- 若工具型 Agent 仍未完成，最後會啟動 direct codegen fallback：用同一模型直接產生單一 Python 檔，再走原本工具寫入、語法檢查、finalize。

注意：
- 本檔只控制 Stage 2 生成流程。
- 不修改 frozen_acceptance_spec.json。
- 不做 Stage 3/4/5/6/7 驗證。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

import ollama
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parent
SERVER_SCRIPT = ROOT / "stage2_server.py"
DEFAULT_MODEL = os.environ.get("GENERATION_MODEL", "gemma4:31b")
MAX_AGENT_STEPS = 24
AUTO_FINALIZE_AFTER_STEP = 8


def _tool_result_to_text(result: Any) -> str:
    contents = getattr(result, "content", None) or []
    parts: list[str] = []
    for item in contents:
        text = getattr(item, "text", None)
        if text:
            parts.append(text)
    return "\n".join(parts) if parts else str(result)


def _safe_json_loads(text: str) -> dict[str, Any]:
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else {"ok": False, "raw": data}
    except Exception:
        return {"ok": False, "raw_text": text}


def _message_to_dict(message: Any) -> dict[str, Any]:
    if isinstance(message, dict):
        return message
    if hasattr(message, "model_dump"):
        return message.model_dump(exclude_none=True)
    if hasattr(message, "dict"):
        return message.dict(exclude_none=True)
    raise TypeError(f"無法解析 Ollama message 型別：{type(message)!r}")


def _response_message(response: Any) -> dict[str, Any]:
    if isinstance(response, dict):
        return _message_to_dict(response.get("message", {}))
    return _message_to_dict(response.message)


def _mcp_tools_to_ollama(tools: list[Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for tool in tools:
        schema = getattr(tool, "inputSchema", None) or getattr(tool, "input_schema", None) or {"type": "object", "properties": {}}
        result.append({
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description or "",
                "parameters": schema,
            },
        })
    return result


def _extract_call(call: Any) -> tuple[str, dict[str, Any]]:
    if isinstance(call, dict):
        fn = call.get("function", {})
        name = fn.get("name", "")
        args = fn.get("arguments", {})
    else:
        fn = call.function
        name = fn.name
        args = fn.arguments
    if isinstance(args, str):
        args = json.loads(args) if args.strip() else {}
    return name, dict(args or {})


def _generation_dir(source_run_id: str, generation_id: str) -> Path:
    return ROOT / "workspace" / "code_generation" / source_run_id / generation_id


def _extract_python_code(text: str) -> str | None:
    """從模型文字回覆擷取 Python 程式碼，支援 markdown code fence 與純文字程式碼。"""
    if not text:
        return None

    # 優先抓 ```python ... ```
    m = re.search(r"```(?:python|py)\s*(.*?)```", text, flags=re.IGNORECASE | re.DOTALL)
    if not m:
        # 次優先抓任何 code fence
        m = re.search(r"```\s*(.*?)```", text, flags=re.DOTALL)
    code = (m.group(1) if m else text).strip()

    # 移除常見前後雜訊
    code = re.sub(r"^python\s*\n", "", code, flags=re.IGNORECASE).strip()

    # 粗略確認像 Python 程式，不要把中文說明寫成 generated_solution.py
    markers = ("def solve", "class ", "from ", "import ")
    if not any(marker in code for marker in markers):
        return None
    return code


def _compact_json(data: Any, limit: int = 28000) -> str:
    text = json.dumps(data, ensure_ascii=False, indent=2)
    if len(text) <= limit:
        return text
    return text[:limit] + "\n...（內容過長，已截斷；請仍以可見 frozen spec 與原始需求為準）"


def _system_prompt(run_id: str, generation_id: str) -> str:
    return f"""你是 Stage 2 程式碼生成 Agent。你只負責依據已凍結的驗收規格產生 Python 程式碼。

本輪識別資訊：
- source_run_id: {run_id}
- generation_id: {generation_id}

你必須依序完成：
1. 呼叫 read_frozen_spec 取得不可修改的驗收規格。
2. 根據 source_requirement、core_objectives、constraints、behavior_spec 生成單一 Python 檔案內容。
3. 呼叫 write_generated_solution 寫入程式碼。
4. 呼叫 write_implementation_manifest 記錄你自行選擇的方法名稱、狀態欄位、初始化方式與錯誤處理方式。
5. 呼叫 check_generated_python_syntax；若語法未通過，修正程式碼後重新檢查。
6. 語法通過後呼叫 finalize_generation。

嚴格規則：
- 不可修改或重新定義 frozen_acceptance_spec.json。
- 規格要求 class 題型時，必須保留公開 class 與公開方法。
- 規格要求 original IO 題型時，必須提供 def solve(input_text: str) -> str。
- 不可新增外部 API、網路、資料庫、檔案讀寫、隨機性。
- 本階段不撰寫測試、不宣稱功能已驗證、不進行蛻變測試。
- 不要只在文字回覆中貼程式碼；必須呼叫工具寫入檔案並完成 finalize_generation。
"""


async def _call_tool_json(
    session: ClientSession,
    tool_name: str,
    args: dict[str, Any],
    source_run_id: str,
    generation_id: str | None = None,
) -> dict[str, Any]:
    safe_args = dict(args or {})
    safe_args["source_run_id"] = source_run_id
    if generation_id is not None and tool_name != "read_frozen_spec":
        safe_args["generation_id"] = generation_id
    result = await session.call_tool(tool_name, safe_args)
    text = _tool_result_to_text(result)
    return _safe_json_loads(text)


async def _try_controller_finalize(
    session: ClientSession,
    source_run_id: str,
    generation_id: str,
    *,
    allow_auto_manifest: bool,
    reason: str,
) -> bool:
    """模型卡住時，由主控補齊非語意性的收尾動作。"""
    gen_dir = _generation_dir(source_run_id, generation_id)
    solution = gen_dir / "generated_solution.py"
    manifest = gen_dir / "implementation_manifest.json"
    syntax = gen_dir / "syntax_check.json"
    report = gen_dir / "generation_report.json"

    if report.exists():
        return True
    if not solution.exists():
        return False

    if not manifest.exists():
        if not allow_auto_manifest:
            return False
        auto_manifest = {
            "auto_completed_by_controller": True,
            "reason": reason,
            "note": "模型已寫出 generated_solution.py，但未完成 implementation_manifest；主控補最小紀錄以避免 Stage 2 卡死。",
            "public_interface": "由 generated_solution.py 與後續 Stage 3 AST 抽取確認。",
            "semantic_claim": "none; Stage 2 only performs syntax readiness.",
        }
        data = await _call_tool_json(
            session,
            "write_implementation_manifest",
            {"implementation_decisions": auto_manifest},
            source_run_id,
            generation_id,
        )
        if not data.get("ok"):
            print(f"[AUTO-FINALIZE] manifest 補寫失敗：{data.get('error') or data}")
            return False
        print("[AUTO-FINALIZE] 已補寫最小 implementation_manifest.json")

    # 沒有語法紀錄，或語法紀錄不是 passed，就重跑語法檢查。
    need_syntax = True
    if syntax.exists():
        try:
            syntax_data = json.loads(syntax.read_text(encoding="utf-8"))
            need_syntax = syntax_data.get("passed") is not True
        except Exception:
            need_syntax = True
    if need_syntax:
        data = await _call_tool_json(session, "check_generated_python_syntax", {}, source_run_id, generation_id)
        if not data.get("ok"):
            print(f"[AUTO-FINALIZE] 語法檢查未通過：{data.get('error') or data}")
            return False
        print("[AUTO-FINALIZE] 語法檢查通過")

    final = await _call_tool_json(
        session,
        "finalize_generation",
        {"agent_summary": f"Controller auto-finalized because: {reason}"},
        source_run_id,
        generation_id,
    )
    if final.get("ok"):
        generation_report = final.get("generation_report", {})
        print("\n========== 生成完成（主控自動收尾） ==========")
        print(f"Status: {generation_report.get('status')}")
        print(f"File:   {generation_report.get('generated_file')}")
        print("目前只完成生成與語法解析，尚未驗證功能一致性。")
        return True

    print(f"[AUTO-FINALIZE] finalize_generation 失敗：{final.get('error') or final}")
    return False


async def _try_write_text_code_response(
    session: ClientSession,
    source_run_id: str,
    generation_id: str,
    message: dict[str, Any],
) -> bool:
    content = str(message.get("content") or "")
    code = _extract_python_code(content)
    if not code:
        return False
    print("[TEXT-CODE-FALLBACK] 偵測到模型把程式碼貼在文字回覆中，改由主控寫入 generated_solution.py")
    data = await _call_tool_json(
        session,
        "write_generated_solution",
        {"python_code": code},
        source_run_id,
        generation_id,
    )
    if not data.get("ok"):
        print(f"[TEXT-CODE-FALLBACK] 寫入失敗：{data.get('error') or data}")
        return False
    return await _try_controller_finalize(
        session,
        source_run_id,
        generation_id,
        allow_auto_manifest=True,
        reason="model returned code as text instead of tool call",
    )


async def _direct_codegen_fallback(
    session: ClientSession,
    source_run_id: str,
    generation_id: str,
    model: str,
) -> bool:
    """工具型 agent 卡死時，最後改用直接生成程式碼，再走原本工具落檔。"""
    print("\n[DIRECT-FALLBACK] 工具型 Agent 未完成，啟動直接生成 fallback。")
    spec_data = await _call_tool_json(session, "read_frozen_spec", {}, source_run_id, None)
    if not spec_data.get("ok"):
        print(f"[DIRECT-FALLBACK] 讀取 frozen spec 失敗：{spec_data.get('error') or spec_data}")
        return False

    frozen_spec = spec_data.get("frozen_spec", {})
    prompt = f"""你要依照下列 frozen spec 產生單一 Python 檔案。

要求：
1. 只輸出 Python 程式碼，不要 markdown，不要解釋。
2. 若是原始 IO 題，必須提供 def solve(input_text: str) -> str。
3. 若是 class 題，必須保留題目要求的 class 與公開方法。
4. 不得使用 input() 作為核心邏輯，不得依賴 print() 作為唯一輸出。
5. 不得讀寫檔案、不得使用網路、外部 API、資料庫、隨機性。
6. 程式必須 self-contained、deterministic、local run。

Frozen spec:
{_compact_json(frozen_spec)}
"""

    response = ollama.chat(
        model=model,
        messages=[
            {"role": "system", "content": "你是嚴格的 Python 程式碼生成器，只輸出可執行 Python 程式碼。"},
            {"role": "user", "content": prompt},
        ],
        options={"temperature": 0.15, "num_ctx": 32768},
    )
    message = _response_message(response)
    code = _extract_python_code(str(message.get("content") or ""))
    if not code:
        print("[DIRECT-FALLBACK] 沒有從模型回覆擷取到 Python 程式碼。")
        return False

    write = await _call_tool_json(
        session,
        "write_generated_solution",
        {"python_code": code},
        source_run_id,
        generation_id,
    )
    if not write.get("ok"):
        print(f"[DIRECT-FALLBACK] write_generated_solution 失敗：{write.get('error') or write}")
        return False

    manifest = {
        "generated_by": "direct_codegen_fallback",
        "reason": "tool-agent did not finish within guarded steps",
        "public_interface": "preserved according to frozen spec; verified only by syntax at Stage 2",
        "semantic_claim": "none; Stage 2 only performs generation and syntax check",
    }
    man = await _call_tool_json(
        session,
        "write_implementation_manifest",
        {"implementation_decisions": manifest},
        source_run_id,
        generation_id,
    )
    if not man.get("ok"):
        print(f"[DIRECT-FALLBACK] write_implementation_manifest 失敗：{man.get('error') or man}")
        return False

    # 若第一次語法不過，讓模型修一次。
    syntax = await _call_tool_json(session, "check_generated_python_syntax", {}, source_run_id, generation_id)
    if not syntax.get("ok"):
        syntax_text = json.dumps(syntax, ensure_ascii=False)[:4000]
        print("[DIRECT-FALLBACK] 第一次語法未通過，嘗試要求模型修正一次。")
        repair_prompt = f"""以下 Python 程式語法檢查失敗，請只輸出修正後完整 Python 程式碼，不要 markdown。

語法錯誤：
{syntax_text}

原程式：
{code}
"""
        response2 = ollama.chat(
            model=model,
            messages=[
                {"role": "system", "content": "你是 Python 語法修復器，只輸出修正後完整 Python 程式碼。"},
                {"role": "user", "content": repair_prompt},
            ],
            options={"temperature": 0.1, "num_ctx": 32768},
        )
        msg2 = _response_message(response2)
        fixed_code = _extract_python_code(str(msg2.get("content") or ""))
        if not fixed_code:
            return False
        write2 = await _call_tool_json(
            session,
            "write_generated_solution",
            {"python_code": fixed_code},
            source_run_id,
            generation_id,
        )
        if not write2.get("ok"):
            return False
        syntax = await _call_tool_json(session, "check_generated_python_syntax", {}, source_run_id, generation_id)
        if not syntax.get("ok"):
            print(f"[DIRECT-FALLBACK] 修正後語法仍未通過：{syntax.get('error') or syntax}")
            return False

    return await _try_controller_finalize(
        session,
        source_run_id,
        generation_id,
        allow_auto_manifest=True,
        reason="direct_codegen_fallback completed",
    )


async def run_agent(source_run_id: str, model: str) -> None:
    params = StdioServerParameters(command=sys.executable, args=[str(SERVER_SCRIPT)])
    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()

            prepared = await session.call_tool("prepare_generation", {"source_run_id": source_run_id})
            prepared_data = json.loads(_tool_result_to_text(prepared))
            if not prepared_data.get("ok"):
                raise RuntimeError(prepared_data.get("error", "無法建立生成工作區"))
            generation_id = prepared_data["generation_id"]

            print("\n========== Stage 2：Frozen Spec → Code Generation Agent ==========")
            print(f"Source RUN-ID: {source_run_id}")
            print(f"Generation ID: {generation_id}")
            print(f"Model:         {model}")
            print(f"Workspace:     {prepared_data['output_directory']}")
            print(f"Docker:        {prepared_data['sandbox']['container_name']} ({prepared_data['sandbox']['action']})")
            print("===============================================================\n")

            tools_response = await session.list_tools()
            agent_tool_names = {
                "read_frozen_spec",
                "write_generated_solution",
                "write_implementation_manifest",
                "check_generated_python_syntax",
                "finalize_generation",
            }
            visible_tools = [tool for tool in tools_response.tools if tool.name in agent_tool_names]
            ollama_tools = _mcp_tools_to_ollama(visible_tools)
            messages: list[dict[str, Any]] = [{"role": "system", "content": _system_prompt(source_run_id, generation_id)}]
            finalized = False
            no_tool_reply_count = 0

            allowed = {
                "read_frozen_spec",
                "write_generated_solution",
                "write_implementation_manifest",
                "check_generated_python_syntax",
                "finalize_generation",
            }

            for step in range(1, MAX_AGENT_STEPS + 1):
                response = ollama.chat(
                    model=model,
                    messages=messages,
                    tools=ollama_tools,
                    options={"temperature": 0.25, "num_ctx": 32768},
                )
                message = _response_message(response)
                messages.append(message)
                calls = message.get("tool_calls") or []

                if not calls:
                    no_tool_reply_count += 1
                    if await _try_write_text_code_response(session, source_run_id, generation_id, message):
                        return
                    if await _try_controller_finalize(
                        session,
                        source_run_id,
                        generation_id,
                        allow_auto_manifest=(step >= AUTO_FINALIZE_AFTER_STEP),
                        reason=f"no tool calls at step {step}",
                    ):
                        return
                    messages.append({
                        "role": "user",
                        "content": (
                            "尚未完成。請不要只回覆文字。請依序呼叫工具："
                            "read_frozen_spec → write_generated_solution → write_implementation_manifest "
                            "→ check_generated_python_syntax → finalize_generation。"
                        ),
                    })
                    # 連續多次不叫工具，提早進 fallback，避免浪費時間。
                    if no_tool_reply_count >= 4 and step >= 6:
                        break
                    continue

                no_tool_reply_count = 0
                for call in calls:
                    tool_name, args = _extract_call(call)
                    if tool_name not in allowed:
                        tool_text = json.dumps({"ok": False, "error": f"此階段不允許工具：{tool_name}"}, ensure_ascii=False)
                    else:
                        args["source_run_id"] = source_run_id
                        if tool_name != "read_frozen_spec":
                            args["generation_id"] = generation_id
                        tool_result = await session.call_tool(tool_name, args)
                        tool_text = _tool_result_to_text(tool_result)

                    print(f"[Step {step}] Tool: {tool_name}")
                    print(tool_text[:700] + ("..." if len(tool_text) > 700 else ""))
                    messages.append({"role": "tool", "tool_name": tool_name, "content": tool_text})

                    if tool_name == "finalize_generation":
                        final_data = _safe_json_loads(tool_text)
                        if final_data.get("ok"):
                            finalized = True
                            report = final_data["generation_report"]
                            print("\n========== 生成完成 ==========")
                            print(f"Status: {report['status']}")
                            print(f"File:   {report['generated_file']}")
                            print("目前只完成生成與語法解析，尚未驗證功能一致性。")
                            return

                # 一般情況：模型自己完成檔案與語法後，主控幫忙 finalize。
                if await _try_controller_finalize(
                    session,
                    source_run_id,
                    generation_id,
                    allow_auto_manifest=(step >= AUTO_FINALIZE_AFTER_STEP),
                    reason=f"agent did not finalize after tool step {step}",
                ):
                    return

            if not finalized:
                # 最後再試一次：如果已經有 solution，就補齊收尾；否則直接生成 fallback。
                if await _try_controller_finalize(
                    session,
                    source_run_id,
                    generation_id,
                    allow_auto_manifest=True,
                    reason=f"agent did not finish within {MAX_AGENT_STEPS} steps",
                ):
                    return
                if await _direct_codegen_fallback(session, source_run_id, generation_id, model):
                    return
                raise RuntimeError(f"Agent 在 {MAX_AGENT_STEPS} 步內未完成生成流程，且 direct fallback 也未成功。")


def main() -> None:
    print("========== Stage 2：依凍結規格生成程式碼 ==========")
    run_id = input("請輸入 Stage 1 RUN-ID（例如 RUN-000007）：").strip().upper()
    model = input(f"程式碼生成模型（Enter 使用 {DEFAULT_MODEL}）：").strip() or DEFAULT_MODEL
    asyncio.run(run_agent(run_id, model))


if __name__ == "__main__":
    main()
