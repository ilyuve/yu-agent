"""钉钉文档库 ReportSink 实现（设计文档 §3.5.2）。

注意：归档是任务完成的固定收尾动作，不走 MCP、不经模型，由编排层硬编码调用。
"""
from __future__ import annotations

import logging

import httpx

from app.agent.schemas import ReviewReport
from app.engine.models import Task
from app.integrations.dingtalk_auth import DingTalkAuth

log = logging.getLogger(__name__)

_API_BASE = "https://api.dingtalk.com"


class DingDocSink:
    """归档到钉钉知识库：创建文档 → overwriteContent 写入 markdown。

    需要应用权限：Document.WorkspaceDocument.Write；
    operator_id 是操作人的 unionId（文档 API 强制要求，建议用专用运维账号）。
    """

    def __init__(self, workspace_id: str, operator_id: str, auth: DingTalkAuth,
                 http: httpx.AsyncClient | None = None):
        self.workspace_id = workspace_id
        self.operator_id = operator_id
        self.auth = auth
        self._http = http or auth.http

    async def archive(self, task: Task, report: ReviewReport) -> str:
        markdown = self.render(task, report)
        if len(markdown) > 49000:                       # overwriteContent 上限 50000
            markdown = markdown[:49000] + "\n\n> 内容过长已截断，完整报告见任务审计。"
        p = task.params
        title = f"【审查】{p.get('repo')}@{p.get('branch') or p.get('mr')} {task.id}"
        headers = await self.auth.auth_headers()

        # ① 知识库内创建空文档（operatorId 官方文档要求在请求体里，放 query 会报 MissingoperatorId）
        r = await self._http.post(
            f"{_API_BASE}/v1.0/doc/workspaces/{self.workspace_id}/docs",
            headers=headers,
            json={"name": title, "docType": "DOC", "operatorId": self.operator_id})
        r.raise_for_status()
        doc = r.json()
        doc_key = doc.get("docKey") or doc.get("nodeId")

        # ② markdown 覆写内容
        r = await self._http.post(
            f"{_API_BASE}/v1.0/doc/suites/documents/{doc_key}/overwriteContent",
            headers=headers, params={"operatorId": self.operator_id},
            json={"dataType": "markdown", "content": markdown})
        r.raise_for_status()

        url = doc.get("url") or f"https://alidocs.dingtalk.com/i/nodes/{doc_key}"
        log.info("报告已归档到钉钉文档: %s", url)
        return url

    @staticmethod
    def render(task: Task, report: ReviewReport) -> str:
        p = task.params
        lines = [
            f"# 【审查】{p.get('repo')}@{p.get('branch') or p.get('mr')} {task.id}",
            "",
            "> AI 初审，仅供参考；终审以人工 CR 为准。",
            "",
            "## 结论摘要",
            report.summary,
            "",
            f"## 慢查询风险（{len(report.slow_query_risks)}）",
        ]
        for r in report.slow_query_risks:
            lines += [f"- **[{r.severity.upper()}]** `{r.location}`"
                      + (f"（{r.rule_id}）" if r.rule_id else ""),
                      f"  - 原因：{r.reason}",
                      f"  - 建议：{r.suggestion}"]
        lines.append(f"\n## CR 建议（{len(report.cr_suggestions)}）")
        for c in report.cr_suggestions:
            lines.append(f"- **[{c.category}]** `{c.location}`：{c.description}")
        return "\n".join(lines)
