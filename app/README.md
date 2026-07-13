# Fyodor · Dash

Web implementation of the Android dashboard handoff — a personal dashboard shell for the Android chat app (embedded via WebView), covering Dash / 聊天 / 记忆库 / 用量 / 共读 / 支出 / 经期 / 系统配置.

## Running

```bash
npm install
npm run dev      # dev server
npm run build    # typecheck + production build
```

## Backend contract

The app calls the backend with **relative paths** (`/api/...`) so it works unmodified when served same-origin with the API in production (`https://love-style.xyz`). Set `VITE_API_BASE_URL` in `.env.local` only if you need to point dev at a different host. No auth is sent.

Dashboard data contracts live in `src/lib/api.ts`; the production system-configuration contracts live in `src/lib/systemConfig.ts`. Dashboard views may use deterministic mock fallbacks when the backend is unavailable. System configuration deliberately does not fake successful reads or writes: unavailable data is marked as partial, and unsupported controls such as relay failover remain visibly locked.

Endpoints referenced:

| Endpoint | Used for |
|---|---|
| `GET/PATCH /api/todos` | to-do list, done toggle |
| `GET /api/posts/summary` | 记忆库 core/long/recent counts + 渐变脑 gradient + section items |
| `GET /api/posts/calendar`, `/api/posts/calendar/day` | memory calendar + per-day entries |
| `GET /api/messages/heatmap` | chat heatmap (chat_messages counted per day) |
| `GET /api/usage/summary`, SSE `/api/usage/stream` | 5h/7d usage windows, today's msg/token counts, 7-day bar chart |
| `GET /api/books/current` | 共读 progress + notes |
| `GET /api/ledger/budget` | 本月支出 ring + categories |
| `GET /api/period/stats` | 经期 countdown + stats (phase/days-left are derived client-side in `src/lib/period.ts`) |
| `/api/config/provider`, `/api/config/key-status` | active provider and masked connection status |
| `/api/config/relay-presets`, `/api/config/models`, `/api/config/model-catalog` | relay presets and the unified model view |
| `POST /api/gw/test` | model playground with optional persona/memory injection |

Weather is fetched directly from Open-Meteo client-side (Jilin City coords), exactly as in the original design — no backend involved.

`src/config.ts` holds the handful of static display values that were editable "Tweaks" in the design tool (status quote, together-since date, show-seconds, fallback budget).
