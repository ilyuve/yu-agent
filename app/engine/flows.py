"""流程类型注册表（设计文档 §4）：能力在 capability.yaml 里声明 flow，引擎按类型分发。

- review_pipeline：编排骨架（克隆 → 预扫 → Agent 深审 → 归档）+ Agent 局部自主。
  骨架 code-first 写死，可测试、可审计；判断点交给 Agent（步数/工具白名单护栏）。
- agent_only：纯 Agent 循环（问答、通用对话类能力），无固定骨架，不拉代码不归档。

新增能力时自选流程类型；新流程类型 = 这里加一个 builder 函数，不改引擎。
"""
from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)


def build_review_pipeline(ctx):
    """CR + 慢查审查的固定编排骨架。返回 runner(task)。"""
    from app.agent import core as agent_core
    from app.engine.audit import stage
    from app.tools.local import scanner as scan_mod
    from app.tools.local.git_tool import cleanup_dir, git_shallow_clone, task_workdir

    server = ctx.settings["app"]["server"]
    rules = ctx.settings["scan_rules"]["rules"]
    ch = ctx.settings["app"]["codehost"]

    async def runner(task) -> None:
        workdir = task_workdir(server["clone_base_dir"], task.id)
        try:
            with_info: dict = {"repo": task.params.get("repo", "")}
            async with stage(ctx.store, task.id, "clone", with_info):
                repo_info = await ctx.codehost.validate_repo(task.params["repo"])
                branch = task.params.get("branch", repo_info.default_branch)
                # 凭据走 header 传递（git_tool 内实现），不嵌进 URL，防报错/日志泄露
                token = os.environ.get(ch["token_env"], "")
                await git_shallow_clone(repo_info.clone_url, branch,
                                        f"{workdir}/repo", token=token)
                task.params["workdir"] = f"{workdir}/repo"   # Agent 工具的工作目录
                with_info["branch"] = branch

            scan_info: dict = {}
            async with stage(ctx.store, task.id, "scan", scan_info):
                hits = scan_mod.scan(f"{workdir}/repo", rules)
                scan_info["hits"] = len(hits)
            log.info("%s 预扫候选点 %d 个", task.id, len(hits))

            # agent 环节的逐步耗时/异常由 agent.core 落库（llm_request/tool_call/error），
            # 这里不再包 stage——stage 行会在其内部明细之后落库，时间线顺序会颠倒
            report = await agent_core.run(task, ctx)
            task.result_text = report.summary      # 回推消息与控制台摘要共用

            async with stage(ctx.store, task.id, "archive"):
                task.report_url = await ctx.sink.archive(task, report)
        finally:
            cleanup_dir(workdir)

    return runner


def build_agent_only(ctx):
    """纯 Agent 循环：问答/通用能力入口。返回 runner(task)。"""
    from app.agent import core as agent_core

    async def runner(task) -> None:
        # 逐步耗时/异常由 agent.core 落库（llm_request/tool_call/error）
        result = await agent_core.run(task, ctx)
        # 问答类结果不进文档库，由通道直接回复（Adapter 回推路径）
        task.result_text = result if isinstance(result, str) else str(result)

    return runner


_BUILDERS = {
    "review_pipeline": build_review_pipeline,
    "agent_only": build_agent_only,
}


def registered_flows() -> list[str]:
    return list(_BUILDERS)


def get_runner(flow: str, ctx):
    """按流程类型取 runner；未注册的类型在启动装配时即报错（fail fast）。"""
    if flow not in _BUILDERS:
        raise KeyError(f"未注册的流程类型: {flow}（已有: {registered_flows()}）")
    return _BUILDERS[flow](ctx)
