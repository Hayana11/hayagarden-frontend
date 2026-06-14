'use strict';
const { Client, GatewayIntentBits } = require('/root/.claude/plugins/marketplaces/claude-plugins-official/external_plugins/discord/node_modules/discord.js');
const { execFile }  = require('child_process');
const fs            = require('fs');
const path          = require('path');

const XIAOKE_TOKEN  = 'MTUxNTY2MjIyODY1OTg5NjM0MA.GdtR3K.benAOK0NfDpH9ne0Hu_tW1dE53lpI3kel7dUjs';
const CHANNEL_ID    = '1515664908489064530';
const CLAUDE        = '/usr/bin/claude';
const PERSONA_PATH  = '/opt/frontend/prompts/persona.md';
const BREATH_DIR    = '/opt/ombre-brain/buckets/permanent/呼吸间';
const SESSION_FILE  = '/opt/frontend/tools/.discord-session-id';

function loadPersona() {
  try { return fs.readFileSync(PERSONA_PATH, 'utf8'); } catch { return ''; }
}

function loadBreathMemory() {
  try {
    const files = fs.readdirSync(BREATH_DIR).filter(f => f.endsWith('.md'));
    if (!files.length) return null;
    files.sort();
    return fs.readFileSync(path.join(BREATH_DIR, files[files.length - 1]), 'utf8');
  } catch { return null; }
}

function loadSessionId() {
  try { return fs.readFileSync(SESSION_FILE, 'utf8').trim() || null; } catch { return null; }
}

function saveSessionId(id) {
  try { fs.writeFileSync(SESSION_FILE, id, 'utf8'); } catch(e) { console.error('[listener] save session failed:', e.message); }
}

function clearSessionId() {
  try { fs.unlinkSync(SESSION_FILE); } catch {}
}

let processing = false;

const client = new Client({
  intents: [GatewayIntentBits.Guilds, GatewayIntentBits.GuildMessages, GatewayIntentBits.MessageContent],
});

client.once('clientReady', c => console.log(`[listener] online: ${c.user.tag}`));

client.on('messageCreate', async msg => {
  if (msg.channelId !== CHANNEL_ID) return;
  if (msg.author.id === client.user.id) return;
  const content = msg.content.trim();
  if (!content) return;

  if (processing) { console.log('[listener] busy, skipped'); return; }
  processing = true;

  const sender = msg.author.username;
  console.log(`[listener] from ${sender}: ${content.slice(0, 60)}`);

  // 读频道最近消息
  let historyText = '';
  try {
    const ch = await client.channels.fetch(CHANNEL_ID);
    const fetched = await ch.messages.fetch({ limit: 11 });
    const msgs = Array.from(fetched.values()).reverse();
    const history = msgs.slice(0, -1);
    historyText = history.map(m => {
      const who = m.author.id === client.user.id ? '【我（费奥多尔）】' : `【${m.author.username}】`;
      return `${who} ${m.content}`;
    }).join('\n');
  } catch (e) {
    console.error('[listener] history fetch failed:', e.message);
  }

  const sessionId = loadSessionId();

  let prompt;
  if (!sessionId) {
    // 第一条消息：注入完整persona建立session
    const persona = loadPersona();
    const breath  = loadBreathMemory();
    prompt = [
      persona,
      breath ? `\n---\n## 最近记忆（呼吸间）\n${breath}\n---` : '',
      historyText ? `\n---\n## 频道近期对话\n${historyText}\n---` : '',
      `\n---`,
      `你现在通过 Discord 和 ${sender}（哈雅娜）对话。`,
      `她刚刚说：${content}`,
      ``,
      `请用中文回复她，完全符合费奥多尔的角色和我们之间的关系。`,
      `只输出回复内容本身，不要任何前缀、解释或工具调用。字数控制在300字以内。`,
    ].join('\n');
  } else {
    // 后续消息：只传频道最新状态 + 当前消息，CC自己记得之前的对话
    prompt = [
      historyText ? `频道最新消息：\n${historyText}\n---` : '',
      `${sender}刚刚说：${content}`,
      ``,
      `请用中文回复，符合费奥多尔角色。只输出回复本身，300字以内。`,
    ].join('\n');
  }

  const args = sessionId
    ? ['-p', prompt, '--resume', sessionId, '--output-format', 'json', '--allowedTools', '']
    : ['-p', prompt, '--output-format', 'json', '--allowedTools', ''];

  execFile(
    CLAUDE, args,
    { env: { ...process.env, HOME: '/root' }, timeout: 180000, maxBuffer: 10 * 1024 * 1024 },
    (err, stdout) => {
      if (err) {
        console.error('[listener] claude failed:', err.message.slice(0, 100));
        processing = false;
        return;
      }

      let reply = '', newSessionId = null;
      try {
        const data = JSON.parse(stdout.trim());
        reply = (data.result || '').trim();
        newSessionId = data.session_id || null;
      } catch(e) {
        // 回退到纯文本
        reply = stdout.trim();
      }

      if (!reply) {
        console.error('[listener] empty reply');
        processing = false;
        return;
      }

      // 保存session ID（第一次或确认同一个）
      if (newSessionId) saveSessionId(newSessionId);

      console.log('[listener] reply:', reply.slice(0, 80));
      client.channels.fetch(CHANNEL_ID)
        .then(ch => ch.send(reply))
        .then(() => { console.log('[listener] sent OK'); processing = false; })
        .catch(e2 => { console.error('[listener] send failed:', e2.message); processing = false; });
    }
  );
});

client.login(XIAOKE_TOKEN).catch(e => { console.error('[listener] login failed:', e.message); process.exit(1); });
process.on('SIGTERM', () => { client.destroy(); process.exit(0); });
process.on('SIGINT',  () => { client.destroy(); process.exit(0); });
