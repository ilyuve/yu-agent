"""LLM 意图分类（设计文档 §3.4 意图识别，自 P2 提前落地）。

- 走公司模型池 OpenAI 兼容 API，复用 LLM_POOL_KEY，无需本地模型；
  用 models.yaml 的 intent 用途路由到便宜模型，输出限制为小 JSON，单次调用成本可忽略；
- LLM 只做"从自然语言抽参数"（能力/仓库/分支/置信度）；
  能力是否合法、仓库是否配置，全部由 engine/router.py 确定性判断——LLM 无权决定能不能审。
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable

from pydantic import BaseModel, Field
from pydantic_ai import Agent


class IntentResult(BaseModel):
    capability: str = Field(description="识别出的能力名，必须来自给定能力清单")
    repo: str | None = Field(default=None, description="代码仓库名（从消息或 GitLab 链接中提取）")
    branch: str | None = Field(default=None, description="分支名")
    mr: str | None = Field(default=None, description="MR 编号")
    confidence: float = Field(ge=0.0, le=1.0, description="识别置信度")


def build_classifier(ctx) -> Callable[[str], Awaitable[IntentResult]]:
    """装配意图分类器。ctx 需要 capabilities / settings / model_for。"""
    cap_lines = []
    for name, cap in ctx.capabilities.caps.items():
        desc = cap.get("description", "")
        cmd = cap.get("triggers", {}).get("command", "")
        cap_lines.append(f"- {name}（命令 {cmd}）：{desc}")
    whitelist = ctx.settings["app"]["codehost"]["repo_whitelist"]

    instructions = (
        "你是消息意图分类器。从用户消息中识别意图并抽取参数，只输出结构化结果。\n\n"
        "可用能力清单：\n" + "\n".join(cap_lines) + "\n\n"
        f"已配置仓库清单：{', '.join(whitelist)}\n\n"
        "规则：\n"
        "1. 用户给出 GitLab 链接时，从 URL 中提取仓库名与分支名（/-/tree/<branch>、"
        "/-/merge_requests/<id>）；\n"
        "2. 仓库名做模糊匹配（如\"订单服务\"→order-service），匹配不上填原词；\n"
        "3. 只是提问、没有审查意图时，归到问答能力，params 不需要 repo；\n"
        "4. 拿不准就给低 confidence，不要硬猜。"
    )
    agent = Agent(model=ctx.model_for("intent"), output_type=IntentResult,
                  retries=1, instructions=instructions)

    async def classify(text: str) -> IntentResult:
        result = await agent.run(text)
        return result.output

    return classify
