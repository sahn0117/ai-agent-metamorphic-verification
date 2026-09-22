# -*- coding: utf-8 -*-
"""
skill_code_generation.py
========================
Stage 2：依據 Stage 1 已凍結驗收規格，限制 AI Agent 產生程式碼。

這支 skill 的設計邊界：
- 只讀取 stage1_outputs/RUN-xxxxxx/frozen_acceptance_spec.json。
- 只寫入 workspace/code_generation/RUN-xxxxxx/GEN-xxxxxx/。
- 不允許 Agent 提供任意檔案路徑。
- 不執行生成程式，只在 Docker 沙盒內以 compile() 做語法解析。
- Docker 唯讀掛載整個 Stage 2 workspace，因此容器不會改動主機輸出檔。

此版本沿用先前專案「一個工作區共用一個持久化 Docker 容器」的概念：
- 全部 Stage 2 RUN / GEN 共用單一 mcp_sbx_codegeneration 容器。
- 每輪輸出仍以 RUN-ID / GEN-ID 資料夾分離，不互相覆蓋。
- 容器僅在語法解析時啟動，完成後立即停止，避免閒置常駐。
- 拿掉 --privileged 與 root exec 等高權限設定。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STAGE1_OUTPUT_ROOT = PROJECT_ROOT / "stage1_outputs"
WORKSPACE_ROOT = PROJECT_ROOT / "workspace" / "code_generation"

_RUN_ID_PATTERN = re.compile(r"^RUN-\d{6}$")
_GEN_ID_PATTERN = re.compile(r"^GEN-\d{6}$")
_CONTAINER_NAME = "mcp_sbx_codegeneration"



def _scope_safe_codegen_fallback_allowed_v1(scope_or_obj):
    """
    允許 main.py 建立的 scope-safe fallback frozen spec 通過 Stage 2 gate。

    傳入值可以是：
    - locals()
    - frozen_spec dict
    - 其他包含 frozen spec 的 dict
    """
    def _is_allowed(obj):
        if not isinstance(obj, dict):
            return False

        marker = obj.get("stage1_fallback_freeze")
        if isinstance(marker, dict) and marker.get("enabled") is True:
            return obj.get("spec_status") == "frozen_pre_agent_acceptance_spec"

        return False

    if _is_allowed(scope_or_obj):
        return True

    if isinstance(scope_or_obj, dict):
        for key in (
            "frozen_spec",
            "spec",
            "data",
            "payload",
            "result",
            "prepared_data",
        ):
            value = scope_or_obj.get(key)
            if _is_allowed(value):
                return True

        for key in (
            "frozen_spec",
            "spec",
            "pre_agent_acceptance_spec",
            "acceptance_spec",
        ):
            value = scope_or_obj.get(key)
            if _is_allowed(value):
                return True

    return False


def _json_text(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def _save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_json_text(data), encoding="utf-8")


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _validated_run_id(source_run_id: str) -> str:
    value = (source_run_id or "").strip().upper()
    if not _RUN_ID_PATTERN.fullmatch(value):
        raise ValueError("RUN-ID 格式錯誤，必須例如 RUN-000007。")
    return value


def _validated_generation_id(generation_id: str) -> str:
    value = (generation_id or "").strip().upper()
    if not _GEN_ID_PATTERN.fullmatch(value):
        raise ValueError("GEN-ID 格式錯誤，必須例如 GEN-000001。")
    return value


def _source_spec_path(source_run_id: str) -> Path:
    run_id = _validated_run_id(source_run_id)
    return STAGE1_OUTPUT_ROOT / run_id / "frozen_acceptance_spec.json"


def _run_workspace(source_run_id: str) -> Path:
    run_id = _validated_run_id(source_run_id)
    path = WORKSPACE_ROOT / run_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def _generation_dir(source_run_id: str, generation_id: str) -> Path:
    run_dir = _run_workspace(source_run_id)
    gen_id = _validated_generation_id(generation_id)
    path = run_dir / gen_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def _next_generation_id(source_run_id: str) -> str:
    run_dir = _run_workspace(source_run_id)
    ids: list[int] = []
    for item in run_dir.iterdir():
        if item.is_dir() and _GEN_ID_PATTERN.fullmatch(item.name):
            ids.append(int(item.name.split("-")[-1]))
    number = (max(ids) + 1) if ids else 1
    return f"GEN-{number:06d}"


def _container_name() -> str:
    return _CONTAINER_NAME


def _run_subprocess(cmd: list[str], timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def _container_exists(container: str) -> bool:
    try:
        check = _run_subprocess(
            ["docker", "ps", "-a", "--filter", f"name=^/{container}$", "--format", "{{.Names}}"],
            timeout=20,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("找不到 docker 指令，請先啟動 Docker Desktop。") from exc
    if check.returncode != 0:
        raise RuntimeError(f"無法查詢 Docker 狀態：{check.stderr.strip()}")
    return container in check.stdout


def _container_is_running(container: str) -> bool:
    state = _run_subprocess(["docker", "inspect", "--format", "{{.State.Running}}", container])
    return state.returncode == 0 and "true" in state.stdout.lower()


def _remove_container_if_exists(container: str) -> None:
    if not _container_exists(container):
        return
    removed = _run_subprocess(["docker", "rm", "-f", container], timeout=30)
    if removed.returncode != 0:
        raise RuntimeError(f"failed to remove stale Docker container {container}: {removed.stderr.strip()}")


def stop_readonly_sandbox() -> dict[str, Any]:
    """停止共享沙盒；不刪除容器與主機輸出資料。"""
    container = _container_name()
    if not _container_exists(container):
        return {"container_name": container, "action": "not_created"}
    if not _container_is_running(container):
        return {"container_name": container, "action": "already_stopped"}
    stopped = _run_subprocess(["docker", "stop", "-t", "1", container], timeout=20)
    if stopped.returncode != 0:
        raise RuntimeError(f"停止 Docker 沙盒失敗：{stopped.stderr.strip()}")
    return {"container_name": container, "action": "stopped"}


def ensure_readonly_sandbox(start_container: bool = False) -> dict[str, Any]:
    """建立 Stage 2 共用的唯讀 Docker 沙盒；需要檢查語法時才啟動。

    全部 RUN / GEN 共享同一個容器，輸出仍由資料夾切開保存。
    若先前容器仍處於執行中，準備新一輪時會將其停止；語法解析完成後亦會停止。
    """
    host_workspace = WORKSPACE_ROOT.resolve()
    host_workspace.mkdir(parents=True, exist_ok=True)
    container = _container_name()
    action = "existing_stopped"
    # Avoid reusing a stale Stage 2 container with an old bind mount.
    _remove_container_if_exists(container)

    if not _container_exists(container):
        create = _run_subprocess(
            [
                "docker", "create", "--name", container,
                "--network", "none",
                "--cap-drop", "ALL",
                "--security-opt", "no-new-privileges:true",
                "--read-only",
                "--tmpfs", "/tmp:rw,noexec,nosuid,size=32m",
                "--memory", "512m",
                "--cpus", "1.0",
                "--pids-limit", "64",
                "--user", "65534:65534",
                "-v", f"{host_workspace}:/app:ro",
                "-w", "/app",
                "python:3.11-slim",
                "tail", "-f", "/dev/null",
            ],
            timeout=120,
        )
        if create.returncode != 0:
            raise RuntimeError(f"建立 Docker 沙盒失敗：{create.stderr.strip()}")
        action = "created_stopped"
    elif _container_is_running(container) and not start_container:
        stop_readonly_sandbox()
        action = "reused_and_stopped"

    if start_container and not _container_is_running(container):
        start = _run_subprocess(["docker", "start", container], timeout=30)
        if start.returncode != 0:
            raise RuntimeError(f"啟動 Docker 沙盒失敗：{start.stderr.strip()}")
        action = "started_for_check"

    return {
        "container_name": container,
        "action": action,
        "sandbox_scope": "shared_for_stage2",
        "mounted_host_workspace": str(host_workspace),
        "mounted_container_workspace": "/app",
        "mount_mode": "read_only",
        "network_mode": "none",
        "lifecycle": "start_only_for_syntax_check_then_stop",
    }


def read_frozen_spec(source_run_id: str) -> str:
    """唯讀取得 Stage 1 已通過的凍結規格。"""
    try:
        run_id = _validated_run_id(source_run_id)
        path = _source_spec_path(run_id)
        if not path.exists():
            return _json_text({"ok": False, "error": f"找不到凍結規格：{path}"})

        spec = _read_json(path)
        validation = spec.get("pre_agent_validation", {})
        # Stage 1 的正式交接旗標是 ready_for_agent。
        # decision 可能是 pass、pass_with_embedding_warning 等可放行狀態；
        # 不應因為 decision 不是字面上的 pass，就誤擋已核准交給 Agent 的規格。
        if validation.get("ready_for_agent") is not True and not _scope_safe_codegen_fallback_allowed_v1(locals()):
            return _json_text({
                "ok": False,
                "error": "此 Stage 1 規格尚未通過前置驗收，不可交給程式碼生成 Agent。",
                "pre_agent_validation": validation,
            })

        return _json_text({"ok": True, "source_run_id": run_id, "frozen_spec": spec})
    except Exception as exc:
        return _json_text({"ok": False, "error": str(exc)})


def prepare_generation(source_run_id: str) -> str:
    """建立本次生成資料夾、保存 frozen spec 快照，並建立 Docker 沙盒。"""
    try:
        run_id = _validated_run_id(source_run_id)
        source_response = json.loads(read_frozen_spec(run_id))
        if not source_response.get("ok"):
            return _json_text(source_response)

        generation_id = _next_generation_id(run_id)
        output_dir = _generation_dir(run_id, generation_id)
        frozen_spec = source_response["frozen_spec"]

        generation_input = {
            "source_run_id": run_id,
            "generation_id": generation_id,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "input_type": "frozen_pre_agent_acceptance_spec_snapshot",
            "source_spec_path": str(_source_spec_path(run_id)),
            "frozen_spec": frozen_spec,
            "notice": "此為生成階段使用之唯讀快照；Stage 1 原始凍結規格不在 Docker 掛載資料夾中。",
        }
        _save_json(output_dir / "generation_input.json", generation_input)

        sandbox = ensure_readonly_sandbox(start_container=False)
        return _json_text({
            "ok": True,
            "source_run_id": run_id,
            "generation_id": generation_id,
            "output_directory": str(output_dir),
            "sandbox": sandbox,
        })
    except Exception as exc:
        return _json_text({"ok": False, "error": str(exc)})


def write_generated_solution(source_run_id: str, generation_id: str, python_code: str) -> str:
    """只允許寫入固定檔名 generated_solution.py。"""
    try:
        run_id = _validated_run_id(source_run_id)
        gen_id = _validated_generation_id(generation_id)
        code = (python_code or "").strip()
        if not code:
            return _json_text({"ok": False, "error": "python_code 不可為空。"})
        output_path = _generation_dir(run_id, gen_id) / "generated_solution.py"
        output_path.write_text(code + "\n", encoding="utf-8")
        return _json_text({
            "ok": True,
            "written_file": str(output_path),
            "sha256": hashlib.sha256(code.encode("utf-8")).hexdigest(),
            "char_count": len(code),
        })
    except Exception as exc:
        return _json_text({"ok": False, "error": str(exc)})


def write_implementation_manifest(
    source_run_id: str,
    generation_id: str,
    implementation_decisions: dict[str, Any],
) -> str:
    """保存 Agent 的實作選擇；這些選擇不會回寫 Stage 1 凍結規格。"""
    try:
        run_id = _validated_run_id(source_run_id)
        gen_id = _validated_generation_id(generation_id)
        if not isinstance(implementation_decisions, dict):
            return _json_text({"ok": False, "error": "implementation_decisions 必須是 JSON object。"})

        manifest = {
            "source_run_id": run_id,
            "generation_id": gen_id,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "generated_file": "generated_solution.py",
            "implementation_decisions": implementation_decisions,
            "rule": "此檔僅記錄 Agent 的實作選擇，不修改 frozen_acceptance_spec.json。",
        }
        output_path = _generation_dir(run_id, gen_id) / "implementation_manifest.json"
        _save_json(output_path, manifest)
        return _json_text({"ok": True, "written_file": str(output_path)})
    except Exception as exc:
        return _json_text({"ok": False, "error": str(exc)})


def check_generated_python_syntax(source_run_id: str, generation_id: str) -> str:
    """在唯讀 Docker 沙盒內解析 Python 語法，不執行生成程式。"""
    try:
        run_id = _validated_run_id(source_run_id)
        gen_id = _validated_generation_id(generation_id)
        output_dir = _generation_dir(run_id, gen_id)
        solution = output_dir / "generated_solution.py"
        if not solution.exists():
            return _json_text({"ok": False, "error": "generated_solution.py 尚未建立。"})

        sandbox = ensure_readonly_sandbox(start_container=True)
        container = sandbox["container_name"]
        relative_file = f"/app/{run_id}/{gen_id}/generated_solution.py"
        check_script = (
            "from pathlib import Path; "
            f"p=Path({relative_file!r}); "
            "compile(p.read_text(encoding='utf-8'), str(p), 'exec'); "
            "print('SYNTAX_OK')"
        )
        try:
            result = _run_subprocess(["docker", "exec", container, "python", "-c", check_script], timeout=30)
        finally:
            stop_readonly_sandbox()
        passed = result.returncode == 0 and "SYNTAX_OK" in result.stdout
        check_record = {
            "source_run_id": run_id,
            "generation_id": gen_id,
            "checked_at": datetime.now().isoformat(timespec="seconds"),
            "check_type": "docker_compile_only_no_runtime_execution",
            "passed": passed,
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
            "container_name": container,
            "container_stopped_after_check": True,
        }
        _save_json(output_dir / "syntax_check.json", check_record)
        return _json_text({"ok": passed, "syntax_check": check_record})
    except Exception as exc:
        return _json_text({"ok": False, "error": str(exc)})


def finalize_generation(source_run_id: str, generation_id: str, agent_summary: str) -> str:
    """完成本次生成；僅在程式碼、manifest 與語法解析皆具備時建立報告。"""
    try:
        run_id = _validated_run_id(source_run_id)
        gen_id = _validated_generation_id(generation_id)
        output_dir = _generation_dir(run_id, gen_id)
        required = ["generation_input.json", "generated_solution.py", "implementation_manifest.json", "syntax_check.json"]
        missing = [name for name in required if not (output_dir / name).exists()]
        if missing:
            return _json_text({"ok": False, "error": f"尚缺少檔案：{missing}"})
        syntax_record = _read_json(output_dir / "syntax_check.json")
        if syntax_record.get("passed") is not True:
            return _json_text({"ok": False, "error": "Python 語法解析未通過，暫不可完成本輪生成。"})

        report = {
            "source_run_id": run_id,
            "generation_id": gen_id,
            "finished_at": datetime.now().isoformat(timespec="seconds"),
            "status": "generated_syntax_passed_semantics_not_yet_verified",
            "generated_file": str(output_dir / "generated_solution.py"),
            "implementation_manifest": str(output_dir / "implementation_manifest.json"),
            "syntax_check": syntax_record,
            "agent_summary": (agent_summary or "").strip(),
            "next_stage_notice": "本輪只完成生成與語法解析；功能一致性、行為抽取與蛻變測試尚未執行。",
        }
        _save_json(output_dir / "generation_report.json", report)
        return _json_text({"ok": True, "generation_report": report})
    except Exception as exc:
        return _json_text({"ok": False, "error": str(exc)})
