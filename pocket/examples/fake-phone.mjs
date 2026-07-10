// fake-phone · 假手机
// 冒充一台手机连上 server，把每个指令都一本正经答对。
// 用来在真机接入前，先把 server 侧链路端到端跑通。
//
// POCKET_TOKEN=xxx npm run fake-phone -- [wsBase]
// wsBase 默认 ws://localhost:3897，也可传 wss://your-domain.example

import WebSocket from "ws";
import "dotenv/config";

const TOKEN = process.env.POCKET_TOKEN;
const BASE = process.argv[2] || "ws://localhost:3897";
if (!TOKEN) { console.error("请设置 POCKET_TOKEN"); process.exit(1); }

const ws = new WebSocket(`${BASE}/pocket/ws`, {
  headers: { Authorization: `Bearer ${TOKEN}` },
});

ws.on("open", () => console.log("[fake-phone] connected"));
ws.on("message", (data) => {
  const cmd = JSON.parse(data);
  console.log("[fake-phone] got:", JSON.stringify(cmd));
  const res = { id: cmd.id, ok: true };
  switch (cmd.action) {
    case "ping": res.result = "pong"; break;
    case "goto": res.result = "loaded " + cmd.url; break;
    case "js": res.result = "(fake eval result)"; break;
    case "html": res.result = "<html></html>"; break;
    case "screenshot": res.result = "data:image/png;base64,FAKE"; break;
    default: res.ok = false; res.error = "unknown action";
  }
  ws.send(JSON.stringify(res));
});
ws.on("close", (c, r) => { console.log("[fake-phone] closed", c, String(r)); process.exit(0); });
