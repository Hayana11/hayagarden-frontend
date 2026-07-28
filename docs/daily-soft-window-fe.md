# P-CONTEXT-DAILY-SOFT-WINDOW-FE-R0

## Status

Frontend **Draft** — **preview-only**. Keep Draft; do **not** merge into formal chat yet.

- Does **not** enable `DAILY_SOFT_WINDOW_ENABLED`
- Does **not** call resident / Wake / model
- Does **not** change `/dash/chat` send flow
- Awaits backend **R1.1 round contract** before rewiring live selection

## What this phase ships

Isolated mock playground:

```text
/dash/daily-soft-window
```

Safe for phone visual QA. Uses mock API only (`forceMock`). Can be opened / deployed as a route without turning Soft Window on for formal chat.

### Round visual semantics

Picker options:

```text
不带｜3轮｜5轮｜10轮
```

Mock candidates are grouped as **complete conversation rounds**:

> one `user` message + following `assistant` messages until the next `user`

Selecting `N` packs the last **N rounds** (all message ids inside those rounds). `carryover_count` is the round count.

### Preview surface

- Day soft boundary between chat-days
- Composer card → centered packing modal
- Scenario chips: `ready` / `loading` / `empty` / `404` / `409` / `locked` / `error`
- Preview ↑ simulates lock-0 (mock only)

## Formal chat (`/dash/chat`) — temporarily disabled

Until R1.1:

| Must not | Status |
|----------|--------|
| Call `POST /api/daily-context/select-carryover` | removed from ChatScreen |
| Auto-lock 0 on first send | removed |
| Show picker card / Soft Window modal | removed |
| Change send flow | unchanged |
| Activate via `?dailySoftWindowFe=1` / `localStorage.DAILY_SOFT_WINDOW_FE` | hard-off (`isDailySoftWindowFeEnabled` → `false`) |

## Files

- `app/src/lib/dailySoftWindow.ts` — round helpers, mock client
- `app/src/hooks/useDailySoftWindow.ts` — preview state machine
- `app/src/components/dailySoftWindow/*` — boundary / card / modal
- `app/src/screens/DailySoftWindowPreviewScreen.tsx` — isolated playground
- `app/scripts/test-daily-soft-window.mjs` — unit checks

## Explicit non-goals

- Backend / resident / handoff generation
- Enabling `DAILY_SOFT_WINDOW_ENABLED`
- Live `/dash/chat` Soft Window integration (next Integration PR after R1.1)
- Changing Clean Window / shadow / Wake runtime
