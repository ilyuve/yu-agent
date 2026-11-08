"""管理控制台（设计文档 §15）：FastAPI 后端 + 静态单页前端。

职责：
- 只读分析：任务统计、token 用量、成本（来自 Store，Store 换实现不影响本层）；
- 配置管理：仓库白名单增删（写回 config/app.yaml——当前版本不引入配置数据库，
  界面只是配置文件的编辑器。注意：控制台与审查服务是两个进程，本进程内的
  settings 更新只影响控制台自身的展示；审查进程下次启动才读到新配置，
  前端保存提示已注明"重启后生效"）；
- 配置查看：模型注册表/能力清单（脱敏：不返回任何密钥）。

独立运行（预览/开发）：
    uv run python -m app.admin.server --demo --port 8080
生产模式：scripts/start.sh 与 app.main 同容器双进程拉起（共享同一 SQLite 文件，
不共享内存配置）。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path

import uvicorn
import yaml
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.admin.config_store import (
    SECRET_ENV_KEYS,
    add_capability_path,
    load_yaml,
    mask_secret,
    remove_capability_path,
    save_capability,
    save_models,
    update_channels,
    update_server,
    validate_capability,
    write_env,
)
from app.config import ROOT, load_settings
from app.store.sqlite import SqliteStore

log = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).parent / "static"

# 脱敏：这些键的值不下发到前端
_SECRET_KEYS = re.compile(r"key|secret|token", re.IGNORECASE)


def _sanitize(obj):
    """配置下发前脱敏：密钥/令牌字段只回传是否已设置。"""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if _SECRET_KEYS.search(str(k)) and not str(k).endswith("_env"):
                out[k] = "<set>" if v else "<empty>"
            else:
                out[k] = _sanitize(v)
        return out
    if isinstance(obj, list):
        return [_sanitize(i) for i in obj]
    return obj


class RepoWhitelistUpdate(BaseModel):
    repos: list[str]


class ModelsUpdate(BaseModel):
    models: dict[str, dict]
    routing: dict[str, dict]


class CapabilityCreate(BaseModel):
    name: str
    description: str = ""
    flow: str
    model_purpose: str = "chat"
    output: str = "text"
    command: str | None = None
    skill_names: list[str] = []
    toolsets: list[str] = []


class CapabilityUpdate(BaseModel):
    description: str | None = None
    flow: str | None = None
    model_purpose: str | None = None
    output: str | None = None
    command: str | None = None
    skill_names: list[str] | None = None
    toolsets: list[str] | None = None


class ChannelsUpdate(BaseModel):
    adapters: list[str]
    dingtalk: dict = {}
    report_sink: str
    dingdoc: dict = {}


class ServerUpdate(BaseModel):
    api_base: str | None = None
    workers: int | None = None
    max_steps: int | None = None
    dedup_ttl_sec: int | None = None


class SecretsUpdate(BaseModel):
    secrets: dict[str, str]


def update_whitelist_in_yaml(config_path: Path, repos: list[str]) -> None:
    """定点改写 app.yaml 的 repo_whitelist 行，保留其余内容与注释。

    行内列表格式（当前约定）：repo_whitelist: ["a", "b"]
    匹配不到行内格式时降级为整文件 yaml 重写（会丢注释，打警告日志）。
    """
    text = config_path.read_text(encoding="utf-8")
    rendered = "repo_whitelist: [" + ", ".join(json.dumps(r, ensure_ascii=False) for r in repos) + "]"
    new_text, n = re.subn(r"repo_whitelist:\s*\[[^\]]*\]", rendered, text, count=1)
    if n == 0:
        log.warning("app.yaml 的 repo_whitelist 非行内列表格式，降级为整文件重写（注释将丢失）")
        cfg = yaml.safe_load(text)
        cfg["codehost"]["repo_whitelist"] = repos
        new_text = yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False)
    config_path.write_text(new_text, encoding="utf-8")


def _task_dict(t) -> dict:
    return {
        "id": t.id, "capability": t.capability, "params": t.params,
        "status": t.status.value, "requester": t.requester.nick or t.requester.user_id,
        "channel": t.requester.channel, "report_url": t.report_url,
        "result_text": t.result_text, "error": t.error,
        "created_at": t.created_at, "finished_at": t.finished_at,
    }


def create_admin_app(settings: dict, store: SqliteStore) -> FastAPI:
    app = FastAPI(title="yu-agent admin", docs_url=None, redoc_url=None)
    app_yaml_path = ROOT / "config" / "app.yaml"

    # ── 访问控制：ADMIN_TOKEN 环境变量设置后，/api/* 全部要求令牌 ──
    # 内网部署必须设置（写接口可改模型端点/白名单，裸奔等于交出代码与 prompt）；
    # 未设置仅用于本地开发，启动时打警告。前端在 sessionStorage 存 token 并带头。
    # 令牌按请求读取：/api/secrets 热改 ADMIN_TOKEN 后无需重启本进程。
    if not os.environ.get("ADMIN_TOKEN"):
        log.warning("ADMIN_TOKEN 未设置：管理控制台接口无鉴权（仅限本地开发，内网部署必须配置）")

    @app.middleware("http")
    async def require_admin_token(request, call_next):
        expected = os.environ.get("ADMIN_TOKEN", "")
        if (expected
                and request.url.path.startswith("/api/")
                and request.headers.get("x-admin-token") != expected):
            return JSONResponse({"detail": "未授权：缺少或错误的 X-Admin-Token"},
                                status_code=401)
        return await call_next(request)

    # ── 分析 ──────────────────────────────────────────────
    @app.get("/api/overview")
    async def overview():
        stats = await store.stats_overview()
        return {**stats, "server_time": time.time()}

    @app.get("/api/tasks")
    async def tasks(limit: int = 50, offset: int = 0):
        items = await store.list_tasks(limit=min(limit, 200), offset=offset)
        return [_task_dict(t) for t in items]

    @app.get("/api/tasks/{task_id}")
    async def task_detail(task_id: str):
        """任务详情 + 审计回放（每次 LLM 调用的 token/耗时/成本）。"""
        t = await store.get_task(task_id)
        if t is None:
            raise HTTPException(404, f"任务不存在: {task_id}")
        trail = await store.get_audit_trail(task_id)
        return {"task": _task_dict(t), "audit_trail": trail,
                "totals": {
                    "tokens_in": sum(a["tokens_in"] or 0 for a in trail),
                    "tokens_out": sum(a["tokens_out"] or 0 for a in trail),
                    "cost": round(sum(a["cost"] or 0 for a in trail), 4),
                    "latency_ms": sum(a["latency_ms"] or 0 for a in trail),
                }}

    @app.get("/api/audit")
    async def audit(limit: int = 100):
        return await store.list_audit(limit=min(limit, 500))

    # ── 配置 ──────────────────────────────────────────────
    @app.get("/api/config")
    async def config_view():
        return _sanitize({
            "app": settings["app"],
            "models": settings["models"],
        })

    @app.get("/api/repos")
    async def repos_get():
        return {"repos": settings["app"]["codehost"]["repo_whitelist"]}

    @app.put("/api/repos")
    async def repos_put(body: RepoWhitelistUpdate):
        repos = sorted({r.strip() for r in body.repos if r.strip()})
        if not repos:
            raise HTTPException(400, "白名单不能为空")
        await asyncio.to_thread(update_whitelist_in_yaml, app_yaml_path, repos)
        # 落盘立即生效；本进程 settings 同步只影响控制台自身展示。
        # 审查进程（app.main）的白名单是启动时加载的，需重启才用上新值——
        # 前端保存提示已注明，热加载是 P2 预留。
        settings["app"]["codehost"]["repo_whitelist"] = repos
        await store.bump_config_version()
        log.info("仓库白名单已更新: %s", repos)
        return {"repos": repos}

    # ── 模型注册表（CRUD，写回 models.yaml）────────────────
    models_yaml_path = ROOT / "config" / "models.yaml"

    @app.get("/api/models")
    async def models_get():
        return _sanitize({"models": settings["models"].get("models", {}),
                          "routing": settings["models"].get("routing", {})})

    @app.put("/api/models")
    async def models_put(body: ModelsUpdate):
        try:
            await asyncio.to_thread(save_models, models_yaml_path,
                                    body.models, body.routing)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        settings["models"] = {"models": body.models, "routing": body.routing}
        await store.bump_config_version()
        log.info("模型注册表已更新: %s", list(body.models))
        return {"ok": True, "models": list(body.models)}

    # ── 能力注册（CRUD：capability.yaml + app.yaml 列表）────
    def _cap_dirs() -> dict[str, Path]:
        """name -> 能力目录绝对路径（读 app.yaml 注册表，逐个加载定义）。"""
        out = {}
        for rel in settings["app"].get("capabilities", []):
            d = ROOT / rel
            f = d / "capability.yaml"
            if f.exists():
                cap = load_yaml(f)
                out[cap["name"]] = d
        return out

    @app.get("/api/capabilities")
    async def caps_get():
        items = []
        for name, d in _cap_dirs().items():
            cap = load_yaml(d / "capability.yaml")
            items.append({
                "name": name,
                "description": cap.get("description", ""),
                "flow": cap.get("flow"),
                "model_purpose": cap.get("model_purpose"),
                "output": cap.get("output"),
                "command": (cap.get("triggers") or {}).get("command"),
                "skill_names": list(cap.get("skill_names") or []),
                "toolsets": list(cap.get("toolsets") or []),
                "path": str(d.relative_to(ROOT)),
            })
        return {"capabilities": items}

    @app.post("/api/capabilities")
    async def caps_create(body: CapabilityCreate):
        if body.name in _cap_dirs():
            raise HTTPException(400, f"能力 {body.name} 已存在")
        if body.model_purpose not in settings["models"].get("routing", {}):
            raise HTTPException(400, f"模型用途 {body.model_purpose} 不在 models.yaml 路由中")
        try:
            validate_capability(body.model_dump())
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        rel = f"app/capabilities/{body.name}"
        try:
            await asyncio.to_thread(save_capability, ROOT / rel,
                                    body.model_dump(exclude_none=True))
            caps = await asyncio.to_thread(add_capability_path, app_yaml_path, rel)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        settings["app"]["capabilities"] = caps
        await store.bump_config_version()
        log.info("能力已注册: %s", rel)
        return {"ok": True, "capabilities": caps}

    @app.put("/api/capabilities/{name}")
    async def caps_update(name: str, body: CapabilityUpdate):
        dirs = _cap_dirs()
        if name not in dirs:
            raise HTTPException(404, f"能力不存在: {name}")
        data = {k: v for k, v in body.model_dump().items() if v is not None}
        if not data:
            raise HTTPException(400, "没有要更新的字段")
        data["name"] = name
        try:
            await asyncio.to_thread(save_capability, dirs[name], data)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        await store.bump_config_version()
        log.info("能力已更新: %s", name)
        return {"ok": True}

    @app.delete("/api/capabilities/{name}")
    async def caps_delete(name: str):
        """注销能力：只从 app.yaml 注册表移除，能力目录文件保留（可手动恢复）。"""
        dirs = _cap_dirs()
        if name not in dirs:
            raise HTTPException(404, f"能力不存在: {name}")
        rel = str(dirs[name].relative_to(ROOT))
        try:
            caps = await asyncio.to_thread(remove_capability_path, app_yaml_path, rel)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        settings["app"]["capabilities"] = caps
        await store.bump_config_version()
        log.info("能力已注销: %s（文件保留在 %s）", name, rel)
        return {"ok": True, "capabilities": caps}

    # ── 通道与归档（写回 app.yaml）─────────────────────────
    @app.get("/api/channels")
    async def channels_get():
        a = settings["app"]
        return {
            "adapters": a.get("adapters", []),
            "dingtalk": a.get("dingtalk", {}),
            "report_sink": a.get("report_sink", "local"),
            "dingdoc": a.get("dingdoc", {}),
        }

    @app.put("/api/channels")
    async def channels_put(body: ChannelsUpdate):
        try:
            await asyncio.to_thread(update_channels, app_yaml_path,
                                    adapters=body.adapters, dingtalk=body.dingtalk,
                                    report_sink=body.report_sink, dingdoc=body.dingdoc)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        a = settings["app"]
        a["adapters"] = body.adapters
        a["dingtalk"].update(body.dingtalk)
        a["report_sink"] = body.report_sink
        a.setdefault("dingdoc", {}).update(body.dingdoc)
        await store.bump_config_version()
        log.info("通道与归档配置已更新: adapters=%s report_sink=%s",
                 body.adapters, body.report_sink)
        return {"ok": True}

    # ── 服务与代码托管（app.yaml 的 server.* / codehost.api_base）──
    @app.get("/api/server")
    async def server_get():
        a = settings["app"]
        s = a.get("server", {})
        return {"api_base": a.get("codehost", {}).get("api_base", ""),
                "workers": s.get("workers"), "max_steps": s.get("max_steps"),
                "dedup_ttl_sec": s.get("dedup_ttl_sec")}

    @app.put("/api/server")
    async def server_put(body: ServerUpdate):
        try:
            await asyncio.to_thread(update_server, app_yaml_path,
                                    **body.model_dump(exclude_none=True))
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        a = settings["app"]
        for k in ("workers", "max_steps", "dedup_ttl_sec"):
            v = getattr(body, k)
            if v is not None:
                a["server"][k] = v
        if body.api_base is not None:
            a.setdefault("codehost", {})["api_base"] = body.api_base
        await store.bump_config_version()
        log.info("服务与代码托管配置已更新: %s", body.model_dump(exclude_none=True))
        return {"ok": True}

    # ── 密钥（.env）：只回传星号掩码，写只接受白名单键的非空值 ──
    @app.get("/api/secrets")
    async def secrets_get():
        return {k: mask_secret(os.environ.get(k, "")) for k in SECRET_ENV_KEYS}

    @app.put("/api/secrets")
    async def secrets_put(body: SecretsUpdate):
        updates = {k: v for k, v in body.secrets.items() if v}
        if not updates:
            raise HTTPException(400, "没有要更新的密钥（留空 = 不修改）")
        try:
            await asyncio.to_thread(write_env, ROOT / ".env", updates)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        os.environ.update(updates)          # 本进程（含 ADMIN_TOKEN 校验）即时生效
        await store.bump_config_version()   # Agent 进程经热更新轮询 load_dotenv 生效
        log.info("密钥已更新: %s", {k: mask_secret(v) for k, v in updates.items()})
        return {"ok": True, "updated": sorted(updates)}

    # ── 前端 ──────────────────────────────────────────────
    @app.get("/")
    async def index():
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/healthz")
    async def healthz():
        return JSONResponse({"ok": True})

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


# ── 独立运行入口 ──────────────────────────────────────────

async def _serve(args) -> None:
    settings = load_settings()
    db_path = settings["app"]["server"]["db_path"]
    if args.demo:
        db_path = str(ROOT / "agent-data" / "demo.db")
        settings["app"]["server"]["db_path"] = db_path
    store = SqliteStore(db_path)
    await store.init()
    if args.demo:
        from scripts.seed_demo import seed_if_empty
        await seed_if_empty(store)
        log.info("demo 模式：数据库 %s", db_path)

    config = uvicorn.Config(create_admin_app(settings, store),
                            host=args.host, port=args.port, log_level="info")
    await uvicorn.Server(config).serve()


def main() -> None:
    parser = argparse.ArgumentParser(description="yu-agent 管理控制台")
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    # 预览平台经 PORT 环境变量或追加 --port 参数指定端口（追加的参数优先级更高）
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "7100")))
    parser.add_argument("--demo", action="store_true", help="使用本地 demo 数据库并注入示例数据")
    asyncio.run(_serve(parser.parse_args()))


if __name__ == "__main__":
    main()
