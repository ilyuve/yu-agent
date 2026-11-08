"""真实模型冒烟：本地 git 仓 + 真实 LLM（.env 的 LLM_POOL_KEY）跑完整 review_pipeline。

结果写入 demo 数据库（agent-data/demo.db），控制台可直接看到任务、审计回放与成本。

跑法（须清代理环境变量，本机 NO_PROXY 含 [::1] 会触发 httpx 兼容问题）：
    env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY -u http_proxy -u https_proxy \
        -u all_proxy -u NO_PROXY -u no_proxy \
        uv run python scripts/smoke_live.py
"""
from __future__ import annotations

import asyncio
import logging
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # 脚本直跑时定位 app 包

from app.agent.core import CapabilityRegistry
from app.config import ROOT, load_settings
from app.engine import flows
from app.engine.models import Requester, TaskStatus, new_task
from app.integrations.sinks.local import LocalFileSink
from app.llm.factory import for_purpose
from app.llm.registry import ModelRegistry
from app.store.sqlite import SqliteStore
from app.tools import registry as tool_registry

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("smoke-live")

# 一个仿真订单服务分支：故意埋 3 类慢查 + 2 个 CR 问题
DEMO_FILES = {
    "orders.py": '''
import db

def list_orders(user_id, status=None):
    sql = "SELECT * FROM orders WHERE user_id = %s" % user_id
    if status:
        sql += " AND status LIKE '%" + status + "%'"
    return db.execute(sql).fetchall()

def mark_timeout():
    # 定时任务：把超时订单置为关闭
    db.execute("UPDATE orders SET status = 'closed';")
''',
    "cart.py": '''
import db

def refresh_prices(cart_id):
    items = db.execute("SELECT sku_id FROM cart_items WHERE cart_id=%s" % cart_id)
    for it in items:                     # N+1：循环内逐条查价
        price = db.execute("SELECT price FROM sku WHERE id=%s" % it[0])
        db.execute("UPDATE cart_items SET price=%s WHERE sku_id=%s" % (price, it[0]))

def get_cart(cart_id):
    return db.execute("SELECT * FROM cart_items WHERE cart_id=%s" % cart_id)
''',
    "README.md": "# order-service\\n",
}


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def make_demo_repo(base: Path) -> Path:
    repo = base / "order-service"
    repo.mkdir(parents=True)
    _git(["init", "-b", "main"], repo)
    _git(["config", "user.email", "smoke@local"], repo)
    _git(["config", "user.name", "smoke"], repo)
    (repo / "README.md").write_text("# order-service\n", encoding="utf-8")
    _git(["add", "."], repo)
    _git(["commit", "-m", "init"], repo)
    _git(["checkout", "-b", "feature/pay-v2"], repo)
    for name, content in DEMO_FILES.items():
        (repo / name).write_text(content.replace("\\n", "\n"), encoding="utf-8")
    _git(["add", "."], repo)
    _git(["commit", "-m", "feat: 订单与购物车接口"], repo)
    return repo


class LocalCodehost:
    """冒烟用：clone_url 指向本地仓库（生产环境是 GitLabProvider）。"""
    def __init__(self, repo_path: Path):
        self.repo_path = str(repo_path)

    async def validate_repo(self, repo):
        return SimpleNamespace(name=repo, default_branch="main",
                               clone_url=self.repo_path)

    async def get_head_commit(self, repo, branch):
        out = await asyncio.to_thread(
            subprocess.run, ["git", "rev-parse", branch],
            cwd=self.repo_path, check=True, capture_output=True, text=True)
        return out.stdout.strip()


async def main() -> int:
    settings = load_settings()
    rules = settings["scan_rules"]["rules"]
    model_registry = ModelRegistry(settings["models"])

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        repo = make_demo_repo(tmp)
        head = (await asyncio.to_thread(
            subprocess.run, ["git", "rev-parse", "feature/pay-v2"],
            cwd=repo, check=True, capture_output=True, text=True)).stdout.strip()

        settings["app"]["server"]["clone_base_dir"] = str(tmp / "workspace")
        store = SqliteStore(str(ROOT / "agent-data" / "demo.db"))   # 控制台可见
        await store.init()

        toolsets = tool_registry.build_registry(settings, rules)
        ctx = SimpleNamespace(
            settings=settings,
            capabilities=CapabilityRegistry(
                [str(ROOT / "app/capabilities/code-review")]),
            model_registry=model_registry,
            store=store,
            codehost=LocalCodehost(repo),
            sink=LocalFileSink(str(ROOT / "agent-data" / "reports")),
            toolsets=toolsets,
            model_for=lambda purpose: for_purpose(model_registry, purpose),
            resolve_toolsets=lambda names: tool_registry.resolve(toolsets, names),
        )

        task = new_task(
            "code-review",
            {"repo": "order-service", "branch": "feature/pay-v2", "review_type": "all"},
            Requester(channel="cli", conversation_id="local", user_id="dev",
                      nick="本地冒烟"),
            prefix=settings["app"]["server"]["task_id_prefix"], head_commit=head)
        task.status = TaskStatus.RUNNING
        await store.save_task(task)

        log.info("任务 %s 开始：order-service@feature/pay-v2（真实模型）", task.id)
        t0 = time.time()
        runner = flows.get_runner("review_pipeline", ctx)
        try:
            await runner(task)
            task.status = TaskStatus.DONE
        except Exception as e:
            task.status = TaskStatus.FAILED
            task.error = str(e)
            log.exception("冒烟失败")
        task.finished_at = time.time()
        await store.save_task(task)

        print("\n" + "=" * 60)
        print(f"任务 {task.id}  状态 {task.status.value}  耗时 {task.finished_at - t0:.1f}s")
        if task.status == TaskStatus.FAILED:
            print(f"错误：{task.error}")
            return 1
        print(f"摘要：{task.result_text}")
        print(f"报告：{task.report_url}")
        trail = await store.get_audit_trail(task.id)
        for a in trail:
            print(f"审计 #{a['seq']}  模型 {a['model']}  "
                  f"in={a['tokens_in']} out={a['tokens_out']}  "
                  f"成本 ¥{a['cost']:.4f}  耗时 {a['latency_ms']/1000:.1f}s")
        print("=" * 60)
        print("打开控制台 → 任务 → 点这一行，可以看审计回放。")
        return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
