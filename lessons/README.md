# learn-from-mistakes MCP (v2)

教训库 MCP server：记录错误 → 提炼候选教训 → 人工审核升格 → 改代码前 validate。

- 数据库：`/opt/frontend/memories.db`（表 `lesson_candidates` / `lessons`）
- 部署位置：VPS `/opt/lessons/`，systemd 服务 `lessons-mcp`，监听 `127.0.0.1:5055`
- 对外地址：`https://love-style.xyz/lessons-mcp/mcp`（nginx 反代）

## v2 相对 v1 的改动

1. `lesson_candidates.author` — 记录来源（默认 `cc`）
2. `lessons.reason` — promote 时说明为什么有这条教训
3. `lessons.status`（`active`/`deprecated`）替代 `active` 整数列
4. 新工具 `search_lesson` — 关键词搜索
5. 新工具 `deprecate_lesson` — 标记过时
6. `validate_edit` 两层筛选 — 先 tag 关键词 quick_match，命中才调 Claude
7. `LESSON_MODEL` 环境变量 — 可切换提炼/校验用的模型，默认 haiku
8. `ANTHROPIC_API_URL` 环境变量 — API 地址可指向 relay（默认官方 `api.anthropic.com`）。VPS 上官方 key 不可用，实际配置指向 relay `https://68886868.xyz/v1/messages` + `LESSON_MODEL=[按量3] deepseek-v3.2`（relay 无 haiku 通道；Kiro claude 通道太慢，deepseek-v3.2 快且输出纯 JSON）
9. `parse_json` — 剥掉模型输出可能带的 ` ```json ` 围栏再解析

## 部署

```bash
mkdir -p /opt/lessons
# 复制 server.py + requirements.txt 到 /opt/lessons 后：
cd /opt/lessons && python3 -m venv venv && venv/bin/pip install -r requirements.txt -q

API_KEY=$(grep ANTHROPIC_API_KEY /opt/frontend/.env | cut -d'=' -f2)
cat > /etc/systemd/system/lessons-mcp.service << EOF
[Unit]
Description=Learn From Mistakes MCP
After=network.target
[Service]
WorkingDirectory=/opt/lessons
ExecStart=/opt/lessons/venv/bin/python server.py
Restart=always
Environment=ANTHROPIC_API_KEY=${API_KEY}
Environment=LESSON_MODEL=claude-haiku-4-5-20251001
[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload && systemctl enable --now lessons-mcp
```

nginx（加进现有 server 块）：

```nginx
location /lessons-mcp/ {
    proxy_pass http://127.0.0.1:5055/;
    proxy_http_version 1.1;
    proxy_set_header Connection "";
    proxy_buffering off;
}
```
