"""ReportSink：报告归档抽象（设计文档 §3.5.2）。"""
from __future__ import annotations

from typing import Protocol

from app.agent.schemas import ReviewReport
from app.engine.models import Task


class ReportSink(Protocol):
    async def archive(self, task: Task, report: ReviewReport) -> str:
        """把报告渲染并归档，返回可访问的文档链接。"""
        ...
