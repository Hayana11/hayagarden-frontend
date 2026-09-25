# Production Codex effort overlay recovery (2026-09-25)

Audit date: 2026-09-25 (UTC)

## Production state before this acknowledgement

- Production worktree HEAD: `55ebaf0707ddf5ccdb0ac498351dbf8fd1df34c0`
- Production deployed marker: `b67707fa35381faa81c8d9baa78a63f1f25e8eca`
- GitHub `origin/main` at audit: `9b34cca852f96e2aa8b9e2ce8e0d2ca3ccf460d2` (merged PR #497)
- Recovery branch: `recovery/prod-codex-effort-overlay-20260925`

The production-only commit message is `Recover production Codex effort overlay matching PR 498.` Its parent is the last deployed main SHA `b67707fa`.

## Git topology and provenance

```text
b67707fa  last deployed origin/main / parent of production HEAD
├── 55ebaf07  production-only recovery commit (current VPS HEAD)
└── 4b619f93  PR #497 picker commit
    └── 9b34cca8  origin/main at audit (M3-04A fingerprint refresh)
```

`55ebaf07` is not an ancestor of `origin/main`. `b67707fa` is.

Changed source paths on `55ebaf07` versus its parent:

- `app.py`
- `codex_app_server.py`
- `config_store.py`

Blob identity is exact against `origin/cursor/claude-codex-effort-fae4` (open draft PR #498):

| path | blob SHA |
|---|---|
| `app.py` | `a216c51738fa8b93d726247f73014306cba01d18` |
| `codex_app_server.py` | `da6c4aa50cc07a508b258fa29fd2bb3664976c8d` |
| `config_store.py` | `c74a966d7b1d120a43c411e23eca2863a5e5323c` |

Those blobs are not on `origin/main`. This acknowledgement does not merge PR #498 and does not reintroduce them.

## Recovery decision

- Acknowledge only the exact audited SHA `55ebaf0707ddf5ccdb0ac498351dbf8fd1df34c0` in `deploy/recovered-production-shas.txt`.
- Source remains on `recovery/prod-codex-effort-overlay-20260925` and draft PR #498. It is not copied onto `main`.
- No cherry-pick, source rewrite, reset, or deploy-guard bypass.
- This PR changes deployment provenance only; it does not change product code or the guard.
- After this lands, the guarded deploy of `origin/main` (PR #497 picker) may switch production off `55ebaf07`. The live Codex overlay is therefore replaced by `main` until PR #498 is separately reviewed and merged.
- Runtime databases, uploads, attachments, rotated logs, and the protected TreeGPT artifact overlay are not source recovery. They remain under the guarded deploy script's preservation and backup handling.

## Worktree audit

At audit time, there were no staged changes and no tracked source changes. The only tracked worktree differences were the two expected protected-overlay deletions:

- `artifacts/treegpt-cache-probe-baseline.json`
- `artifacts/treegpt-cache-probe-live.json`

The following rotated log artifacts were untracked runtime files and are not acknowledged as source commits:

- `client_errors.log.1`
- `client_errors.log.2.gz`
- `client_errors.log.3.gz`

`frontend.service` and `frontend-gw.service` were active. Gunicorn for `app:app` had been restarted at 10:50:06 UTC after the overlay files were written at 10:50:03 UTC, so the overlay was live in the running frontend process. `GET /api/config/display-thinking` returned 404 because PR #497 is not in the running tree.
