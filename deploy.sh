#!/bin/bash
# LIQ 一键部署脚本 —— 安全释放内存后重新构建并启动
#
# 用法（在服务器上）：
#   cd /www/wwwroot/LIQ
#   ./deploy.sh                # 完整流程：git pull → 预检 → down → build → up
#   ./deploy.sh --no-pull      # 跳过 git pull（只重 build 当前代码）
#   ./deploy.sh --no-build     # 跳过重 build（只 down + up，用于快速重启）
#
# 设计动机：
#   服务器物理内存 3.5GB 较紧凑，build 期若旧容器仍在跑会撞 swap thrashing 临界点
#   （iowait 95% + 内存 92% + 整机假死）。因此 build 前必须先 down 释放旧容器内存。

set -euo pipefail

cd "$(dirname "$0")"

DO_PULL=true
DO_BUILD=true

for arg in "$@"; do
  case "$arg" in
    --no-pull)  DO_PULL=false ;;
    --no-build) DO_BUILD=false ;;
    -h|--help)
      sed -n '2,12p' "$0"
      exit 0
      ;;
    *)
      echo "未知参数: $arg"
      exit 1
      ;;
  esac
done

echo "==================================================="
echo "  LIQ 部署开始 @ $(date '+%Y-%m-%d %H:%M:%S')"
echo "==================================================="

if [ "$DO_PULL" = true ]; then
  echo
  echo "==> [1/5] 拉取最新代码（旧容器继续服务）..."
  git pull --rebase --autostash
else
  echo
  echo "==> [1/5] 跳过 git pull"
fi

echo
echo "==> [2/5] 校验运行环境和CoinGlass代理..."
python3 backend/scripts/preflight_coinglass.py

export APP_GIT_SHA="$(git rev-parse HEAD)"
export APP_BUILD_TIME="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"

echo
echo "==> [3/5] 停止旧容器，释放内存..."
# down 会停 + 删容器，但 bind volumes 保留（数据安全）
docker compose down --remove-orphans

echo
echo "==> [4/5] 构建并启动容器..."
if [ "$DO_BUILD" = true ]; then
  docker compose up -d --build
else
  docker compose up -d
fi

echo
echo "==> [5/5] 部署完成，查看状态..."
sleep 2
docker compose ps
echo
echo "--- 内存占用 ---"
free -h || true
echo
echo "==================================================="
echo "  部署完成 @ $(date '+%Y-%m-%d %H:%M:%S')"
echo "==================================================="
