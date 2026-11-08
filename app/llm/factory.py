"""模型工厂：models.yaml → OpenAIChatModel + FallbackModel（设计文档 §3.6）。"""
from __future__ import annotations

import os

from pydantic_ai.models.fallback import FallbackModel
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIChatModelSettings
from pydantic_ai.providers.openai import OpenAIProvider

from app.llm.registry import ModelEntry, ModelRegistry


class DeepSeekChatModel(OpenAIChatModel):
    """DeepSeek 专用：关闭 tool 强制（tool_choice=required）。

    DeepSeek V4.1 起思考模式为服务端默认开启，而思考模式拒绝
    tool_choice=required（实测 400："Thinking mode does not support
    this tool_choice"）；auto / 不传则 200 且照常调用工具。
    pydantic-ai 在结构化输出（output_type）场景默认会发 required，
    这里声明不支持强制，框架即退化为发送 auto，输出工具照常可用。
    """

    def _supports_tool_forcing(self, model_settings, model_request_parameters) -> bool:
        return False


def build_model(entry: ModelEntry, thinking: str | None = None) -> OpenAIChatModel:
    api_key = os.environ.get(entry.api_key_env)
    if not api_key:
        raise RuntimeError(f"缺少环境变量 {entry.api_key_env}（模型 {entry.key}）")
    # thinking 开关：DeepSeek V4.1 思考默认开，轻任务（intent）在路由里配
    # "disabled" 关掉；非 DeepSeek 端点不认识该参数时，把路由里的 thinking 去掉即可
    settings = None
    if thinking:
        settings = OpenAIChatModelSettings(extra_body={"thinking": {"type": thinking}})
    cls = DeepSeekChatModel if "deepseek" in entry.base_url else OpenAIChatModel
    return cls(
        entry.model,
        provider=OpenAIProvider(base_url=entry.base_url, api_key=api_key),
        settings=settings,
    )


def for_purpose(registry: ModelRegistry, purpose: str) -> FallbackModel:
    """按用途返回带降级链的模型。主模型异常（超时/限流/5xx）时框架自动落备。"""
    chain = registry.chain_for(purpose)        # 先走校验（未定义用途报友好错误）
    thinking = registry.routing[purpose].thinking
    return FallbackModel(*[build_model(e, thinking) for e in chain])
