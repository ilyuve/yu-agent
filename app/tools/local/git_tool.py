"""本地工具：git 浅克隆（每个任务独立目录，无共享工作副本，无"切换分支"）。"""
from __future__ import annotations

import asyncio
import base64
import os
import shutil
from pathlib import Path


def _git_env() -> dict:
    """git 子进程环境：去掉 GIT_* 变量（GIT_SSH_COMMAND 等可改变行为），关闭交互提问。"""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


async def git_shallow_clone(repo_url: str, branch: str, dest: str,
                            depth: int = 1, token: str = "") -> str:
    """浅克隆指定分支到 dest。分支在克隆时指定，不做 checkout。

    凭据走 http.extraHeader（与 URL 内嵌 oauth2:token 等效），避免 token 出现在
    进程参数/git 报错/审计日志里；报错文本仍做 token 脱敏兜底。
    """
    args = ["git"]
    if token:
        cred = base64.b64encode(f"oauth2:{token}".encode()).decode()
        args += ["-c", f"http.extraHeader=Authorization: Basic {cred}"]
    args += ["clone", "--depth", str(depth), "-b", branch, repo_url, dest]
    proc = await asyncio.create_subprocess_exec(
        *args, env=_git_env(),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        err = stderr.decode(errors="replace")
        if token:
            err = err.replace(token, "***")
        raise RuntimeError(f"git clone 失败: {err[:500]}")
    return dest


def cleanup_dir(path: str) -> None:
    shutil.rmtree(path, ignore_errors=True)


def task_workdir(base: str, task_id: str) -> str:
    p = Path(base) / task_id
    p.mkdir(parents=True, exist_ok=True)
    return str(p)
