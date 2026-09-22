# -*- coding: utf-8 -*-
"""
stage2_server.py
================
Stage 2 專用 MCP server：只暴露「依凍結規格產生程式碼」所需工具。
不覆蓋舊的多角色 server.py，也不提供任意讀寫、任意 shell 或網路工具。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from mcp.server.fastmcp import FastMCP
from skills.skill_code_generation import (
    prepare_generation,
    read_frozen_spec,
    write_generated_solution,
    write_implementation_manifest,
    check_generated_python_syntax,
    finalize_generation,
)

mcp = FastMCP("stage2_code_generation")

mcp.tool()(prepare_generation)
mcp.tool()(read_frozen_spec)
mcp.tool()(write_generated_solution)
mcp.tool()(write_implementation_manifest)
mcp.tool()(check_generated_python_syntax)
mcp.tool()(finalize_generation)

if __name__ == "__main__":
    mcp.run(transport="stdio")
