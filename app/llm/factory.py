"""模型工厂：models.yaml → OpenAIChatModel + FallbackModel（设计文档 §3.6）。"""
from __future__ import annotations

import os

from pydantic_ai.models.fallback import FallbackModel
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIChatModelSettings
from pydantic_ai.providers.openai import OpenAIProvider

from app.llm.registry import ModelEntry, ModelRegistry


def build_model(entry: ModelEntry, thinking: str | None = None) -> OpenAIChatModel:
    api_key = os.environ.get(entry.api_key_env)
    if not api_key:
        raise RuntimeError(f"缺少环境变量 {entry.api_key_env}（模型 {entry.key}）")
    # thinking 开关：DeepSeek V4.1 思考默认开，轻任务（intent）在路由里配
    # "disabled" 关掉；非 DeepSeek 端点不认识该参数时，把路由里的 thinking 去掉即可
    settings = None
    if thinking:
        settings = OpenAIChatModelSettings(extra_body={"thinking": {"type": thinking}})
    return OpenAIChatModel(
        entry.model,
        provider=OpenAIProvider(base_url=entry.base_url, api_key=api_key),
        settings=settings,
    )


def for_purpose(registry: ModelRegistry, purpose: str) -> FallbackModel:
    """按用途返回带降级链的模型。主模型异常（超时/限流/5xx）时框架自动落备。"""
    chain = registry.chain_for(purpose)        # 先走校验（未定义用途报友好错误）
    thinking = registry.routing[purpose].thinking
    return FallbackModel(*[build_model(e, thinking) for e in chain])
