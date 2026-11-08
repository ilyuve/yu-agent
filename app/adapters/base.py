"""Adapter Protocol（设计文档 §3.1）——通道细节不许漏出本层。"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol

from app.engine.models import Task


class Adapter(Protocol):
    name: str

    async def start(self, on_task: Callable[[Task], Awaitable[None]]) -> None:
        """启动通道监听，收到触发消息后构造 Task 回调 on_task。"""
        ...

    async def notify_accepted(self, task: Task, queued_ahead: int = 0) -> None:
        """秒回受理（通道快速回复通道，如钉钉 sessionWebhook）。"""
        ...

    async def notify_result(self, task: Task, subscribers: list | None = None) -> None:
        """结果回推（通道主动消息接口，不依赖快速回复通道有效期）。
        消息内容从 task 字段组装：result_text（摘要/问答回复）、report_url、error。
        subscribers：被幂等去重拦下的其他触发者，完成时一并 @（§3.2 订阅机制）。"""
        ...
