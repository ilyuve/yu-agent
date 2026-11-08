"""CodeHostProvider：代码托管确定性调用面（设计文档 §3.5.1①）。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass
class RepoInfo:
    name: str
    default_branch: str
    clone_url: str


@dataclass
class ChangeSummary:
    ref: str
    head_commit: str
    changed_files: list[str]


class CodeHostProvider(Protocol):
    async def validate_repo(self, repo: str) -> RepoInfo:
        """白名单 + 存在性校验，返回仓库信息（含 clone_url）。"""
        ...

    async def get_head_commit(self, repo: str, branch: str) -> str:
        """分支当前 head commit，幂等去重/任务锚点。"""
        ...

    async def get_change_summary(self, repo: str, branch: str) -> ChangeSummary:
        """分支相对默认分支的变更文件清单（任务描述与预扫范围用）。"""
        ...

    async def get_mr_ref(self, repo: str, mr: str) -> tuple[str, str]:
        """MR → (源分支, 源分支 head sha)。审查 MR = 审查其源分支当前快照。"""
        ...
