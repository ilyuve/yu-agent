"""SQLite 实现的 Store（起步方案，Postgres 迁移时只换本文件）。"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import aiosqlite

from app.engine.models import Requester, Task, TaskStatus

_DDL = """
CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY, capability TEXT, params TEXT, status TEXT,
  dedup_key TEXT, requester TEXT, report_url TEXT, result_text TEXT, error TEXT,
  created_at REAL, finished_at REAL
);
CREATE TABLE IF NOT EXISTS task_histories (
  task_id TEXT PRIMARY KEY, message_history BLOB, updated_at REAL
);
CREATE TABLE IF NOT EXISTS audit_logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT, seq INTEGER, kind TEXT, model TEXT,
  tokens_in INTEGER, tokens_out INTEGER, cost REAL, latency_ms INTEGER,
  payload TEXT, created_at REAL
);
CREATE TABLE IF NOT EXISTS feedback (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT, user_id TEXT, score INTEGER, comment TEXT, created_at REAL
);
CREATE TABLE IF NOT EXISTS task_subscribers (
  task_id TEXT, user_id TEXT, requester TEXT, created_at REAL,
  UNIQUE(task_id, user_id)
);
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY, value TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_dedup ON tasks(dedup_key, created_at);
CREATE INDEX IF NOT EXISTS idx_audit_task ON audit_logs(task_id);
"""


# 审计统计只聚合 LLM 调用类条目（llm_run=旧版任务汇总行，llm_request=新版逐请求行），
# stage/tool_call/error 不计 token 与成本。
_LLM_KINDS = ("llm_run", "llm_request")

# 单条 payload 落库上限：防止超大工具返回/堆栈把库写胀、拖慢序列化
_PAYLOAD_MAX_CHARS = 4000


def _payload_json(payload: Any) -> str:
    text = json.dumps(payload or {}, ensure_ascii=False)
    if len(text) > _PAYLOAD_MAX_CHARS:
        text = text[:_PAYLOAD_MAX_CHARS] + "…<truncated>"
    return text


class SqliteStore:
    def __init__(self, db_path: str):
        self.db_path = db_path

    async def init(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.executescript(_DDL)

    async def save_task(self, task: Task) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT OR REPLACE INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (task.id, task.capability, json.dumps(task.params, ensure_ascii=False),
                 task.status.value, task.dedup_key,
                 json.dumps(vars(task.requester), ensure_ascii=False),
                 task.report_url, task.result_text, task.error,
                 task.created_at, task.finished_at))
            await db.commit()

    async def list_tasks(self, limit: int = 50, offset: int = 0) -> list[Task]:
        async with aiosqlite.connect(self.db_path) as db:
            rows = await (await db.execute(
                "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (limit, offset))).fetchall()
        return [self._row_to_task(r) for r in rows]

    async def stats_overview(self) -> dict[str, Any]:
        """管理控制台概览：任务状态计数 + token/成本合计 + 分模型用量。"""
        async with aiosqlite.connect(self.db_path) as db:
            by_status = dict(await (await db.execute(
                "SELECT status, COUNT(*) FROM tasks GROUP BY status")).fetchall())
            totals = await (await db.execute(
                "SELECT COALESCE(SUM(tokens_in),0), COALESCE(SUM(tokens_out),0),"
                " COALESCE(SUM(cost),0) FROM audit_logs WHERE kind IN ('llm_run','llm_request')")).fetchone()
            by_model = await (await db.execute(
                "SELECT model, COUNT(*), SUM(tokens_in), SUM(tokens_out), SUM(cost)"
                " FROM audit_logs WHERE kind IN ('llm_run','llm_request') GROUP BY model")).fetchall()
        return {
            "tasks_by_status": by_status,
            "tasks_total": sum(by_status.values()),
            "tokens_in": int(totals[0]), "tokens_out": int(totals[1]),
            "cost_total": round(float(totals[2]), 4),
            "by_model": [{"model": m, "runs": c, "tokens_in": int(ti or 0),
                          "tokens_out": int(to or 0), "cost": round(float(co or 0), 4)}
                         for m, c, ti, to, co in by_model],
        }

    async def list_audit(self, limit: int = 100) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            rows = await (await db.execute(
                "SELECT task_id,seq,kind,model,tokens_in,tokens_out,cost,latency_ms,created_at"
                " FROM audit_logs ORDER BY id DESC LIMIT ?", (limit,))).fetchall()
        cols = ["task_id", "seq", "kind", "model", "tokens_in", "tokens_out",
                "cost", "latency_ms", "created_at"]
        return [dict(zip(cols, r)) for r in rows]

    async def get_task(self, task_id: str) -> Task | None:
        async with aiosqlite.connect(self.db_path) as db:
            row = await (await db.execute("SELECT * FROM tasks WHERE id=?", (task_id,))).fetchone()
        return self._row_to_task(row) if row else None

    async def find_by_dedup_key(self, key: str, ttl_sec: int) -> Task | None:
        """只匹配在途任务（pending/running）：已完成的相同问题/变更允许再次触发。"""
        since = time.time() - ttl_sec
        async with aiosqlite.connect(self.db_path) as db:
            row = await (await db.execute(
                "SELECT * FROM tasks WHERE dedup_key=? AND created_at>?"
                " AND status IN ('pending','running')"
                " ORDER BY created_at DESC LIMIT 1",
                (key, since))).fetchone()
        return self._row_to_task(row) if row else None

    async def list_stale_running(self, timeout_sec: int) -> list[Task]:
        before = time.time() - timeout_sec
        async with aiosqlite.connect(self.db_path) as db:
            rows = await (await db.execute(
                "SELECT * FROM tasks WHERE status=? AND created_at<?",
                (TaskStatus.RUNNING.value, before))).fetchall()
        return [self._row_to_task(r) for r in rows]

    async def log_audit(self, entry: dict[str, Any]) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO audit_logs(task_id,seq,kind,model,tokens_in,tokens_out,cost,latency_ms,payload,created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (entry["task_id"], entry.get("seq", 0), entry["kind"], entry.get("model"),
                 entry.get("tokens_in", 0), entry.get("tokens_out", 0), entry.get("cost", 0.0),
                 entry.get("latency_ms", 0), _payload_json(entry.get("payload")),
                 time.time()))
            await db.commit()

    async def log_audit_batch(self, entries: list[dict[str, Any]]) -> None:
        """同一任务的一批审计条目一次事务写入：要么全落要么不落，且只开关一次连接。"""
        if not entries:
            return
        now = time.time()
        async with aiosqlite.connect(self.db_path) as db:
            await db.executemany(
                "INSERT INTO audit_logs(task_id,seq,kind,model,tokens_in,tokens_out,cost,latency_ms,payload,created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                [(e["task_id"], e.get("seq", 0), e["kind"], e.get("model"),
                  e.get("tokens_in", 0), e.get("tokens_out", 0), e.get("cost", 0.0),
                  e.get("latency_ms", 0), _payload_json(e.get("payload")), now)
                 for e in entries])
            await db.commit()

    async def next_audit_seq(self, task_id: str) -> int:
        """任务内下一条审计序号（基于 MAX(seq)，不读全量回放）。"""
        async with aiosqlite.connect(self.db_path) as db:
            row = await (await db.execute(
                "SELECT COALESCE(MAX(seq),0)+1 FROM audit_logs WHERE task_id=?",
                (task_id,))).fetchone()
        return int(row[0])

    async def get_audit_trail(self, task_id: str) -> list[dict[str, Any]]:
        cols = ["id", "task_id", "seq", "kind", "model", "tokens_in",
                "tokens_out", "cost", "latency_ms", "payload", "created_at"]
        async with aiosqlite.connect(self.db_path) as db:
            rows = await (await db.execute(
                "SELECT id,task_id,seq,kind,model,tokens_in,tokens_out,cost,latency_ms,payload,created_at"
                " FROM audit_logs WHERE task_id=? ORDER BY seq", (task_id,))).fetchall()
        out = []
        for r in rows:
            entry = dict(zip(cols, r))
            entry["payload"] = json.loads(entry["payload"] or "{}")
            out.append(entry)
        return out

    async def save_message_history(self, task_id: str, history: bytes) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("INSERT OR REPLACE INTO task_histories VALUES (?,?,?)",
                             (task_id, history, time.time()))
            await db.commit()

    async def load_message_history(self, task_id: str) -> bytes | None:
        async with aiosqlite.connect(self.db_path) as db:
            row = await (await db.execute(
                "SELECT message_history FROM task_histories WHERE task_id=?", (task_id,))).fetchone()
        return row[0] if row else None

    async def save_feedback(self, task_id: str, user_id: str, score: int, comment: str = "") -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("INSERT INTO feedback(task_id,user_id,score,comment,created_at) VALUES (?,?,?,?,?)",
                             (task_id, user_id, score, comment, time.time()))
            await db.commit()

    # ── 配置版本号（热更新信标：控制台写入后 +1，Agent 进程轮询感知）──
    async def config_version(self) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            row = await (await db.execute(
                "SELECT value FROM meta WHERE key='config_version'")).fetchone()
        return int(row[0]) if row else 0

    async def bump_config_version(self) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO meta(key,value) VALUES('config_version','1')"
                " ON CONFLICT(key) DO UPDATE SET"
                " value=CAST(CAST(value AS INTEGER)+1 AS TEXT)")
            await db.commit()
            row = await (await db.execute(
                "SELECT value FROM meta WHERE key='config_version'")).fetchone()
        return int(row[0])

    # ── 订阅（去重拦截的触发者，任务完成时一并 @）──────────
    async def add_subscriber(self, task_id: str, requester: Requester) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT OR IGNORE INTO task_subscribers VALUES (?,?,?,?)",
                (task_id, requester.user_id,
                 json.dumps(vars(requester), ensure_ascii=False), time.time()))
            await db.commit()

    async def list_subscribers(self, task_id: str) -> list[Requester]:
        async with aiosqlite.connect(self.db_path) as db:
            rows = await (await db.execute(
                "SELECT requester FROM task_subscribers WHERE task_id=? ORDER BY created_at",
                (task_id,))).fetchall()
        return [Requester(**json.loads(r[0])) for r in rows]

    @staticmethod
    def _row_to_task(row) -> Task:
        (tid, cap, params, status, dedup, requester, url, result_text, err,
         created, finished) = row
        return Task(id=tid, capability=cap, params=json.loads(params),
                    requester=Requester(**json.loads(requester)), dedup_key=dedup,
                    status=TaskStatus(status), report_url=url, result_text=result_text,
                    error=err, created_at=created, finished_at=finished)
