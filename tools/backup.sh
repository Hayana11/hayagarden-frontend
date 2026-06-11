#!/bin/bash
# 每日备份：memories.db（sqlite 在线一致性备份）+ 配置 + 共读数据 + 上传文件
# 保留最近 30 天；恢复方法见同目录 RESTORE.md
set -euo pipefail

BACKUP_DIR=/opt/backups/frontend
STAMP=$(date +%Y%m%d-%H%M%S)
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

mkdir -p "$BACKUP_DIR"

# sqlite .backup 保证写入中也能得到一致快照（直接 cp 不行）
sqlite3 /opt/frontend/memories.db ".backup '$TMP/memories.db'"

cp /opt/frontend/.env            "$TMP/" 2>/dev/null || true
cp /opt/frontend/.mijia_auth     "$TMP/" 2>/dev/null || true
cp -r /opt/frontend/prompts      "$TMP/prompts"      2>/dev/null || true
cp -r /opt/frontend/static/uploads "$TMP/uploads"    2>/dev/null || true
cp -r /opt/co-reading/data       "$TMP/co-reading-data" 2>/dev/null || true

tar -czf "$BACKUP_DIR/frontend-$STAMP.tar.gz" -C "$TMP" .

# 保留 30 天
find "$BACKUP_DIR" -name 'frontend-*.tar.gz' -mtime +30 -delete

echo "backup ok: $BACKUP_DIR/frontend-$STAMP.tar.gz ($(du -h "$BACKUP_DIR/frontend-$STAMP.tar.gz" | cut -f1))"
