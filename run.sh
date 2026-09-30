#!/usr/bin/env bash
# 一条本地命令启动平台（含正常开梯/跳停/复检退回三个演示样例说明）
set -euo pipefail
cd "$(dirname "$0")"

PORT="${PORT:-8000}"
echo "=============================================="
echo " 商场扶梯启停巡检与封控复开平台"
echo " 演示数据：6 部扶梯 / 8 名人员"
echo " 访问地址：http://localhost:${PORT}"
echo " 命令行样例：python3 -m app.demo         （正常开梯/跳停/复检退回）"
echo "=============================================="

# 传 --reset 重建演示数据；数据库不存在时自动初始化
RESET=""
if [ "${1:-}" = "--reset" ] || [ ! -f data/platform.db ]; then
  RESET="--reset"
fi

exec python3 -m app.main --host 0.0.0.0 --port "${PORT}" ${RESET}
