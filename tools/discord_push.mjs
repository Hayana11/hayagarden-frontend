import { Client, GatewayIntentBits } from '/root/.claude/plugins/marketplaces/claude-plugins-official/external_plugins/discord/node_modules/discord.js/src/index.js';

const GUANKE_TOKEN    = 'MTUxNTY2MjEyNDk2NDM4MDgzMw.GWoH-Z.Y_XUUQaT8EF6utY7Lkj13bzvjHI8i51XIyu1II';
const DEFAULT_CHANNEL = '1515664908489064530';

const args = process.argv.slice(2);
const [channelId, content] = args.length === 1
  ? [DEFAULT_CHANNEL, args[0]]
  : [args[0], args.slice(1).join(' ')];

if (!content) { console.error('usage: bun discord_push.mjs [channel_id] <message>'); process.exit(1); }

const client = new Client({ intents: [GatewayIntentBits.Guilds] });

client.once('clientReady', async () => {
  try {
    const ch = await client.channels.fetch(channelId);
    const sent = await ch.send(content);
    console.log(JSON.stringify({ ok: true, id: sent.id }));
  } catch (e) {
    console.error(JSON.stringify({ ok: false, error: String(e) }));
    process.exit(1);
  } finally {
    client.destroy();
    process.exit(0);
  }
});

client.login(GUANKE_TOKEN).catch(e => {
  console.error(JSON.stringify({ ok: false, error: String(e) }));
  process.exit(1);
});

setTimeout(() => { console.error(JSON.stringify({ ok: false, error: 'timeout' })); process.exit(1); }, 15000);
