#!/bin/bash
# 容器入口：同时拉起管理控制台（:17100）和审查服务（钉钉长连接）。
# 任一进程退出 → 本脚本退出 → 容器随 restart 策略整体重启，保证两个进程版本/生死一致。
set -u

uv run --no-sync python -m app.admin.server --host 0.0.0.0 --port 17100 &
uv run --no-sync python -m app.main &

# 等待任意一个子进程退出（bash wait -n），随后把另一个也停掉再退出
wait -n
EXIT_CODE=$?
kill %1 %2 2>/dev/null
wait 2>/dev/null
echo "子进程退出（code=$EXIT_CODE），容器退出等待编排重启" >&2
exit "${EXIT_CODE:-1}"
