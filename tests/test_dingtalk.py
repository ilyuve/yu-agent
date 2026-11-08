"""钉钉 Adapter / DingDocSink / MCP 注册的离线测试（httpx.MockTransport，不打外网）。"""
import json

import httpx

from app.adapters import copywriting
from app.adapters.dingtalk import DingTalkAdapter
from app.agent.schemas import ReviewReport
from app.engine.models import Requester, Task, TaskStatus
from app.integrations.dingtalk_auth import DingTalkAuth
from app.integrations.sinks.dingdoc import DingDocSink
from app.tools import registry as tool_registry


def _requester(**kw):
    base = {"channel": "dingtalk", "conversation_id": "cidG1", "user_id": "u1",
            "nick": "张三", "session_webhook": None}
    return Requester(**(base | kw))


def _adapter(handler) -> DingTalkAdapter:
    """MockTransport 注入：handler(request) -> httpx.Response。"""
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    auth = DingTalkAuth("appkey1", "secret1", http=http)
    return DingTalkAdapter("appkey1", "secret1", router=None, auth=auth)


# ── token 缓存 ──

async def test_token_cached_across_calls():
    calls = {"token": 0, "send": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if "oauth2/accessToken" in str(request.url):
            calls["token"] += 1
            return httpx.Response(200, json={"accessToken": "tok1", "expireIn": 7200})
        calls["send"] += 1
        assert request.headers["x-acs-dingtalk-access-token"] == "tok1"
        return httpx.Response(200, json={"processQueryKey": "k1"})

    adapter = _adapter(handler)
    r = _requester()
    await adapter.send_group_message(r, "第一条")
    await adapter.send_group_message(r, "第二条")
    assert calls == {"token": 1, "send": 2}       # token 只取一次


# ── 回复路径 ──

async def test_reply_text_uses_session_webhook_first():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json={"errcode": 0})

    adapter = _adapter(handler)
    await adapter.reply_text(_requester(session_webhook="https://hook.test/s1"), "你好")
    assert seen == ["https://hook.test/s1"]       # 快速通道成功就不再走主动消息


async def test_reply_text_falls_back_when_webhook_expired():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        seen.append(url)
        if "hook.test" in url:
            return httpx.Response(200, json={"errcode": 310000})   # session 过期
        if "oauth2/accessToken" in url:
            return httpx.Response(200, json={"accessToken": "t", "expireIn": 7200})
        return httpx.Response(200, json={"processQueryKey": "k"})

    adapter = _adapter(handler)
    await adapter.reply_text(_requester(session_webhook="https://hook.test/s1"), "你好")
    assert seen[0] == "https://hook.test/s1"
    assert "groupMessages/send" in seen[-1]       # 降级到主动群消息


async def test_notify_result_message_shapes():
    sent = []

    def handler(request: httpx.Request) -> httpx.Response:
        if "oauth2/accessToken" in str(request.url):
            return httpx.Response(200, json={"accessToken": "t", "expireIn": 7200})
        body = json.loads(request.content)
        sent.append(json.loads(body["msgParam"])["text"])
        return httpx.Response(200, json={"processQueryKey": "k"})

    adapter = _adapter(handler)
    req = _requester()

    ok = Task(id="R1", capability="code-review", params={}, requester=req,
              status=TaskStatus.DONE, result_text="发现 2 处风险",
              report_url="https://doc.test/r1")
    await adapter.notify_result(ok)
    assert "审查完成" in sent[-1] and "https://doc.test/r1" in sent[-1]
    assert "@u1" in sent[-1]                  # 真实 @ 由 reply_text 拼 userId，不再拼昵称

    local = Task(id="R1b", capability="code-review", params={}, requester=req,
                 status=TaskStatus.DONE, result_text="发现 1 处风险",
                 report_url="agent-data/reports/x/R1b.md")
    await adapter.notify_result(local)
    assert "[完整报告](" not in sent[-1]      # 本地路径不渲染死链
    assert "控制台" in sent[-1]

    failed = Task(id="R2", capability="code-review", params={}, requester=req,
                  status=TaskStatus.FAILED, error="git clone 失败")
    await adapter.notify_result(failed)
    assert "失败" in sent[-1] and "git clone 失败" in sent[-1]

    qa = Task(id="R3", capability="qa", params={}, requester=req,
              status=TaskStatus.DONE, result_text="覆盖索引是……")
    await adapter.notify_result(qa)
    assert "覆盖索引是" in sent[-1]


# ── DingDocSink ──

