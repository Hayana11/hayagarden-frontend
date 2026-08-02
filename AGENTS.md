# AGENTS.md

## Cursor Cloud specific instructions

This repo (despite the name) is a **full-stack self-hosted Flask app**, not just a frontend.
See `ARCHITECTURE.md` for the full service topology. Notes below are the non-obvious
things needed to run it in the Cloud VM.

### Services (dev)
- **Main site** — `python3 app.py` → port **5050**. Serves the static pages (`static/*.html`) and
  the chat-store / board / calendar / ledger / todos / artifacts APIs.
- **AI gateway** — `python3 gateway.py` → port **5051**. Chat streaming pipeline + tool execution.
- Both are plain Flask dev servers (no build step; the frontend is static vanilla JS/HTML).
- Optional services (workspace `5053`, mijia light daemon `5052`, MCP servers `5055/5056/3100`,
  external `ombre-brain` memory at `127.0.0.1:8000`) are not needed for core chat/calendar/board.

### Hardcoded `/opt/frontend` path (important)
The code hardcodes absolute paths to `/opt/frontend` (DB, `.env`, `static/`, `prompts/`, `sys.path`).
The update script symlinks `/opt/frontend` → this repo, so run everything as `/opt/frontend`.
If the symlink is missing, nothing imports. Recreate with:
`sudo ln -sfn /agent/repos/hayagarden-frontend /opt/frontend`.

### One-time setup NOT done by the update script (git-ignored, so absent on a fresh checkout)
1. **`.env`** — `app.py` does `open('/opt/frontend/.env')` at import time with **no** error handling,
   so it will crash if the file is missing. A minimal dev `.env` is enough (keys can be blank):
   `BOARD_TOKEN_FYODOR`, `ANTHROPIC_API_KEY`, `API_URL`, `MODEL`, `GW_PROVIDER=api_relay`.
2. **`memories.db`** — create with `python3 init_db.py` (opens `/opt/frontend/memories.db`).
   Run it **only once on a fresh DB**: it does `INSERT INTO chat_sessions` unconditionally, so
   re-running duplicates the default session. Most other tables (`runtime_config`, `todos`,
   `ledger`, attachments/gallery/commands) auto-create on first use; the `board`/`board_replies`
   tables are **not** created by `init_db.py` (so `/api/board` errors until they exist elsewhere).

Both files live under `/opt/frontend` (this repo) and are git-ignored, so they persist in a VM
snapshot but must be recreated on a truly fresh clone.

### Running the AI chat end-to-end
Core features (calendar / todos / ledger / message posts) work with no external services.
Real AI replies require a working **relay** endpoint: `API_URL` + a relay key in
`ANTHROPIC_API_KEY` (official `api.anthropic.com` returns 401 here — see `ARCHITECTURE.md` rule 6).
Without it, `/api/gw/chat/stream` produces no model output but the app otherwise runs.

### Routing note
In production nginx routes `/api/gw/` → 5051 and everything else → 5050. In dev without nginx,
hit the ports directly (`:5050` for pages/APIs, `:5051` for gateway). The static chat page issues
relative `/api/gw/...` calls, so a browser hitting `:5050` directly will 404 on gateway calls
unless a proxy is in front.

### Lint / test / build
There is **no** lint config, **no** automated test suite, and **no** build step in this repo
(`package.json` `test` is a placeholder). Validation is manual (run the services and exercise the UI/APIs).
