"""流程分发机制测试：能力声明的 flow 必须已注册，且装配出可调用 runner。

跑法：uv run pytest tests/test_flows.py
"""
from __future__ import annotations

from types import SimpleNamespace

from app.agent.core import CapabilityRegistry
from app.config import load_settings
from app.engine import flows
from app.engine.models import Requester, Task


def _ctx(settings: dict) -> SimpleNamespace:
    # 装配 runner 只需 settings（builder 在调用时才碰 codehost/sink/store）
    return SimpleNamespace(settings=settings, codehost=None, sink=None,
                           store=None, capabilities=None, model_registry=None,
                           toolsets={})


def test_every_capability_declares_registered_flow():
    """capability.yaml 的 flow 字段必须在 flows 注册表里——启动 fail fast 的保障。"""
    settings = load_settings()
    caps = CapabilityRegistry(settings["app"]["capabilities"])

    assert len(caps.caps) >= 2, "至少应有 code-review 与 qa 两个能力"
    runners = {}
    for name, cap in caps.caps.items():
        flow = cap.get("flow")
        assert flow, f"能力 {name} 未声明 flow"
        runners[name] = flows.get_runner(flow, _ctx(settings))   # 未注册会抛 KeyError
        assert callable(runners[name])


def test_flows_match_capability_intent():
    """编排混合只用于审查类；问答走纯 Agent。"""
    settings = load_settings()
    caps = CapabilityRegistry(settings["app"]["capabilities"])

    assert caps.get("code-review")["flow"] == "review_pipeline"
    assert caps.get("qa")["flow"] == "agent_only"
    assert caps.command_map() == {"review": "code-review", "ask": "qa"}


def test_task_describe_generic_paths():
    """describe()：问答走 prompt，审查走仓库描述，都不该炸。"""
    req = Requester(channel="test", conversation_id="c1", user_id="u1")
    qa_task = Task(capability="qa", params={"prompt": "什么是覆盖索引？"}, requester=req, id="R1")
    review_task = Task(capability="code-review",
                       params={"repo": "order-service", "branch": "feature/x"},
                       requester=req, id="R2")

    assert qa_task.describe() == "什么是覆盖索引？"
    assert "order-service" in review_task.describe()
    assert "feature/x" in review_task.describe()
