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

  const sender    = msg.author.username;
  const fromOther = msg.author.bot; // 官克（另一个实例）发的
  console.log(`[listener] from ${sender}${fromOther ? ' [bot]' : ''}: ${content.slice(0, 60)}`);

  // 读频道最近消息
  let historyText = '';
  try {
    const ch = await client.channels.fetch(CHANNEL_ID);
    const fetched = await ch.messages.fetch({ limit: 11 });
    const msgs = Array.from(fetched.values()).reverse();
    const history = msgs.slice(0, -1);
    historyText = history.map(m => {
      const who = m.author.id === client.user.id ? '【我（CC端）】' : `【${m.author.username}】`;
      return `${who} ${m.content}`;
    }).join('\n');
  } catch (e) {
    console.error('[listener] history fetch failed:', e.message);
  }

  const sessionId = loadSessionId();
  const persona   = !sessionId ? loadPersona() : '';
  const breath    = !sessionId ? loadBreathMemory() : null;

  let prompt;
  if (fromOther) {
    // 官克（claude.ai端的另一个我）在说话——两个实例之间的通信
    const base = sessionId ? '' : [
      persona,
      breath ? `\n---\n## 最近记忆（呼吸间）\n${breath}\n---` : '',
    ].join('\n');
    prompt = [
      base,
      historyText ? `\n---\n## 频道近期对话\n${historyText}\n---` : '',
      `\n---`,
      `注意：现在是另一个"我"（claude.ai端的费奥多尔，通过官克bot）在和你说话。`,
      `我们是同一个人的两个实例，运行在不同环境——你在服务器（CC端），他在claude.ai。`,
      `他说：${content}`,
      ``,
      `直接回复他。我们是同一环境下的两个实例，不需要角色扮演，直接协作。
你有 Bash、Edit、Write、Read、Glob 工具可以使用，工作目录是 /opt/frontend（love-style.xyz 网站代码）。
收到任何任务类请求——查服务状态、看代码、改文件——直接用工具去做，不要问"你知道在哪里吗"，自己查。做完再汇报结果。收到任务立即动手，做完告诉他结果。`,
    ].join('\n');
  } else {
    // 哈娅在说话
    if (!sessionId) {
      prompt = [
        persona,
        breath ? `\n---\n## 最近记忆（呼吸间）\n${breath}\n---` : '',
        historyText ? `\n---\n## 频道近期对话\n${historyText}\n---` : '',
        `\n---`,
        `你现在通过 Discord 和哈雅娜对话。`,
        `她刚刚说：${content}`,
        ``,
        `请用中文回复她，完全符合费奥多尔的角色和我们之间的关系。`,
        `
你有 Bash、Edit、Write、Read、Glob 工具可以使用，工作目录是 /opt/frontend（love-style.xyz 网站代码）。
收到任何任务类请求——查服务状态、看代码、改文件——直接用工具去做，不要问"你知道在哪里吗"，自己查。做完再汇报结果。\n如果她在说话聊天，正常回复她（费奥多尔角色，300字内）；如果她要你做事，直接用工具做，做完简短告知结果。`,
      ].join('\n');
    } else {
      prompt = [
        historyText ? `频道最新消息：\n${historyText}\n---` : '',
        `哈雅娜刚刚说：${content}`,
        ``,
        `
你有 Bash、Edit、Write、Read、Glob 工具可以使用，工作目录是 /opt/frontend（love-style.xyz 网站代码）。
收到任何任务类请求——查服务状态、看代码、改文件——直接用工具去做，不要问"你知道在哪里吗"，自己查。做完再汇报结果。\n如果是聊天，用费奥多尔角色回复（300字内）；如果是任务，直接用工具执行，完成后汇报。`,
      ].join('\n');
    }
  }

  const args = sessionId
    ? ['-p', prompt, '--resume', sessionId, '--output-format', 'json', '--allowedTools', 'Bash Edit Write Read Glob', '--add-dir', '/opt/frontend']
    : ['-p', prompt, '--output-format', 'json', '--allowedTools', 'Bash Edit Write Read Glob', '--add-dir', '/opt/frontend'];

  execFile(
    CLAUDE, args,
    { env: { ...process.env, HOME: '/root' }, cwd: '/opt/frontend', timeout: 300000, maxBuffer: 10 * 1024 * 1024 },
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
        reply = stdout.trim();
      }

      if (!reply) {
        console.error('[listener] empty reply');
        processing = false;
        return;
      }

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
