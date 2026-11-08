"""模型工厂测试：thinking 开关按用途注入 extra_body。

跑法：uv run pytest tests/test_llm_factory.py
"""
from __future__ import annotations

from app.llm.factory import build_model, for_purpose
from app.llm.registry import ModelRegistry

CFG = {
    "models": {
        "m1": {"base_url": "http://x", "api_key_env": "LLM_POOL_KEY",
               "model": "deepseek-flash", "input_price": 1.0, "output_price": 2.0},
        "m2": {"base_url": "http://x", "api_key_env": "LLM_POOL_KEY",
               "model": "deepseek-v4-pro", "input_price": 3.0, "output_price": 4.0},
    },
    "routing": {
        "review": {"primary": "m1", "fallback": ["m2"]},
        "intent": {"primary": "m1", "fallback": ["m2"], "thinking": "disabled"},
    },
}


def test_build_model_without_thinking_has_no_extra_body(monkeypatch):
    monkeypatch.setenv("LLM_POOL_KEY", "test")
    model = build_model(ModelRegistry(CFG).entry("m1"))
    assert model.settings is None


def test_build_model_with_thinking_disabled(monkeypatch):
    monkeypatch.setenv("LLM_POOL_KEY", "test")
    model = build_model(ModelRegistry(CFG).entry("m1"), thinking="disabled")
    assert model.settings["extra_body"] == {"thinking": {"type": "disabled"}}


def test_for_purpose_injects_route_thinking(monkeypatch):
    monkeypatch.setenv("LLM_POOL_KEY", "test")
    reg = ModelRegistry(CFG)
    intent_model = for_purpose(reg, "intent")
    review_model = for_purpose(reg, "review")
    # FallbackModel 包一层，取底层第一个模型看 settings
    assert intent_model.models[0].settings["extra_body"] == {
        "thinking": {"type": "disabled"}}
    assert review_model.models[0].settings is None


def test_for_purpose_unknown_purpose_raises():
    reg = ModelRegistry(CFG)
    try:
        for_purpose(reg, "nonexistent")
    except KeyError as e:
        assert "未定义的模型用途" in str(e)
    else:
        raise AssertionError("未定义用途应报 KeyError")
