"""端到端冒烟：本地 git 仓库 + FunctionModel（不依赖外部凭据）打穿 review_pipeline。

覆盖链路：codehost 校验 → 浅克隆 → 静态预扫 → Agent 深审（FunctionModel 替身）
→ 报告归档（LocalFileSink）→ 审计落库（含 seq）→ result_text 回推字段。
另含一条问答会话连续性测试（同一群同一人带历史，换人/换群隔离）。

跑法：uv run pytest tests/e2e/
"""
from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from app.agent import core as agent_core
from app.agent.core import CapabilityRegistry
from app.config import ROOT, load_settings
from app.engine import flows
from app.engine.models import Requester, Task
from app.integrations.sinks.local import LocalFileSink
from app.store.sqlite import SqliteStore
from app.tools import registry as tool_registry

BAD_SQL = '''
def list_orders(uid):
    sql = "SELECT * FROM orders WHERE user_id = %s" % uid
    rows = db.execute(sql)
    for r in rows:
        db.execute("UPDATE orders SET flag = 1;")   # 无 WHERE + 循环内 SQL
    return rows
'''


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True,
                   capture_output=True, text=True)


@pytest.fixture
def local_repo(tmp_path):
    """造一个本地仓库：main 干净，feature/pay 分支带慢查代码。"""
    repo = tmp_path / "order-service"
    repo.mkdir()
    _git(["init", "-b", "main"], repo)
    _git(["config", "user.email", "t@t"], repo)
    _git(["config", "user.name", "t"], repo)
    (repo / "app.py").write_text("print('ok')\n", encoding="utf-8")
    _git(["add", "."], repo)
    _git(["commit", "-m", "init"], repo)
    _git(["checkout", "-b", "feature/pay"], repo)
    (repo / "orders.py").write_text(BAD_SQL, encoding="utf-8")
    _git(["add", "."], repo)
    _git(["commit", "-m", "add orders"], repo)
    return repo


class LocalCodehost:
    """CodeHostProvider 的本地实现：clone_url 直接指向本地仓库路径。"""
    def __init__(self, repo_path):
        self.repo_path = str(repo_path)

    async def validate_repo(self, repo):
        return SimpleNamespace(name=repo, default_branch="main",
                               clone_url=self.repo_path)

    async def get_head_commit(self, repo, branch):
        def _rev():
            return subprocess.run(["git", "rev-parse", branch], cwd=self.repo_path,
                                  check=True, capture_output=True, text=True)
        out = await asyncio.to_thread(_rev)
        return out.stdout.strip()

    async def get_mr_ref(self, repo, mr):
        raise NotImplementedError

    async def get_change_summary(self, repo, branch):
        raise NotImplementedError


class _FakeModelEntry:
    key = "function-model"

    @staticmethod
    def cost(tokens_in, tokens_out):
        return 0.0


def _review_model(report_dict: dict) -> FunctionModel:
    """FunctionModel：直接产出结构化报告（模拟深审结论）。"""
    def fn(messages, info):
        if info.output_tools:
            return ModelResponse(parts=[
                ToolCallPart(info.output_tools[0].name, report_dict)])
        return ModelResponse(parts=[TextPart("ok")])
    return FunctionModel(fn)


def _ctx(settings, tmp_path, repo, model) -> SimpleNamespace:
    rules = settings["scan_rules"]["rules"]
    return SimpleNamespace(
        settings=settings,
        capabilities=CapabilityRegistry([str(ROOT / "app/capabilities/code-review"),
                                         str(ROOT / "app/capabilities/qa")]),
        model_registry=SimpleNamespace(chain_for=lambda purpose: [_FakeModelEntry()]),
        store=None,                                        # 由测试注入
        codehost=LocalCodehost(repo),
        sink=LocalFileSink(str(tmp_path / "reports")),
        toolsets=tool_registry.build_registry(settings, rules),
        model_for=lambda purpose: model,
        resolve_toolsets=lambda names: tool_registry.resolve(
            tool_registry.build_registry(settings, rules), names),
    )


