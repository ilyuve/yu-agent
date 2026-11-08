"""Toolset 注册表：本地工具 + MCP 聚合（设计文档 §3.5）。"""
from __future__ import annotations

import logging
from pathlib import Path

from pydantic_ai.toolsets import FunctionToolset

from app.tools.local import bash_tool, scanner

log = logging.getLogger(__name__)


def build_registry(settings: dict, rules: list[dict]) -> dict:
    """按 capability.yaml 的 toolsets 声明解析工具集。

    MCP 部分：pydantic-ai 2.x 用 load_mcp_toolsets(mcp_config.json) 从配置加载，
    工具名自动带 server key 前缀（如 codehost_*）——命名空间隔离开箱即有。
    """
    # bash 工具的目录围栏：模型传入的 workdir 必须落在克隆根目录内
    #（测试/极简 settings 没有该键时退化为限定在 workdir 自身）
    clone_base_dir = (settings.get("app", {}).get("server", {}) or {}).get("clone_base_dir")
    local_git = FunctionToolset(tools=[], id="local.git")       # git 浅克隆由编排层调用，不暴露给模型
    local_scanner = FunctionToolset(id="local.scanner")
    local_bash = FunctionToolset(id="local.bash")

    @local_scanner.tool_plain
    def sql_static_scan(workdir: str) -> str:
        """对任务工作目录执行慢查静态预扫，返回候选点清单。"""
        return scanner.format_hits(scanner.scan(workdir, rules))

    @local_bash.tool_plain
    async def bash(command: str, workdir: str) -> str:
        """在任务工作目录内执行白名单只读命令（grep/find/cat/ls 等）。"""
        return await bash_tool.run_bash(command, cwd=workdir,
                                        allowed_base=clone_base_dir)

    registry = {
        "local.git": local_git,
        "local.scanner": local_scanner,
        "local.bash": local_bash,
    }

    # MCP：配置文件在就加载（连接是懒的，不调用不建连）；文件缺失/为空则跳过。
    # 能力要用就在 capability.yaml 的 toolsets 里声明 mcp.<server key>。
    mcp_path = Path(settings.get("mcp_config_path") or "")
    if mcp_path.exists() and mcp_path.read_text(encoding="utf-8").strip() not in ("", "{}"):
        from pydantic_ai.mcp import load_mcp_toolsets
        for ts in load_mcp_toolsets(mcp_path):
            server_id = getattr(getattr(ts, "wrapped", ts), "id", None)
            if server_id:
                registry[f"mcp.{server_id}"] = ts
                log.info("MCP server 已注册: mcp.%s", server_id)
    return registry


def resolve(registry: dict, names: list[str]) -> list:
    toolsets = []
    for name in names:
        if name not in registry:
            raise KeyError(f"未注册的工具集: {name}")
        toolsets.append(registry[name])
    return toolsets
