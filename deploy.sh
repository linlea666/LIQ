#!/bin/bash
# LIQ 一键部署脚本 —— 安全释放内存后重新构建并启动
#
# 用法（在服务器上）：
#   cd /www/wwwroot/LIQ
#   ./deploy.sh                # 完整流程：down → git pull → build → up
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

echo
echo "==> [1/4] 停止旧容器，释放内存..."
# down 会停 + 删容器，但 volumes 保留（数据安全）
docker compose down --remove-orphans

if [ "$DO_PULL" = true ]; then
  echo
  echo "==> [2/4] 拉取最新代码..."
  git pull --rebase --autostash
else
  echo
  echo "==> [2/4] 跳过 git pull"
fi

echo
echo "==> [3/4] 构建并启动容器..."
if [ "$DO_BUILD" = true ]; then
  docker compose up -d --build
else
  docker compose up -d
fi

echo
echo "==> [4/4] 部署完成，查看状态..."
sleep 2
docker compose ps
echo
echo "--- 内存占用 ---"
free -h || true
echo
echo "==================================================="
echo "  部署完成 @ $(date '+%Y-%m-%d %H:%M:%S')"
echo "==================================================="
