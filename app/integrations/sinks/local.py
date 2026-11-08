"""本地文件 ReportSink：报告渲染为 markdown 落盘（开发/内网未配文档库时的兜底）。

归档是任务完成的固定收尾动作，不走 MCP、不经模型，由编排层硬编码调用。
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

from app.agent.schemas import ReviewReport
from app.engine.models import Task
from app.integrations.sinks.dingdoc import DingDocSink

log = logging.getLogger(__name__)


class LocalFileSink:
    def __init__(self, reports_dir: str):
        self.reports_dir = Path(reports_dir)
        self.reports_dir.mkdir(parents=True, exist_ok=True)

    async def archive(self, task: Task, report: ReviewReport) -> str:
        markdown = DingDocSink.render(task, report)     # 渲染格式复用，与文档库一致
        repo = re.sub(r"[^\w-]", "_", str(task.params.get("repo", "misc")))
        path = self.reports_dir / repo / f"{task.id}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(markdown, encoding="utf-8")
        log.info("报告已归档到本地文件 %s", path)
        return str(path)
