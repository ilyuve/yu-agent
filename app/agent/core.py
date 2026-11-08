"""Agent 内核（设计文档 §3.3）：capability.yaml → Pydantic AI Agent 组装与执行。"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

import yaml
from pydantic_ai import Agent
from pydantic_ai.messages import (
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.usage import UsageLimits

from app.agent import skill_loader
from app.agent.schemas import ReviewReport
from app.engine import audit as audit_mod
from app.engine.models import Task

log = logging.getLogger(__name__)

_OUTPUTS = {"review_report": ReviewReport, "text": str}   # text：问答类自由文本输出

# 群会话上下文：同一（群, 人）保留最近 N 条消息，支持追问
_HISTORY_KEEP = 20


class CapabilityRegistry:
    def __init__(self, capability_dirs: list[str]):
        self.caps: dict[str, dict] = {}
        for d in capability_dirs:
            d = Path(d)
            cap = yaml.safe_load((d / "capability.yaml").read_text(encoding="utf-8"))
            cap["_dir"] = str(d)
            self.caps[cap["name"]] = cap

    def get(self, name: str) -> dict:
        if name not in self.caps:
            raise KeyError(f"未注册的能力: {name}（已有: {list(self.caps)}）")
        return self.caps[name]

    def command_map(self) -> dict[str, str]:
        """触发命令 → 能力名，供 Adapter 路由。"""
        return {cap["triggers"]["command"]: name for name, cap in self.caps.items()}


async def run(task: Task, ctx) -> ReviewReport | str:
    """ctx: RunContext(capability_registry, model_registry, toolset_registry, store, settings)"""
    cap = ctx.capabilities.get(task.capability)
    instructions = skill_loader.load_skills(cap["_dir"], cap.get("skill_names", []))
    toolsets = ctx.resolve_toolsets(cap.get("toolsets", []))
    model = ctx.model_for(cap["model_purpose"])

    # 问答类能力挂会话历史：同一（通道, 群, 人）上下文连续，群与群、人与人隔离；
    # 审查类（review_pipeline）无状态——每次以分支当前快照为准，不带历史
    session_key = None
    history = None
    if cap["output"] == "text":
        r = task.requester
        session_key = f"{r.channel}:{r.conversation_id}:{r.user_id}"
        raw = await ctx.store.load_message_history(session_key)
        if raw:
            history = ModelMessagesTypeAdapter.validate_json(raw)

    agent = Agent(
        model=model,
        instructions=instructions,
        toolsets=toolsets,
        output_type=_OUTPUTS[cap["output"]],
        retries=2,
    )
    t0 = time.perf_counter()
    try:
        result = await agent.run(
            task.describe(),
            deps=ctx,
            message_history=history,
            usage_limits=UsageLimits(request_limit=ctx.settings["app"]["server"]["max_steps"]),
        )
    except Exception as e:
        # 失败任务也必须有审计数据（§12）：环节 + 异常 + 完整堆栈，之后原样上抛
        latency_ms = int((time.perf_counter() - t0) * 1000)
        await audit_mod.record_error(ctx.store, task.id, "agent_run", e, latency_ms)
        raise
    latency_ms = int((time.perf_counter() - t0) * 1000)

    if session_key is not None:
        await ctx.store.save_message_history(
            session_key,
            ModelMessagesTypeAdapter.dump_json(result.all_messages()[-_HISTORY_KEEP:]))

    # 审计（§12 ①②）：从消息时间戳重建每次 LLM 请求与每次工具调用，逐条落库。
    # 事后重建而非循环内打钩子——Agent 执行热路径零侵入。
    await _record_steps(task, ctx, cap, result, latency_ms)
    return result.output


def _price_entry(ctx, cap: dict, model_name: str | None):
    """按真实命中的模型名找注册表条目（算单价）；匹配不到回退路由链首。

    ctx.model_registry 在测试替身里可能没有 .models 全量表，容错跳过精确匹配。
    """
    chain = ctx.model_registry.chain_for(cap["model_purpose"])
    models = getattr(ctx.model_registry, "models", None) or {}
    if model_name:
        for entry in models.values():
            if getattr(entry, "model", None) == model_name or getattr(entry, "key", None) == model_name:
                return entry
    return chain[0]


def _build_step_entries(task: Task, ctx, cap: dict, result) -> list[dict[str, Any]]:
    """把一次 agent.run 的消息流拆成 llm_request / tool_call 审计条目（按时间排序）。

    时序结构：ModelRequest(t) → ModelResponse(t') 是一次 LLM 请求，latency = t'-t；
    ModelResponse 里的 ToolCallPart 与下一条 ModelRequest 里的 ToolReturnPart 配对，
    工具耗时 = return.timestamp - call 所在 response.timestamp。
    """
    entries: list[dict[str, Any]] = []
    pending_calls: dict[str, tuple[str, str, float]] = {}   # tool_call_id -> (名称, 参数摘要, 发起时刻)
    prev_ts: float | None = None
    llm_round = 0

    for msg in result.all_messages():
        ts = msg.timestamp.timestamp()
        if isinstance(msg, ModelRequest):
            # 工具返回与调用配对
            for part in msg.parts:
                if isinstance(part, ToolReturnPart):
                    name, args_summary, call_ts = pending_calls.pop(
                        part.tool_call_id, (part.tool_name, "", ts))
                    ok = getattr(part, "outcome", None) != "error"
                    entries.append({
                        "task_id": task.id, "kind": "tool_call",
                        "latency_ms": max(0, int((ts - call_ts) * 1000)),
                        "payload": {
                            "capability": task.capability,
                            "tool": name,
                            "args": args_summary,
                            "ok": ok,
                            "result": audit_mod.summarize(part.content),
                        },
                    })
            prev_ts = ts
        elif isinstance(msg, ModelResponse):
            llm_round += 1
            usage = getattr(msg, "usage", None)
            tokens_in = getattr(usage, "input_tokens", 0) or 0
            tokens_out = getattr(usage, "output_tokens", 0) or 0
            model_name = msg.model_name or ""
            entry = _price_entry(ctx, cap, model_name or None)
            entries.append({
                "task_id": task.id, "kind": "llm_request",
                "model": model_name or entry.key,
                "tokens_in": tokens_in, "tokens_out": tokens_out,
                "cost": entry.cost(tokens_in, tokens_out),
                "latency_ms": max(0, int((ts - prev_ts) * 1000)) if prev_ts else 0,
                "payload": {"capability": task.capability, "round": llm_round},
            })
            for part in msg.parts:
                if isinstance(part, ToolCallPart):
                    pending_calls[part.tool_call_id] = (
                        part.tool_name, audit_mod.summarize(part.args), ts)
            prev_ts = ts
    return entries


async def _record_steps(task: Task, ctx, cap: dict, result, run_latency_ms: int) -> None:
    try:
        entries = _build_step_entries(task, ctx, cap, result)
        base = await ctx.store.next_audit_seq(task.id)
        for i, e in enumerate(entries):
            e["seq"] = base + i
        await audit_mod.safe_log_batch(ctx.store, entries)
    except Exception:
        # 审计重建失败不影响任务结果
        log.warning("任务 %s 审计重建失败（已忽略）", task.id, exc_info=True)
