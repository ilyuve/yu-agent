"""报告结构定义（设计文档 §7）——Agent 的 output_type，框架保证结构合法。"""
from __future__ import annotations

from pydantic import BaseModel, Field


class SlowQueryRisk(BaseModel):
    severity: str = Field(description="high / medium / low")
    location: str = Field(description="文件路径:行号")
    rule_id: str | None = Field(default=None, description="命中的预扫规则 ID，如 SQ001")
    reason: str = Field(description="为什么有慢查风险")
    suggestion: str = Field(description="修复建议")


class CrSuggestion(BaseModel):
    category: str = Field(description="类别：命名/异常处理/事务边界/日志脱敏/并发安全/接口兼容性 等")
    location: str = Field(description="文件路径:行号")
    description: str
    example: str | None = None


class ReviewReport(BaseModel):
    summary: str = Field(description="一段式结论摘要，用于群消息推送")
    slow_query_risks: list[SlowQueryRisk] = Field(default_factory=list)
    cr_suggestions: list[CrSuggestion] = Field(default_factory=list)

    def risk_counts(self) -> dict[str, int]:
        counts = {"high": 0, "medium": 0, "low": 0}
        for r in self.slow_query_risks:
            counts[r.severity] = counts.get(r.severity, 0) + 1
        return counts
