#!/bin/sh
# 云端数据备份：SQLite（在线备份，不锁库）+ 产物目录打包，保留最近 7 份。
# 在 deploy/ 目录下跑，建议 crontab 每天 4:00 一次：
#   0 4 * * * cd /opt/text2stata/deploy && ./backup.sh >> /var/log/t2s-backup.log 2>&1

set -eu
cd "$(dirname "$0")"

BACKUP_DIR="${T2S_BACKUP_DIR:-../backups}"
KEEP=7
STAMP=$(date +%Y%m%d-%H%M%S)
mkdir -p "$BACKUP_DIR"

echo "[$STAMP] 开始备份"

# 1) SQLite 在线备份：用 Python 的 backup API 而不是拷文件——
#    直接 cp 一个正在写的 SQLite 会拿到损坏副本。
docker compose exec -T cloud python -c "
import sqlite3
src = sqlite3.connect('/data/cloud.db')
dst = sqlite3.connect('/data/backup-tmp.db')
src.backup(dst)
dst.close(); src.close()
print('sqlite backup done')
"
docker compose cp cloud:/data/backup-tmp.db "$BACKUP_DIR/cloud-$STAMP.db"
docker compose exec -T cloud rm /data/backup-tmp.db

# 2) 产物目录打包（rtf/csv 报告，不含 .dta——本来就不会存上来）
docker compose exec -T cloud tar -C /data -czf /tmp/artifacts.tar.gz artifacts
docker compose cp cloud:/tmp/artifacts.tar.gz "$BACKUP_DIR/artifacts-$STAMP.tar.gz"
docker compose exec -T cloud rm /tmp/artifacts.tar.gz

# 3) 轮转：只留最近 KEEP 份
ls -1t "$BACKUP_DIR"/cloud-*.db 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm --
ls -1t "$BACKUP_DIR"/artifacts-*.tar.gz 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm --

echo "[$STAMP] 备份完成，当前留存："
ls -lh "$BACKUP_DIR" | tail -n +2
