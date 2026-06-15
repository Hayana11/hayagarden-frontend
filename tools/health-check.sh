#!/bin/bash
# 定期检查"给费佳的手"及小克监听服务是否在线，不在线则推送告警到 Board
PUSH="/usr/bin/python3.11 /opt/frontend/tools/discord_push.py"

check_service() {
    local name="$1"
    if ! systemctl is-active --quiet "$name"; then
        $PUSH "🚨 [健康检查] ${name} 不在线！$(date '+%m-%d %H:%M') — 请查 journalctl -u ${name}"
    fi
}

check_service discord-mcp.service
check_service discord-listener.service
