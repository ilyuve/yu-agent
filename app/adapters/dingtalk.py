"""钉钉 Adapter（设计文档 §3.1）。

要点：
- dingtalk-stream 长连接收消息，无需公网回调；群里只有 @ 机器人的消息会推送；
  SDK 的 start_forever 是阻塞调用且自带事件循环，放线程里跑，
  消息经 run_coroutine_threadsafe 交回主事件循环后立即 ack（避免钉钉超时重投）；
- 受理回复走 sessionWebhook（快但会过期），失败自动降级为主动消息；
  结果回推按会话类型分流：群聊走 /v1.0/robot/groupMessages/send，
  单聊走 /v1.0/robot/privateChatMessages/send；
- access_token 进程内缓存，提前 60s 过期刷新；
- 安全闸：senderStaffId 为空（外部群/外部用户）直接丢弃；
- 消息 → 能力/参数的路由全部交给 engine.router.MessageRouter（通道无关），
  本文件不解析任何命令格式。
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable

from app.adapters import copywriting
from app.engine.models import Requester, Task, TaskStatus
from app.engine.router import MessageRouter
from app.integrations.dingtalk_auth import DingTalkAuth

log = logging.getLogger(__name__)

_API_BASE = "https://api.dingtalk.com"


class DingTalkAdapter:
    name = "dingtalk"

    def __init__(self, client_id: str, client_secret: str, router: MessageRouter | None,
                 auth: DingTalkAuth | None = None):
        self.client_id = client_id          # 企业内部应用 AppKey = robotCode
        self.client_secret = client_secret
        self.router = router
        self.on_task_raw: Callable | None = None      # 由 main.py 注入编排
        self.auth = auth or DingTalkAuth(client_id, client_secret)
        self._http = self.auth.http

    # ── 收消息 ────────────────────────────────────────────
    async def start(self, on_task: Callable[[Task], Awaitable[None]] | None) -> None:
        """启动 stream 长连接（常驻）。dingtalk_stream 不装进不了这里。"""
        import dingtalk_stream  # 延迟导入：单测/开发机无 SDK 也能跑

        adapter = self
        loop = asyncio.get_running_loop()

        class _Handler(dingtalk_stream.ChatbotHandler):
            async def process(self, callback):
                msg = dingtalk_stream.ChatbotMessage.from_dict(callback.data)
                text = (msg.text.content or "").strip() if msg.text else ""
                if text:
                    future = asyncio.run_coroutine_threadsafe(
                        adapter._on_message(
                            conversation_id=msg.conversation_id,
                            # 钉钉会话类型："1"=单聊，"2"=群聊
                            conversation_type=("direct" if msg.conversation_type == "1"
                                               else "group"),
                            sender_staff_id=msg.sender_staff_id,
                            sender_union_id=msg.sender_id,   # stream 回调的 senderId 即 unionId
                            nick=msg.sender_nick or "",
                            text=text,
                            session_webhook=msg.session_webhook,
                        ), loop)
                    future.add_done_callback(
                        lambda f: f.exception() and log.exception(
                            "消息处理异常", exc_info=f.exception()))
                return dingtalk_stream.AckMessage.STATUS_OK, "OK"

        credential = dingtalk_stream.Credential(self.client_id, self.client_secret)
        client = dingtalk_stream.DingTalkStreamClient(credential)
        client.register_callback_handler(dingtalk_stream.ChatbotMessage.TOPIC, _Handler())
        log.info("钉钉 stream 长连接启动中（robotCode=%s）", self.client_id)
        await asyncio.to_thread(client.start_forever)   # 常驻阻塞，放线程

    async def _on_message(self, *, conversation_id: str, conversation_type: str,
                          sender_staff_id: str | None, sender_union_id: str | None = None,
                          nick: str, text: str, session_webhook: str | None) -> None:
        # 安全闸：外部群/外部用户无 senderStaffId，直接丢弃
        if not sender_staff_id:
            log.warning("丢弃无 senderStaffId 的消息（外部群/外部用户）")
            return
        requester = Requester(channel=self.name, conversation_id=conversation_id,
                              user_id=sender_staff_id, nick=nick,
                              session_webhook=session_webhook,
                              conversation_type=conversation_type,
                              union_id=sender_union_id)

        result = await self.router.route(text)
        if result.reply is not None:                    # 未配置仓库/无法识别/帮助
            await self.reply_text(requester, result.reply)
            return
        # 受理/去重提示由装配层（main.submit_from_channel）回调 notify_accepted
        await self.on_task_raw(result.capability, result.params, requester)

    # ── 发消息 ────────────────────────────────────────────
    async def reply_text(self, requester: Requester, text: str,
                         at_user: bool = False,
                         extra_at_ids: list[str] | None = None) -> None:
        """sessionWebhook 快速回复；过期/失败降级主动消息（单聊/群聊自动分流）。

        at_user=True 且群聊时：markdown 文本里带 @userId 并在 at 字段声明，
        产生真实的钉钉 @ 提醒（单聊无意义，自动忽略）。
        extra_at_ids：除发起人外需要一并 @ 的人（订阅机制，仅群聊生效）。
        """
        at_ids: list[str] = []
        if requester.conversation_type == "group":
            if at_user:
                at_ids.append(requester.user_id)
            for uid in extra_at_ids or []:
                if uid not in at_ids:
                    at_ids.append(uid)
        if at_ids:
            text = " ".join(f"@{u}" for u in at_ids) + "\n" + text   # 换行保证下方引用块仍在行首渲染
        if requester.session_webhook:
            try:
                body: dict = {"msgtype": "markdown",
                              "markdown": {"title": "yu-agent", "text": text}}
                if at_ids:
                    body["at"] = {"atUserIds": at_ids}
                r = await self._http.post(requester.session_webhook, json=body)
                if r.status_code == 200 and r.json().get("errcode") == 0:
                    return
                log.warning("sessionWebhook 回复失败(%s)，降级主动消息", r.text[:200])
            except Exception:
                log.exception("sessionWebhook 异常，降级主动消息")
        await self.send_proactive(requester, text)

    async def send_proactive(self, requester: Requester, text: str) -> None:
        """主动消息（结果回推，不依赖 sessionWebhook 有效期）：按会话类型分流。"""
        if requester.conversation_type == "direct":
            await self.send_direct_message(requester, text)
        else:
            await self.send_group_message(requester, text)

    async def send_direct_message(self, requester: Requester, text: str) -> None:
        """单聊主动消息：POST /v1.0/robot/privateChatMessages/send（按 userId 直达）。

        旧接口 otoMessages/batchSend 已下线（404），新接口参数形态相同。
        """
        r = await self._http.post(
            f"{_API_BASE}/v1.0/robot/privateChatMessages/send",
            headers=await self.auth.auth_headers(),
            json={
                "robotCode": self.client_id,
                "userIds": [requester.user_id],
                "msgKey": "sampleMarkdown",
                "msgParam": json.dumps({"title": "yu-agent", "text": text},
                                       ensure_ascii=False),
            })
        if r.status_code != 200:
            log.error("单聊主动消息失败: %s %s", r.status_code, r.text[:300])
        r.raise_for_status()
        log.info("[单聊消息] 已发送到 %s", requester.user_id)

    async def send_group_message(self, requester: Requester, text: str) -> None:
        """主动群消息（结果回推，不依赖 sessionWebhook 有效期）。"""
        r = await self._http.post(
            f"{_API_BASE}/v1.0/robot/groupMessages/send",
            headers=await self.auth.auth_headers(),
            json={
                "robotCode": self.client_id,
                "openConversationId": requester.conversation_id,
                "msgKey": "sampleMarkdown",
                "msgParam": json.dumps({"title": "yu-agent", "text": text},
                                       ensure_ascii=False),
            })
        if r.status_code != 200:
            log.error("群主动消息失败: %s %s", r.status_code, r.text[:300])
        r.raise_for_status()
        log.info("[群消息] 已发送到 %s", requester.conversation_id)

    # ── 生命周期通知（装配层回调）──────────────────────────
    @staticmethod
    def _quote_source(task: Task) -> str:
        """引用块回显触发内容（机器人 API 不支持真正的引用回复，用 markdown > 近似）。"""
        prompt = (task.params.get("prompt") or "").replace("\n", " ").strip()
        if prompt:
            return f"> {prompt[:80]}{'…' if len(prompt) > 80 else ''}\n\n"
        repo = task.params.get("repo")
        if repo:
            ref = task.params.get("branch") or f"mr{task.params.get('mr')}"
            return f"> {repo}@{ref}\n\n"
        return ""

    async def notify_accepted(self, task: Task, queued_ahead: int = 0) -> None:
        text = self._quote_source(task) + copywriting.accepted(task.params, task.id)
        if queued_ahead:
            text += f"，前面还有 {queued_ahead} 个任务排队"
        await self.reply_text(task.requester, text, at_user=True)

    async def notify_result(self, task: Task,
                            subscribers: list[Requester] | None = None) -> None:
        quote = self._quote_source(task)
        if task.status == TaskStatus.FAILED:
            text = f"{quote}❌ 任务 #{task.id} 失败：{task.error}"
        elif task.report_url:
            # 钉钉 markdown 只认 http(s) 链接：本地归档路径渲染成死链，改提示去控制台看
            if task.report_url.startswith("http"):
                link = f"\n\n📄 [完整报告]({task.report_url})"
            else:
                link = "\n\n📄 完整报告：管理控制台 → 任务 → 本任务详情"
            text = (f"{quote}{copywriting.done(task.params, task.id)}"
                    f"\n\n{task.result_text or ''}{link}")
        else:                                           # 问答类：直接回文本
            text = quote + (task.result_text or "")
        # 订阅机制（§3.2）：被去重拦下的触发者在完成时一并 @。
        # 同群订阅者合并进主消息（一条消息 @ 多人），跨会话订阅者各自单独推一条。
        # 过滤条件只排"同会话 + 同人"（真正的重复触发）；同一个人从不同会话订阅
        # 说明他在那边也想知道结果，不能按 user_id 全局过滤（联调回归）。
        subs = [s for s in (subscribers or [])
                if not (s.user_id == task.requester.user_id
                        and s.conversation_id == task.requester.conversation_id)]
        same_conv = [s for s in subs
                     if s.conversation_id == task.requester.conversation_id]
        other_conv = [s for s in subs
                      if s.conversation_id != task.requester.conversation_id]
        extra_at = ([s.user_id for s in same_conv]
                    if task.requester.conversation_type == "group" else [])
        # sessionWebhook 优先（实测长有效期且单聊场景主动接口苛刻），失败自动降级主动消息；
        # at_user=True：群聊里产生真实 @ 提醒（反 @ 提问人，不再重复拼昵称文本），单聊自动忽略
        await self.reply_text(task.requester, text, at_user=True, extra_at_ids=extra_at)
        for s in other_conv:
            try:
                await self.reply_text(s, text, at_user=True)
            except Exception:
                log.exception("订阅者 %s 结果通知失败（不影响主流程）", s.user_id)
