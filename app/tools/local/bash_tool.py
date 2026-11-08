"""本地工具：受限只读命令。工作目录强制限定在任务克隆根目录内。

安全模型（该工具直接暴露给 LLM，命令与 workdir 都按不可信输入处理）：
- 不走 shell：shlex 切词后直接 exec，`;` `|` `&&` `$()` 反引号等全部失效；
- 命令白名单 + git 子命令白名单（只读），并屏蔽 `-c`/`--exec-path`/`--output`/
  `-exec` 这类可改配置、写文件或执行二段命令的参数；
- 所有路径参数（绝对/相对）解析后必须落在允许的根目录内，防 `cat /etc/passwd`
  与 `../..` 逃逸；
- 子进程用最小环境（PATH/HOME），不继承 GIT_* 等可改变 git 行为的环境变量。
"""
from __future__ import annotations

import asyncio
import os
import shlex
from pathlib import Path

# 命令白名单：审查只需要只读类命令
_ALLOWED = ("grep", "rg", "find", "cat", "head", "tail", "ls", "wc", "git")

# git 只允许只读子命令（clone/config/remote/push 等一律拒绝）
_GIT_SUBCOMMANDS = {"log", "show", "diff", "blame", "status", "grep",
                    "ls-files", "rev-parse"}

# 可逃逸/可落盘的参数：git -c 改配置、git --exec-path 执行、--output 写文件、
# find -exec/-delete 执行与删除
_BLOCKED_ARGS = {"-c", "--config", "--exec-path", "--output", "-o",
                 "-exec", "-execdir", "-delete", "-f", "--force"}


def _check_args(argv: list[str], cwd: Path, root: Path) -> str | None:
    """逐参数校验。返回拒绝原因；None 表示通过。"""
    for arg in argv[1:]:
        if arg in _BLOCKED_ARGS:
            return f"参数 {arg} 被禁止（可写文件/改配置/执行命令）"
        if arg.startswith("-") or not arg:
            continue
        # 路径类参数：绝对路径或含分隔符的相对路径都必须解析在根目录内
        p = Path(arg)
        if p.is_absolute() or "/" in arg or ".." in arg:
            resolved = (p if p.is_absolute() else cwd / p).resolve()
            if not resolved.is_relative_to(root):
                return f"路径越界: {arg}（只允许访问任务目录内的文件）"
    return None


async def run_bash(command: str, cwd: str, allowed_base: str | None = None) -> str:
    """在任务目录内执行白名单只读命令。返回 stdout（截断 8000 字符）。

    allowed_base：允许的根目录（一般为 server.clone_base_dir）；传入时
    cwd 本身也必须落在其内。
    """
    try:
        argv = shlex.split(command)
    except ValueError as e:
        return f"命令解析失败: {e}"
    if not argv:
        return "空命令"
    cmd0 = argv[0]
    if cmd0 not in _ALLOWED:
        return f"命令 {cmd0} 不在白名单内（允许: {', '.join(_ALLOWED)}）"
    base = Path(cwd).resolve()
    if allowed_base:
        root = Path(allowed_base).resolve()
        if not base.is_relative_to(root):
            return f"workdir 越界: {cwd}（只允许任务克隆目录内）"
    else:
        root = base
    err = _check_args(argv, base, root)
    if err:
        return err
    if cmd0 == "git" and (len(argv) < 2 or argv[1] not in _GIT_SUBCOMMANDS):
        return f"git 只允许只读子命令: {', '.join(sorted(_GIT_SUBCOMMANDS))}"

    # 最小环境：不给 GIT_* 变量（GIT_SSH_COMMAND/GIT_ASKPASS 等可执行任意命令）
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
           "HOME": str(base), "LC_ALL": "C.UTF-8"}
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=str(base), env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    except FileNotFoundError:
        # 命令未安装（如 rg）：作为观测返回给 Agent，让它换 grep 等替代，
        # 而不是异常炸掉整个任务
        return f"命令 {cmd0} 在当前环境未安装，请改用白名单内其他命令（如 grep）"
    except OSError as e:
        return f"命令执行失败: {e}"
    out, _ = await proc.communicate()
    return out.decode(errors="replace")[:8000]


async def grep(pattern: str, path: str, glob: str = "") -> str:
    argv = ["grep", "-rn", "--", pattern, path]
    if glob:
        argv += [f"--include={glob}"]
    return await run_bash(shlex.join(argv), cwd=path)
