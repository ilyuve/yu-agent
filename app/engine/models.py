"""任务模型与状态机（设计文档 §3.2）。"""
from __future__ import annotations

import hashlib
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


@dataclass
class Requester:
    """通道无关的发起人抽象（不存任何钉钉/飞书特有字段）。"""
    channel: str            # "dingtalk" / "feishu" / "cron" / "api"
    conversation_id: str
    user_id: str
    nick: str = ""
    session_webhook: str | None = None        # 通道快速回复通道（钉钉 sessionWebhook）
    conversation_type: str = "group"          # "group" / "direct"（单聊），决定结果回推走哪个接口
    union_id: str | None = None               # 通道全局唯一 ID（钉钉 senderId=unionId），文档归档归属用


@dataclass
class Task:
    capability: str
    params: dict            # repo, branch/mr, review_type...
    requester: Requester
    id: str = ""
    dedup_key: str = ""
    status: TaskStatus = TaskStatus.PENDING
    report_url: str | None = None
    result_text: str | None = None     # 问答类能力的直接回复内容（agent_only 流程）
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None

    def describe(self) -> str:
        # 问答/通用能力：原始提问直接作为 prompt
        if self.params.get("prompt"):
            return str(self.params["prompt"])
        p = self.params
        if p.get("repo"):
            ref = f"分支 {p['branch']}" if p.get("branch") else f"MR {p.get('mr')}"
            text = (f"审查任务 {self.id}：仓库 {p['repo']}，{ref}，"
                    f"类型 {p.get('review_type', 'all')}。"
                    f"请结合静态预扫结果与 Skill 规则完成审查，输出结构化报告。")
            if p.get("workdir"):
                # 编排层浅克隆的本地快照路径：工具（预扫/只读命令）都基于这个目录
                text += (f"\n代码已浅克隆到本地目录：{p['workdir']}"
                         f"（调用工具时 workdir 参数传这个路径，先跑预扫再针对性深审）。")
            return text
        return f"任务 {self.id}（能力 {self.capability}），参数：{p}"


def new_task(capability: str, params: dict, requester: Requester,
             prefix: str, head_commit: str) -> Task:
    seq = uuid.uuid4().hex[:6]
    if params.get("repo"):
        # 代码类能力：同一变更（repo+ref+head）无论谁触发都视为重复，避免重复跑昂贵审查
        raw = f"{params['repo']}|{params.get('branch') or params.get('mr')}|{head_commit}|{capability}"
    else:
        # 问答类能力：去重键 = 人 + 问题内容，不同消息/不同人互不误伤
        raw = f"{capability}|{requester.user_id}|{params.get('prompt', '')}"
    return Task(
        id=f"{prefix}{seq}",
        capability=capability,
        params=params,
        requester=requester,
        dedup_key=hashlib.sha256(raw.encode()).hexdigest()[:24],
    )
