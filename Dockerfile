# yu-agent 一体化镜像：审查服务（app.main）+ 管理控制台（app.admin.server，:17100）
# 双进程编排：scripts/start.sh 同时拉起两个进程，任一退出则容器退出，
# 由 compose 的 restart: unless-stopped 整体重启——不会出现"只重启了一个"的版本漂移。
#
# 构建：docker build -t yu-agent .
# 运行：docker compose -f deploy/docker-compose.yml up -d --build
# 基础镜像走 1ms.run 镜像站（docker.io 直连不通；内网部署换成内部 registry 即可）
FROM docker.1ms.run/library/python:3.12-slim

# git：浅克隆硬依赖；curl：健康检查；ca-certificates：内网 LLM 池 / GitLab HTTPS 证书
# apt/PyPI 走国内镜像站加速（内网部署时删除 sed 与 UV_INDEX_URL，或换成内部源）
RUN sed -i 's|deb.debian.org|mirrors.ustc.edu.cn|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends git curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# uv 管理依赖（PyPI 直连可达，免去 ghcr.io 镜像依赖；版本与 uv.lock 生成端一致）
RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple uv==0.10.2

ENV UV_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple

WORKDIR /app

# 先拷依赖清单，利用 Docker 层缓存
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --locked --no-dev --no-install-project

# 再拷业务代码、配置与脚本
COPY app ./app
COPY config ./config
COPY scripts ./scripts
RUN chmod +x scripts/start.sh

# 运行态数据（SQLite / 克隆工作区 / 本地报告归档）挂卷持久化，不进镜像
RUN mkdir -p /data/yu-agent
VOLUME ["/data/yu-agent"]

# 非 root 运行
RUN useradd -m -u 10001 yuagent && chown -R yuagent:yuagent /app /data
USER yuagent

ENV PYTHONUNBUFFERED=1 \
    UV_PROJECT_ENVIRONMENT=/app/.venv

EXPOSE 17100
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s \
  CMD curl -fsS http://localhost:17100/healthz || exit 1

ENTRYPOINT ["bash", "scripts/start.sh"]
