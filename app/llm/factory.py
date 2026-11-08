"""模型工厂：models.yaml → OpenAIChatModel + FallbackModel（设计文档 §3.6）。"""
from __future__ import annotations

import os

from pydantic_ai.models.fallback import FallbackModel
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from app.llm.registry import ModelEntry, ModelRegistry


def build_model(entry: ModelEntry) -> OpenAIChatModel:
    api_key = os.environ.get(entry.api_key_env)
    if not api_key:
        raise RuntimeError(f"缺少环境变量 {entry.api_key_env}（模型 {entry.key}）")
    return OpenAIChatModel(
        entry.model,
        provider=OpenAIProvider(base_url=entry.base_url, api_key=api_key),
    )


def for_purpose(registry: ModelRegistry, purpose: str) -> FallbackModel:
    """按用途返回带降级链的模型。主模型异常（超时/限流/5xx）时框架自动落备。"""
    chain = registry.chain_for(purpose)
    return FallbackModel(*[build_model(e) for e in chain])
