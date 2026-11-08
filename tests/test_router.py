"""消息路由测试：显式命令 / LLM 意图 / 白名单校验 / 兜底（分类器用 fake，不碰 LLM）。

跑法：uv run pytest tests/test_router.py
"""
from __future__ import annotations

from app.agent.intent import IntentResult
from app.engine.router import MessageRouter

WHITELIST = ["order-service", "user-center", "payment"]
COMMAND_MAP = {"review": "code-review", "ask": "qa"}


def make_router(classify=None) -> MessageRouter:
    return MessageRouter(command_map=COMMAND_MAP, repo_whitelist=WHITELIST,
                         repo_required={"code-review"}, classify=classify)


def fake_classifier(intent: IntentResult | Exception):
    async def classify(text: str) -> IntentResult:
        if isinstance(intent, Exception):
            raise intent
        return intent
    return classify


# ── ① 显式命令快速路径 ──────────────────────────────────────────────

async def test_explicit_command_routes_to_review():
    r = await make_router().route("review order-service feature/pay-v2")
    assert r.capability == "code-review"
    assert r.params == {"repo": "order-service", "branch": "feature/pay-v2",
                       "review_type": "all"}


async def test_explicit_command_with_mr_and_type():
    r = await make_router().route("review order-service mr128 --type slow-query")
    assert r.params == {"repo": "order-service", "mr": "128",
                       "review_type": "slow-query"}


async def test_ask_command_routes_to_qa():
    r = await make_router().route("ask 什么是覆盖索引？")
    assert r.capability == "qa"
    assert r.params["prompt"] == "什么是覆盖索引？"


# ── ② 自然语言 + LLM 意图分类 ───────────────────────────────────────

async def test_natural_language_review_intent():
    classify = fake_classifier(IntentResult(
        capability="code-review", repo="order-service",
        branch="feature/pay-v2", confidence=0.92))
    r = await make_router(classify).route("帮我看看 order-service 的 feature/pay-v2 分支有没有慢查")
    assert r.capability == "code-review"
    assert r.params["repo"] == "order-service"
    assert r.params["branch"] == "feature/pay-v2"


async def test_natural_language_gitlab_url_intent():
    """LLM 从 GitLab 链接抽出仓库与分支（此处由 fake 模拟抽取结果）。"""
    classify = fake_classifier(IntentResult(
        capability="code-review", repo="payment", branch="hotfix/refund",
        confidence=0.88))
    url = "review 一下 https://gitlab.example.com/team/payment/-/tree/hotfix/refund"
    r = await make_router(classify).route(url)
    assert r.capability == "code-review"
    assert r.params["repo"] == "payment"


# ── ③ 仓库白名单：确定性校验，LLM 说了算不算 ─────────────────────────

async def test_unconfigured_repo_rejected_explicit():
    r = await make_router().route("review secret-repo main")
    assert r.capability is None
    assert "未配置" in r.reply and "secret-repo" in r.reply


async def test_unconfigured_repo_rejected_llm():
    """LLM 抽出的仓库名同样过白名单。"""
    classify = fake_classifier(IntentResult(
        capability="code-review", repo="not-configured", branch="main",
        confidence=0.95))
    r = await make_router(classify).route("帮我 review not-configured 的 main 分支")
    assert r.capability is None
    assert "未配置" in r.reply


async def test_repo_missing_asks_for_repo():
    classify = fake_classifier(IntentResult(
        capability="code-review", repo=None, branch="main", confidence=0.9))
    r = await make_router(classify).route("帮我 review 一下 main 分支")
    assert r.capability is None
    assert "请说明要审查的仓库" in r.reply


# ── ④ 兜底 ──────────────────────────────────────────────────────────

async def test_low_confidence_falls_back_to_qa():
    classify = fake_classifier(IntentResult(
        capability="code-review", repo="order-service", confidence=0.3))
    r = await make_router(classify).route("那个分支好像有问题？")
    assert r.capability == "qa"
    assert r.params["prompt"] == "那个分支好像有问题？"


async def test_classifier_failure_falls_back_to_qa():
    r = await make_router(fake_classifier(RuntimeError("模型池超时"))).route("随便说点什么")
    assert r.capability == "qa"


async def test_unknown_intent_falls_back_to_qa():
    classify = fake_classifier(IntentResult(capability="hack-the-planet", confidence=0.99))
    r = await make_router(classify).route("做点坏事")
    assert r.capability == "qa"


async def test_no_classifier_returns_help():
    r = await make_router(classify=None).route("你好")
    assert r.capability is None
    assert "未理解" in r.reply and "review" in r.reply


# ── MR 引用形态：只认 mr+数字，mr 开头的分支名不误判 ──

async def test_branch_starting_with_mr_not_misread_as_mr():
    r = await make_router().route("review order-service mrf-fix")
    assert r.params.get("branch") == "mrf-fix"
    assert "mr" not in r.params


async def test_mr_ref_with_separator():
    for ref, expect in [("mr128", "128"), ("mr!128", "128"), ("mr#128", "128")]:
        r = await make_router().route(f"review order-service {ref}")
        assert r.params.get("mr") == "128", ref
