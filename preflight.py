"""Read-only environment check for the thesis framework release."""
from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import subprocess
import urllib.request


REQUIRED_PACKAGES = ("ollama", "mcp")
REQUIRED_MODELS = ("gemma4:31b", "nemotron-cascade-2:30b", "bge-m3")


def command_version(command: list[str]) -> dict:
    executable = shutil.which(command[0])
    if not executable:
        return {"available": False, "detail": "not found on PATH"}
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=15)
        detail = (result.stdout or result.stderr).strip().splitlines()
        return {"available": result.returncode == 0, "detail": detail[0] if detail else "available"}
    except Exception as exc:
        return {"available": False, "detail": f"{type(exc).__name__}: {exc}"}


def ollama_models(base_url: str) -> dict:
    try:
        with urllib.request.urlopen(base_url.rstrip("/") + "/api/tags", timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
        names = sorted(item.get("name", "") for item in payload.get("models", []))
        missing = [name for name in REQUIRED_MODELS if name not in names]
        return {"available": True, "installed": names, "missing_defaults": missing}
    except Exception as exc:
        return {"available": False, "detail": f"{type(exc).__name__}: {exc}"}


def main() -> int:
    parser = argparse.ArgumentParser(description="Check external dependencies without running the framework")
    parser.add_argument("--ollama-base-url", default="http://localhost:11434")
    args = parser.parse_args()

    packages = {name: importlib.util.find_spec(name) is not None for name in REQUIRED_PACKAGES}
    report = {
        "python_packages": packages,
        "docker": command_version(["docker", "--version"]),
        "ollama_cli": command_version(["ollama", "--version"]),
        "ollama_service": ollama_models(args.ollama_base_url),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    ok = all(packages.values()) and report["docker"]["available"] and report["ollama_service"]["available"]
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
