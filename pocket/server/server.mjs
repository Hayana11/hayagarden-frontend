// pocket-relay · HayaGarden 自维护中继（协议兼容 pocket-browser）
// 手机 WebView 经 WebSocket 注册；gateway 经 localhost HTTP 下指令。
//
// 环境变量：
// POCKET_TOKEN 必填，HTTP/WS 统一 Bearer 鉴权
// POCKET_PORT 可选，默认 3897
// POCKET_HOST 可选，默认 127.0.0.1（nginx 只反代 /pocket/ws；health/status/cmd 不出公网）

import http from "http";
import crypto from "crypto";
import "dotenv/config";
import { WebSocketServer } from "ws";

const PORT = Number(process.env.POCKET_PORT) || 3897;
const HOST = process.env.POCKET_HOST || "127.0.0.1";
const TOKEN = process.env.POCKET_TOKEN;

if (!TOKEN) {
  console.error("请设置环境变量 POCKET_TOKEN");
  process.exit(1);
}

let phone = null; // 当前手机连接（只保留最新一个）
let phoneLastSeen = null;
const pending = new Map(); // id -> { resolve, timer }

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, "http://x");
  const auth = (req.headers.authorization || "").replace("Bearer ", "");

  // 健康检查不需要鉴权
  if (url.pathname === "/pocket/health") {
    res.writeHead(200, { "Content-Type": "application/json" });
    return res.end(JSON.stringify({ ok: true, phone: !!phone, last_seen: phoneLastSeen }));
  }

  if (auth !== TOKEN) {
    res.writeHead(401);
    return res.end('{"error":"bad token"}');
  }

  // 手机在不在线
  if (url.pathname === "/pocket/status") {
    res.writeHead(200, { "Content-Type": "application/json" });
    return res.end(JSON.stringify({ phone_connected: !!phone, last_seen: phoneLastSeen }));
  }

  // 下指令
  if (url.pathname === "/pocket/cmd" && req.method === "POST") {
    if (!phone) {
      res.writeHead(503);
      return res.end('{"error":"phone not connected"}');
    }
    let body = "";
    for await (const c of req) body += c;
    let cmd;
    try { cmd = JSON.parse(body); } catch { res.writeHead(400); return res.end('{"error":"bad json"}'); }

    // cmd: { action:"ping"|"goto"|"js"|"screenshot"|"html", url?, js?, timeout_ms? }
    const id = crypto.randomUUID();
    const timeoutMs = Math.min(cmd.timeout_ms || 30000, 120000);
    const result = await new Promise((resolve) => {
      const timer = setTimeout(() => { pending.delete(id); resolve({ ok: false, error: "timeout" }); }, timeoutMs);
      pending.set(id, { resolve, timer });
      phone.send(JSON.stringify({ id, ...cmd }));
    });
    res.writeHead(200, { "Content-Type": "application/json" });
    return res.end(JSON.stringify(result));
  }

  res.writeHead(404);
  res.end('{"error":"not found"}');
});

function bearerToken(header) {
  const m = (header || "").match(/^Bearer\s+(.+)$/i);
  return m ? m[1] : "";
}

const wss = new WebSocketServer({ server, path: "/pocket/ws" });
wss.on("connection", (ws, req) => {
  if (bearerToken(req.headers.authorization) !== TOKEN) return ws.close(4001, "bad token");

  // 新手机顶掉旧连接
  if (phone) { try { phone.close(4002, "replaced"); } catch {} }
  phone = ws;
  phoneLastSeen = new Date().toISOString();
  console.log("[pocket] phone connected");

  ws.on("message", (data) => {
    phoneLastSeen = new Date().toISOString();
    let msg;
    try { msg = JSON.parse(data); } catch { return; }
    const p = pending.get(msg.id);
    if (p) { clearTimeout(p.timer); pending.delete(msg.id); p.resolve(msg); }
  });
  ws.on("pong", () => { phoneLastSeen = new Date().toISOString(); });
  ws.on("close", () => { if (phone === ws) { phone = null; console.log("[pocket] phone disconnected"); } });
});

// 心跳，保持连接活着 + 及时发现掉线
setInterval(() => { if (phone) { try { phone.ping(); } catch {} } }, 25000);

server.listen(PORT, HOST, () => console.log(`[pocket] listening on ${HOST}:${PORT}`));