async def test_review_pipeline_e2e(local_repo, tmp_path):
    settings = load_settings()
    settings["app"]["server"]["clone_base_dir"] = str(tmp_path / "workspace")
    ctx = _ctx(settings, tmp_path, local_repo, _review_model({
        "summary": "发现 2 处慢查风险：SELECT * 与循环内 UPDATE。",
        "slow_query_risks": [
            {"severity": "medium", "location": "orders.py:2", "rule_id": "SQ001",
             "reason": "SELECT * 全字段读取", "suggestion": "显式列出字段"},
            {"severity": "high", "location": "orders.py:5", "rule_id": "SQ006",
             "reason": "循环内执行 UPDATE 且无 WHERE", "suggestion": "批量更新并加 WHERE"},
        ],
        "cr_suggestions": [
            {"category": "异常处理", "location": "orders.py:3",
             "description": "db.execute 未处理异常", "example": None},
        ],
    }))
    store = SqliteStore(str(tmp_path / "e2e.db"))
    await store.init()
    ctx.store = store

    task = Task(capability="code-review",
                params={"repo": "order-service", "branch": "feature/pay",
                        "review_type": "all"},
                requester=Requester(channel="test", conversation_id="c1",
                                    user_id="u1", nick="测试"),
                id="Re2e01")

    # 预扫本身就该命中（编排骨架的第一步）
    runner = flows.get_runner("review_pipeline", ctx)
    await runner(task)

    # 报告归档：markdown 落盘且内容完整
    assert task.report_url and task.report_url.endswith("Re2e01.md")
    md = Path(task.report_url).read_text(encoding="utf-8")
    assert "SELECT *" in md or "SQ001" in md
    assert "循环内执行" in md
    # result_text 供通道回推
    assert task.result_text == "发现 2 处慢查风险：SELECT * 与循环内 UPDATE。"
    # 审计（§12 新粒度）：编排环节行 + 每次 LLM 请求逐条落库，seq 从 1 连续
    trail = await store.get_audit_trail("Re2e01")
    kinds = [e["kind"] for e in trail]
    assert trail[0]["kind"] == "stage" and trail[0]["payload"]["stage"] == "clone"
    assert trail[1]["kind"] == "stage" and trail[1]["payload"]["stage"] == "scan"
    assert "llm_request" in kinds                      # 每次模型调用逐条记录
    assert trail[-1]["kind"] == "stage" and trail[-1]["payload"]["stage"] == "archive"
    assert [e["seq"] for e in trail] == list(range(1, len(trail) + 1))
    llm = next(e for e in trail if e["kind"] == "llm_request")
    assert llm["model"]                                # 真实命中的模型名
    # 克隆目录已清理
    assert not (tmp_path / "workspace" / "Re2e01").exists()


async def test_qa_session_history(tmp_path):
    """同一（群,人）第二次问答带上历史；换人/换群隔离。"""
    settings = load_settings()
    seen_lengths: list[int] = []

    def fn(messages, info):
        seen_lengths.append(len(messages))
        return ModelResponse(parts=[TextPart(f"回答 {len(seen_lengths)}")])

    ctx = _ctx(settings, tmp_path, tmp_path, FunctionModel(fn))
    store = SqliteStore(str(tmp_path / "qa.db"))
    await store.init()
    ctx.store = store

    async def ask(conv, user, prompt, tid):
        t = Task(capability="qa", params={"prompt": prompt},
                 requester=Requester(channel="dingtalk", conversation_id=conv,
                                     user_id=user, nick=user),
                 id=tid)
        return await agent_core.run(t, ctx)

    r1 = await ask("groupA", "u1", "什么是覆盖索引？", "Rq1")
    r2 = await ask("groupA", "u1", "那和联合索引的区别？", "Rq2")   # 同群同人 → 带历史
    r3 = await ask("groupA", "u2", "什么是覆盖索引？", "Rq3")       # 换人了 → 无历史
    r4 = await ask("groupB", "u1", "什么是覆盖索引？", "Rq4")       # 换群了 → 无历史

    assert (r1, r2, r3, r4) == ("回答 1", "回答 2", "回答 3", "回答 4")
    assert seen_lengths[1] > seen_lengths[0]     # 第二次带上了历史
    assert seen_lengths[2] == seen_lengths[0]    # 换人隔离
    assert seen_lengths[3] == seen_lengths[0]    # 换群隔离

    # 审计：逐请求粒度，seq 从 1 开始
    trail = await store.get_audit_trail("Rq1")
    assert trail and trail[0]["kind"] == "llm_request" and trail[0]["seq"] == 1
