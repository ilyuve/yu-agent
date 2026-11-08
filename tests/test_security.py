"""安全回归测试：bash 沙箱 / 管理控制台鉴权。

跑法：uv run pytest tests/test_security.py
"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from app.tools.local.bash_tool import run_bash

# ── bash 沙箱 ──────────────────────────────────────────────

@pytest.fixture
def workdir(tmp_path):
    d = tmp_path / "task1" / "repo"
    d.mkdir(parents=True)
    (d / "hello.txt").write_text("hello world\n", encoding="utf-8")
    return d


async def test_bash_allows_readonly_command(workdir):
    out = await run_bash("cat repo/hello.txt", cwd=str(workdir.parent))
    assert "hello world" in out


async def test_bash_blocks_shell_injection(workdir, tmp_path):
    # 首个 token 合法、分号后藏命令：不走 shell，分号是字面参数，不会执行
    marker = tmp_path / "pwned"
    out = await run_bash(f"ls ; touch {marker}", cwd=str(workdir))
    assert not marker.exists(), f"注入命令被执行了: {out}"


async def test_bash_blocks_non_whitelist(workdir):
    out = await run_bash("rm -rf .", cwd=str(workdir))
    assert "不在白名单" in out
    assert (workdir / "hello.txt").exists()


async def test_bash_blocks_path_escape(workdir):
    out = await run_bash("cat ../../etc/passwd", cwd=str(workdir))
    assert "路径越界" in out
    out = await run_bash("cat /etc/passwd", cwd=str(workdir))
    assert "路径越界" in out


async def test_bash_blocks_workdir_escape(tmp_path):
    out = await run_bash("ls", cwd="/etc", allowed_base=str(tmp_path))
    assert "workdir 越界" in out


async def test_bash_blocks_git_dangerous_args(workdir):
    assert "只允许只读子命令" in await run_bash("git clone http://x/y", cwd=str(workdir))
    assert "被禁止" in await run_bash("git -c core.pager=id log", cwd=str(workdir))
    assert "被禁止" in await run_bash("find . -exec rm {} ;", cwd=str(workdir))


async def test_bash_missing_command_returns_observation(workdir, monkeypatch):
    # 命令未安装（如 rg）时返回观测文本让 Agent 换工具，而不是异常炸掉任务
    import asyncio as _a

    async def _boom(*args, **kwargs):
        raise FileNotFoundError(2, "No such file or directory", "rg")
    monkeypatch.setattr(_a, "create_subprocess_exec", _boom)
    out = await run_bash("rg pattern .", cwd=str(workdir))
    assert "未安装" in out


async def test_bash_clean_env(workdir):
    # 子进程不继承 GIT_* 环境变量（防 GIT_SSH_COMMAND 类劫持）
    os.environ["GIT_SSH_COMMAND"] = "touch /tmp/should_not_run"
    try:
        out = await run_bash("git status", cwd=str(workdir))
    finally:
        del os.environ["GIT_SSH_COMMAND"]
    assert "should_not_run" not in out
    assert not os.path.exists("/tmp/should_not_run")


# ── 管理控制台鉴权 ─────────────────────────────────────────

@pytest.fixture
def admin_client(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "test-secret")
    import asyncio

    from app.admin.server import create_admin_app
    from app.store.sqlite import SqliteStore
    store = SqliteStore(str(tmp_path / "t.db"))
    asyncio.get_event_loop_policy()  # noop，保持 import 顺序清晰
    import anyio
    anyio.run(store.init)
    return TestClient(create_admin_app(
        {"app": {"server": {}, "capabilities": [], "codehost": {"repo_whitelist": []}},
         "models": {"models": {}, "routing": {}}}, store))


def test_admin_api_requires_token(admin_client):
    assert admin_client.get("/api/overview").status_code == 401
    assert admin_client.get("/api/config").status_code == 401
    r = admin_client.get("/api/overview", headers={"X-Admin-Token": "test-secret"})
    assert r.status_code == 200
    r = admin_client.get("/api/overview", headers={"X-Admin-Token": "wrong"})
    assert r.status_code == 401


def test_admin_healthz_open_without_token(admin_client):
    assert admin_client.get("/healthz").status_code == 200
