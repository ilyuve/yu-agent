# yu-agent

通用智能体框架（Python / Pydantic AI 2.x）。首期内置能力：IM 群内 @ 触发 GitLab 分支/MR 代码审查（慢查询风险 + 通用 CR），审查报告可归档到钉钉知识库或本地文件；另内置通用问答能力（群内连续对话，按 群×人 隔离上下文）。

架构上五轴可替换，方便扩展新能力：

| 轴 | 抽象 | 当前实现 |
|---|---|---|
| 能力 | `capability.yaml` + SKILL.md | code-review（慢查预扫 + CR）、qa（问答） |
| 模型 | `models.yaml` 注册表 + 用途路由 | OpenAI 兼容协议，主备 Fallback |
| 通道 | Adapter Protocol | 钉钉（dingtalk-stream 长连接 + OpenAPI 回推） |
| 归档 | ReportSink Protocol | 本地文件（默认）/ 钉钉知识库 |
| 托管平台 | CodeHost Protocol | GitLab REST v4（分支 + MR） |

新增能力 = 新增一个 capability 目录 + 一行配置，或在管理控制台点「新建能力」，不改核心代码；换模型/通道/归档/托管平台只改配置或新增一个实现文件。

## 环境要求

- [uv](https://docs.astral.sh/uv/)（包管理与虚拟环境）
- Python 3.12（uv 依据 `.python-version` 自动管理）
- git CLI（浅克隆用）；Docker（部署用）

## 快速开始

```bash
uv sync                      # 创建 .venv 并安装依赖
cp .env.example .env         # 填写真实凭据（严禁提交）
uv run pytest                # 跑测试（含端到端冒烟，不依赖外部凭据）
uv run python -m app.main    # 启动审查服务（IM Stream 长连接 + 任务引擎）
```

没有 IM/模型凭据也能体验控制台与数据结构：

```bash
sh scripts/dev.sh            # demo 模式起管理控制台，默认 http://localhost:17100/
```

## 管理控制台

`app.admin.server`：任务观测（token/成本/审计回放抽屉）+ 配置管理。
仓库白名单、模型注册表、用途路由、能力注册、通道与归档都可在界面上增删改，
写回 `config/*.yaml`（注释保留），不引入配置数据库。

## 配置

`config/` 下全部为占位符配置，可安全进库；真实值走 `.env` 与 `config/*.local.yaml`（已 gitignore）。

| 文件 | 作用 |
|---|---|
| `config/app.yaml` | 服务/通道/托管平台/归档/能力注册 |
| `config/models.yaml` | 模型注册表 + 用途路由 + 单价（OpenAI 兼容） |
| `config/scan_rules.yaml` | 慢查静态预扫规则集（改规则不改代码） |
| `config/mcp_config.json` | MCP server 清单（配置即加载，注册名 `mcp.<key>`） |

## 审查流程（编排层固定 + Agent 局部自主）

```
群内 @ → 路由（显式命令 → LLM 意图分类 → 问答兜底；白名单代码裁决）
→ 受理秒回 → 队列（幂等去重）→ 校验仓库 → 浅克隆 → 静态预扫（确定性规则）
→ Agent 深审（工具白名单 + 步数上限）→ 结构化输出（Pydantic schema 强约束）
→ 归档 → 群里 @ 发起人回推结果
```

骨架在编排层写死（可测试、可审计），判断点交给 Agent（有护栏），输出由 schema 管控。

## 部署

```bash
docker compose -f deploy/docker-compose.yml up -d --build
```

单容器内由 `scripts/start.sh` 同时拉起两个进程：**审查服务**（app.main，钉钉长连接，不暴露端口）和**管理控制台**（app.admin.server，17100 端口）。任一进程退出容器即整体退出，由 `restart: unless-stopped` 拉起——两个进程永远同版本、同生死，不存在"只重启一个"的漂移。数据挂卷 `/data/yu-agent`（SQLite / 克隆工作区 / 报告归档）。

镜像构建说明：Dockerfile 在仓库根目录（compose 的 `build: ..` 默认使用它）；国内构建时基础镜像走 `docker.1ms.run` 镜像站、apt/PyPI 走 USTC/清华源，内网部署时替换为内部 registry 与制品源即可。

## 钉钉侧准备工作

1. 开放平台创建企业内部应用，拿到 Client ID / Secret（填 `.env`）；
2. 应用内添加机器人能力，消息接收模式选 **Stream**，把机器人拉进审查群；
3. 开通权限：机器人发消息（`qyapi_robot_sendmsg` 相关）；如需归档到知识库，
   再开通 `Document.WorkspaceDocument.Write`，并在控制台「通道与归档」填
   workspace_id 与操作人 unionId，把 report_sink 切到 `dingdoc`。
