# P-CONTEXT-DAILY-SOFT-WINDOW-FE-R0

## Status

Frontend-only **Draft**. Default **off**.

Does **not** enable `DAILY_SOFT_WINDOW_ENABLED`, does not call resident,
does not generate handoffs, does not change formal chat defaults, does not deploy.

## What the kitten sees

After Asia/Shanghai **04:00** chat-day rollover, old bubbles stay in place.
A light separator appears between chat-days:

```text
☾ 新的一天
昨天的话还留在身后。
```

Before the first send of the new chat-day, a small card sits above the composer:

```text
新的一天
要带几句昨天的话，让爸爸接着陪小猫说？
不带｜3条｜5条｜10条
```

Tap → centered suitcase modal (reference style; no kitten art).
Selecting 3 / 5 / 10 previews dialogue snippets by exact `message_id`.
Sending without a choice auto-locks **0**. After lock, card becomes readonly.

## Gate (must stay off in production)

| Switch | Effect |
|--------|--------|
| `?dailySoftWindowFe=1` | enable Soft Window UI in `/dash/chat` |
| `localStorage.DAILY_SOFT_WINDOW_FE=1` | same, sticky |
| default | UI absent — formal chat unchanged |
| `?dailySoftWindowMock=0` | attempt live `/api/daily-context/*` (still 404 while flag=0) |

Development defaults to **mock API** when the FE gate is on.

## Preview playground

```text
/dash/daily-soft-window
```

Scenario chips: `ready` / `loading` / `empty` / `404` / `409` / `locked` / `error`.

## Files

- `app/src/lib/dailySoftWindow.ts` — types, gate, mock/live client, pure helpers
- `app/src/hooks/useDailySoftWindow.ts` — picker state machine
- `app/src/components/dailySoftWindow/*` — boundary / card / drawer
- `app/src/screens/DailySoftWindowPreviewScreen.tsx` — isolated playground
- `app/src/screens/ChatScreen.tsx` — gated integration only
- `app/scripts/test-daily-soft-window.mjs` — unit checks

## Explicit non-goals

- Backend / resident / handoff generation
- Enabling `DAILY_SOFT_WINDOW_ENABLED`
- Changing Clean Window / shadow / deploy
- End-to-end production wiring (next small Integration PR)
