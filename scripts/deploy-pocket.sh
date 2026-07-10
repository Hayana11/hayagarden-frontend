#!/bin/bash
# VPS 一键部署 pocket-relay（P0）
# 用法：在 /opt/frontend 工作区拉最新代码后
#   sudo ./scripts/deploy-pocket.sh
set -euo pipefail

FRONTEND_ROOT="${FRONTEND_ROOT:-/opt/frontend}"
POCKET_ROOT="/opt/pocket"
SERVICE_NAME="pocket-relay"
NGINX_CONF="/etc/nginx/conf.d/frontend.conf"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "请用 sudo 运行：sudo $0"
  exit 1
fi

echo "[pocket] 同步 server 到 ${POCKET_ROOT} ..."
mkdir -p "${POCKET_ROOT}"
rsync -a --delete \
  "${FRONTEND_ROOT}/pocket/server/" "${POCKET_ROOT}/server/"
rsync -a \
  "${FRONTEND_ROOT}/pocket/package.json" "${POCKET_ROOT}/"
rsync -a \
  "${FRONTEND_ROOT}/pocket/examples/" "${POCKET_ROOT}/examples/" 2>/dev/null || true

if [[ ! -f "${POCKET_ROOT}/.env" ]]; then
  TOKEN="$(openssl rand -hex 32)"
  cat > "${POCKET_ROOT}/.env" <<EOF
POCKET_TOKEN=${TOKEN}
POCKET_PORT=3897
POCKET_HOST=127.0.0.1
EOF
  chmod 600 "${POCKET_ROOT}/.env"
  echo "[pocket] 已生成 ${POCKET_ROOT}/.env ，POCKET_TOKEN=${TOKEN}"
  echo "[pocket] 请把同一 token 写入手机壳 App（与 VPS 配对）"
else
  echo "[pocket] 保留已有 ${POCKET_ROOT}/.env"
fi

echo "[pocket] npm install ..."
cd "${POCKET_ROOT}"
if ! command -v node >/dev/null 2>&1; then
  echo "未找到 node，请先安装 Node.js 18+"
  exit 1
fi
npm install --omit=dev

echo "[pocket] 安装 systemd 单元 ..."
cp "${FRONTEND_ROOT}/deploy/pocket-relay.service" "/etc/systemd/system/${SERVICE_NAME}.service"
systemctl daemon-reload
systemctl enable "${SERVICE_NAME}"
systemctl restart "${SERVICE_NAME}"

sleep 1
if ! systemctl is-active --quiet "${SERVICE_NAME}"; then
  echo "[pocket] 服务启动失败，journalctl -u ${SERVICE_NAME} -n 30"
  journalctl -u "${SERVICE_NAME}" -n 30 --no-pager || true
  exit 1
fi
echo "[pocket] ${SERVICE_NAME} 已运行"

if [[ -f "${NGINX_CONF}" ]] && ! grep -q 'location /pocket/' "${NGINX_CONF}"; then
  echo "[pocket] nginx 尚未配置 /pocket/，请把 deploy/nginx-pocket.snippet 内容加进 ${NGINX_CONF}"
  echo "      然后：sudo nginx -t && sudo systemctl reload nginx"
else
  echo "[pocket] nginx 已有 /pocket/ 或配置文件不存在，跳过"
fi

echo ""
echo "验收（本机，token 从 ${POCKET_ROOT}/.env 读取）："
echo "  source ${POCKET_ROOT}/.env"
echo "  curl -s http://127.0.0.1:3897/pocket/health"
echo "  curl -s http://127.0.0.1:3897/pocket/status -H \"Authorization: Bearer \$POCKET_TOKEN\""
echo "  cd ${POCKET_ROOT} && POCKET_TOKEN=\$POCKET_TOKEN npm run fake-phone -- ws://127.0.0.1:3897"
echo "  curl -s -X POST http://127.0.0.1:3897/pocket/cmd -H \"Authorization: Bearer \$POCKET_TOKEN\" -H 'Content-Type: application/json' -d '{\"action\":\"ping\"}'"
