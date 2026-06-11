# 备份与恢复

## 备份机制
- 脚本：`/opt/frontend/tools/backup.sh`
- 计划：每天北京时间 04:00（cron，UTC 20:00）
- 位置：`/opt/backups/frontend/frontend-<日期时间>.tar.gz`
- 保留：最近 30 天
- 日志：`/var/log/frontend-backup.log`
- 内容：memories.db（sqlite 一致性快照）、.env、.mijia_auth、prompts/、static/uploads/、co-reading 全部书籍与批注数据

## 手动备份
```bash
/opt/frontend/tools/backup.sh
```

## 恢复
```bash
# 1. 解压最新备份
cd /tmp && mkdir restore && tar -xzf /opt/backups/frontend/frontend-最新.tar.gz -C restore

# 2. 停服务
systemctl stop frontend frontend-gw co-reading

# 3. 恢复文件
cp /tmp/restore/memories.db   /opt/frontend/memories.db
cp /tmp/restore/.env          /opt/frontend/.env
cp /tmp/restore/.mijia_auth   /opt/frontend/.mijia_auth
cp -r /tmp/restore/prompts/*  /opt/frontend/prompts/
cp -r /tmp/restore/uploads/*  /opt/frontend/static/uploads/ 2>/dev/null
cp -r /tmp/restore/co-reading-data/* /opt/co-reading/data/

# 4. 重启
systemctl start frontend frontend-gw co-reading
```

## 注意
- 备份在仓库目录外（/opt/backups），不会进 git
- 异地备份尚未配置：VPS 磁盘损坏时本地备份也会丢失。
  如需异地，可配 rclone 到网盘，在 backup.sh 末尾追加一行 `rclone copy` 即可
