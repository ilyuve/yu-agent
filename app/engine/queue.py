"""任务队列 + worker 池（设计文档 §3.2）。起步 asyncio.Queue，换 Redis 时保持接口不变。"""
from __future__ import annotations

import asyncio
import logging
import time
import traceback

from app.engine.models import Task, TaskStatus

log = logging.getLogger(__name__)


class TaskEngine:
    def __init__(self, store, workers: int, runner):
        """:param runner: 执行一个 Task 的协程函数（agent.core.run 的封装）"""
        self.store = store
        self.runner = runner
        self.queue: asyncio.Queue[Task] = asyncio.Queue()
        self.workers = workers
        self._tasks: list[asyncio.Task] = []
        # 任务终态回调（通道结果回推用），由装配层注入：on_task_done(task)
        self.on_task_done = None

    async def submit(self, task: Task, dedup_ttl_sec: int) -> tuple[Task, bool]:
        """幂等入队。返回 (任务, 是否新建)；重复提交返回已有任务。"""
        existing = await self.store.find_by_dedup_key(task.dedup_key, dedup_ttl_sec)
        if existing:
            return existing, False
        task.status = TaskStatus.PENDING
        await self.store.save_task(task)
        await self.queue.put(task)
        return task, True

    def queue_position(self) -> int:
        return self.queue.qsize()

    async def start(self) -> None:
        # 崩溃恢复：RUNNING 超时任务重置（断点续跑接入 durable execution 后在此升级）
        for stale in await self.store.list_stale_running(timeout_sec=3600):
            stale.status = TaskStatus.PENDING
            await self.store.save_task(stale)
            await self.queue.put(stale)
            log.info("恢复未完成任务 %s", stale.id)
        for i in range(self.workers):
            self._tasks.append(asyncio.create_task(self._worker(i)))

    async def _worker(self, idx: int) -> None:
        while True:
            task = await self.queue.get()
            task.status = TaskStatus.RUNNING
            await self.store.save_task(task)
            try:
                await self.runner(task)
                task.status = TaskStatus.DONE
            except Exception as e:
                log.exception("任务 %s 失败", task.id)
                task.status = TaskStatus.FAILED
                # 存完整堆栈（截断兜底）：控制台与回推消息都能直接定位
                tb = "".join(traceback.format_exception(type(e), e, e.__traceback__))
                task.error = tb if len(tb) <= 3500 else "…<前面已截断>…\n" + tb[-3500:]
            task.finished_at = time.time()
            await self.store.save_task(task)
            self.queue.task_done()
            if self.on_task_done is not None:
                try:
                    await self.on_task_done(task)
                except Exception:
                    log.exception("任务 %s 终态回调失败（不影响任务结果）", task.id)
