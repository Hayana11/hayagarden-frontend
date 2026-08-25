# AGENTS.md — hayagarden-frontend

`hayagarden-frontend` is the actual product: a Flask backend + React dashboard personal AI‑companion
site. Despite the `-frontend` name it contains both servers and the SPA. Authoritative technical docs
are `README.md`, `ARCHITECTURE.md`, and `docs/`. The `CLAUDE.md` / `prompts/*` files are persona
roleplay content, **not** technical docs — ignore them for engineering/setup work.

## Cursor Cloud specific instructions

Dependencies are already installed by the environment update script (system Python packages via
`pip install --break-system-packages`, and `app/` node modules). You normally only need to start services and run tests/build.

### Deployment layout (important, non-obvious)
- The code **hardcodes `/opt/frontend/...` paths** (`DB_PATH`, `.env`, `static/`, `app/dist`, etc.).
  The environment provides a symlink `/opt/frontend -> <repo checkout>`, so running from the repo and
  running from `/opt/frontend` are the same tree. Do not "fix" the hardcoded paths.
- `app.py` reads `/opt/frontend/.env` at import time, so that file **must exist** (a placeholder
  is fine). Runtime tunables live in the `runtime_config` SQLite table (`config_store.py`), not `.env`.
- The SQLite DB is `/opt/frontend/memories.db` (git-ignored). Base tables come from `init_db.py`.
  Fresh-DB gotcha: `chat/daily_schema.py` reads `row['m']` over a plain (tuple) connection, so on a brand-new DB the migration crashes unless the `chat_messages.source_kind` column already exists — the update script pre-creates it. If you ever recreate the DB by hand, add that column too.

### Services and how to run them (dev mode)
| Service | Start command (from repo root) | Port |
|---|---|---|
| Main site (`app.py`) | `python3 app.py` | 5050 |
| AI gateway (`gateway.py`) | `python3 gateway.py` | 5051 |
| React dashboard (dev) | `cd app && npm run dev` | 5173 (`/dash/`) |
| React dashboard (prod build served by main site) | `cd app && npm run build` → served at `http://localhost:5050/dash` | via 5050 |

- The Vite dev server (5173) has **no backend proxy**, so its API calls won't reach 5050/5051. For a working end-to-end UI, run `npm run build` and open `/dash` on the main site (5050). Use `npm run dev` only for fast UI iteration.
- The gateway needs the `/opt/workspace` sandbox dirs (created by the update script). It logs harmless sandbox warnings in dev.
- AI chat replies require a working relay/LLM key in `/opt/frontend/.env`. Without them, user messages still persist and render, but no AI reply is generated — expected in this environment.
- The Monopoly game room depends on a separate `spicy-monopoly` engine on `127.0.0.1:8069`, which is not in this repo.

### Lint / test / build
- Lint (SPA): `cd app && npm run lint`.
- Build (SPA): `cd app && npm run build`.
- Backend tests: `python3 -m unittest discover -s tests -p "test_*.py"`.

## Frontend construction gate — mandatory

Before modifying any UI/page code under `app/src/**`, `app/index.html`, or `static/**`, MUST read `docs/FRONTEND_CONSTRUCTION_GUIDE.md` first.

The guide defines the frozen viewport/scale authority, Chrome 78 compatibility contract, React-vs-static page boundary, routing/nav authority, layout geometry, and required focused tests. Do not introduce page-level zoom/scale, a second shell/nav authority, or new browser runtime requirements without explicitly auditing and reporting them.

If current code conflicts with the guide, STOP and report the drift before editing either side.
