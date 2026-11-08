"""模型工厂测试：thinking 开关按用途注入 extra_body。

跑法：uv run pytest tests/test_llm_factory.py
"""
from __future__ import annotations

from app.llm.factory import DeepSeekChatModel, build_model, for_purpose
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


def test_deepseek_model_disables_tool_forcing(monkeypatch):
    """DeepSeek 思考模式（服务端默认开）拒绝 tool_choice=required，必须声明不支持强制。"""
    monkeypatch.setenv("LLM_POOL_KEY", "test")
    cfg = {"models": {"ds": {**CFG["models"]["m1"],
                             "base_url": "https://api.deepseek.com/v1"}},
           "routing": {"review": {"primary": "ds"}}}
    model = for_purpose(ModelRegistry(cfg), "review").models[0]
    assert isinstance(model, DeepSeekChatModel)
    assert model._supports_tool_forcing(None, None) is False


def test_non_deepseek_endpoint_keeps_default_model(monkeypatch):
    """非 DeepSeek 端点不受此 hack 影响。"""
    monkeypatch.setenv("LLM_POOL_KEY", "test")
    cfg = {"models": {"other": {**CFG["models"]["m1"],
                                "base_url": "https://pool.example.com/v1"}},
           "routing": {"review": {"primary": "other"}}}
    model = for_purpose(ModelRegistry(cfg), "review").models[0]
    assert not isinstance(model, DeepSeekChatModel)
