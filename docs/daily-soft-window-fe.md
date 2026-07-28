# P-CONTEXT-DAILY-SOFT-WINDOW-FE-R1

## Status

Frontend Soft Window **formal chat wiring** via same-origin BFF. Flag-off truth is upstream **404** (UI fully hidden after one silent `GET /current` probe).

- Does **not** enable `DAILY_SOFT_WINDOW_ENABLED`
- Does **not** put `DAILY_SOFT_WINDOW_TOKEN` in app/src, `VITE_*`, localStorage, URL, or bundle
- Does **not** call resident / Wake / model
- Browser only talks to `/api/gw/daily-context/*` (credentials include); BFF injects Bearer server-side

## Auth bridge

```text
Browser  →  /api/gw/daily-context/{current|carryover-candidates|select-carryover}
nginx    →  strips /api/gw
gateway  →  daily_context_bff.py  →  http://127.0.0.1:5050/api/daily-context/*
           injects Authorization: Bearer <DAILY_SOFT_WINDOW_TOKEN>
```

Original `/api/daily-context/*` Bearer protection is unchanged.

## Canonical rounds (backend #147)

Carryover unit is **`round`**:

- `round_id` = first user `message_id`
- `message_ids` / `messages` aligned
- First message must be `user`; assistant-only groups rejected
- Select count ∈ `{0, 3, 5, 10}`; `carryover_count` = selected **round** count
- Locked recovery: draft uses `requested_round_count` when valid tier, else `selected_round_count`; card shows `selected_round_count`

## Formal chat (`/dash/chat`)

| Behavior | Detail |
|----------|--------|
| Mount | Always probe `GET …/current` (`{ live: true }`) |
| 200 unselected | Show picker card; default draft **10**; candidates fetched only on modal open |
| 200 locked | Readonly card; highlight `selected_message_ids` |
| 404 | Hide all Soft Window UI |
| 423 | Hide; retry 750 / 2000 / 5000 ms (max 3); then wait for window focus |
| 401/403 | Fail-hidden; `console.error` with `AUTH_BRIDGE` |
| Send | `notifySendStarted` closes modal / suppresses picker; **never** POST 0 before send; `notifySendSettled` refreshes current |
| Boundary | Insert by `boundary_message_id` only when neighborhood is on the loaded page |

## Preview

```text
/dash/daily-soft-window
```

`forceMock: true`. Scenario chips include `ready` / `loading` / `empty` / `404` / `409` / `locked` / `423` / `error`. Preview ↑ may simulate lock-0 (mock only).

## Files

- `daily_context_bff.py` — gateway BFF
- `app/src/lib/dailySoftWindow.ts` — canonical types, helpers, mock + live client
- `app/src/hooks/useDailySoftWindow.ts` — formal + preview state machine
- `app/src/components/dailySoftWindow/*` — #148 visuals
- `app/src/screens/ChatScreen.tsx` — live wiring
- `app/src/screens/DailySoftWindowPreviewScreen.tsx` — mock playground
- `app/scripts/test-daily-soft-window.mjs` — FE unit checks
- `tests/test_daily_context_bff.py` — BFF proxy / token injection

## Explicit non-goals

- Enabling `DAILY_SOFT_WINDOW_ENABLED`
- Changing `chat/daily_context.py` contract
- Merge / deploy notes in this doc
- Redesigning #148 packing UI
