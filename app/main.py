"""入口：装配各层、启动 Adapter + Engine（设计文档 §2/§4）。"""
from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass

import httpx

from app.adapters import copywriting
from app.adapters.dingtalk import DingTalkAdapter
from app.agent.core import CapabilityRegistry
from app.config import ROOT, load_settings, require_env
from app.engine.models import Requester, new_task
from app.engine.queue import TaskEngine
from app.integrations.codehost.gitlab import GitLabProvider
from app.integrations.sinks.dingdoc import DingDocSink
from app.llm.factory import for_purpose
from app.llm.registry import ModelRegistry
from app.store.sqlite import SqliteStore
from app.tools import registry as tool_registry

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("yu-agent")


@dataclass
class RunContext:
    settings: dict
    capabilities: CapabilityRegistry
    model_registry: ModelRegistry
    store: SqliteStore
    codehost: GitLabProvider
    sink: DingDocSink
    toolsets: dict

    def model_for(self, purpose: str):
        return for_purpose(self.model_registry, purpose)

    def resolve_toolsets(self, names: list[str]):
        return tool_registry.resolve(self.toolsets, names)


async def main() -> None:
    settings = load_settings()
    app_cfg = settings["app"]
    server = app_cfg["server"]

    store = SqliteStore(server["db_path"])
    await store.init()

    model_registry = ModelRegistry(settings["models"])
    capabilities = CapabilityRegistry(app_cfg["capabilities"])

    ch = app_cfg["codehost"]
    codehost = GitLabProvider(ch["api_base"], ch["token_env"], ch["repo_whitelist"])
    # ReportSink 选择：local = 本地文件兜底（开发/未配文档库）；dingdoc = 钉钉文档库
    sink_name = app_cfg.get("report_sink", "local")
    if sink_name == "dingdoc":
        from app.integrations.dingtalk_auth import DingTalkAuth
        auth = DingTalkAuth(require_env(app_cfg["dingtalk"]["client_id_env"]),
                            require_env(app_cfg["dingtalk"]["client_secret_env"]))
        sink = DingDocSink(app_cfg["dingdoc"]["workspace_id"],
                           app_cfg["dingdoc"]["operator_id"], auth)
    elif sink_name == "local":
        from app.integrations.sinks.local import LocalFileSink
        sink = LocalFileSink(server.get("reports_dir", "/data/yu-agent/reports"))
    else:
        raise ValueError(f"未注册的 report_sink: {sink_name}（已有: local, dingdoc）")

    rules = settings["scan_rules"]["rules"]
    toolsets = tool_registry.build_registry(settings, rules)

    ctx = RunContext(settings=settings, capabilities=capabilities,
                     model_registry=model_registry, store=store,
                     codehost=codehost, sink=sink, toolsets=toolsets)

    # 按能力声明的 flow 类型分发（设计文档 §4）：启动时装配并校验，未注册的 flow 直接 fail fast
    from app.engine import flows
    runners = {name: flows.get_runner(cap.get("flow", "agent_only"), ctx)
               for name, cap in capabilities.caps.items()}
    log.info("流程装配完成: %s", {n: type(r).__name__ for n, r in runners.items()})

    async def dispatch(task):
        await runners[task.capability](task)

    engine = TaskEngine(store, workers=server["workers"], runner=dispatch)

    adapters = {}
    if "dingtalk" in app_cfg.get("adapters", []):
        dingtalk = DingTalkAdapter(
            require_env(app_cfg["dingtalk"]["client_id_env"]),
            require_env(app_cfg["dingtalk"]["client_secret_env"]),
            router=None)          # router 在下方装配后补注
        adapters["dingtalk"] = dingtalk

    async def submit_from_channel(capability: str, params: dict, requester: Requester):
        # 服务参数每次从 ctx.settings 现读（热更新后即刻生效，不抓启动时的快照）
        srv = ctx.settings["app"]["server"]
        # 仅代码类能力需要 head commit（幂等键）；问答类无 repo 参数
        head = ""
        if params.get("repo"):
            branch = params.get("branch")
            try:
                if not branch and params.get("mr"):
                    branch, head = await codehost.get_mr_ref(params["repo"], params["mr"])
                    params["branch"] = branch          # MR 落到源分支上审查
                if not head:
                    head = await codehost.get_head_commit(params["repo"], branch)
            except (LookupError, PermissionError, httpx.HTTPError) as e:
                # 受理前失败（MR/分支/仓库不存在、无权限、GitLab 不可达）：任务还没进队列，
                # 引擎的失败通知管不到，必须在这里直接回复用户
                log.warning("受理前校验失败: %s", e)
                adapter = adapters.get(requester.channel)
                if adapter:
                    await adapter.reply_text(requester, f"❌ 无法开始审查：{e}")
                return None, False
        task = new_task(capability, params, requester,
                        prefix=srv["task_id_prefix"], head_commit=head)
        task, created = await engine.submit(task, srv["dedup_ttl_sec"])
        adapter = adapters.get(requester.channel)
        if adapter:
            # 受理通知只对长流程（编排骨架类）发；问答类（agent_only）秒级完成，直接出答案
            is_long_flow = capabilities.caps.get(capability, {}).get("flow") != "agent_only"
            if created:
                if is_long_flow:
                    # 随机抽一套文案风格（受理/完成配对），随 params 落库保证同一任务人格一致
                    task.params["_style"] = random.randrange(len(copywriting.STYLES))
                    # queue_position 含刚入队的本任务，"前面排队数"要减 1
                    await adapter.notify_accepted(task, max(0, engine.queue_position() - 1))
            else:
                if is_long_flow:
                    # 订阅机制（§3.2）：被去重拦下的触发者登记为订阅者，完成时一并 @
                    await store.add_subscriber(task.id, requester)
                dup_text = (f"相同变更的任务 #{task.id} 已在处理中，"
                            f"{srv['dedup_ttl_sec']} 秒内不重复排队，完成后会 @ 你。"
                            if is_long_flow else "相同的问题正在回答中，请稍等。")
                await adapter.reply_text(requester, dup_text)
        return task, created

    async def on_task_done(task):
        adapter = adapters.get(task.requester.channel)
        if adapter:
            subscribers = await store.list_subscribers(task.id)
            await adapter.notify_result(task, subscribers=subscribers)
    engine.on_task_done = on_task_done

    # 装配 Adapters：消息路由 = 显式命令快速路径 → LLM 意图分类（懒装配） → 问答兜底
    from app.engine.router import MessageRouter

    async def classify(text: str):
        # 懒装配：首次自然语言请求时才构造分类器（需要 LLM_POOL_KEY）；
        # 构造失败会被 router 捕获并落问答兜底，不影响显式命令路径
        nonlocal _classifier
        if _classifier is None:
            from app.agent import intent as intent_mod
            _classifier = intent_mod.build_classifier(ctx)
        return await _classifier(text)
    _classifier = None

    repo_required = {name for name, cap in capabilities.caps.items()
                     if cap.get("flow") == "review_pipeline"}
    router = MessageRouter(
        command_map=capabilities.command_map(),
        repo_whitelist=ch["repo_whitelist"],
        repo_required=repo_required,
        classify=classify)

    # ── 配置热更新（§15 P2 落地）──────────────────────────────
    # 控制台写配置后 bump SQLite 里的 config_version（两个进程共享同一库文件）；
    # 本进程每处理一条消息前比对一次（单点 sqlite 读，毫秒级），版本变了就重读
    # yaml/.env 并原地重建注册表。在途任务持有旧对象快照不受影响；
    # workers 数与钉钉 stream 连接（钉钉凭据）是进程级资源，仍需重启——界面已标注。
    from dotenv import load_dotenv
    # 基线在启动时就定格为本进程加载配置时的版本——若等首次轮询才记基线，
    # 启动后到首次轮询之间的变更会被当成"基线"吞掉，永远补不上
    reload_state = {"version": await store.config_version()}

    async def maybe_reload() -> None:
        try:
            v = await store.config_version()
        except Exception:
            log.warning("config_version 读取失败（跳过热更新检查）", exc_info=True)
            return
        if reload_state["version"] is None:            # 首次只记基线
            reload_state["version"] = v
            return
        if v == reload_state["version"]:
            return
        reload_state["version"] = v
        nonlocal _classifier
        log.info("检测到配置变更（config_version=%s），热加载中", v)
        load_dotenv(ROOT / ".env", override=True)      # 密钥轮换（GitLab/模型池 token）
        settings.clear()
        settings.update(load_settings())               # 原地替换，ctx 引用保持有效
        app_cfg = settings["app"]
        ch_new = app_cfg["codehost"]
        ctx.model_registry = ModelRegistry(settings["models"])
        capabilities.caps = CapabilityRegistry(app_cfg["capabilities"]).caps
        ctx.toolsets = tool_registry.build_registry(
            settings, settings["scan_rules"]["rules"])
        ctx.codehost = GitLabProvider(ch_new["api_base"], ch_new["token_env"],
                                      ch_new["repo_whitelist"])
        runners.clear()
        for name, cap in capabilities.caps.items():
            runners[name] = flows.get_runner(cap.get("flow", "agent_only"), ctx)
        router.command_map = capabilities.command_map()
        router.repo_whitelist = set(ch_new["repo_whitelist"])
        router.repo_required = {n for n, c in capabilities.caps.items()
                                if c.get("flow") == "review_pipeline"}
        _classifier = None      # 能力清单/模型可能变了，下条消息重建分类器
        log.info("热加载完成：白名单 %d 个仓库，能力 %s",
                 len(router.repo_whitelist), list(capabilities.caps))

    _orig_route = router.route

    async def _route_with_reload(text: str):
        await maybe_reload()
        return await _orig_route(text)

    router.route = _route_with_reload

    async def _reload_loop() -> None:
        # 后台轮询：消息前检查保证"下条消息即时生效"，这里兜底"静默期也能生效"
        #（默认 15s；配置写入到生效的最大延迟）
        while True:
            await asyncio.sleep(15)
            await maybe_reload()

    for adapter in adapters.values():
        adapter.router = router
        adapter.on_task_raw = submit_from_channel

    await engine.start()
    asyncio.create_task(_reload_loop())
    for adapter in adapters.values():
        await adapter.start(on_task=None)
    log.info("yu-agent 已启动（workers=%d, capabilities=%s, adapters=%s）",
             server["workers"], list(capabilities.caps), list(adapters))

    await asyncio.Event().wait()         # 常驻


if __name__ == "__main__":
    asyncio.run(main())
