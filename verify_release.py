"""Static, read-only integrity checks for the standalone release."""
from __future__ import annotations

import ast
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parent
REQUIRED = {
    "main.py",
    "main_pipeline.py",
    "intent_structuring.py",
    "code_generation_agent.py",
    "stage2_server.py",
    "behavior_extraction.py",
    "behavior_alignment.py",
    "verification_framework.py",
    "repair_feedback.py",
    "repair_generation.py",
    "skills/__init__.py",
    "skills/skill_planner_intent.py",
    "skills/skill_code_generation.py",
    "skills/skill_verification_framework.py",
    "skills/skill_repair_feedback.py",
    "examples/minimal_requirement.txt",
    "requirements.txt",
    "environment.yml",
    "README.md",
    "preflight.py",
    "verify_release.py",
    ".gitignore",
    "CITATION.cff",
    "docs/FRAMEWORK.md",
    "docs/RESULTS.md",
    "docs/REPRODUCIBILITY.md",
}
ALLOWED_LOCAL_MODULES = {Path(item).stem for item in REQUIRED if item.endswith(".py")}
ALLOWED_LOCAL_MODULES.add("skills")
FORBIDDEN_PATH_PATTERNS = (
    re.compile(r"(?<![A-Za-z])[A-Za-z]:[\\/]"),
    re.compile(r"/home/[^/]+/"),
    re.compile(r"/Users/[^/]+/"),
)

EXPECTED_MODEL_DEFAULTS = {
    "intent_structuring.py": ("gemma4:31b", "nemotron-cascade-2:30b", "bge-m3"),
    "code_generation_agent.py": ("gemma4:31b",),
    "repair_generation.py": ("gemma4:31b",),
    "preflight.py": ("gemma4:31b", "nemotron-cascade-2:30b", "bge-m3"),
}
TEXT_SUFFIXES = {".py", ".md", ".yml", ".yaml", ".txt", ".cff"}


def main() -> int:
    errors: list[str] = []
    warnings: list[str] = []

    missing = sorted(item for item in REQUIRED if not (ROOT / item).exists())
    if missing:
        errors.append("missing required files: " + ", ".join(missing))

    for path in sorted(ROOT.rglob("*.py")):
        relative = path.relative_to(ROOT).as_posix()
        source = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source, filename=relative)
        except SyntaxError as exc:
            errors.append(f"syntax error in {relative}: {exc}")
            continue

        if path.resolve() != Path(__file__).resolve():
            for pattern in FORBIDDEN_PATH_PATTERNS:
                if pattern.search(source):
                    errors.append(f"absolute user path pattern in {relative}")

        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                top = node.module.split(".", 1)[0]
                if top.startswith("mcp_paper"):
                    errors.append(f"external project import in {relative}: {node.module}")

    # Cross-file model consistency checks for the thesis-aligned reference release.
    for relative, expected_values in EXPECTED_MODEL_DEFAULTS.items():
        path = ROOT / relative
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        for value in expected_values:
            if value not in text:
                errors.append(f"expected model reference missing in {relative}: {value}")

    for path in sorted(ROOT.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        relative = path.relative_to(ROOT).as_posix()
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            errors.append(f"text file is not valid UTF-8: {relative}")
            continue

    # Verify local Markdown links so README/docs do not point at missing files.
    markdown_link = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
    for path in sorted(ROOT.rglob("*.md")):
        relative = path.relative_to(ROOT).as_posix()
        text = path.read_text(encoding="utf-8")
        for target in markdown_link.findall(text):
            target = target.strip()
            if not target or target.startswith(("http://", "https://", "#", "mailto:")):
                continue
            target_path = (path.parent / target.split("#", 1)[0]).resolve()
            try:
                target_path.relative_to(ROOT.resolve())
            except ValueError:
                errors.append(f"markdown link escapes repository in {relative}: {target}")
                continue
            if not target_path.exists():
                errors.append(f"broken markdown link in {relative}: {target}")

    # Public release should not accidentally contain benchmark/raw results or nested archives.
    forbidden_suffixes = {".zip", ".7z", ".rar", ".xlsx", ".xls", ".csv"}
    for path in ROOT.rglob("*"):
        if path.is_file() and path.suffix.lower() in forbidden_suffixes:
            warnings.append(f"unexpected data/archive file in release: {path.relative_to(ROOT).as_posix()}")

    forbidden_names = {".git", "__pycache__", "workspace", "stage1_outputs"}
    present_forbidden = sorted(
        path.relative_to(ROOT).as_posix()
        for path in ROOT.rglob("*")
        if path.name in forbidden_names
    )
    if present_forbidden:
        warnings.append("runtime/generated directories present: " + ", ".join(present_forbidden))

    report = {
        "release_root": str(ROOT),
        "required_file_count": len(REQUIRED),
        "python_file_count": len(list(ROOT.rglob("*.py"))),
        "errors": errors,
        "warnings": warnings,
        "status": "passed" if not errors else "failed",
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
