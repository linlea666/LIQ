#!/bin/bash
# 潜力币雷达数据备份
#
# 用法：
#   ./radar/scripts/backup.sh                    # 备份到 radar/backups/
#   ./radar/scripts/backup.sh /path/to/dir       # 备份到指定目录
#   ./radar/scripts/backup.sh --verify-only      # 只校验最近一次备份
#
# 为什么必须用 sqlite3 .backup 而不是 cp：
#   数据库开着 WAL 模式，写入协程随时在提交事务。直接 cp 会拷到一个
#   处于事务中间态的文件——它看起来完全正常，能打开、能查询，
#   直到某天真正需要它的时候才发现有一段数据是撕裂的。
#   .backup 走 SQLite 的在线备份 API，保证拿到一致性快照且不阻塞写入。
#
# 为什么每次备份后立即校验：
#   一个从未被验证过的备份和没有备份没有区别。唯一比"没有备份"更糟的，
#   是"以为有备份"。

set -euo pipefail

RADAR_DIR="$(cd "$(dirname "$0")/.." && pwd)"
DB_PATH="${RADAR_DIR}/data/radar.db"
BACKUP_DIR="${1:-${RADAR_DIR}/backups}"
# 保留天数可在 crontab 里用环境变量覆盖，不读 config.yaml：
# 备份脚本必须在服务进程挂掉时也能独立工作，不能依赖它的配置栈
KEEP_DAYS="${RADAR_BACKUP_KEEP_DAYS:-30}"

if [ "${1:-}" = "--verify-only" ]; then
  BACKUP_DIR="${RADAR_DIR}/backups"
  # 备份落盘后会被压缩，因此这里必须找 .db.gz。
  # 早先只找 .db 时，明明有备份却报"没有找到"——
  # 一个把好备份误报成没备份的校验器，比不做校验更容易让人做错决定
  latest="$(ls -t "${BACKUP_DIR}"/radar-*.db.gz 2>/dev/null | head -1 || true)"
  if [ -z "$latest" ]; then
    echo "没有找到任何备份"
    exit 1
  fi
  echo "校验 ${latest}"
  tmp="$(mktemp -t radar-verify)"
  trap 'rm -f "$tmp"' EXIT
  gzip -dc "$latest" > "$tmp"
  result="$(sqlite3 "$tmp" "PRAGMA integrity_check;")"
  if [ "$result" != "ok" ]; then
    echo "校验失败：${result}"
    exit 1
  fi
  counts="$(sqlite3 "$tmp" \
    "SELECT (SELECT COUNT(*) FROM token_master) || '/' || \
            (SELECT COUNT(*) FROM alerts) || '/' || \
            (SELECT COUNT(*) FROM outcomes);")"
  echo "校验通过｜代币/警报/结局：${counts}"
  exit 0
fi

if [ ! -f "$DB_PATH" ]; then
  echo "数据库不存在：${DB_PATH}"
  exit 1
fi

mkdir -p "$BACKUP_DIR"
stamp="$(date '+%Y%m%d-%H%M%S')"
target="${BACKUP_DIR}/radar-${stamp}.db"

echo "==> 在线备份 ${DB_PATH} → ${target}"
sqlite3 "$DB_PATH" ".backup '${target}'"

echo "==> 完整性校验"
result="$(sqlite3 "$target" "PRAGMA integrity_check;")"
if [ "$result" != "ok" ]; then
  # 校验失败就删掉：留着一个损坏的备份文件，
  # 只会在真正需要恢复的那天制造虚假的安全感
  echo "备份校验失败：${result}"
  rm -f "$target"
  exit 1
fi

# 抽查关键表能否读出：integrity_check 只检查页结构，
# 它对"文件完好但表是空的"完全满意
counts="$(sqlite3 "$target" \
  "SELECT (SELECT COUNT(*) FROM token_master) || '/' || \
          (SELECT COUNT(*) FROM alerts) || '/' || \
          (SELECT COUNT(*) FROM outcomes);")"
echo "==> 校验通过｜代币/警报/结局：${counts}"

echo "==> 压缩"
gzip -f "$target"
size="$(du -h "${target}.gz" | cut -f1)"
echo "备份完成：${target}.gz（${size}）"

echo "==> 清理 ${KEEP_DAYS} 天前的备份"
find "$BACKUP_DIR" -name 'radar-*.db.gz' -mtime "+${KEEP_DAYS}" -print -delete || true

echo "现存备份："
ls -lh "$BACKUP_DIR"/radar-*.db.gz 2>/dev/null | tail -5 || echo "（无）"