async def test_dingdoc_archive_creates_and_writes():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        calls.append(url)
        if "oauth2/accessToken" in url:
            return httpx.Response(200, json={"accessToken": "t", "expireIn": 7200})
        if "overwriteContent" in url:
            body = json.loads(request.content)
            assert body["dataType"] == "markdown" and "审查" in body["content"]
            assert "operatorId=op1" in url          # 覆写内容：operatorId 在 query
            return httpx.Response(200, json={})
        assert url.split("?")[0].endswith("/docs")
        assert json.loads(request.content)["operatorId"] == "op1"   # 创建文档：operatorId 在 body
        return httpx.Response(200, json={"docKey": "dk123"})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    sink = DingDocSink("ws1", "op1", DingTalkAuth("k", "s", http=http), http=http)
    task = Task(id="R9", capability="code-review",
                params={"repo": "order-service", "branch": "feat/x"},
                requester=_requester())
    report = ReviewReport(summary="发现 1 处风险", slow_query_risks=[], cr_suggestions=[])
    url = await sink.archive(task, report)
    assert url == "https://alidocs.dingtalk.com/i/nodes/dk123"
    assert any("/docs" in u for u in calls) and any("overwriteContent" in u for u in calls)


# ── MCP 注册（配置驱动）──

def test_mcp_loaded_when_config_present(tmp_path):
    cfg = tmp_path / "mcp.json"
    cfg.write_text('{"mcpServers": {"codehost": {"url": "https://g.example.com/-/mcp"}}}',
                   encoding="utf-8")
    settings = {"mcp_config_path": str(cfg)}
    reg = tool_registry.build_registry(settings, [])
    assert "mcp.codehost" in reg
    assert {"local.git", "local.scanner", "local.bash"} <= set(reg)


def test_mcp_skipped_when_config_missing(tmp_path):
    settings = {"mcp_config_path": str(tmp_path / "nope.json")}
    reg = tool_registry.build_registry(settings, [])
    assert not any(k.startswith("mcp.") for k in reg)


# ── 单聊分流（联调回归：单聊结果误走群接口 400）──

async def test_notify_result_direct_chat_uses_oto():
    sent = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "oauth2/accessToken" in url:
            return httpx.Response(200, json={"accessToken": "t", "expireIn": 7200})
        sent.append(url)
        if "privateChatMessages/send" in url:
            body = json.loads(request.content)
            assert body["userIds"] == ["u1"]
        return httpx.Response(200, json={"processQueryKey": "k"})

    adapter = _adapter(handler)
    req = _requester(conversation_type="direct")
    qa = Task(id="R4", capability="qa", params={}, requester=req,
              status=TaskStatus.DONE, result_text="答案")
    await adapter.notify_result(qa)
    assert any("privateChatMessages/send" in u for u in sent)
    assert not any("groupMessages" in u for u in sent)


async def test_reply_text_fallback_direct_goes_oto():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        seen.append(url)
        if "hook.test" in url:
            return httpx.Response(200, json={"errcode": 310000})
        if "oauth2/accessToken" in url:
            return httpx.Response(200, json={"accessToken": "t", "expireIn": 7200})
        return httpx.Response(200, json={"processQueryKey": "k"})

    adapter = _adapter(handler)
    req = _requester(session_webhook="https://hook.test/s1",
                     conversation_type="direct")
    await adapter.reply_text(req, "你好")
    assert "privateChatMessages/send" in seen[-1]


async def test_group_reply_carries_real_at():
    """群聊 at_user=True：webhook 消息体必须带 at.atUserIds，文本含 @userId（真 @ 提醒）。"""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if "hook.test" in str(request.url):
            seen.update(json.loads(request.content))
        return httpx.Response(200, json={"errcode": 0})

    adapter = _adapter(handler)
    req = _requester(session_webhook="https://hook.test/s1")   # 默认群聊
    await adapter.reply_text(req, "审查完成", at_user=True)
    assert seen["at"]["atUserIds"] == ["u1"]
    assert seen["markdown"]["text"].startswith("@u1")


async def test_direct_reply_never_ats():
    """单聊忽略 at_user，不拼 @ 文本也不带 at 字段。"""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if "hook.test" in str(request.url):
            seen.update(json.loads(request.content))
        return httpx.Response(200, json={"errcode": 0})

    adapter = _adapter(handler)
    req = _requester(session_webhook="https://hook.test/s1", conversation_type="direct")
    await adapter.reply_text(req, "答案", at_user=True)
    assert "at" not in seen
    assert seen["markdown"]["text"] == "答案"


async def test_result_quotes_source_question():
    """群聊回复头部带触发内容引用块（机器人无真引用回复，用 markdown > 近似）。"""
    sent = []

    def handler(request: httpx.Request) -> httpx.Response:
        if "oauth2/accessToken" in str(request.url):
            return httpx.Response(200, json={"accessToken": "t", "expireIn": 7200})
        sent.append(json.loads(json.loads(request.content)["msgParam"])["text"])
        return httpx.Response(200, json={"processQueryKey": "k"})

    adapter = _adapter(handler)
    req = _requester()
    qa = Task(id="R5", capability="qa",
              params={"prompt": "什么是覆盖索引"}, requester=req,
              status=TaskStatus.DONE, result_text="覆盖索引是……")
    await adapter.notify_result(qa)
    assert "\n> 什么是覆盖索引" in sent[-1]

    ok = Task(id="R6", capability="code-review",
              params={"repo": "a/b", "branch": "feat/x"}, requester=req,
              status=TaskStatus.DONE, result_text="ok",
              report_url="https://doc.test/r6")
    await adapter.notify_result(ok)
    assert "\n> a/b@feat/x" in sent[-1]

    await adapter.notify_accepted(ok)
    assert "\n> a/b@feat/x" in sent[-1]


