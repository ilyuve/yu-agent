"""真实 GitLab（极狐）联调脚本：对测试仓库的 3 个分支跑完整 review_pipeline。

结果写入 agent-data/demo.db 与 agent-data/reports/，控制台可直接查看。

跑法（须清代理环境变量）：
    env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY -u http_proxy -u https_proxy \
        -u all_proxy -u NO_PROXY -u no_proxy \
        uv run python scripts/verify_jihu.py [branch ...]
"""
from __future__ import annotations

import asyncio
import logging
import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # 脚本直跑时定位 app 包

from app.agent.core import CapabilityRegistry
from app.config import ROOT, load_settings
from app.engine import flows
from app.engine.models import Requester, TaskStatus, new_task
from app.integrations.codehost.gitlab import GitLabProvider
from app.integrations.sinks.local import LocalFileSink
from app.llm.factory import for_purpose
from app.llm.registry import ModelRegistry
from app.store.sqlite import SqliteStore
from app.tools import registry as tool_registry

logging.basicConfig(level=logging.WARNING,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("verify-jihu")
log.setLevel(logging.INFO)

REPO = "quantum_spark/quantum_spark"
DEFAULT_BRANCHES = ["feature/slow-query", "feature/cr-issues", "feature/clean-change"]


async def run_one(ctx, store, settings, branch: str) -> tuple[str, str]:
    head = await ctx.codehost.get_head_commit(REPO, branch)
    task = new_task(
        "code-review",
        {"repo": REPO, "branch": branch, "review_type": "all"},
        Requester(channel="cli", conversation_id="local", user_id="dev",
                  nick="极狐联调"),
        prefix=settings["app"]["server"]["task_id_prefix"], head_commit=head)
    task.status = TaskStatus.RUNNING
    await store.save_task(task)

    t0 = time.time()
    runner = flows.get_runner("review_pipeline", ctx)
    try:
        await runner(task)
        task.status = TaskStatus.DONE
    except Exception as e:
        task.status = TaskStatus.FAILED
        task.error = str(e)
        log.exception("分支 %s 审查失败", branch)
    task.finished_at = time.time()
    await store.save_task(task)
    return task.id, f"{task.status.value}（{task.finished_at - t0:.0f}s）"


async def main() -> int:
    settings = load_settings()
    rules = settings["scan_rules"]["rules"]
    model_registry = ModelRegistry(settings["models"])
    ch = settings["app"]["codehost"]

    store = SqliteStore(str(ROOT / "agent-data" / "demo.db"))   # 控制台可见
    await store.init()
    toolsets = tool_registry.build_registry(settings, rules)
    ctx = SimpleNamespace(
        settings=settings,
        capabilities=CapabilityRegistry([str(ROOT / "app/capabilities/code-review")]),
        model_registry=model_registry,
        store=store,
        codehost=GitLabProvider(ch["api_base"], ch["token_env"], ch["repo_whitelist"]),
        sink=LocalFileSink(str(ROOT / "agent-data" / "reports")),
        toolsets=toolsets,
        model_for=lambda purpose: for_purpose(model_registry, purpose),
        resolve_toolsets=lambda names: tool_registry.resolve(toolsets, names),
    )

    branches = sys.argv[1:] or DEFAULT_BRANCHES
    results = []
    for branch in branches:                       # 串行，避免打满模型 RPM
        tid, outcome = await run_one(ctx, store, settings, branch)
        results.append((tid, branch, outcome))
        print(f"[完成] {tid}  {branch}  {outcome}")

    print("\n===== 汇总 =====")
    for tid, branch, outcome in results:
        print(f"{tid}  {branch:28s}  {outcome}")
    print("控制台 → 任务 → 点对应行可看审计回放与报告。")
    return 0 if all("done" in o for _, _, o in results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
