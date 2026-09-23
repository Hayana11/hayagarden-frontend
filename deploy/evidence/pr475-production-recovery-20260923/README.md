# PR #475 production history reconciliation

Audit date: 2026-09-23 (UTC)

## Production state before recovery

- Production worktree HEAD: `38123fceccb5298ab394bfc9df6f37f248620734`
- Production deployed marker: `28290720d916156f5285c66b56caa774b65bd18c`
- GitHub main before this recovery: `4f4b829d97371d17384c2da528e0104739851b9b`
- The production HEAD commit tree is `e009caecb7e1a0fbe4bcb62442aa4901a75c81b3`.

## Git topology and provenance

```text
28290720  prior main / common parent
├── 38123fce  PR #475 first branch commit / production worktree HEAD
└── 4f4b829d  PR #475 final squash merge / main before recovery
```

GitHub PR #475 commit history contains `38123fceccb5298ab394bfc9df6f37f248620734` as its first commit. Its title is `feat: add Codex and DeepSeek model controls`; its parent is the same `28290720...` base. Comparing that parent to the production HEAD shows only these source paths:

- `app.py`
- `app/src/lib/groupChat.ts`
- `app/src/lib/systemConfig.ts`
- `app/src/screens/SettingsScreen.tsx`
- `codex_app_server.py`
- `config_store.py`
- `gateway.py`
- `tests/test_codex_app_server.py`

All eight paths are in the final squash merge's changed-file scope. The final merge contains the complete PR #475 branch history and subsequent corrections. The earlier production tree is therefore superseded by the final merged implementation; this acknowledgment does not reintroduce its older blobs.

## Recovery decision

- Acknowledge only the exact audited SHA `38123fceccb5298ab394bfc9df6f37f248620734` in `deploy/recovered-production-shas.txt`.
- No cherry-pick, source rewrite, reset, or deploy-guard bypass.
- This PR changes deployment provenance only; it does not change product code or the guard.
- Runtime databases, uploads, attachments, rotated logs, and the protected TreeGPT artifact overlay are not source recovery. They remain under the guarded deploy script's preservation and backup handling.

## Worktree audit

At audit time, there were no staged changes and no tracked source changes. The only tracked worktree differences were the two expected protected-overlay deletions:

- `artifacts/treegpt-cache-probe-baseline.json`
- `artifacts/treegpt-cache-probe-live.json`

The following rotated log artifacts were untracked runtime files and are not acknowledged as source commits:

- `client_errors.log.1`
- `client_errors.log.2.gz`
- `client_errors.log.3.gz`
