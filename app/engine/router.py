"""消息路由（通道无关，设计文档 §3.1.1）：

优先级：显式命令（确定性快速路径） → LLM 意图分类 → 问答兜底。
LLM 只抽参数；能力合法性、仓库白名单全部由本模块确定性判断。
"""
from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

_CMD_RE = re.compile(r"^\s*(\w+)\s+(\S+)\s+(\S+)(?:\s+--type\s+(\S+))?\s*$")
# MR 引用只认 mr128 / mr!128 / mr#128 这种"mr + 数字"形态；
# 分支名以 mr 开头（如 mrf-fix）不会被误判成 MR
_MR_REF_RE = re.compile(r"^mr[!#]?(\d+)$", re.IGNORECASE)
_CONFIDENCE_THRESHOLD = 0.6


@dataclass
class RouteResult:
    capability: str | None = None          # None = 直接回复，不产生任务
    params: dict = field(default_factory=dict)
    reply: str | None = None               # 直接回复文本（未配置仓库/无法识别/帮助）


class MessageRouter:
    def __init__(self, *, command_map: dict[str, str], repo_whitelist: list[str],
                 repo_required: set[str], qa_capability: str = "qa",
                 classify: Callable[[str], Awaitable] | None = None):
        self.command_map = command_map                  # "review" -> "code-review"
        self.repo_whitelist = set(repo_whitelist)
        self.repo_required = repo_required              # 需要仓库的能力（flow=review_pipeline）
        self.qa_capability = qa_capability
        self.classify = classify

    async def route(self, text: str) -> RouteResult:
        text = text.strip()

        # ① 显式命令快速路径：review <repo> <ref> [--type x]
        m = _CMD_RE.match(text)
        if m and m.group(1) in self.command_map:
            command, repo, ref, review_type = m.groups()
            if repo in self.repo_whitelist or self.classify is None:
                return self._review_result(self.command_map[command], repo, ref,
                                           review_type or "all")
            # 仓库名未命中白名单且有分类器：可能是自然语言（如 "review 一下 <链接>"）
            # 或仓库名打错——落 LLM 路径做模糊匹配，抽出的名字仍会过白名单校验

        # ①' 问答命令：ask <问题>（自由文本，无固定格式）
        for cmd, cap in self.command_map.items():
            if cap == self.qa_capability and text.startswith(cmd + " "):
                return RouteResult(capability=cap, params={"prompt": text[len(cmd):].strip()})

        # ② LLM 意图分类（自然语言）
        if self.classify is None:
            return RouteResult(reply=self._help_text())
        try:
            intent = await self.classify(text)
        except Exception:                               # 分类器故障（如模型池不可用）
            log.exception("意图分类失败，落问答兜底")
            return RouteResult(capability=self.qa_capability, params={"prompt": text})

        known_caps = set(self.command_map.values())
        if intent.capability not in known_caps or intent.confidence < _CONFIDENCE_THRESHOLD:
            return RouteResult(capability=self.qa_capability, params={"prompt": text})

        if intent.capability in self.repo_required:
            ref = intent.branch or (f"mr{intent.mr}" if intent.mr else "")
            return self._review_result(intent.capability, intent.repo or "", ref, "all")

        return RouteResult(capability=intent.capability, params={"prompt": text})

    def _review_result(self, capability: str, repo: str, ref: str,
                       review_type: str) -> RouteResult:
        """仓库白名单确定性校验——LLM 抽出来的名字也必须过这一关。"""
        if not repo:
            return RouteResult(reply=(
                "请说明要审查的仓库。\n"
                f"已配置仓库：{', '.join(sorted(self.repo_whitelist))}"))
        if repo not in self.repo_whitelist:
            return RouteResult(reply=(
                f"仓库 {repo} 未配置，无法审查。\n"
                f"已配置仓库：{', '.join(sorted(self.repo_whitelist))}\n"
                "如需新增，请在管理控制台/配置文件中添加。"))
        params: dict = {"repo": repo, "review_type": review_type}
        mr_match = _MR_REF_RE.match(ref)
        if mr_match:
            params["mr"] = mr_match.group(1)
        elif ref:
            params["branch"] = ref
        return RouteResult(capability=capability, params=params)

    def _help_text(self) -> str:
        cmds = "、".join(self.command_map)
        return (f"未理解你的意思。可用命令：{cmds}\n"
                f"例：review order-service feature/xxx")
