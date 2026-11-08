"""GitLab REST API v4 实现（设计文档 §3.5.1①）。"""
from __future__ import annotations

import os
from urllib.parse import quote

import httpx

from app.integrations.codehost.base import ChangeSummary, RepoInfo


class GitLabProvider:
    def __init__(self, api_base: str, token_env: str, whitelist: list[str],
                 gitlab_web_base: str | None = None):
        self.api_base = api_base.rstrip("/")
        self.token = os.environ.get(token_env, "")
        self.whitelist = set(whitelist)
        # clone 用的 web 地址，默认同 api_base 推导（占位配置下无意义，内网配置真实值）
        self.web_base = gitlab_web_base or self.api_base.replace("/api/v4", "")

    def _headers(self) -> dict:
        return {"PRIVATE-TOKEN": self.token}

    async def validate_repo(self, repo: str) -> RepoInfo:
        if repo not in self.whitelist:
            raise PermissionError(f"仓库 {repo} 不在白名单内")
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(f"{self.api_base}/projects/{quote(repo, safe='')}",
                                 headers=self._headers())
            if r.status_code == 404:
                raise LookupError(f"仓库不存在或无权限: {repo}")
            r.raise_for_status()
            data = r.json()
        return RepoInfo(name=repo, default_branch=data["default_branch"],
                        clone_url=data["http_url_to_repo"])

    async def get_head_commit(self, repo: str, branch: str) -> str:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(
                f"{self.api_base}/projects/{quote(repo, safe='')}/repository/branches/{quote(branch, safe='')}",
                headers=self._headers())
            if r.status_code == 404:
                raise LookupError(f"分支不存在: {repo}@{branch}")
            r.raise_for_status()
            return r.json()["commit"]["id"]

    async def get_mr_ref(self, repo: str, mr: str) -> tuple[str, str]:
        """MR → (源分支, 源分支 head sha)。"""
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(
                f"{self.api_base}/projects/{quote(repo, safe='')}/merge_requests/{mr}",
                headers=self._headers())
            if r.status_code == 404:
                raise LookupError(f"MR 不存在: {repo}!{mr}")
            r.raise_for_status()
            data = r.json()
        return data["source_branch"], data["sha"]

    async def get_change_summary(self, repo: str, branch: str) -> ChangeSummary:
        """分支 vs 默认分支的 compare，取变更文件清单。"""
        info = await self.validate_repo(repo)
        head = await self.get_head_commit(repo, branch)
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(
                f"{self.api_base}/projects/{quote(repo, safe='')}/repository/compare",
                params={"from": info.default_branch, "to": branch},
                headers=self._headers())
            r.raise_for_status()
            diffs = r.json().get("diffs", [])
        return ChangeSummary(ref=branch, head_commit=head,
                             changed_files=[d["new_path"] for d in diffs])
