"""模型注册表加载与校验（设计文档 §3.6）。"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ModelEntry:
    key: str
    base_url: str
    api_key_env: str
    model: str
    capabilities: list[str] = field(default_factory=list)
    context_window: int = 0
    input_price: float = 0.0      # 元/百万 tokens
    output_price: float = 0.0

    def cost(self, tokens_in: int, tokens_out: int) -> float:
        return (tokens_in * self.input_price + tokens_out * self.output_price) / 1_000_000


@dataclass
class Route:
    primary: str
    fallback: list[str] = field(default_factory=list)
    # thinking 开关（DeepSeek V4.1：思考默认开启；intent 这类轻任务可关）。
    # 取值 "enabled" / "disabled" / None（不写 = 跟随端点默认）
    thinking: str | None = None


class ModelRegistry:
    def __init__(self, cfg: dict):
        self.models: dict[str, ModelEntry] = {
            k: ModelEntry(key=k, **v) for k, v in cfg["models"].items()
        }
        self.routing: dict[str, Route] = {
            k: Route(**v) for k, v in cfg["routing"].items()
        }

    def chain_for(self, purpose: str) -> list[ModelEntry]:
        if purpose not in self.routing:
            raise KeyError(f"未定义的模型用途: {purpose}")
        route = self.routing[purpose]
        return [self.models[route.primary], *[self.models[m] for m in route.fallback]]

    def entry(self, key: str) -> ModelEntry:
        return self.models[key]
