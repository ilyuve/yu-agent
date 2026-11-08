"""SqliteStore 关键路径测试：审计回放读取 + 任务往返。"""
import pytest

from app.engine.models import Requester, Task, TaskStatus
from app.store.sqlite import SqliteStore


@pytest.fixture
async def store(tmp_path):
    s = SqliteStore(str(tmp_path / "test.db"))
    await s.init()
    return s


def _task(task_id="Rtest01") -> Task:
    return Task(
        id=task_id,
        capability="code_review",
        params={"repo": "order-service", "branch": "feat/x"},
        requester=Requester(channel="dingtalk", conversation_id="cid1",
                            user_id="u1", nick="测试同学"),
        dedup_key="dedup1",
        status=TaskStatus.DONE,
        result_text="审查完成：2 处慢查风险。",
        created_at=1000.0,
        finished_at=1012.5,
    )


async def test_save_and_get_task_roundtrip(store):
    await store.save_task(_task())
    t = await store.get_task("Rtest01")
    assert t is not None
    assert t.capability == "code_review"
    assert t.params["repo"] == "order-service"
    assert t.requester.channel == "dingtalk"
    assert t.requester.nick == "测试同学"
    assert t.status is TaskStatus.DONE
    assert t.result_text == "审查完成：2 处慢查风险。"
    assert t.finished_at == 1012.5
    assert await store.get_task("R_nope") is None


async def test_get_audit_trail_columns_and_payload(store):
    await store.save_task(_task())
    await store.log_audit({
        "task_id": "Rtest01", "seq": 1, "kind": "llm_run", "model": "qwen-max",
        "tokens_in": 1200, "tokens_out": 300, "cost": 0.012, "latency_ms": 800,
        "payload": {"flow": "review", "step": "scan"},
    })
    await store.log_audit({
        "task_id": "Rtest01", "seq": 2, "kind": "llm_run", "model": "qwen-max",
        "tokens_in": 800, "tokens_out": 200, "cost": 0.008, "latency_ms": 500,
        "payload": {"step": "report"},
    })
    # 别的任务的记录不能混进来
    await store.log_audit({"task_id": "Rother9", "seq": 1, "kind": "llm_run",
                           "model": "qwen-max", "tokens_in": 1})

    trail = await store.get_audit_trail("Rtest01")
    assert len(trail) == 2
    assert [e["seq"] for e in trail] == [1, 2]  # 按 seq 排序
    e1 = trail[0]
    for col in ("id", "task_id", "seq", "kind", "model", "tokens_in",
                "tokens_out", "cost", "latency_ms", "payload", "created_at"):
        assert col in e1
    assert isinstance(e1["payload"], dict)          # payload 被解析成 dict
    assert e1["payload"]["step"] == "scan"
    assert e1["model"] == "qwen-max"
    assert e1["cost"] == 0.012

    assert await store.get_audit_trail("R_empty") == []


async def test_dedup_ignores_finished_tasks(store):
    """已完成/失败的同键任务不应拦截新任务（联调回归：答完的问题 300s 内不能再问）。"""
    import time
    t = _task()
    t.created_at = time.time()          # 在 TTL 窗口内但已 done
    await store.save_task(t)
    assert await store.find_by_dedup_key("dedup1", 300) is None


async def test_dedup_matches_inflight_task(store):
    import time
    t = _task()
    t.created_at = time.time()
    t.status = TaskStatus.RUNNING
    await store.save_task(t)
    hit = await store.find_by_dedup_key("dedup1", 300)
    assert hit is not None and hit.id == "Rtest01"
    assert await store.find_by_dedup_key("dedup1", 0) is None   # 超出 TTL 不匹配


# ── 订阅（§3.2：去重拦截的触发者，完成时一并 @）──

async def test_subscriber_add_list_dedupe(store):
    r2 = Requester(channel="dingtalk", conversation_id="cidG1",
                   user_id="u2", nick="李四")
    r3 = Requester(channel="dingtalk", conversation_id="cidG2",
                   user_id="u3", nick="王五")
    await store.add_subscriber("Rtest01", r2)
    await store.add_subscriber("Rtest01", r2)        # 同一人重复触发不重复登记
    await store.add_subscriber("Rtest01", r3)
    subs = await store.list_subscribers("Rtest01")
    assert [s.user_id for s in subs] == ["u2", "u3"]  # 按订阅时间序
    assert subs[0].nick == "李四"                      # requester 全字段往返
    assert await store.list_subscribers("R_nope") == []
