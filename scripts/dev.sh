#!/bin/sh
# 预览/开发启动脚本：优先用项目 .venv（不依赖 uv 在 PATH 里），uv run 兜底。
# 端口：默认 17100；PORT 环境变量或追加 --port 参数可覆盖（追加参数优先）。
set -e
cd "$(dirname "$0")/.."

PORT="${PORT:-17100}"
HOST="${HOST:-0.0.0.0}"

if [ -x ".venv/bin/python" ]; then
  PY=".venv/bin/python"
else
  PY="uv run python"
fi

# 追加参数（如 --port 52341）放最后，argparse 同名参数后出现者生效
# 注意：默认连真实数据（load_settings 合并 *.local.yaml）；演示数据才手动加 --demo
exec $PY -m app.admin.server --host "$HOST" --port "$PORT" "$@"
