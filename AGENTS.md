# AGENTS.md

## Cursor Cloud specific instructions

Despite the repo name, this is a **full-stack self-hosted Flask app** (personal AI-companion
chat + dashboard), not just a frontend. See `ARCHITECTURE.md` for the full service topology.
The notes below are the non-obvious things needed to run it in the Cloud VM; the update script
already installs deps, creates the `/opt/frontend` symlink, and bootstraps `.env` + `memories.db`.

### Hardcoded `/opt/frontend` path
All Python code hardcodes absolute paths under `/opt/frontend` (DB, `.env`, `static/`,
`prompts/`, `sys.path`, `app/dist`). The update script symlinks `/opt/frontend` → this repo, so
everything resolves. If imports fail, recreate it: `sudo ln -sfn "$PWD" /opt/frontend`.

### Run the backend with gunicorn, NOT `python3 app.py` (important gotcha)
`app.py` has its `if __name__ == '__main__': app.run(...)` block **in the middle of the file**
(~line 2209) with **many more `@app.route` definitions after it** (through ~line 3428). Running
`python3 app.py` blocks at `app.run()` and never registers the later routes, so endpoints like
`/api/board` return **404** even though they exist. Production serves via gunicorn (module import),
which registers everything. Always run:

- Main site (5050): `python3 -m gunicorn -w 2 -b 0.0.0.0:5050 app:app`
- AI gateway (5051): `python3 -m gunicorn -k gthread -w 4 -b 0.0.0.0:5051 --timeout 320 gateway:app`

(`gunicorn` installs to `~/.local/bin` which isn't on PATH; invoke via `python3 -m gunicorn`.)
`gateway.py` happens to have all routes before its `__main__` block, so it also runs with
`python3 gateway.py`, but use gunicorn for both to match prod.

### DB tables not created by code
`init_db.py` creates a minimal schema and **INSERTs the default chat session unconditionally** —
run it only against a missing/fresh DB (the update script guards on file existence). Most tables
(`runtime_config`, `todos`, `ledger`, attachments/gallery/commands, posts) auto-create on first use,
but the **`board` / `board_replies` tables are not created by any code** — on the VPS they are
provisioned out-of-band. They already exist in the VM snapshot's `memories.db`; on a truly fresh DB,
`/api/board` fails until they are created (schema: see the INSERTs in `app.py` `post_board` /
`post_board_reply`). `.env` and `memories.db` are git-ignored, so they persist in the snapshot but
must be recreated on a bare clone (the update script handles both).

### Frontend (`app/` — React + Vite, dashboard "Fyodor · Dash")
Two ways to run it:
- **Prod-like, same-origin (real backend data):** `npm run build` in `app/` → `app/dist`, then hit
  Flask at `http://localhost:5050/dash` (note: `/dash`, no trailing slash; `/dash/` 404s). This is
  the path that shows real todos/board/ledger from SQLite.
- **Dev mode:** `npm run dev` in `app/` → `http://localhost:5173/dash/` (Vite `base` is `/dash/`).
  The backend has **no CORS** and there is **no Vite proxy**, so the dev server can't reach the
  `:5050` API cross-origin and falls back to deterministic mock data (`src/lib/mock.ts`). For real
  data in dev either use the same-origin Flask `/dash` route, or set `VITE_API_BASE_URL` in
  `app/.env.local` (still CORS-limited in the browser).

### Lint / test / build
- Frontend: `npm run lint` (oxlint) and `npm run build` (`tsc -b && vite build`) in `app/`.
- There is **no** Python lint config and **no** automated test suite; backend validation is manual
  (run the services and exercise the UI/APIs).

### AI replies need a working relay
Core features (dashboard, todos, board, calendar, ledger, chat message storage) work with no
external services. Real AI chat replies require a working **relay** endpoint (`API_URL` + relay key
in `ANTHROPIC_API_KEY`); official `api.anthropic.com` returns 401 here (see `ARCHITECTURE.md` rule 6).
Also, in dev without nginx the `/chat` page's `/api/gw/...` calls hit 5050 and 404 — the gateway is
on 5051 (nginx proxies `/api/gw/` → 5051 in prod).
