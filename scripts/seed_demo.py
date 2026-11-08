"""demo 数据注入：让管理控制台在本地预览时有内容可看。

只往 demo 数据库写，不影响任何真实数据。幂等：已有数据则跳过。
"""
from __future__ import annotations

import time

from app.engine.models import Requester, Task, TaskStatus


async def seed_if_empty(store) -> None:
    stats = await store.stats_overview()
    if stats["tasks_total"] > 0:
        return

    now = time.time()
    req = Requester(channel="dingtalk", conversation_id="cid-demo",
                    user_id="u1001", nick="张三")

    demo_tasks = [
        Task(id="R0a1b2c", capability="code-review",
             params={"repo": "order-service", "branch": "feature/pay-v2",
                     "review_type": "all"},
             requester=req, status=TaskStatus.DONE,
             report_url="https://docs.example.com/report/R0a1b2c",
             created_at=now - 86400 * 2, finished_at=now - 86400 * 2 + 187),
        Task(id="R3d4e5f", capability="code-review",
             params={"repo": "payment", "mr": "128", "review_type": "slow-query"},
             requester=Requester(channel="dingtalk", conversation_id="cid-demo",
                                 user_id="u1002", nick="李四"),
             status=TaskStatus.DONE,
             report_url="https://docs.example.com/report/R3d4e5f",
             created_at=now - 86400, finished_at=now - 86400 + 142),
        Task(id="R6g7h8i", capability="qa",
             params={"prompt": "覆盖索引和普通索引的区别？"},
             requester=req, status=TaskStatus.DONE,
             result_text="覆盖索引指 SELECT 的列全部包含在索引中，无需回表……",
             created_at=now - 3600 * 5, finished_at=now - 3600 * 5 + 8),
        Task(id="R9j0k1l", capability="code-review",
             params={"repo": "user-center", "branch": "hotfix/login",
                     "review_type": "all"},
             requester=req, status=TaskStatus.FAILED,
             error="模型池连接超时（FallbackModel 两次尝试均失败）",
             created_at=now - 3600 * 2, finished_at=now - 3600 * 2 + 61),
        Task(id="R2m3n4o", capability="code-review",
             params={"repo": "order-service", "branch": "feature/stock",
                     "review_type": "all"},
             requester=Requester(channel="dingtalk", conversation_id="cid-demo",
                                 user_id="u1003", nick="王五"),
             status=TaskStatus.RUNNING,
             created_at=now - 95),
    ]
    for t in demo_tasks:
        await store.save_task(t)

    # 执行时间线示例：按新粒度（stage / llm_request / tool_call / error）构造，
    # 兼容旧粒度 llm_run（R3d4e5f 保留旧汇总行，验证新老数据混排显示）
    tb_sample = (
        'Traceback (most recent call last):\n'
        '  File "/app/agent/core.py", line 70, in run\n'
        '    result = await agent.run(task.describe(), deps=ctx)\n'
        '  File "/pydantic_ai/agent.py", line 812, in run\n'
        '    async with self.iter(...) as run:\n'
        'TimeoutError: 模型池连接超时（30s）：https://llm-pool.internal/v1 '
        '（FallbackModel 两次尝试均失败）'
    )

    async def log(tid, seq, kind, lat, model=None, ti=0, to=0, cost=0.0, payload=None):
        await store.log_audit({
            "task_id": tid, "seq": seq, "kind": kind, "model": model,
            "tokens_in": ti, "tokens_out": to, "cost": cost, "latency_ms": lat,
            "payload": payload or {},
        })

    # R0a1b2c：一次完整的审查对话（克隆→预扫→两轮 LLM+两次工具→归档）
    await log("R0a1b2c", 1, "stage", 3200, payload={"stage": "clone", "repo": "order-service", "branch": "feature/pay-v2"})
    await log("R0a1b2c", 2, "stage", 400, payload={"stage": "scan", "hits": 7})
    await log("R0a1b2c", 3, "llm_request", 8100, "deepseek-chat", 12400, 300, 0.0272, {"capability": "code-review", "round": 1})
    await log("R0a1b2c", 4, "tool_call", 200, payload={"capability": "code-review", "tool": "git_log", "args": "{'workdir': '/x', 'n': 5}", "ok": True, "result": "a1b2c3d fix: 支付回调重复入账 …"})
    await log("R0a1b2c", 5, "llm_request", 6700, "deepseek-chat", 12800, 850, 0.0324, {"capability": "code-review", "round": 2})
    await log("R0a1b2c", 6, "tool_call", 150, payload={"capability": "code-review", "tool": "bash", "args": "{'cmd': 'grep -rn SELECT order/'}", "ok": True, "result": "order/dao.py:88: SELECT * FROM orders WHERE …"})
    await log("R0a1b2c", 7, "llm_request", 5200, "deepseek-chat", 13100, 1950, 0.0418, {"capability": "code-review", "round": 3})
    await log("R0a1b2c", 8, "stage", 1500, payload={"stage": "archive"})

    # R3d4e5f：旧粒度 llm_run 汇总行（兼容显示）
    await log("R3d4e5f", 1, "llm_run", 33800, "deepseek-chat", 12877, 2041, 0.0421, {"capability": "code-review"})
    await log("R3d4e5f", 2, "llm_run", 9100, "deepseek-reasoner", 5102, 880, 0.0345, {"capability": "code-review"})

    # R6g7h8i：问答能力（agent_only 流程，无编排环节行）
    await log("R6g7h8i", 1, "llm_request", 2200, "deepseek-chat", 1024, 356, 0.0049, {"capability": "qa", "round": 1})

    # R9j0k1l：失败任务——第二轮 LLM 超时，error 行带完整堆栈
    await log("R9j0k1l", 1, "stage", 3100, payload={"stage": "clone", "repo": "user-center", "branch": "hotfix/login"})
    await log("R9j0k1l", 2, "stage", 300, payload={"stage": "scan", "hits": 2})
    await log("R9j0k1l", 3, "llm_request", 8000, "deepseek-chat", 4302, 120, 0.0096, {"capability": "code-review", "round": 1})
    await log("R9j0k1l", 4, "error", 60000, payload={"stage": "agent_run", "error": "TimeoutError: 模型池连接超时（FallbackModel 两次尝试均失败）", "traceback": tb_sample})

    # R2m3n4o：运行中任务的部分时间线（走到哪看到哪）
    await log("R2m3n4o", 1, "stage", 2900, payload={"stage": "clone", "repo": "order-service", "branch": "feature/stock"})
    await log("R2m3n4o", 2, "stage", 400, payload={"stage": "scan", "hits": 5})
    await log("R2m3n4o", 3, "llm_request", 7500, "deepseek-chat", 9108, 210, 0.0199, {"capability": "code-review", "round": 1})
