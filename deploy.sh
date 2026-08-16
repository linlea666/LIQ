#!/bin/bash
# LIQ 一键部署脚本 —— 安全释放内存后串行构建并分阶段启动
#
# 用法（在服务器上）：
#   cd /www/wwwroot/LIQ
#   ./deploy.sh                # 完整流程：git pull → 预检 → down → 串行 build → 分阶段 up
#   ./deploy.sh --no-pull      # 跳过 git pull（只重 build 当前代码）
#   ./deploy.sh --no-build     # 跳过重 build（只 down + up，用于快速重启）
#   ./deploy.sh --no-radar     # 不部署潜力币雷达（主链路故障时优先保主服务）
#
# 设计动机：
#   服务器物理内存 3.5GB 较紧凑，build 期若旧容器仍在跑会撞 swap thrashing 临界点
#   （iowait 95% + 内存 92% + 整机假死）。因此 build 前必须先 down 释放旧容器内存，
#   且后端与前端必须串行构建：Node 构建峰值 2GB，不能与 Python 引擎或彼此叠加。
#   启动同样分阶段：先等后端 /api/ready 通过，再拉起前端。
#
#   雷达排在后端与前端之间启动：它是独立服务，起不来不应阻断前端；
#   但要在前端 build 完成之后再启动，避免与 Node 构建的 2GB 峰值叠加。

set -euo pipefail

cd "$(dirname "$0")"

DO_PULL=true
DO_BUILD=true
DO_RADAR=true
READY_TIMEOUT_SEC=600
RADAR_READY_TIMEOUT_SEC=180

for arg in "$@"; do
  case "$arg" in
    --no-pull)  DO_PULL=false ;;
    --no-build) DO_BUILD=false ;;
    --no-radar) DO_RADAR=false ;;
    -h|--help)
      sed -n '2,15p' "$0"
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
  echo "==> [1/6] 拉取最新代码（旧容器继续服务）..."
  git pull --rebase --autostash
else
  echo
  echo "==> [1/6] 跳过 git pull"
fi

echo
echo "==> [2/6] 校验运行环境和CoinGlass代理..."
python3 backend/scripts/preflight_coinglass.py

if [ "$DO_RADAR" = true ] && [ ! -f radar/.env ]; then
  # 早失败：缺 .env 时容器能起来、能采集、能评分，唯独邮件默默发不出去。
  # 这种"一切正常但没人收到通知"的故障最难被发现
  echo "缺少 radar/.env（可从 radar/.env.example 复制并填入 SMTP 凭据）"
  exit 1
fi

export APP_GIT_SHA="$(git rev-parse HEAD)"
export APP_BUILD_TIME="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"

echo
echo "==> [3/6] 停止旧容器，释放内存..."
# down 会停 + 删容器，但 bind volumes 保留（数据安全）
docker compose down --remove-orphans

echo
if [ "$DO_BUILD" = true ]; then
  echo "==> [4/6] 串行构建镜像（后端 → 雷达 → 前端，构建期不运行任何容器）..."
  docker compose build backend
  if [ "$DO_RADAR" = true ]; then
    docker compose build radar
  fi
  docker compose build frontend
else
  echo "==> [4/6] 跳过镜像构建"
fi

echo
echo "==> [5/6] 启动后端并等待 /api/ready（最长 ${READY_TIMEOUT_SEC}s）..."
docker compose up -d --no-deps backend

deadline=$(( $(date +%s) + READY_TIMEOUT_SEC ))
until curl -fsS -o /dev/null --max-time 5 http://localhost:8800/api/ready; do
  if [ "$(date +%s)" -ge "$deadline" ]; then
    echo "后端在 ${READY_TIMEOUT_SEC}s 内未就绪，前端不启动。诊断信息："
    curl -sS --max-time 5 http://localhost:8800/api/health || true
    echo
    docker compose logs --tail 80 backend || true
    exit 1
  fi
  sleep 5
done
echo "后端已就绪。"

if [ "$DO_RADAR" = true ]; then
  echo
  echo "==> [5.5/6] 启动潜力币雷达（独立服务，失败不阻断前端）..."
  docker compose up -d --no-deps radar

  radar_deadline=$(( $(date +%s) + RADAR_READY_TIMEOUT_SEC ))
  radar_ok=false
  until curl -fsS -o /dev/null --max-time 5 http://localhost:8802/api/radar/ready; do
    if [ "$(date +%s)" -ge "$radar_deadline" ]; then break; fi
    sleep 5
  done
  if curl -fsS -o /dev/null --max-time 5 http://localhost:8802/api/radar/ready; then
    radar_ok=true
  fi

  if [ "$radar_ok" = true ]; then
    echo "雷达已就绪。"
  else
    # 刻意不 exit：雷达是研究工具，主链路（行情大屏）不该为它停摆。
    # 但必须把日志打出来，否则一次静默失败会让人几天后才发现"最近没有新币"
    echo "雷达在 ${RADAR_READY_TIMEOUT_SEC}s 内未就绪（不阻断部署）。近期日志："
    docker compose logs --tail 60 radar || true
  fi
fi

echo
echo "==> [6/6] 启动前端..."
docker compose up -d --no-deps frontend

sleep 2
docker compose ps
echo
echo "--- 内存占用 ---"
free -h || true
docker stats --no-stream --format 'table {{.Name}}\t{{.MemUsage}}\t{{.MemPerc}}' || true
echo
echo "==================================================="
echo "  部署完成 @ $(date '+%Y-%m-%d %H:%M:%S')"
echo "==================================================="
