"""验证脚本：逐步审计重建 + 环节埋点 + 异常落库 + 管理台接口数据形态。

不触碰真实模型池/GitLab：用构造的消息流喂 _build_step_entries，
用真实 SqliteStore（临时目录）验证 stage()/record_error/批量落库，
最后起 admin app 用 httpx 直连 ASGI 验证 /api/tasks/{id} 返回形态。
"""
import asyncio
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.usage import RequestUsage

from app.agent.core import _build_step_entries
from app.config import load_settings
from app.engine import audit as audit_mod
from app.engine.models import Requester, Task, TaskStatus
from app.llm.registry import ModelRegistry
from app.store.sqlite import SqliteStore


def _ts(base: datetime, **kw) -> datetime:
    return base + timedelta(**kw)


async def main() -> None:
    settings = load_settings()
    registry = ModelRegistry(settings["models"])
    ctx = SimpleNamespace(model_registry=registry)
    cap = {"model_purpose": "review"}
    task = Task(id="Rvfy01", capability="code-review",
                params={"repo": "order-service", "branch": "feat/x"},
                requester=Requester(channel="test", conversation_id="c1", user_id="u1"))

    t0 = datetime(2026, 10, 18, 21, 0, 0, tzinfo=UTC)
    # 模拟一次两轮对话：LLM(3s) → 工具 git_log(2s) → LLM(4s) → 工具 scanner(1s) → LLM(5s)
    messages = [
        ModelRequest(parts=[UserPromptPart(content="审查任务…")], timestamp=t0),
        ModelResponse(parts=[TextPart(content="先看一下提交记录"),
                             ToolCallPart(tool_name="git_log", args={"workdir": "/x", "n": 5},
                                          tool_call_id="c1")],
                      usage=RequestUsage(input_tokens=12000, output_tokens=300),
                      model_name="deepseek-chat", timestamp=_ts(t0, seconds=3)),
        ModelRequest(parts=[ToolReturnPart(tool_name="git_log", content="abc123 fix pay …",
                                           tool_call_id="c1", timestamp=_ts(t0, seconds=5))],
                     timestamp=_ts(t0, seconds=5)),
        ModelResponse(parts=[TextPart(content="跑预扫"),
                             ToolCallPart(tool_name="prescan", args={"workdir": "/x"},
                                          tool_call_id="c2")],
                      usage=RequestUsage(input_tokens=12500, output_tokens=120),
                      model_name="deepseek-chat", timestamp=_ts(t0, seconds=9)),
        ModelRequest(parts=[ToolReturnPart(tool_name="prescan", content="命中 7 个候选点",
                                           tool_call_id="c2", timestamp=_ts(t0, seconds=10))],
                     timestamp=_ts(t0, seconds=10)),
        ModelResponse(parts=[TextPart(content="报告：2 处慢查风险")],
                      usage=RequestUsage(input_tokens=13000, output_tokens=900),
                      model_name="deepseek-reasoner",   # 第二轮走了 fallback 模型
                      timestamp=_ts(t0, seconds=15)),
    ]
    fake_result = SimpleNamespace(all_messages=lambda: messages)

    entries = _build_step_entries(task, ctx, cap, fake_result)
    print("── 重建出", len(entries), "条审计 ──")
    for e in entries:
        print(f"  {e['kind']:<12} model={e.get('model') or '-':<18} "
              f"lat={e['latency_ms']:>5}ms  in={e.get('tokens_in', 0):>6} "
              f"out={e.get('tokens_out', 0):>5} cost=¥{e.get('cost', 0):.4f}  {e['payload']}")

    kinds = [e["kind"] for e in entries]
    assert kinds == ["llm_request", "tool_call", "llm_request", "tool_call", "llm_request"], kinds
    assert entries[0]["latency_ms"] == 3000 and entries[1]["latency_ms"] == 2000
    assert entries[2]["latency_ms"] == 4000 and entries[3]["latency_ms"] == 1000
    assert entries[4]["latency_ms"] == 5000
    # 成本按真实命中模型计价：deepseek-chat 2/8 元每百万，reasoner 4/16
    assert abs(entries[0]["cost"] - (12000 * 2 + 300 * 8) / 1e6) < 1e-9
    assert abs(entries[4]["cost"] - (13000 * 4 + 900 * 16) / 1e6) < 1e-9
    assert entries[1]["payload"]["tool"] == "git_log" and entries[1]["payload"]["ok"] is True
    print("✔ 重建逻辑断言通过（轮次/耗时/工具配对/真实模型计价）")

    # ── 真实 store：stage 成功/失败 + record_error + 批量落库 + seq 连续性 ──
    with tempfile.TemporaryDirectory() as td:
        store = SqliteStore(str(Path(td) / "t.db"))
        await store.init()
        task.status = TaskStatus.RUNNING
        await store.save_task(task)

        info = {"repo": "order-service"}
        async with audit_mod.stage(store, task.id, "clone", info):
            info["branch"] = "feat/x"
            await asyncio.sleep(0.01)
        try:
            async with audit_mod.stage(store, task.id, "archive"):
                raise ConnectionError("钉钉文档网关 502")
        except ConnectionError:
            pass
        base = await store.next_audit_seq(task.id)
        for i, e in enumerate(entries):
            e["seq"] = base + i
        await store.log_audit_batch(entries)

        trail = await store.get_audit_trail(task.id)
        print("── 落库回放（含失败环节）──")
        for r in trail:
            p = r["payload"]
            extra = p.get("stage") or p.get("tool") or f"round {p.get('round')}"
            print(f"  seq={r['seq']:<3} {r['kind']:<12} {extra:<10} lat={r['latency_ms']}ms")
        assert [r["seq"] for r in trail] == list(range(1, len(trail) + 1))
        assert trail[0]["kind"] == "stage" and trail[0]["payload"]["branch"] == "feat/x"
        assert trail[1]["kind"] == "error" and "ConnectionError" in trail[1]["payload"]["error"]
        assert "502" in trail[1]["payload"]["traceback"]
        stats = await store.stats_overview()
        # 统计只算 LLM 类条目：3 次请求、2 个模型
        assert stats["tokens_in"] == 12000 + 12500 + 13000, stats["tokens_in"]
        assert len(stats["by_model"]) == 2
        print("✔ 存储层断言通过（seq 连续 / 失败环节落 error 含堆栈 / 统计口径正确）")

        # ── 管理台接口形态 ──
        from httpx import ASGITransport, AsyncClient

        from app.admin.server import create_admin_app
        app = create_admin_app(settings, store)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as cli:
            d = (await cli.get(f"/api/tasks/{task.id}")).json()
        assert len(d["audit_trail"]) == len(trail)
        assert d["totals"]["tokens_in"] == stats["tokens_in"]
        print("✔ /api/tasks/{id} 返回", len(d["audit_trail"]), "条回放 + totals:",
              d["totals"])

    print("\n全部验证通过 ✅")


asyncio.run(main())
