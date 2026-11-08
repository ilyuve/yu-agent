"""去重键回归测试（联调发现：问答消息 dedup_key 不含内容，同一人不同问题被误去重）。"""
from app.engine.models import Requester, new_task


def _req(user="u1"):
    return Requester(channel="dingtalk", conversation_id="c1", user_id=user)


def test_qa_dedup_key_varies_by_prompt():
    a = new_task("qa", {"prompt": "你好"}, _req(), "R", "")
    b = new_task("qa", {"prompt": "介绍一下自己"}, _req(), "R", "")
    assert a.dedup_key != b.dedup_key


def test_qa_dedup_key_varies_by_user():
    a = new_task("qa", {"prompt": "你好"}, _req("u1"), "R", "")
    b = new_task("qa", {"prompt": "你好"}, _req("u2"), "R", "")
    assert a.dedup_key != b.dedup_key


def test_qa_same_prompt_same_user_dedups():
    a = new_task("qa", {"prompt": "你好"}, _req(), "R", "")
    b = new_task("qa", {"prompt": "你好"}, _req(), "R", "")
    assert a.dedup_key == b.dedup_key


def test_review_dedup_ignores_requester():
    """同一变更（repo+branch+head）无论谁触发都算重复，避免重复跑昂贵审查。"""
    p = {"repo": "payment", "branch": "feat/x"}
    a = new_task("code-review", p, _req("u1"), "R", "abc123")
    b = new_task("code-review", dict(p), _req("u2"), "R", "abc123")
    assert a.dedup_key == b.dedup_key
    c = new_task("code-review", dict(p), _req("u1"), "R", "def456")
    assert a.dedup_key != c.dedup_key        # head 变了就是新变更
