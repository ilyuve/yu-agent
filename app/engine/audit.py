"""审计埋点工具（设计文档 §12 ① Tracing 落地）。

- `stage(...)`：编排环节计时上下文管理器。正常结束落 kind='stage'，
  抛异常时先落 kind='error'（含 traceback）再原样上抛——失败任务也能看到"死在哪一步"。
- 所有审计写入都经 `_safe_log`：落库失败只打警告，绝不拖垮主流程。

粒度约定：stage（编排环节）/ llm_request（单次 LLM 请求）/ tool_call（单次工具调用）
/ error（异常）；llm_run 为旧版任务汇总行，新代码不再写入。
"""
from __future__ import annotations

import logging
import time
import traceback
from contextlib import asynccontextmanager
from typing import Any

log = logging.getLogger(__name__)

# 单字段摘要上限（store 层还有整体 payload 截断兜底）
_TRACE_MAX_CHARS = 3500
_SUMMARY_MAX_CHARS = 500


def summarize(value: Any, limit: int = _SUMMARY_MAX_CHARS) -> str:
    """把工具参数/返回压成单行摘要，超限截断。"""
    text = str(value).replace("\n", " ")
    return text if len(text) <= limit else text[:limit] + "…"


async def _safe_log(store, entry: dict[str, Any]) -> None:
    try:
        await store.log_audit(entry)
    except Exception:
        log.warning("审计落库失败（已忽略，不影响任务）: task=%s kind=%s",
                    entry.get("task_id"), entry.get("kind"), exc_info=True)


async def safe_log_batch(store, entries: list[dict[str, Any]]) -> None:
    try:
        await store.log_audit_batch(entries)
    except Exception:
        log.warning("审计批量落库失败（已忽略，不影响任务）: task=%s 共 %d 条",
                    entries[0].get("task_id") if entries else "?", len(entries), exc_info=True)


async def record_error(store, task_id: str, stage: str, exc: BaseException,
                       latency_ms: int | None = None) -> None:
    """异常落审计：环节名 + 异常信息 + 完整堆栈（截断）。"""
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    if len(tb) > _TRACE_MAX_CHARS:
        tb = "…<前面已截断>…\n" + tb[-_TRACE_MAX_CHARS:]
    await _safe_log(store, {
        "task_id": task_id, "seq": await _next_seq(store, task_id),
        "kind": "error", "latency_ms": latency_ms or 0,
        "payload": {"stage": stage, "error": f"{type(exc).__name__}: {exc}",
                    "traceback": tb},
    })


async def _next_seq(store, task_id: str) -> int:
    try:
        return await store.next_audit_seq(task_id)
    except Exception:
        log.warning("审计序号获取失败，回退为 0: task=%s", task_id, exc_info=True)
        return 0


@asynccontextmanager
async def stage(store, task_id: str, name: str, payload: dict | None = None,
                record_failure: bool = True):
    """编排环节计时埋点：with 块正常结束落 stage 行，异常落 error 行再上抛。

    record_failure=False 用于内部已有更细异常记录的环节（如 agent_run 由
    agent.core 落 error），避免同一失败写两行。
    """
    t0 = time.perf_counter()
    try:
        yield
    except Exception as e:
        if record_failure:
            latency_ms = int((time.perf_counter() - t0) * 1000)
            await record_error(store, task_id, name, e, latency_ms)
        raise
    latency_ms = int((time.perf_counter() - t0) * 1000)
    await _safe_log(store, {
        "task_id": task_id, "seq": await _next_seq(store, task_id),
        "kind": "stage", "latency_ms": latency_ms,
        "payload": {"stage": name, **(payload or {})},
    })