# ── 订阅机制（§3.2：去重拦下的触发者，完成时一并 @）──

async def test_notify_result_ats_subscribers():
    """同群订阅者合并进主消息 @ 多人；跨群订阅者各自单独收一条。"""
    sent = []   # (conversation_id, text)

    def handler(request: httpx.Request) -> httpx.Response:
        if "oauth2/accessToken" in str(request.url):
            return httpx.Response(200, json={"accessToken": "t", "expireIn": 7200})
        body = json.loads(request.content)
        text = json.loads(body["msgParam"])["text"]
        sent.append((body.get("openConversationId"), text))
        return httpx.Response(200, json={"processQueryKey": "k"})

    adapter = _adapter(handler)
    req = _requester()                                   # u1 在 cidG1 发起
    sub_same = _requester(user_id="u2", nick="李四")      # 同群订阅者
    sub_other = _requester(user_id="u3", nick="王五",
                           conversation_id="cidG2")      # 另一个群的订阅者
    task = Task(id="R7", capability="code-review",
                params={"repo": "a/b", "branch": "feat/x"}, requester=req,
                status=TaskStatus.DONE, result_text="发现 2 处风险",
                report_url="https://doc.test/r7")
    await adapter.notify_result(task, subscribers=[sub_same, sub_other, req])

    assert len(sent) == 2                                # 同群合并，只发两条
    main_msg = next(t for cid, t in sent if cid == "cidG1")
    assert "@u1" in main_msg and "@u2" in main_msg       # 发起人 + 同群订阅者都被 @
    assert "审查完成" in main_msg
    other_msg = next(t for cid, t in sent if cid == "cidG2")
    assert "@u3" in other_msg and "@u1" not in other_msg
    # 发起人重复出现在订阅列表里（自己重复触发）不会造成二次 @
    assert main_msg.count("@u1") == 1


async def test_notify_result_same_user_other_conversation_still_notified():
    """联调回归：同一个人在私聊发起、在群里又触发同分支（被去重），
    群聊是他主动订阅的会话，必须照发——不能按 user_id 全局过滤。"""
    sent = []   # (url_kind, text)

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "oauth2/accessToken" in url:
            return httpx.Response(200, json={"accessToken": "t", "expireIn": 7200})
        body = json.loads(request.content)
        text = json.loads(body["msgParam"])["text"]
        kind = ("group" if "groupMessages" in url else "direct")
        sent.append((kind, text))
        return httpx.Response(200, json={"processQueryKey": "k"})

    adapter = _adapter(handler)
    req = _requester(conversation_id="cidDirect1",
                     conversation_type="direct")          # u1 私聊发起
    sub_group = _requester(conversation_id="cidG1")        # 同一 u1 在群里又触发
    task = Task(id="R8", capability="code-review",
                params={"repo": "a/b", "branch": "feat/x"}, requester=req,
                status=TaskStatus.DONE, result_text="发现 1 处风险",
                report_url="https://doc.test/r8")
    await adapter.notify_result(task, subscribers=[sub_group])

    kinds = [k for k, _ in sent]
    assert kinds == ["direct", "group"]                  # 私聊主消息 + 群订阅消息都发了
    group_msg = next(t for k, t in sent if k == "group")
    assert "@u1" in group_msg                            # 群里的他也被 @


# ── 文案风格库（受理/完成成对，按任务随机）──

def test_copywriting_pairs_render_and_fallback():
    for i in range(len(copywriting.STYLES)):
        params = {"_style": i}
        assert "R9" in copywriting.accepted(params, "R9")
        assert "R9" in copywriting.done(params, "R9")
    # 无 _style（存量任务）或越界下标：回退默认风格，不炸
    assert copywriting.accepted({}, "R9") == copywriting.STYLES[0][0].format(id="R9")
    assert copywriting.done({"_style": 999}, "R9") == copywriting.STYLES[0][1].format(id="R9")


async def test_accept_and_done_use_same_paired_style():
    """同一任务受理/完成必须用同一套文案（_style 落 params，人格一致）。"""
    sent = []

    def handler(request: httpx.Request) -> httpx.Response:
        if "oauth2/accessToken" in str(request.url):
            return httpx.Response(200, json={"accessToken": "t", "expireIn": 7200})
        sent.append(json.loads(json.loads(request.content)["msgParam"])["text"])
        return httpx.Response(200, json={"processQueryKey": "k"})

    adapter = _adapter(handler)
    req = _requester()
    task = Task(id="R10", capability="code-review",
                params={"_style": 4, "repo": "a/b", "branch": "feat/x"},   # H 外卖小哥
                requester=req, status=TaskStatus.DONE, result_text="ok",
                report_url="https://doc.test/r10")
    await adapter.notify_accepted(task)
    assert "已接单" in sent[-1]
    await adapter.notify_result(task)
    assert "已送达" in sent[-1]                          # 完成文案与受理同风格配对
